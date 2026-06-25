"""Trade rationale capture (Tier 4 / O1).

The agent's "why" flows from the execute_trade tool -> the trade record -> the audit log
-> the live execution event (and on to the dashboard's decision disclosure, which already
renders it). These tests cover the bridge end of that chain.
"""

import pytest
from unittest.mock import AsyncMock

from bot import webhooks
from bot.portfolio import Portfolio
from bot.tools import handle_tool_call


def test_portfolio_buy_sell_store_reasoning(tmp_path, monkeypatch):
    import bot.portfolio as pmod
    monkeypatch.setattr(pmod, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(pmod, "_PORTFOLIO_FILE", tmp_path / "pf.json")
    pf = Portfolio()
    pf._state["cash"] = 100000.0

    bought = pf.buy("BTC", 1000.0, 50000.0, reasoning="EMA cross + bullish regime")
    assert bought["reasoning"] == "EMA cross + bullish regime"
    assert pf._state["trade_history"][-1]["reasoning"] == "EMA cross + bullish regime"  # persisted

    sold = pf.sell("BTC", bought["quantity"], 51000.0, reasoning="hit take-profit")
    assert sold["reasoning"] == "hit take-profit"


def test_execution_payload_carries_reasoning():
    trade = {"asset": "BTC", "action": "BUY", "quantity": 0.1, "fill_price": 50000.0,
             "reasoning": "oversold bounce"}
    assert webhooks._execution_payload(trade)["reasoning"] == "oversold bounce"


@pytest.mark.asyncio
async def test_execute_trade_threads_reasoning(monkeypatch, mocker):
    monkeypatch.setenv("TRADING_DRY_RUN", "0")
    monkeypatch.delenv("MAX_TRADE_USD", raising=False)
    mocker.patch("bot.tools.price_feed.get_prices",
                 new=AsyncMock(return_value={"BTC": {"price": 50000.0, "change_24h": 0.0}}))
    mocker.patch("bot.tools.price_feed.get_volatility", new=AsyncMock(return_value={}))
    mocker.patch("bot.tools.price_feed.get_technical_indicators", new=AsyncMock(return_value={}))
    mocker.patch("bot.tools.portfolio.get_total_value", return_value=1e9)
    mocker.patch.dict("bot.tools.portfolio._state", {"holdings": {}})
    buy = mocker.patch("bot.tools.portfolio.buy",
                       return_value={"asset": "BTC", "action": "BUY", "quantity": 0.001,
                                     "fill_price": 50000.0, "reasoning": "breakout confirmed"})
    audit = mocker.patch("bot.tools.audit_event")

    await handle_tool_call("execute_trade",
                           {"action": "BUY", "asset": "BTC", "amount": 500,
                            "reasoning": "breakout confirmed"})

    assert buy.call_args.kwargs.get("reasoning") == "breakout confirmed"     # to the writer
    trade_audits = [c for c in audit.call_args_list if c.args and c.args[0] == "trade"]
    assert trade_audits and trade_audits[0].kwargs.get("reasoning") == "breakout confirmed"  # audited
