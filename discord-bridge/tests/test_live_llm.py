"""Direct online smoke tests against the local LM Studio model.

Named *live* so the default `pytest -k "not live"` gate skips them; run them explicitly with
LM Studio up to verify the REAL model path end to end against the local model:
connectivity, a real completion through generate_response, and a real tool-call loop (the
spine of every meeting).

The agent_llm singleton bakes its base_url from .env at import (often the Docker
host.docker.internal value the host can't reach), so these resolve a reachable local endpoint
themselves and re-point the client for the test — mirroring run_evals.py. The probe is lazy
(inside the fixture) so a normal `-k "not live"` run never touches the network: the tests are
deselected by name before the fixture ever runs.
"""
import asyncio
import os

import pytest
from openai import AsyncOpenAI

from bot.agents import agent_llm, AGENTS


def _candidate_urls():
    """Local endpoints to try, in order. LLM_LIVE_BASE_URL lets a caller force one."""
    urls = [
        os.getenv("LLM_LIVE_BASE_URL"),
        "http://127.0.0.1:1234/v1",
        "http://localhost:1234/v1",
        str(agent_llm._client.base_url),  # whatever .env configured (e.g. Docker)
    ]
    out = []
    for u in urls:
        u = (u or "").rstrip("/")
        if u and u not in out:
            out.append(u)
    return out


async def _first_reachable():
    for url in _candidate_urls():
        try:
            await AsyncOpenAI(base_url=url, api_key="lm-studio").models.list(timeout=3.0)
            return url
        except Exception:
            continue
    return None


@pytest.fixture
def local_llm(monkeypatch):
    """Point the real agent_llm singleton at a reachable local endpoint for the test, so we
    exercise the actual generate_response path against the live local model. Skips (not fails)
    when no local model is reachable, so the suite stays green without LM Studio."""
    url = asyncio.run(_first_reachable())
    if url is None:
        pytest.skip("local LM Studio not reachable on any candidate endpoint")
    monkeypatch.setattr(agent_llm, "_client", AsyncOpenAI(base_url=url, api_key="lm-studio"))
    return agent_llm


@pytest.mark.asyncio
async def test_live_llm_health(local_llm):
    """The local model server answers a models.list ping (the same check meetings gate on)."""
    assert await local_llm.check_health() is True


@pytest.mark.asyncio
async def test_live_llm_generates_response(local_llm):
    """generate_response returns a real, non-error completion from the local model."""
    agent_id = next(iter(AGENTS))  # any real persona
    content, latency = await local_llm.generate_response(
        agent_id,
        [{"role": "user", "content": "Reply with exactly one word: ACK"}],
        max_tokens=16,
    )
    assert isinstance(content, str) and content.strip(), "expected non-empty content"
    assert not content.startswith("Error"), f"generate_response errored: {content!r}"
    assert latency > 0.0


@pytest.mark.asyncio
async def test_live_llm_tool_call_loop(local_llm):
    """The tool-enabled path works against the local model: given a tool, the model either
    invokes it (preferred — this is the meeting spine) or still returns a usable reply. Either
    way the loop must complete without error, and if a tool fired it must be the one offered."""
    called = []

    async def handler(name, args):
        called.append(name)
        return "Portfolio: $8,393 cash, 0.015 BTC."

    tools = [{
        "type": "function",
        "function": {
            "name": "get_portfolio",
            "description": "Return the desk's current cash and holdings.",
            "parameters": {"type": "object", "properties": {}},
        },
    }]
    agent_id = next(iter(AGENTS))
    content, _ = await local_llm.generate_response(
        agent_id,
        [{"role": "user", "content": "Use your get_portfolio tool to check holdings, then reply briefly."}],
        tools=tools,
        tool_handler=handler,
        max_tokens=200,
    )
    assert isinstance(content, str) and content.strip()
    assert not content.startswith("Error"), f"tool-enabled call errored: {content!r}"
    # If the model chose to tool-call, it must have called the only tool offered.
    assert all(name == "get_portfolio" for name in called)
