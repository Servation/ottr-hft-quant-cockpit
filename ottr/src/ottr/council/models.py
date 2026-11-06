"""
Model adapters: one async `complete(prompt)` per provider.

What it does: Claude goes through the Claude API (`ClaudeAPI`, the default) or the
`claude -p` CLI (`ClaudeCLI`, `council.claude_backend = "cli"`); GPT (`codex exec`) and
Gemini (`agy`) run as CLI subprocesses with the prompt on stdin. Each returns raw text;
`ask_json` extracts + validates JSON with one repair retry. Why the API for Claude: no CLI
sign-in to expire or be signed out (the 2026-10-23 abstained meeting), a pinned model id,
and token counts in the log; it bills per token to the key's Console account. GPT and
Gemini stay on their CLIs and the user's subscriptions. All adapters share one tiny
interface, so tests swap in a fake. `make_adapter` builds one by name; the council passes
its own effort, `ottr chat` a lower one so replies are quick.
"""

import asyncio
import glob
import json
import logging
import os
import shutil
import tempfile
from typing import Any, Callable, Optional, Tuple

import anthropic

from ottr.config import Council as CouncilConfig
from ottr.proc import NO_WINDOW, kill_tree

logger = logging.getLogger(__name__)


class ModelError(RuntimeError):
    """The model call failed (missing CLI, timeout, non-zero exit, empty output)."""


# Hard ceiling on one prompt, in characters (about 100k tokens). Real council prompts are
# a few tens of thousands; anything near this means a runaway packet (a bug), so fail with
# a clear error before spawning a CLI instead of getting its opaque error or a context the
# model silently truncates.
MAX_PROMPT_CHARS = 400_000


def check_prompt(name: str, prompt: str) -> None:
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ModelError(f"{name}: prompt is {len(prompt):,} chars, over the {MAX_PROMPT_CHARS:,} limit")


class ModelAdapter:
    """Base interface. `name` is the provider id used in logs and the council record."""

    name = "model"

    def locate(self) -> Optional[str]:
        """Path of the CLI this adapter runs, or None if it isn't installed."""
        return None

    async def complete(self, prompt: str, timeout: float = 240.0) -> str:
        raise NotImplementedError


async def _run(argv, stdin_text: Optional[str], timeout: float, cwd: str) -> tuple:
    # No console window: under the supervisor the engine runs hidden, and a console
    # child would otherwise pop a window on the desktop at every meeting.
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if stdin_text is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        creationflags=NO_WINDOW,
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(stdin_text.encode("utf-8") if stdin_text is not None else None),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        kill_tree(proc)
        await proc.wait()
        raise ModelError(f"timed out after {timeout:.0f}s: {argv[0]}")
    except asyncio.CancelledError:
        # Engine shutdown mid-meeting: don't leave a model CLI running on its own.
        kill_tree(proc)
        raise
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


# The claude CLI signs in with this long-lived subscription token when it is set (made by
# `claude setup-token`, kept in ottr/.env), so the engine doesn't share, and can't lose,
# this PC's interactive Claude Code sign-in.
CLAUDE_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"


def cli_message(out: str, err: str, tail: bool = False, limit: int = 300,
                token_env: Optional[str] = None) -> str:
    """What a failed CLI said: stderr, else stdout (the claude CLI prints "Not logged in"
    on stdout), clipped. A CLI that has been signed out, or whose sign-in was rejected, gets
    the fix spelled out, because this text ends up in a Discord alarm. `token_env` names
    the variable that holds the CLI's token, if it takes one."""
    text = (err.strip() or out.strip() or "no output")
    text = text[-limit:] if tail else text[:limit]
    low = text.lower()
    if "not logged in" in low or "/login" in low or "please log in" in low:
        text += " (the CLI is signed out: sign it back in from a terminal on this PC)"
    elif "failed to authenticate" in low or "token is invalid" in low or "token has expired" in low:
        if token_env and os.environ.get(token_env):
            text += (f" (the {token_env} in ottr/.env was rejected, expired or revoked: make a new "
                     "one with `claude setup-token`, replace it, then restart the engine)")
        else:
            text += " (the CLI's sign-in was rejected: sign it back in from a terminal on this PC)"
    return text


def _normalize(text: str) -> str:
    # Models sometimes emit narrow no-break spaces / non-breaking hyphens that break JSON.
    return text.replace(" ", " ").replace(" ", " ").replace("‑", "-")


