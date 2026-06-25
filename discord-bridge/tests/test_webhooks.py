"""Tests for the bridge -> gateway live event pipeline (Tier 4 / O0).

Covers the payload shaping (bridge trade dict -> the frontend's expected shape), that a
trade fires both the execution + portfolio events, that publish is fire-and-forget, and
that execute_trade wires it in. Network sends are mocked — nothing leaves the process.
"""

import pytest
from unittest.mock import AsyncMock

from bot import webhooks


def test_execution_payload_normalizes_shape():
    trade = {"timestamp": 1.0, "asset": "SOL", "action": "SELL", "quantity": 2.5,
             "fill_price": 100.0, "slippage_pct": 0.1, "fee_usd": 0.25}
    p = webhooks._execution_payload(trade)
    assert p["symbol"] == "SOL"        # asset -> symbol (frontend mapLogEntry reads `symbol`)
    assert p["price"] == 100.0         # fill_price -> price
    assert p["action"] == "SELL" and p["quantity"] == 2.5
    assert p["reasoning"] == ""        # O1 populates this


def test_portfolio_payload_shape():
    class _PF:
        def get_summary(self, prices):
            return {"total_portfolio_value": 10000.0, "cash": 5000.0,
                    "holdings": {"BTC": {"quantity": 0.1, "avg_cost": 50000.0, "current_price": 55000.0}}}
    p = webhooks._portfolio_payload(_PF(), {})
    assert p["total_value"] == 10000.0 and p["usd_cash"] == 5000.0
    assert p["holdings"]["BTC"]["quantity"] == 0.1
    assert p["current_prices"]["BTC"] == 55000.0
    assert p["purchase_prices"]["BTC"] == 50000.0


def test_publish_trade_fires_execution_and_portfolio(monkeypatch):
    fired = []
    monkeypatch.setattr(webhooks, "publish", lambda ev, data: fired.append(ev))

    class _PF:
        def get_summary(self, prices):
            return {"total_portfolio_value": 1.0, "cash": 1.0, "holdings": {}}

    trade = {"asset": "BTC", "action": "BUY", "quantity": 0.1, "fill_price": 50000.0}
    webhooks.publish_trade(trade, _PF(), {})
    assert "execution" in fired and "portfolio" in fired


@pytest.mark.asyncio
async def test_publish_is_fire_and_forget(monkeypatch):
    # publish schedules send_gateway_event as a task and returns immediately (no await).
    import asyncio
    scheduled = []

    async def _fake_send(ev, data):
        scheduled.append(ev)

    monkeypatch.setattr(webhooks, "send_gateway_event", _fake_send)
    webhooks.publish("execution", {"x": 1})
    assert scheduled == []          # nothing ran synchronously
    await asyncio.sleep(0)          # let the scheduled task run
    assert scheduled == ["execution"]


@pytest.mark.asyncio
async def test_execute_trade_publishes_event(monkeypatch, mocker):
    from bot.tools import handle_tool_call
    monkeypatch.setenv("TRADING_DRY_RUN", "0")
    monkeypatch.delenv("MAX_TRADE_USD", raising=False)
    mocker.patch("bot.tools.price_feed.get_prices",
                 new=AsyncMock(return_value={"BTC": {"price": 50000.0, "change_24h": 0.0}}))
    mocker.patch("bot.tools.price_feed.get_volatility", new=AsyncMock(return_value={}))
    mocker.patch("bot.tools.price_feed.get_technical_indicators", new=AsyncMock(return_value={}))
    mocker.patch("bot.tools.portfolio.get_total_value", return_value=1e9)
    mocker.patch.dict("bot.tools.portfolio._state", {"holdings": {}})
    mocker.patch("bot.tools.portfolio.buy",
                 return_value={"asset": "BTC", "action": "BUY", "quantity": 0.001, "fill_price": 50000.0})
    pub = mocker.patch("bot.tools.webhooks.publish_trade")

    await handle_tool_call("execute_trade", {"action": "BUY", "asset": "BTC", "amount": 500})
    pub.assert_called_once()
