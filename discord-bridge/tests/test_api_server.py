"""Auth tests for the discord-bridge /api/directive endpoint, plus the
read-only /api/performance metrics endpoint."""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from bot import api_server, equity


class FakeRequest:
    def __init__(self, headers, json_data, bot):
        self.headers = headers
        self._json = json_data
        self.app = {"bot": bot}

    async def json(self):
        return self._json


def make_bot():
    bot = MagicMock()
    bot._trading_floor_channel = MagicMock()
    bot._trading_floor_channel.send = AsyncMock()
    return bot


@pytest.mark.asyncio
async def test_directive_rejected_without_key(monkeypatch):
    monkeypatch.setenv("OTTR_API_KEY", "secret")
    bot = make_bot()
    resp = await api_server.handle_directive(FakeRequest({}, {"message": "SELL EVERYTHING"}, bot))
    assert resp.status == 401
    bot._trading_floor_channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_directive_rejected_with_wrong_key(monkeypatch):
    monkeypatch.setenv("OTTR_API_KEY", "secret")
    bot = make_bot()
    resp = await api_server.handle_directive(FakeRequest({"X-API-Key": "wrong"}, {"message": "x"}, bot))
    assert resp.status == 401
    bot._trading_floor_channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_directive_fails_closed_when_unconfigured(monkeypatch):
    monkeypatch.delenv("OTTR_API_KEY", raising=False)
    bot = make_bot()
    resp = await api_server.handle_directive(FakeRequest({"X-API-Key": "anything"}, {"message": "x"}, bot))
    assert resp.status == 503
    bot._trading_floor_channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_directive_accepted_with_valid_key(monkeypatch):
    monkeypatch.setenv("OTTR_API_KEY", "secret")
    bot = make_bot()
    resp = await api_server.handle_directive(FakeRequest({"X-API-Key": "secret"}, {"message": "check drawdown"}, bot))
    assert resp.status == 200
    bot._trading_floor_channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_directive_errors_are_not_leaked(monkeypatch):
    monkeypatch.setenv("OTTR_API_KEY", "secret")
    bot = make_bot()
    bot._trading_floor_channel.send = AsyncMock(side_effect=Exception("internal path /secret/x"))
    resp = await api_server.handle_directive(FakeRequest({"X-API-Key": "secret"}, {"message": "x"}, bot))
    assert resp.status == 500
    # The raw exception text must not be returned to the caller.
    body = json.loads(resp.body.decode())
    assert body["reason"] == "Internal server error"


# --- /api/performance (read-only metrics) ---------------------------------
# The equity log is isolated to a tmp file by the autouse conftest fixture, so
# these tests neither read nor write the live data/equity_curve.jsonl.


@pytest.mark.asyncio
async def test_performance_empty_curve_reports_insufficient():
    resp = await api_server.handle_performance(None)
    assert resp.status == 200
    body = json.loads(resp.body.decode())
    assert body["num_points"] == 0
    assert body["metrics"]["insufficient_data"] is True


@pytest.mark.asyncio
async def test_performance_computes_metrics_and_benchmark():
    rows = [
        {"ts": 0.0, "total_value": 100.0, "btc_price": 20000.0},
        {"ts": 86400.0, "total_value": 110.0, "btc_price": 21000.0},  # strat +10%, BTC +5%
    ]
    equity._EQUITY_FILE.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )

    resp = await api_server.handle_performance(None)
    assert resp.status == 200
    body = json.loads(resp.body.decode())
    assert body["num_points"] == 2
    m = body["metrics"]
    assert m["insufficient_data"] is False
    assert abs(m["total_return"] - 0.10) < 1e-9
    assert abs(m["benchmark_return"] - 0.05) < 1e-9
    assert abs(m["alpha"] - 0.05) < 1e-9


@pytest.mark.asyncio
async def test_performance_includes_risk_block(mocker):
    # Tier 3: the read-only risk block reflects the configured switch + the persisted latch.
    mocker.patch.dict("bot.settings", {"risk_limits": {"enabled": True}})
    import bot.risk_state as rs
    rs.save({"halted": True, "halted_since": 123.0, "last_action_ts": {}})
    resp = await api_server.handle_performance(None)
    assert resp.status == 200
    body = json.loads(resp.body.decode())
    assert "risk" in body
    assert body["risk"]["enabled"] is True         # reflects the configured switch
    assert body["risk"]["halted"] is True          # reflects the persisted latch
    assert body["risk"]["halted_since"] == 123.0


# --- /api/health (component health) ---------------------------------------

@pytest.mark.asyncio
async def test_health_reports_components(monkeypatch):
    # Tier 4: a read-only component-health rollup. Mock the LLM ping for determinism.
    from bot import agents
    monkeypatch.setattr(agents.agent_llm, "check_health", AsyncMock(return_value=True))
    resp = await api_server.handle_health(None)
    assert resp.status == 200
    body = json.loads(resp.body.decode())
    assert body["status"] in ("OK", "DEGRADED", "DOWN")
    assert set(body["components"]) >= {"llm", "price_feed", "scheduler", "portfolio"}
    assert body["components"]["llm"]["status"] == "OK"
