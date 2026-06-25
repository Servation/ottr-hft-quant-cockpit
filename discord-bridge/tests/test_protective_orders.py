"""Agent-placed protective orders (Tier 3 / F2).

place_limit_order now accepts order_type STOP / TAKE_PROFIT (SELL-side), which
portfolio.check_orders already executes. These tests cover the tool layer: the type
flows through to place_order, STOP/TAKE_PROFIT are rejected for a BUY, and LIMIT stays
the default (backward compatible). The portfolio singleton's place_order is mocked, so
nothing real is written.
"""

import pytest

from bot.tools import handle_tool_call


@pytest.fixture(autouse=True)
def _no_dry_run(monkeypatch):
    # place_limit_order is kill-switch-blocked; ensure the gate isn't what we're testing.
    monkeypatch.setenv("TRADING_DRY_RUN", "0")


@pytest.mark.asyncio
async def test_place_stop_order(mocker):
    place = mocker.patch("bot.tools.portfolio.place_order", return_value="ord123")
    res = await handle_tool_call("place_limit_order", {
        "action": "SELL", "asset": "SOL", "amount": 10, "target_price": 60, "order_type": "STOP",
    })
    place.assert_called_once()
    assert place.call_args[0][0] == "STOP"            # order_type flows through
    assert "Stop-Loss Order" in res and "≤" in res    # stop triggers on a drop


@pytest.mark.asyncio
async def test_place_take_profit_order(mocker):
    place = mocker.patch("bot.tools.portfolio.place_order", return_value="ord124")
    res = await handle_tool_call("place_limit_order", {
        "action": "SELL", "asset": "BTC", "amount": 0.1, "target_price": 90000,
        "order_type": "TAKE_PROFIT",
    })
    assert place.call_args[0][0] == "TAKE_PROFIT"
    assert "Take-Profit Order" in res and "≥" in res  # take-profit triggers on a rise


@pytest.mark.asyncio
async def test_stop_must_be_sell_side(mocker):
    place = mocker.patch("bot.tools.portfolio.place_order")
    res = await handle_tool_call("place_limit_order", {
        "action": "BUY", "asset": "BTC", "amount": 100, "target_price": 50000, "order_type": "STOP",
    })
    assert "SELL-side only" in res
    place.assert_not_called()


@pytest.mark.asyncio
async def test_defaults_to_limit(mocker):
    place = mocker.patch("bot.tools.portfolio.place_order", return_value="ord125")
    await handle_tool_call("place_limit_order", {
        "action": "BUY", "asset": "ETH", "amount": 500, "target_price": 1500,
    })
    assert place.call_args[0][0] == "LIMIT"           # backward compatible (no order_type)


@pytest.mark.asyncio
async def test_invalid_order_type_rejected(mocker):
    place = mocker.patch("bot.tools.portfolio.place_order")
    res = await handle_tool_call("place_limit_order", {
        "action": "SELL", "asset": "BTC", "amount": 1, "target_price": 60000, "order_type": "BRACKET",
    })
    assert "invalid order_type" in res
    place.assert_not_called()
