"""
Model adapter tests: how each CLI is invoked and how its output is read.

What it covers: Gemini's prompt travels on stdin as one stream-json line (never on the
command line, which Windows caps at ~32k chars), agy's result event is parsed for success,
error, missing and empty replies, and every adapter refuses an oversized prompt before
spawning anything. `_run` is faked, so nothing here starts a real CLI except the `live`
test at the bottom.
"""

import json
import shutil

import pytest

from ottr.council import models
from ottr.council.models import (MAX_PROMPT_CHARS, ClaudeCLI, CodexCLI, GeminiCLI, ModelError,
                                 agy_response)


def stream(*events) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


def ok_result(text="hello", status="SUCCESS", error=None):
    return {"event": "result", "result": {"status": status, "response": text, "error": error}}


class FakeRun:
    """Records each call to models._run and answers with a scripted (rc, out, err)."""

    def __init__(self, rc=0, out="", err=""):
        self.reply = (rc, out, err)
        self.calls = []

    async def __call__(self, argv, stdin_text, timeout, cwd):
        self.calls.append((argv, stdin_text))
        return self.reply


@pytest.fixture
def gemini(monkeypatch):
    g = GeminiCLI(model="gemini-test")
    monkeypatch.setattr(g, "locate", lambda: "agy.exe")
    return g


async def test_gemini_sends_the_prompt_on_stdin_not_argv(gemini, monkeypatch):
    run = FakeRun(out=stream({"event": "init"}, ok_result('{"a": 1}')))
    monkeypatch.setattr(models, "_run", run)
    prompt = "line one\nline two \"quoted\" " + "x" * 50_000  # well past Windows' 32k limit
    assert await gemini.complete(prompt) == '{"a": 1}'
    argv, stdin = run.calls[0]
    assert argv[-1] == "-p="                        # empty -p: the prompt comes from stdin
    assert "--input-format" in argv and argv[argv.index("--input-format") + 1] == "stream-json"
    assert all(prompt not in a and len(a) < 200 for a in argv)
    assert stdin.endswith("\n") and stdin.count("\n") == 1  # exactly one NDJSON line
    assert json.loads(stdin) == {"event": "user", "message": {"content": prompt}}


async def test_gemini_effort_flag_goes_before_the_prompt_flag(monkeypatch):
    g = GeminiCLI(model="m", effort="low")
    monkeypatch.setattr(g, "locate", lambda: "agy.exe")
    run = FakeRun(out=stream(ok_result()))
    monkeypatch.setattr(models, "_run", run)
    await g.complete("hi")
    argv = run.calls[0][0]
    assert argv[argv.index("--effort") + 1] == "low" and argv[-1] == "-p="


def test_agy_response_reads_the_final_result_event():
    out = stream({"event": "init"}, {"event": "step_update", "step_update": {"text_delta": "x"}},
                 ok_result("the answer\n"))
    assert agy_response(0, "agy notice: not json\n" + out, "") == "the answer\n"


@pytest.mark.parametrize("rc,out,err,needle", [
    (1, stream(ok_result("", status="ERROR", error="quota exceeded")), "", "quota exceeded"),
    (2, "", "flag needs an argument: -p", "no result event"),
    (0, stream(ok_result("   ")), "", "empty response"),
    (1, stream(ok_result("text")), "crashed after replying", "exit 1"),
])
def test_agy_response_failures_raise_model_error(rc, out, err, needle):
    with pytest.raises(ModelError, match=needle):
        agy_response(rc, out, err)


@pytest.mark.parametrize("adapter", [ClaudeCLI(), CodexCLI(), GeminiCLI()], ids=["claude", "gpt", "gemini"])
async def test_every_adapter_refuses_an_oversized_prompt_without_spawning(adapter, monkeypatch):
    monkeypatch.setattr(type(adapter), "locate", lambda self: "cli.exe")
    if isinstance(adapter, CodexCLI):
        monkeypatch.setattr(CodexCLI, "_find", staticmethod(lambda: "codex.exe"))
    monkeypatch.setattr(models.shutil, "which", lambda name: "cli.exe")
    run = FakeRun(out=stream(ok_result()))
    monkeypatch.setattr(models, "_run", run)
    with pytest.raises(ModelError, match="over the"):
        await adapter.complete("x" * (MAX_PROMPT_CHARS + 1))
    assert run.calls == []


@pytest.mark.live
@pytest.mark.skipif(shutil.which("agy") is None, reason="agy CLI not installed")
async def test_live_agy_answers_a_prompt_longer_than_the_windows_command_line():
    filler = "\n".join(f"Line {i}: filler to ignore." for i in range(2000))  # ~50k chars
    prompt = filler + "\n\nIgnore the filler. Reply with exactly the word LONGOK."
    assert len(prompt) > 40_000
    reply = await GeminiCLI(model="gemini-3.8-flash-low").complete(prompt, timeout=180)
    assert "LONGOK" in reply
