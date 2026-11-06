"""
Model adapter tests: how each model is called and how its output is read.

What it covers: Gemini's prompt travels on stdin as one stream-json line (never on the
command line, which Windows caps at ~32k chars), agy's result event is parsed for success,
error, missing and empty replies, and every adapter refuses an oversized prompt before
spawning anything. `_run` is faked, so nothing here starts a real CLI except the `live`
tests. The Claude API adapter runs the real SDK against a mock HTTP transport, so the
request OTTR sends (model, effort, the refusal fallback) and how each failure reads in an
alarm are checked without the network.
"""

import asyncio
import json
import os
import shutil

import anthropic
import httpx2
import pytest

from ottr.config import Council
from ottr.council import models
from ottr.council.models import (FALLBACK_BETA, MAX_PROMPT_CHARS, ClaudeAPI, ClaudeCLI, CodexCLI, GeminiCLI,
                                 ModelError, agy_response, check_claude_api, make_adapter)

TEST_KEY = "test-key-never-shown"


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


@pytest.mark.parametrize("adapter", [ClaudeCLI(), CodexCLI(), GeminiCLI(), ClaudeAPI()],
                         ids=["claude", "gpt", "gemini", "claude-api"])
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


async def test_a_signed_out_cli_says_so_in_the_error(monkeypatch):
    """2026-10-23: the claude CLI had been signed out. It printed "Not logged in" on stdout
    and nothing on stderr, so the alarm read only "claude exit 1:"."""
    c = ClaudeCLI()
    monkeypatch.setattr(c, "locate", lambda: "claude.exe")
    monkeypatch.setattr(models, "_run", FakeRun(1, "Not logged in · Please run /login\n", ""))
    with pytest.raises(ModelError) as e:
        await c.complete("hi")
    assert str(e.value) == ("claude exit 1: Not logged in · Please run /login (the CLI is signed out: sign it "
                            "back in from a terminal on this PC)")
    monkeypatch.setattr(models, "_run", FakeRun(2, "", "boom"))
    with pytest.raises(ModelError, match="claude exit 2: boom$"):
        await c.complete("hi")


async def test_a_rejected_token_says_which_one_and_how_to_replace_it(monkeypatch):
    """What the claude CLI prints when CLAUDE_CODE_OAUTH_TOKEN is bad (reproduced with a
    made-up token): the alarm should say the token in ottr/.env needs replacing."""
    c = ClaudeCLI()
    monkeypatch.setattr(c, "locate", lambda: "claude.exe")
    rejected = "Failed to authenticate. API Error: 401 OAuth access token is invalid.\n"
    monkeypatch.setattr(models, "_run", FakeRun(1, rejected, ""))
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-test")
    with pytest.raises(ModelError) as e:
        await c.complete("hi")
    assert str(e.value).endswith("(the CLAUDE_CODE_OAUTH_TOKEN in ottr/.env was rejected, expired or revoked: "
                                 "make a new one with `claude setup-token`, replace it, then restart the engine)")
    assert "sk-ant-oat01-test" not in str(e.value)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN")
    with pytest.raises(ModelError) as e:
        await c.complete("hi")
    assert str(e.value).endswith("(the CLI's sign-in was rejected: sign it back in from a terminal on this PC)")


# -- the Claude API ------------------------------------------------------------------------------
def sdk(handler):
    """A factory for a real AsyncAnthropic whose HTTP goes to `handler` (no retries, so a
    failure is seen at once)."""
    return lambda: anthropic.AsyncAnthropic(
        api_key=TEST_KEY, max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)))