class ClaudeCLI(ModelAdapter):
    name = "claude"

    def __init__(self, model: str = "opus", effort: str = "high") -> None:
        self.model, self.effort = model, effort

    def locate(self) -> Optional[str]:
        return shutil.which("claude")

    async def complete(self, prompt: str, timeout: float = 240.0) -> str:
        exe = self.locate()
        if not exe:
            raise ModelError("claude CLI not found on PATH")
        check_prompt(self.name, prompt)
        # --tools "" + --disable-slash-commands: pure text in/out, no file or shell access.
        argv = [exe, "-p", "--output-format", "text", "--no-session-persistence",
                "--tools", "", "--disable-slash-commands",
                "--model", self.model, "--effort", self.effort]
        rc, out, err = await _run(argv, prompt, timeout, tempfile.gettempdir())
        if rc != 0 or not out.strip():
            raise ModelError(f"claude exit {rc}: {cli_message(out, err, token_env=CLAUDE_TOKEN_ENV)}")
        return _normalize(out)


# OTTR's Claude API key, from ottr/.env (loaded into the environment at start-up) and handed
# to the SDK explicitly. Not the SDK's default ANTHROPIC_API_KEY: every model CLI the engine
# runs inherits its environment, and the claude CLI bills a key under that name instead of
# the subscription, so going back to claude_backend = "cli" would quietly keep paying per
# token. A key under that name elsewhere on this PC is ignored for the same reason.
API_KEY_ENV = "CLAUDE_API_KEY"


def _sdk() -> "anthropic.AsyncAnthropic":
    return anthropic.AsyncAnthropic(api_key=os.environ.get(API_KEY_ENV))
# On a policy refusal the API re-runs the same request, inside the same call, on a model it
# picks by the refusal's category, so a false positive costs a meeting nothing.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Room for the model's thinking plus the JSON reply. Streaming keeps a long answer from
# tripping the SDK's guard against non-streaming requests that may run past 10 minutes.
API_MAX_TOKENS = 32000
KEY_FIX = (f"make a new key in the Claude Console (console.anthropic.com > API keys), put it in "
           f"ottr/.env as {API_KEY_ENV}=..., then restart the engine")


def api_message(e: Exception) -> str:
    """A Claude API failure in a line an alarm can carry: what went wrong and the fix.
    Never the key itself: the SDK's errors don't contain it, and we only use status codes
    and the API's own message."""
    if isinstance(e, anthropic.AuthenticationError):
        return f"the {API_KEY_ENV} in ottr/.env was rejected (revoked, expired or mistyped): {KEY_FIX}"
    if isinstance(e, anthropic.PermissionDeniedError):
        return "HTTP 403: the API key may not use this model or workspace"
    if isinstance(e, anthropic.NotFoundError):
        return "HTTP 404: model not found (check council.claude_model in config.toml)"
    if isinstance(e, anthropic.RateLimitError):
        return "HTTP 429: rate limited, still after the SDK's retries"
    if isinstance(e, anthropic.APIStatusError):
        # The API's own words ("Overloaded"), not the SDK's "Error code: 529 - {...}" wrapper.
        body = e.body if isinstance(e.body, dict) else {}
        err = body.get("error") if isinstance(body.get("error"), dict) else body
        text = " ".join(str(err.get("message") or e.message).split())[:300]
        if "credit balance" in text.lower():
            text += " (add credits in the Claude Console under Billing)"
        return f"HTTP {e.status_code}: {text}"
    if isinstance(e, anthropic.APITimeoutError):
        return "the request timed out"
    if isinstance(e, anthropic.APIConnectionError):
        return "can't reach the API (network or DNS)"
    return type(e).__name__


