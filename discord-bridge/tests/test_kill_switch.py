"""Tests for the TRADING_DRY_RUN kill-switch in bot.tools."""
import asyncio
import pytest

from bot import tools
from bot.tools import handle_tool_call, dry_run_enabled, DRY_RUN_BLOCKED_TOOLS


@pytest.fixture
def dry_run_on(monkeypatch):
    monkeypatch.setenv("TRADING_DRY_RUN", "1")


@pytest.fixture
def dry_run_off(monkeypatch):
    monkeypatch.setenv("TRADING_DRY_RUN", "0")


def test_flag_parsing(monkeypatch):
    for val in ("1", "true", "TRUE", "yes", "on"):
        monkeypatch.setenv("TRADING_DRY_RUN", val)
        assert dry_run_enabled() is True
    for val in ("0", "false", "no", "", "off"):
        monkeypatch.setenv("TRADING_DRY_RUN", val)
        assert dry_run_enabled() is False


@pytest.mark.asyncio
async def test_mutating_tools_blocked_under_dry_run(dry_run_on, mocker):
    # Ensure no real execution path is reached: spy on the portfolio.
    buy = mocker.patch("bot.tools.portfolio.buy")
    sell = mocker.patch("bot.tools.portfolio.sell")
    audited = []

    async def audit(msg):
        audited.append(msg)

    for name in DRY_RUN_BLOCKED_TOOLS:
        result = await handle_tool_call(name, {"action": "BUY", "asset": "BTC", "amount": 100, "value": 1}, audit_log_fn=audit)
        assert "[DRY-RUN]" in result

    buy.assert_not_called()
    sell.assert_not_called()
    assert len(audited) == len(DRY_RUN_BLOCKED_TOOLS)


@pytest.mark.asyncio
async def test_read_tools_allowed_under_dry_run(dry_run_on, mocker):
    mocker.patch(
        "bot.tools.price_feed.get_prices",
        new=mocker.AsyncMock(return_value={"BTC": {"price": 50000.0, "change_24h": 0.0}}),
    )
    result = await handle_tool_call("get_asset_price", {"symbol": "BTC"})
    assert "50000" in result
    assert "[DRY-RUN]" not in result