def streamed(text, stop="end_turn", model="claude-opus-5-5"):
    """A Messages API stream as Opus 5.5 sends it: a thinking block (its text omitted, the
    default), then the reply text, then the stop reason and usage."""
    msg = {"id": "msg_test", "type": "message", "role": "assistant", "model": model, "content": [],
           "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 1234, "output_tokens": 1}}
    events = [{"type": "message_start", "message": msg},
              {"type": "content_block_start", "index": 0,
               "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
              {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig"}},
              {"type": "content_block_stop", "index": 0}]
    if text is not None:
        events += [{"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
                   {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": text}},
                   {"type": "content_block_stop", "index": 1}]
    events += [{"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None},
                "usage": {"output_tokens": 567}},
               {"type": "message_stop"}]
    body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)
    return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())


def api_error(status, kind, message):
    return httpx2.Response(status, json={"type": "error", "error": {"type": kind, "message": message}})


async def test_claude_api_sends_the_prompt_with_the_model_effort_and_refusal_fallback():
    seen = []

    def handler(request):
        seen.append(request)
        return streamed('{"assets": []}')

    prompt = "packet line\n" * 2000
    reply = await ClaudeAPI(model="claude-opus-5-5", effort="high", client=sdk(handler)).complete(prompt)
    assert reply == '{"assets": []}'
    (req,) = seen
    body = json.loads(req.content)
    assert req.url.path == "/v1/messages" and FALLBACK_BETA in req.headers["anthropic-beta"]
    assert body["model"] == "claude-opus-5-5" and body["stream"] is True
    assert body["output_config"] == {"effort": "high"}       # Opus 5.5 would default to medium
    assert body["fallbacks"] == "default" and "thinking" not in body
    assert body["messages"] == [{"role": "user", "content": prompt}]
    assert body["max_tokens"] == models.API_MAX_TOKENS


@pytest.mark.parametrize("response,needle", [
    (api_error(401, "authentication_error", "invalid x-api-key"),
     "the CLAUDE_API_KEY in ottr/.env was rejected (revoked, expired or mistyped): make a new key"),
    (api_error(404, "not_found_error", "model: claude-opus-9"), "HTTP 404: model not found (check council.claude_model"),
    (api_error(400, "invalid_request_error", "Your credit balance is too low to access the Anthropic API."),
     "credit balance is too low to access the Anthropic API. (add credits in the Claude Console under Billing)"),
    (api_error(529, "overloaded_error", "Overloaded"), "HTTP 529: Overloaded"),
    (api_error(429, "rate_limit_error", "slow down"), "HTTP 429: rate limited"),
], ids=["bad-key", "no-model", "no-credit", "overloaded", "rate-limited"])
async def test_claude_api_failures_read_as_an_alarm_with_the_fix(response, needle):
    with pytest.raises(ModelError) as e:
        await ClaudeAPI(client=sdk(lambda request: response)).complete("hi")
    assert str(e.value).startswith("claude api: ") and needle in str(e.value)
    assert TEST_KEY not in str(e.value)


async def test_claude_api_unreachable_says_network():
    """2026-11-06: the PC lost DNS for five hours; the alarm should name the network."""
    def handler(request):
        raise httpx2.ConnectError("getaddrinfo failed", request=request)
    with pytest.raises(ModelError, match=r"^claude api: can't reach the API \(network or DNS\)$"):
        await ClaudeAPI(client=sdk(handler)).complete("hi")


@pytest.mark.parametrize("response,needle", [
    (streamed(None, stop="refusal"), "declined to answer (refusal"),
    (streamed('{"assets": [', stop="max_tokens"), f"cut off at {models.API_MAX_TOKENS} tokens"),
    (streamed(None), "empty reply (stop reason end_turn)"),
], ids=["refusal", "cut-off", "no-text"])
async def test_claude_api_a_reply_that_is_not_an_answer_is_an_error(response, needle):
    with pytest.raises(ModelError) as e:
        await ClaudeAPI(client=sdk(lambda request: response)).complete("hi")
    assert needle in str(e.value)


async def test_claude_api_logs_tokens_and_says_when_the_fallback_answered(caplog):
    caplog.set_level("INFO", logger="ottr.council.models")
    out = await ClaudeAPI(client=sdk(lambda request: streamed("ok", model="claude-opus-4-8"))).complete("hi")
    assert out == "ok"
    assert "claude api claude-opus-4-8 (effort high): 1234 input, 567 output tokens" in caplog.text
    assert "answered by the fallback; claude-opus-5-5 declined" in caplog.text


async def test_claude_api_without_a_key_says_how_to_fix_it_and_sends_nothing(monkeypatch):
    monkeypatch.delenv("CLAUDE_API_KEY", raising=False)
    with pytest.raises(ModelError, match=r"CLAUDE_API_KEY is not set: make a new key in the Claude Console"):
        await ClaudeAPI().complete("hi")
    assert ClaudeAPI().locate() is None
    monkeypatch.setenv("CLAUDE_API_KEY", TEST_KEY)
    assert ClaudeAPI().locate() == "the Claude API (CLAUDE_API_KEY set)"


async def test_claude_api_uses_only_its_own_key(monkeypatch):
    """Not the SDK's default ANTHROPIC_API_KEY: the model CLIs inherit the engine's
    environment, and the claude CLI would bill a key under that name instead of the
    subscription after a switch back to claude_backend = "cli"."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "some-other-key")
    monkeypatch.delenv("CLAUDE_API_KEY", raising=False)
    with pytest.raises(ModelError, match="CLAUDE_API_KEY is not set"):
        await ClaudeAPI().complete("hi")
    monkeypatch.setenv("CLAUDE_API_KEY", TEST_KEY)
    async with models._sdk() as client:
        assert client.api_key == TEST_KEY


async def test_claude_api_timeout_caps_the_whole_call():
    async def handler(request):
        await asyncio.sleep(5)
        return streamed("late")
    with pytest.raises(ModelError, match="claude api: timed out after"):
        await ClaudeAPI(client=sdk(handler)).complete("hi", timeout=0.3)


def test_make_adapter_follows_the_claude_backend():
    api = make_adapter("claude", Council(claude_backend="api", claude_model="claude-opus-5-5", reasoning_effort="high"))
    assert isinstance(api, ClaudeAPI) and (api.model, api.effort) == ("claude-opus-5-5", "high")
    cli = make_adapter("claude", Council(claude_backend="cli", claude_model="opus"), effort="low")
    assert isinstance(cli, ClaudeCLI) and (cli.model, cli.effort) == ("opus", "low")
    assert isinstance(make_adapter("claude", Council()), ClaudeAPI)   # the default


async def test_check_claude_api_looks_the_model_up_without_spending_tokens(monkeypatch):
    seen = []

    def ok(request):
        seen.append(request)
        return httpx2.Response(200, json={"type": "model", "id": "claude-opus-5-5", "display_name": "Claude Opus 5.5",
                                          "created_at": "2026-09-01T00:00:00Z"})
    assert await check_claude_api("claude-opus-5-5", client=sdk(ok)) == (
        True, "key accepted; claude-opus-5-5 (Claude Opus 5.5) is available")
    assert [(r.method, r.url.path) for r in seen] == [("GET", "/v1/models/claude-opus-5-5")]
    bad = await check_claude_api("claude-opus-5-5", client=sdk(lambda r: api_error(401, "authentication_error", "no")))
    assert bad[0] is False and "was rejected" in bad[1]
    monkeypatch.delenv("CLAUDE_API_KEY", raising=False)
    assert (await check_claude_api("claude-opus-5-5"))[1].startswith("CLAUDE_API_KEY is not set")


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("CLAUDE_API_KEY"), reason="CLAUDE_API_KEY not set")
async def test_live_claude_api_answers():
    reply = await ClaudeAPI(effort="low").complete("Reply with exactly the word APIOK.", timeout=120)
    assert "APIOK" in reply
