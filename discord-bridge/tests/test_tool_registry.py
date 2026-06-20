"""Every declared tool schema must have a working handler branch in
handle_tool_call — guards against the 'tool advertised but doesn't run' class
of bug.
"""
import pytest
from unittest.mock import AsyncMock

from bot.tools import handle_tool_call, READ_TOOLS, ACTION_TOOLS

DECLARED = [t["function"]["name"] for t in (READ_TOOLS + ACTION_TOOLS)]


@pytest.fixture(autouse=True)
def _mock_deps(monkeypatch, mocker):
    # Mutating tools short-circuit safely under dry-run; mock read deps + scheduler.
    monkeypatch.setenv("TRADING_DRY_RUN", "1")
    mocker.patch("bot.tools.price_feed.get_prices",
                 new=AsyncMock(return_value={"BTC": {"price": 1.0, "change_24h": 0.0}}))
    mocker.patch("bot.tools.price_feed.get_volatility", new=AsyncMock(return_value={"BTC": 0.1}))
    mocker.patch("bot.tools.portfolio.get_summary", return_value={"cash": 0})
    mocker.patch("bot.tools.meeting_scheduler.schedule_dynamic_meeting")
    mocker.patch("bot.tools.meeting_scheduler.schedule_emergency", new=AsyncMock())


def test_at_least_the_core_tools_are_declared():
    for name in ("get_asset_price", "get_portfolio_summary", "execute_trade"):
        assert name in DECLARED


@pytest.mark.parametrize("name", DECLARED)
@pytest.mark.asyncio
async def test_every_declared_tool_has_handler(name):
    res = await handle_tool_call(name, {})
    # The catch-all "not found" sentinel must NOT fire for a declared tool.
    assert res != f"Error: Tool {name} not found."


@pytest.mark.asyncio
async def test_unknown_tool_returns_sentinel():
    res = await handle_tool_call("nonexistent_tool", {})
    assert res == "Error: Tool nonexistent_tool not found."