class ClaudeAPI(ModelAdapter):
    """Claude through the Messages API, streamed. Thinking is always on for Opus 5.5;
    `effort` sets how hard it thinks (its default would be medium, so it is always sent)."""

    name = "claude"

    def __init__(self, model: str = "claude-opus-5-5", effort: str = "high",
                 client: Optional[Callable[[], Any]] = None) -> None:
        # `client` builds the AsyncAnthropic for one call; tests hand in one on a mock
        # transport. A fresh client per call: calls are minutes apart and each `async with`
        # closes its connections, so nothing outlives the event loop it was made on.
        self.model, self.effort = model, effort
        self._client = client

    def locate(self) -> Optional[str]:
        return f"the Claude API ({API_KEY_ENV} set)" if os.environ.get(API_KEY_ENV) else None

    async def complete(self, prompt: str, timeout: float = 240.0) -> str:
        check_prompt(self.name, prompt)
        if self._client is None and not os.environ.get(API_KEY_ENV):
            # Checked here: without credentials the SDK raises a bare TypeError mid-request.
            raise ModelError(f"claude api: {API_KEY_ENV} is not set: {KEY_FIX}")
        try:
            # The SDK retries connection errors, 429 and 5xx itself; the outer timeout caps
            # the whole call, retries included, like the CLIs' timeout does.
            msg = await asyncio.wait_for(self._ask(prompt, timeout), timeout)
        except asyncio.TimeoutError:
            raise ModelError(f"claude api: timed out after {timeout:.0f}s") from None
        except anthropic.AnthropicError as e:
            raise ModelError(f"claude api: {api_message(e)}") from None
        u = msg.usage
        logger.info("claude api %s (effort %s): %s input, %s output tokens%s", msg.model, self.effort,
                    u.input_tokens, u.output_tokens,
                    f" (answered by the fallback; {self.model} declined)" if msg.model != self.model else "")
        if msg.stop_reason == "refusal":
            why = getattr(getattr(msg, "stop_details", None), "category", None)
            raise ModelError(f"claude api: declined to answer (refusal{f': {why}' if why else ''})")
        if msg.stop_reason == "max_tokens":
            raise ModelError(f"claude api: the reply was cut off at {API_MAX_TOKENS} tokens")
        text = "".join(b.text for b in msg.content if b.type == "text")
        if not text.strip():
            raise ModelError(f"claude api: empty reply (stop reason {msg.stop_reason})")
        return _normalize(text)

    async def _ask(self, prompt: str, timeout: float) -> Any:
        async with (self._client or _sdk)() as client:
            async with client.beta.messages.stream(
                model=self.model,
                max_tokens=API_MAX_TOKENS,
                output_config={"effort": self.effort},
                messages=[{"role": "user", "content": prompt}],
                betas=[FALLBACK_BETA],
                fallbacks="default",
                timeout=timeout,
            ) as stream:
                return await stream.get_final_message()


async def check_claude_api(model: str, client: Optional[Callable[[], Any]] = None) -> Tuple[bool, str]:
    """Whether the key in the environment may use `model`: a Models API lookup, which
    spends no tokens. For `ottr data --check`."""
    if client is None and not os.environ.get(API_KEY_ENV):
        return False, f"{API_KEY_ENV} is not set: {KEY_FIX}"
    try:
        async with (client or _sdk)() as c:
            m = await c.models.retrieve(model, timeout=20.0)
    except anthropic.AnthropicError as e:
        return False, api_message(e)
    return True, f"key accepted; {m.id} ({m.display_name}) is available"


class CodexCLI(ModelAdapter):
    name = "gpt"

    def __init__(self, effort: str = "high") -> None:
        self.effort = effort

    @staticmethod
    def _find() -> Optional[str]:
        base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "OpenAI", "Codex", "bin")
        hits = sorted(glob.glob(os.path.join(base, "*", "codex.exe")))
        return hits[0] if hits else shutil.which("codex")

    def locate(self) -> Optional[str]:
        return self._find()

    async def complete(self, prompt: str, timeout: float = 240.0) -> str:
        exe = self._find()
        if not exe:
            raise ModelError("codex CLI not found (expected inside the Codex desktop app)")
        check_prompt(self.name, prompt)
        fd, out_path = tempfile.mkstemp(suffix=".txt")
        os.close(fd)
        try:
            # "-" makes codex read the prompt from stdin (avoids Windows arg-length limits).
            argv = [exe, "exec", "--skip-git-repo-check", "--sandbox", "read-only",
                    "-c", f"model_reasoning_effort={self.effort}", "-o", out_path, "-"]
            rc, out, err = await _run(argv, prompt, timeout, tempfile.gettempdir())
            with open(out_path, encoding="utf-8", errors="replace") as f:
                text = f.read()
            if rc != 0 or not text.strip():
                raise ModelError(f"codex exit {rc}: {cli_message(out, err, tail=True)}")
            return _normalize(text)
        finally:
            try:
                os.remove(out_path)
            except OSError:
                pass


