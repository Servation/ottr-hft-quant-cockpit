"""Tests for the MAX_TRADE_USD trade-execution authorization gate (Phase 2)."""
import pytest
from unittest.mock import AsyncMock

from bot.tools import handle_tool_call

PRICES = {"BTC": {"price": 50000.0, "change_24h": 0.0}}


@pytest.fixture(autouse=True)
def _no_dry_run(monkeypatch, mocker):
    monkeypatch.setenv("TRADING_DRY_RUN", "0")  # ensure kill-switch isn't what blocks
    mocker.patch("bot.tools.price_feed.get_prices", new=AsyncMock(return_value=PRICES))
    # Neutralize the new sizing + concentration guards for the MAX_TRADE_USD tests;
    # the sizing/concentration tests below override these with specific values.
    mocker.patch("bot.tools.price_feed.get_volatility", new=AsyncMock(return_value={}))
    mocker.patch("bot.tools.price_feed.get_technical_indicators", new=AsyncMock(return_value={}))
    mocker.patch("bot.tools.portfolio.get_total_value", return_value=1e9)


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


# --- Regime/volatility position sizing (S2) -------------------------------

@pytest.mark.asyncio
async def test_buy_resized_in_trending_regime(monkeypatch, mocker):
    monkeypatch.delenv("MAX_TRADE_USD", raising=False)
    mocker.patch("bot.tools.price_feed.get_volatility", new=AsyncMock(return_value={"BTC": 0.8}))
    mocker.patch("bot.tools.price_feed.get_technical_indicators",
                 new=AsyncMock(return_value={"BTC": {"regime": "TRENDING"}}))
    mocker.patch("bot.tools.portfolio.get_total_value", return_value=10000.0)
    mocker.patch.dict("bot.tools.portfolio._state", {"min_trade_usd": 100.0, "holdings": {}})
    buy = mocker.patch("bot.tools.portfolio.buy", return_value={"quantity": 0.005, "fill_price": 50000.0})

    await handle_tool_call("execute_trade", {"action": "BUY", "asset": "BTC", "amount": 3000})

    buy.assert_called_once()
    # $3000 passes the 35% cap (23%), then sizing resizes it: 2% * 10000 / 0.8 = $250.
    called_amount = buy.call_args[0][1]
    assert abs(called_amount - 250.0) < 1.0


@pytest.mark.asyncio
async def test_buy_sized_out_in_choppy_regime(monkeypatch, mocker):
    monkeypatch.delenv("MAX_TRADE_USD", raising=False)
    mocker.patch("bot.tools.price_feed.get_volatility", new=AsyncMock(return_value={"BTC": 0.8}))
    mocker.patch("bot.tools.price_feed.get_technical_indicators",
                 new=AsyncMock(return_value={"BTC": {"regime": "CHOPPY"}}))
    mocker.patch("bot.tools.portfolio.get_total_value", return_value=10000.0)
    mocker.patch.dict("bot.tools.portfolio._state", {"min_trade_usd": 100.0, "holdings": {}})
    buy = mocker.patch("bot.tools.portfolio.buy")

    res = await handle_tool_call("execute_trade", {"action": "BUY", "asset": "BTC", "amount": 3000})

    # choppy: $250 * 0.25 = $62.5 < $100 minimum -> blocked, no trade.
    assert "sized out" in res.lower()
    buy.assert_not_called()


# --- Generalized concentration cap ----------------------------------------

@pytest.mark.asyncio
async def test_concentration_cap_blocks_btc(monkeypatch, mocker):
    monkeypatch.delenv("MAX_TRADE_USD", raising=False)
    mocker.patch("bot.tools.portfolio.get_total_value", return_value=1000.0)
    mocker.patch.dict("bot.tools.portfolio._state", {"holdings": {}})
    buy = mocker.patch("bot.tools.portfolio.buy")

    # $3000 BUY on a $1000 book -> 75% of portfolio, over the 35% general cap.
    res = await handle_tool_call("execute_trade", {"action": "BUY", "asset": "BTC", "amount": 3000})

    assert "exposure cap" in res.lower() and "BTC" in res
    buy.assert_not_called()


@pytest.mark.asyncio
async def test_sol_override_is_stricter_than_general(monkeypatch, mocker):
    monkeypatch.delenv("MAX_TRADE_USD", raising=False)
    mocker.patch("bot.tools.price_feed.get_prices",
                 new=AsyncMock(return_value={"SOL": {"price": 100.0, "change_24h": 0.0}}))
    mocker.patch("bot.tools.portfolio.get_total_value", return_value=10000.0)
    mocker.patch.dict("bot.tools.portfolio._state", {"holdings": {}})
    buy = mocker.patch("bot.tools.portfolio.buy")

    # $3000 SOL -> ~23% of portfolio: under the 35% general cap, but over SOL's 20%.
    res = await handle_tool_call("execute_trade", {"action": "BUY", "asset": "SOL", "amount": 3000})

    assert "exposure cap" in res.lower() and "SOL" in res
    buy.assert_not_called()


# --- Drawdown halt gate (Tier 3 R2) ---------------------------------------

@pytest.mark.asyncio
async def test_drawdown_halt_blocks_buy(monkeypatch, mocker):
    monkeypatch.delenv("MAX_TRADE_USD", raising=False)
    mocker.patch.dict("bot.tools.settings", {"risk_limits": {"enabled": True}})
    import bot.risk_state as rs
    rs.save({"halted": True, "halted_since": 1.0, "last_action_ts": {}})
    buy = mocker.patch("bot.tools.portfolio.buy")
    res = await handle_tool_call("execute_trade", {"action": "BUY", "asset": "BTC", "amount": 500})
    assert "halt" in res.lower()
    buy.assert_not_called()


@pytest.mark.asyncio
async def test_drawdown_halt_allows_sell(monkeypatch, mocker):
    monkeypatch.delenv("MAX_TRADE_USD", raising=False)
    mocker.patch.dict("bot.tools.settings", {"risk_limits": {"enabled": True}})
    import bot.risk_state as rs
    rs.save({"halted": True, "halted_since": 1.0, "last_action_ts": {}})
    sell = mocker.patch("bot.tools.portfolio.sell", return_value={"quantity": 0.01, "fill_price": 50000.0})
    await handle_tool_call("execute_trade", {"action": "SELL", "asset": "BTC", "amount": 0.01})
    sell.assert_called_once()   # SELLs (de-risking) stay allowed during a halt


@pytest.mark.asyncio
async def test_drawdown_halt_ignored_while_disabled(monkeypatch, mocker):
    monkeypatch.delenv("MAX_TRADE_USD", raising=False)
    import bot.risk_state as rs
    rs.save({"halted": True, "halted_since": 1.0, "last_action_ts": {}})   # latched but dark
    mocker.patch.dict("bot.tools.portfolio._state", {"holdings": {}})
    buy = mocker.patch("bot.tools.portfolio.buy", return_value={"quantity": 0.01, "fill_price": 50000.0})
    await handle_tool_call("execute_trade", {"action": "BUY", "asset": "BTC", "amount": 500})
    buy.assert_called_once()    # risk_limits.enabled is false -> latch ignored
