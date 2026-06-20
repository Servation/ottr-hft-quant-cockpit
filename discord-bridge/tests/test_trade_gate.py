"""Tests for the MAX_TRADE_USD trade-execution authorization gate (Phase 2)."""
import pytest
from unittest.mock import AsyncMock

from bot.tools import handle_tool_call

PRICES = {"BTC": {"price": 50000.0, "change_24h": 0.0}}


@pytest.fixture(autouse=True)
def _no_dry_run(monkeypatch, mocker):
    monkeypatch.setenv("TRADING_DRY_RUN", "0")  # ensure kill-switch isn't what blocks
    mocker.patch("bot.tools.price_feed.get_prices", new=AsyncMock(return_value=PRICES))


@pytest.mark.asyncio
async def test_buy_over_cap_blocked(monkeypatch, mocker):
    monkeypatch.setenv("MAX_TRADE_USD", "1000")
    buy = mocker.patch("bot.tools.portfolio.buy")
    res = await handle_tool_call("execute_trade", {"action": "BUY", "asset": "BTC", "amount": 5000})
    assert "blocked" in res.lower()
    buy.assert_not_called()


@pytest.mark.asyncio
async def test_buy_under_cap_executes(monkeypatch, mocker):
    monkeypatch.setenv("MAX_TRADE_USD", "1000")
    buy = mocker.patch("bot.tools.portfolio.buy", return_value={"quantity": 0.01, "fill_price": 50000.0})
    await handle_tool_call("execute_trade", {"action": "BUY", "asset": "BTC", "amount": 500})
    buy.assert_called_once()


@pytest.mark.asyncio
async def test_sell_over_cap_blocked(monkeypatch, mocker):
    monkeypatch.setenv("MAX_TRADE_USD", "1000")
    sell = mocker.patch("bot.tools.portfolio.sell")
    # 1 BTC * $50,000 = $50,000 notional > $1,000 cap
    res = await handle_tool_call("execute_trade", {"action": "SELL", "asset": "BTC", "amount": 1})
    assert "blocked" in res.lower()
    sell.assert_not_called()


@pytest.mark.asyncio
async def test_no_cap_when_unset(monkeypatch, mocker):
    monkeypatch.delenv("MAX_TRADE_USD", raising=False)
    buy = mocker.patch("bot.tools.portfolio.buy", return_value={"quantity": 20.0, "fill_price": 50000.0})
    await handle_tool_call("execute_trade", {"action": "BUY", "asset": "BTC", "amount": 1000000})
    buy.assert_called_once()  # gate disabled -> not blocked
