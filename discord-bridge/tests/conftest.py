"""Shared pytest fixtures for the discord-bridge suite."""
import pytest
from unittest.mock import AsyncMock


@pytest.fixture(autouse=True)
def _isolate_equity_log(tmp_path, monkeypatch):
    """Redirect the equity-curve log to a temp file for every test.

    The meeting/scheduler paths snapshot equity via the real `bot.equity`
    singleton paths; without this, tests that drive a full _execute_meeting
    would append to the live data/equity_curve.jsonl. Live `data/` is shared
    state the suite must never mutate (same rule as the portfolio fixture).
    """
    import bot.equity as _equity
    monkeypatch.setattr(_equity, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(_equity, "_EQUITY_FILE", tmp_path / "equity_curve.jsonl")


@pytest.fixture(autouse=True)
def _isolate_risk_state(tmp_path, monkeypatch):
    """Redirect the risk-state latch to a temp file for every test.

    bot.risk_state is the sole writer of data/risk_state.json. Live data/ is shared
    state the suite must never mutate (same rule as the equity/portfolio fixtures), so
    point it at tmp for any test that loads or saves the latch.
    """
    import bot.risk_state as _risk_state
    monkeypatch.setattr(_risk_state, "_DATA_DIR", tmp_path)
    monkeypatch.setattr(_risk_state, "_STATE_FILE", tmp_path / "risk_state.json")


@pytest.fixture(autouse=True)
def _isolate_audit_log(tmp_path, monkeypatch):
    """Redirect the audit log to a temp file for every test.

    bot.audit.audit_event appends to AUDIT_LOG_FILE (or the live data/audit_log.jsonl when
    unset). Mutating-tool tests (execute_trade, place_limit_order, cancel_orders, risk
    actions) call audit_event, so without this they leak fake records into the live audit
    trail — e.g. the recurring 'order_placed ord124' lines from test_protective_orders that
    looked like a real $90k take-profit. Live data/ is shared state the suite must never
    mutate (same rule as the equity / risk-state fixtures).
    """
    monkeypatch.setenv("AUDIT_LOG_FILE", str(tmp_path / "audit_log.jsonl"))


@pytest.fixture(autouse=True)
def _no_real_api_server(mocker):
    """Stop on_ready() tests from binding the real aiohttp port (:8001).

    Several tests exercise TradingFloorBot.on_ready(), which calls
    start_api_server() and binds a real socket. Across a full run the socket
    isn't freed between tests, causing intermittent 'address already in use'
    failures. on_ready tests care about channel resolution, not the live server,
    so mock it out everywhere.
    """
    try:
        mocker.patch("bot.main.start_api_server", new_callable=AsyncMock)
    except (ImportError, AttributeError):
        pass
