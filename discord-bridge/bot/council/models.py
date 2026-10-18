"""
Model adapters: one async `complete(prompt)` per provider, all via their local CLIs.

What it does: runs Claude (`claude -p`), GPT (`codex exec`) and Gemini (`agy -p`) as
subprocesses and returns raw text; `ask_json` extracts + validates JSON with one repair
retry. Why CLIs: they use the user's existing subscriptions (no API keys in this repo)
and the three share one tiny interface, so tests can swap in a fake.
"""

import asyncio
import glob
import json
import logging
import os
import shutil
import tempfile
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class ModelError(RuntimeError):
    """The model call failed (missing CLI, timeout, non-zero exit, empty output)."""


class ModelAdapter:
    """Base interface. `name` is the provider id used in logs and the council record."""

    name = "model"

    async def complete(self, prompt: str, timeout: float = 240.0) -> str:
        raise NotImplementedError


async def _run(argv, stdin_text: Optional[str], timeout: float, cwd: str) -> tuple:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if stdin_text is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(stdin_text.encode("utf-8") if stdin_text is not None else None),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ModelError(f"timed out after {timeout:.0f}s: {argv[0]}")
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def _normalize(text: str) -> str:
    # Models sometimes emit narrow no-break spaces / non-breaking hyphens that break JSON.
    return text.replace(" ", " ").replace(" ", " ").replace("‑", "-")


class ClaudeCLI(ModelAdapter):
    name = "claude"

    def __init__(self, model: str = "opus", effort: str = "high") -> None:
        self.model, self.effort = model, effort

    async def complete(self, prompt: str, timeout: float = 240.0) -> str:
        exe = shutil.which("claude")
        if not exe:
            raise ModelError("claude CLI not found on PATH")
        # --tools "" + --disable-slash-commands: pure text in/out, no file or shell access.
        argv = [exe, "-p", "--output-format", "text", "--no-session-persistence",
                "--tools", "", "--disable-slash-commands",
                "--model", self.model, "--effort", self.effort]
        rc, out, err = await _run(argv, prompt, timeout, tempfile.gettempdir())
        if rc != 0 or not out.strip():
            raise ModelError(f"claude exit {rc}: {err.strip()[:300]}")
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

    async def complete(self, prompt: str, timeout: float = 240.0) -> str:
        exe = self._find()
        if not exe:
            raise ModelError("codex CLI not found (expected inside the Codex desktop app)")
        fd, out_path = tempfile.mkstemp(suffix=".txt")
        os.close(fd)
        try:
            # "-" makes codex read the prompt from stdin (avoids Windows arg-length limits).
            argv = [exe, "exec", "--skip-git-repo-check", "--sandbox", "read-only",
                    "-c", f"model_reasoning_effort={self.effort}", "-o", out_path, "-"]
            rc, _out, err = await _run(argv, prompt, timeout, tempfile.gettempdir())
            with open(out_path, encoding="utf-8", errors="replace") as f:
                text = f.read()
            if rc != 0 or not text.strip():
                raise ModelError(f"codex exit {rc}: {err.strip()[-300:]}")
            return _normalize(text)
        finally:
            try:
                os.remove(out_path)
            except OSError:
                pass


class GeminiCLI(ModelAdapter):
    name = "gemini"

    def __init__(self, model: str = "gemini-3.8-flash-high") -> None:
        self.model = model

    async def complete(self, prompt: str, timeout: float = 240.0) -> str:
        exe = shutil.which("agy")
        if not exe:
            raise ModelError("agy (Antigravity CLI) not found on PATH")
        argv = [exe, "-p", prompt, "--model", self.model, "--sandbox"]
        rc, out, err = await _run(argv, None, timeout, tempfile.gettempdir())
        if rc != 0 or not out.strip():
            raise ModelError(f"agy exit {rc}: {err.strip()[:300]}")
        return _normalize(out)


def extract_json(text: str) -> Any:
    """Pull the first JSON object out of model text (tolerates fences and chatter)."""
    dec = json.JSONDecoder()
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
