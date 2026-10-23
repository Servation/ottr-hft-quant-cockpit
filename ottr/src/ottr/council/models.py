"""
Model adapters: one async `complete(prompt)` per provider, all via their local CLIs.

What it does: runs Claude (`claude -p`), GPT (`codex exec`) and Gemini (`agy`) as
subprocesses, always passing the prompt on stdin, and returns raw text; `ask_json` extracts + validates JSON with one repair
retry. Why CLIs: they use the user's existing subscriptions (no API keys in this repo)
and the three share one tiny interface, so tests can swap in a fake. `make_adapter` builds
one by name; the council passes its own effort, `ottr chat` a lower one so replies are quick.
"""

import asyncio
import glob
import json
import logging
import os
import shutil
import tempfile
from typing import Any, Callable, Optional

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
        return ClaudeCLI(model=council.claude_model, effort=effort or council.reasoning_effort)
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
