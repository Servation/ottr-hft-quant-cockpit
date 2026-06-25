"""Tests for the structured audit trail (Phase 6)."""
import json
import pytest
from unittest.mock import AsyncMock

from bot import audit
from bot.tools import handle_tool_call


@pytest.fixture
def audit_file(tmp_path, monkeypatch):
    f = tmp_path / "audit.jsonl"
    monkeypatch.setenv("AUDIT_LOG_FILE", str(f))
    return f


def _read(f):
    return [json.loads(line) for line in f.read_text().splitlines() if line.strip()]


def test_audit_event_writes_jsonl(audit_file):
    audit.audit_event("trade", action="BUY", asset="BTC", usd_amount=100)
    rows = _read(audit_file)
    assert len(rows) == 1
    assert rows[0]["kind"] == "trade"
    assert rows[0]["action"] == "BUY"
    assert "ts" in rows[0]


@pytest.mark.asyncio
async def test_trade_execution_is_audited(audit_file, monkeypatch, mocker):
    monkeypatch.setenv("TRADING_DRY_RUN", "0")
    monkeypatch.delenv("MAX_TRADE_USD", raising=False)
    mocker.patch("bot.tools.price_feed.get_prices",
                 new=AsyncMock(return_value={"BTC": {"price": 50000.0, "change_24h": 0.0}}))
    # Keep position sizing inert so it doesn't resize/block this audited test trade.
    mocker.patch("bot.tools.price_feed.get_volatility", new=AsyncMock(return_value={}))
    mocker.patch("bot.tools.price_feed.get_technical_indicators", new=AsyncMock(return_value={}))
    mocker.patch("bot.tools.portfolio.get_total_value", return_value=1e6)
    mocker.patch("bot.tools.portfolio.buy",
                 return_value={"quantity": 0.01, "fill_price": 50000.0})
    await handle_tool_call("execute_trade", {"action": "BUY", "asset": "BTC", "amount": 500})
    rows = _read(audit_file)
    assert any(r["kind"] == "trade" and r["action"] == "BUY" and r["asset"] == "BTC" for r in rows)


@pytest.mark.asyncio
async def test_dry_run_block_is_audited(audit_file, monkeypatch):
    monkeypatch.setenv("TRADING_DRY_RUN", "1")
    await handle_tool_call("execute_trade", {"action": "BUY", "asset": "BTC", "amount": 500})
    rows = _read(audit_file)
    assert any(r["kind"] == "tool_blocked" and r["reason"] == "dry_run" for r in rows)
