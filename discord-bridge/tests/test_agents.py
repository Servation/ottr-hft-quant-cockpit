"""Tests for AgentLLM.generate_response resilience (retry-on-empty guard)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from bot.agents import agent_llm


def _resp(content, tool_calls=None):
    """Build a minimal OpenAI-style chat completion response."""
    msg = MagicMock()
    msg.content = content
    msg.tool_calls = tool_calls
    msg.model_dump = lambda **k: {"role": "assistant", "content": content}
    choice = MagicMock()
    choice.message = msg
    r = MagicMock()
    r.choices = [choice]
    return r


def _native_call(name, arguments, call_id="call_1"):
    """Build a native tool_call object as the OpenAI client would return it."""
    tc = MagicMock()
    tc.function.name = name
    tc.function.arguments = arguments
    tc.id = call_id
    return tc


@pytest.fixture(autouse=True)
def _no_backoff(mocker):
    mocker.patch("asyncio.sleep", new_callable=AsyncMock)


@pytest.mark.asyncio
async def test_retries_once_on_empty_response(mocker):
    """An empty completion (no side effects) is retried once and recovers."""
    calls = []

    async def fake_create(**kwargs):
        calls.append(1)
        return _resp("" if len(calls) == 1 else "Initial Assessment: HOLD BTC")

    mocker.patch.object(agent_llm._client.chat.completions, "create", side_effect=fake_create)
    resp, _ = await agent_llm.generate_response("technical_analyst", [{"role": "user", "content": "x"}])

    assert resp == "Initial Assessment: HOLD BTC"
    assert len(calls) == 2  # original + one retry


@pytest.mark.asyncio
async def test_does_not_retry_forever_when_persistently_empty(mocker):
    """A persistently-empty model retries exactly once, then gives up (no loop)."""
    calls = []

    async def fake_create(**kwargs):
        calls.append(1)
        return _resp("")

    mocker.patch.object(agent_llm._client.chat.completions, "create", side_effect=fake_create)
    resp, _ = await agent_llm.generate_response("technical_analyst", [{"role": "user", "content": "x"}])

    assert resp == ""
    assert len(calls) == 2  # one retry only


@pytest.mark.asyncio
async def test_no_retry_after_a_tool_executed(mocker):
    """SAFETY: if a tool ran, an empty final text must NOT trigger a retry (a retry
    could re-execute a trade). The tool's side effect happened; return as-is."""
    calls = []
    tc = MagicMock()
    tc.function.name = "get_asset_price"
    tc.function.arguments = "{}"
    tc.id = "call_1"

    async def fake_create(**kwargs):
        calls.append(1)
        # First turn: a tool call. Second turn: empty final text.
        return _resp(None, tool_calls=[tc]) if len(calls) == 1 else _resp("")

    mocker.patch.object(agent_llm._client.chat.completions, "create", side_effect=fake_create)

    async def handler(name, args):
        return "price: 100"

    resp, _ = await agent_llm.generate_response(
        "technical_analyst", [{"role": "user", "content": "x"}],
        tools=[{"type": "function"}], tool_handler=handler,
    )

    assert resp == ""
    assert len(calls) == 2  # tool turn + final turn, NO third (retry) call


@pytest.mark.asyncio
async def test_text_narrated_tool_call_is_reasked_then_executes(mocker):
    """A model that WRITES `execute_trade(...)` as text (instead of a native call)
    is nudged once, re-emits it natively, and the trade actually executes."""
    calls = []
    executed = []

    async def fake_create(**kwargs):
        calls.append(1)
        if len(calls) == 1:
            # Narrates the call as plain text — no native tool_calls.
            return _resp('Final Decision: Execute\nexecute_trade(action="BUY", asset="ADA", amount=500)')
        if len(calls) == 2:
            # After the re-ask, emits it natively.
            return _resp(None, tool_calls=[_native_call(
                "execute_trade", '{"action":"BUY","asset":"ADA","amount":500}')])
        return _resp("Done — BUY ADA executed.")

    mocker.patch.object(agent_llm._client.chat.completions, "create", side_effect=fake_create)

    async def handler(name, args):
        executed.append((name, args))
        return "FILLED"

    resp, _ = await agent_llm.generate_response(
        "meeting_chair", [{"role": "user", "content": "close the meeting"}],
        tools=[{"type": "function"}], tool_handler=handler,
    )

    assert executed == [("execute_trade", {"action": "BUY", "asset": "ADA", "amount": 500})]
    assert len(calls) == 3  # narrate -> re-ask -> native exec -> final text
    assert "Done" in resp


@pytest.mark.asyncio
async def test_persistent_text_narration_reasks_only_once(mocker):
    """SAFETY: if the model keeps narrating instead of calling natively, the re-ask
    happens at most once (bounded), and nothing is executed from free text."""
    calls = []
    executed = []

    async def fake_create(**kwargs):
        calls.append(1)
        return _resp('Final Decision: Execute\nexecute_trade(action="BUY", asset="ADA", amount=500)')

    mocker.patch.object(agent_llm._client.chat.completions, "create", side_effect=fake_create)

    async def handler(name, args):
        executed.append((name, args))
        return "FILLED"

    resp, _ = await agent_llm.generate_response(
        "meeting_chair", [{"role": "user", "content": "close the meeting"}],
        tools=[{"type": "function"}], tool_handler=handler,
    )

    assert executed == []           # never executes a trade from plain text
    assert len(calls) == 2          # original + exactly one re-ask, then gives up
    assert "execute_trade" in resp  # returns the (unexecuted) narration as-is