class GeminiCLI(ModelAdapter):
    name = "gemini"

    def __init__(self, model: str = "gemini-3.8-flash-high", effort: Optional[str] = None) -> None:
        # None keeps the effort the model name implies (the council's setting); a value
        # adds agy's --effort flag.
        self.model, self.effort = model, effort

    def locate(self) -> Optional[str]:
        return shutil.which("agy")

    async def complete(self, prompt: str, timeout: float = 240.0) -> str:
        exe = self.locate()
        if not exe:
            raise ModelError("agy (Antigravity CLI) not found on PATH")
        check_prompt(self.name, prompt)
        # The prompt goes in on stdin, never as an argument: Windows caps a command line at
        # about 32k characters, which a full packet can exceed. agy only reads stdin in its
        # stream-json mode, as one NDJSON {"event":"user","message":{"content":...}} line,
        # and its -p flag always takes a value, so `-p=` (empty) hands the prompt to stdin.
        # Plain-text stdin does not work: `-p -` sends a literal "-" as the prompt.
        argv = [exe, "--input-format", "stream-json", "--output-format", "stream-json",
                "--model", self.model, "--sandbox"]
        if self.effort:
            argv += ["--effort", self.effort]
        argv.append("-p=")
        line = json.dumps({"event": "user", "message": {"content": prompt}}) + "\n"
        rc, out, err = await _run(argv, line, timeout, tempfile.gettempdir())
        return _normalize(agy_response(rc, out, err))


def agy_response(rc: int, out: str, err: str) -> str:
    """The reply text from agy's stream-json output, or ModelError.

    The final `{"event":"result","result":{...}}` line carries `status` (SUCCESS or
    ERROR), `response` and `error`; the step_update lines before it are streaming deltas.
    """
    result = None
    for raw in out.splitlines():
        try:
            ev = json.loads(raw)
        except ValueError:
            continue  # agy may print a non-JSON notice; only the events matter
        if isinstance(ev, dict) and ev.get("event") == "result":
            result = ev.get("result") or {}
    if result is None:
        raise ModelError(f"agy exit {rc}: no result event ({cli_message(out, err)})")
    if rc != 0 or result.get("status") != "SUCCESS":
        why = result.get("error") or err.strip()
        raise ModelError(f"agy exit {rc}, status {result.get('status')}: {str(why)[:300]}")
    text = result.get("response") or ""
    if not text.strip():
        raise ModelError("agy returned an empty response")
    return text


def make_adapter(name: str, council: CouncilConfig, effort: Optional[str] = None) -> ModelAdapter:
    """The adapter for provider `name` ("claude" | "gpt" | "gemini") with the council's model
    choices. `effort` overrides the council's reasoning effort (chat runs lower)."""
    if name == "claude":
        cls = ClaudeAPI if council.claude_backend == "api" else ClaudeCLI
        return cls(model=council.claude_model, effort=effort or council.reasoning_effort)
    if name == "gpt":
        return CodexCLI(effort=effort or council.reasoning_effort)
    if name == "gemini":
        return GeminiCLI(model=council.gemini_model, effort=effort)
    raise ValueError(f"unknown model {name!r} (expected claude, gpt or gemini)")


def extract_json(text: str) -> Any:
    """Pull the first JSON object out of model text (tolerates fences and chatter)."""
    def _no_nan(token: str):
        raise ValueError(f"non-finite number {token} in model output")

    dec = json.JSONDecoder(parse_constant=_no_nan)
    idx = text.find("{")
    while idx != -1:
        try:
            obj, _ = dec.raw_decode(text[idx:])
            return obj
        except json.JSONDecodeError:
            idx = text.find("{", idx + 1)
    raise ValueError("no JSON object found in model output")


async def ask_json(adapter: ModelAdapter, prompt: str, validate: Callable[[Any], Any],
                   timeout: float = 240.0, retries: int = 1) -> Any:
    """Call a model, parse + validate JSON; on a bad shape re-ask once with the error."""
    attempt_prompt = prompt
    last: Exception = ModelError("no attempt")
    for _ in range(retries + 1):
        raw = await adapter.complete(attempt_prompt, timeout=timeout)
        try:
            return validate(extract_json(raw))
        except (ValueError, TypeError) as e:  # SchemaError subclasses ValueError
            last = e
            logger.warning("%s returned invalid JSON (%s); retrying", adapter.name, e)
            attempt_prompt = (
                f"{prompt}\n\nYour previous reply was rejected: {e}\n"
                "Reply again with ONLY the corrected JSON object."
            )
    raise ModelError(f"{adapter.name}: invalid output after retry: {last}")
