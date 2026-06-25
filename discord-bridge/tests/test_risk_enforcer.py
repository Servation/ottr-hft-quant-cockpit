"""Tests for the Tier 3 (R1) risk enforcer — the thin execution layer.

Covers the autonomous stop-loss path: it fires once past the threshold, is idempotent
within the cooldown, respects the kill-switch and the master `enabled` switch, and skips
on bad data. The portfolio writer, audit sink, and emergency-meeting call are mocked so
nothing real trades; the risk-state latch is isolated to a temp file by the autouse
`_isolate_risk_state` conftest fixture.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from bot import risk_enforcer, risk_state


def _holdings(**kv):
    """_holdings(BTC=(1.0, 100.0)) -> {'BTC': {'quantity': 1.0, 'avg_cost': 100.0}}."""
    return {asset: {"quantity": q, "avg_cost": c} for asset, (q, c) in kv.items()}


def _prices(**kv):
    return {asset: {"price": price} for asset, price in kv.items()}


@pytest.fixture
def fake_bot():
    bot = MagicMock()
    bot._trading_floor_channel = MagicMock()
    bot._trading_floor_channel.send = AsyncMock()
    bot.post_audit_log = AsyncMock()
    return bot


@pytest.fixture
def patched(monkeypatch):
    """Enable enforcement and stub the portfolio writer, audit sink, and meeting call.

    Returns the `sell` mock so tests can assert on forced exits. A held BTC bought at
    $100 is the standard fixture; feed prices to drive the stop. stop_loss_pct (10) and
    the cooldown (900s) come from the real settings.yaml risk_limits block.
    """
    sell = MagicMock(return_value={"quantity": 1.0, "fill_price": 80.0})
    monkeypatch.setattr(risk_enforcer.portfolio, "sell", sell)
    monkeypatch.setattr(risk_enforcer.portfolio, "_state",
                        {"holdings": _holdings(BTC=(1.0, 100.0))})
    monkeypatch.setattr(risk_enforcer, "audit_event", MagicMock())
    monkeypatch.setattr(risk_enforcer, "_convene_emergency", AsyncMock())
    monkeypatch.setattr(risk_enforcer, "_enabled", lambda: True)
    monkeypatch.setattr(risk_enforcer, "_dry_run", lambda: False)
    return sell


@pytest.mark.asyncio
async def test_disabled_is_a_noop(fake_bot, patched, monkeypatch):
    # Master switch off: a deep loss must NOT trigger a forced sell.
    monkeypatch.setattr(risk_enforcer, "_enabled", lambda: False)
    await risk_enforcer.enforce(fake_bot, _prices(BTC=80.0))   # -20%
    patched.assert_not_called()


@pytest.mark.asyncio
async def test_stop_loss_force_sells_full_position(fake_bot, patched):
    await risk_enforcer.enforce(fake_bot, _prices(BTC=80.0))   # -20%, stop at 10%
    patched.assert_called_once()
    asset, qty, price = patched.call_args[0]
    assert asset == "BTC" and qty == 1.0 and price == 80.0     # full liquidation
    fake_bot._trading_floor_channel.send.assert_awaited()      # announced
    risk_enforcer._convene_emergency.assert_awaited()          # desk told why


@pytest.mark.asyncio
async def test_above_stop_does_nothing(fake_bot, patched):
    await risk_enforcer.enforce(fake_bot, _prices(BTC=95.0))   # -5%, inside the stop
    patched.assert_not_called()


@pytest.mark.asyncio
async def test_cooldown_blocks_second_fire(fake_bot, patched):
    await risk_enforcer.enforce(fake_bot, _prices(BTC=80.0))   # fires + stamps cooldown
    patched.reset_mock()
    await risk_enforcer.enforce(fake_bot, _prices(BTC=80.0))   # still within cooldown
    patched.assert_not_called()


@pytest.mark.asyncio
async def test_dry_run_suppresses_the_sell(fake_bot, patched, monkeypatch):
    monkeypatch.setattr(risk_enforcer, "_dry_run", lambda: True)
    await risk_enforcer.enforce(fake_bot, _prices(BTC=80.0))   # would stop, but killed
    patched.assert_not_called()
    kinds = [call.args[0] for call in risk_enforcer.audit_event.call_args_list]
    assert "risk_action_blocked" in kinds


@pytest.mark.asyncio
async def test_missing_price_is_skipped(fake_bot, patched):
    # BTC is held but absent from the feed: a missing price must never read as a loss.
    await risk_enforcer.enforce(fake_bot, _prices(ETH=2000.0))
    patched.assert_not_called()


# ── R2: drawdown circuit breaker ──────────────────────────────────────

def _curve(n, value=100.0):
    return [{"total_value": value}] * n


@pytest.mark.asyncio
async def test_drawdown_trips_and_latches(fake_bot, patched, monkeypatch):
    # Peak 100, live 80 -> 20% drawdown, halt at 15% -> trips and latches.
    monkeypatch.setattr(risk_enforcer.equity, "load_curve", lambda: _curve(30))
    monkeypatch.setattr(risk_enforcer.portfolio, "get_total_value", lambda p: 80.0)
    monkeypatch.setattr(risk_enforcer.portfolio, "_state", {"holdings": {}})
    await risk_enforcer.enforce(fake_bot, _prices(BTC=80.0))
    assert risk_state.load()["halted"] is True
    fake_bot._trading_floor_channel.send.assert_awaited()    # halt announced
    risk_enforcer._convene_emergency.assert_awaited()        # desk convened


@pytest.mark.asyncio
async def test_drawdown_thin_curve_never_trips(fake_bot, patched, monkeypatch):
    # Only 5 points (< min_curve_points): a thin series must not trip, even on a deep loss.
    monkeypatch.setattr(risk_enforcer.equity, "load_curve", lambda: _curve(5))
    monkeypatch.setattr(risk_enforcer.portfolio, "get_total_value", lambda p: 40.0)
    monkeypatch.setattr(risk_enforcer.portfolio, "_state", {"holdings": {}})
    await risk_enforcer.enforce(fake_bot, _prices(BTC=40.0))
    assert risk_state.load()["halted"] is False


@pytest.mark.asyncio
async def test_drawdown_resumes_below_resume_line(fake_bot, patched, monkeypatch):
    # Already halted; recover to 8% (< the 10% resume line) -> clears the latch.
    risk_state.save({"halted": True, "halted_since": 1.0, "last_action_ts": {}})
    monkeypatch.setattr(risk_enforcer.equity, "load_curve", lambda: _curve(30))
    monkeypatch.setattr(risk_enforcer.portfolio, "get_total_value", lambda p: 92.0)
    monkeypatch.setattr(risk_enforcer.portfolio, "_state", {"holdings": {}})
    await risk_enforcer.enforce(fake_bot, _prices(BTC=92.0))
    assert risk_state.load()["halted"] is False


# ── R3: concentration trim ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_concentration_trims_over_cap(fake_bot, patched, monkeypatch):
    # SOL is $5000 of a $10k book (50%); SOL's 20% override (vs the 35% default) plus the
    # 5% band means 50% > 25% -> trim down to the 20% cap.
    monkeypatch.setattr(risk_enforcer.equity, "load_curve", lambda: [])   # no drawdown step
    monkeypatch.setattr(risk_enforcer.portfolio, "get_total_value", lambda p: 10000.0)
    monkeypatch.setattr(risk_enforcer.portfolio, "_state",
                        {"holdings": _holdings(SOL=(50.0, 80.0))})
    await risk_enforcer.enforce(fake_bot, _prices(SOL=100.0))
    patched.assert_called_once()
    asset, qty, price = patched.call_args[0]
    # value 5000, target 20% * 10000 = 2000 -> trim 3000 / $100 = 30 coins.
    assert asset == "SOL" and round(qty, 6) == 30.0


@pytest.mark.asyncio
async def test_concentration_within_band_not_trimmed(fake_bot, patched, monkeypatch):
    # BTC at 38% with the 35% default cap + 5% band (= 40%): inside the band -> no trim.
    monkeypatch.setattr(risk_enforcer.equity, "load_curve", lambda: [])
    monkeypatch.setattr(risk_enforcer.portfolio, "get_total_value", lambda p: 10000.0)
    monkeypatch.setattr(risk_enforcer.portfolio, "_state",
                        {"holdings": _holdings(BTC=(38.0, 90.0))})
    await risk_enforcer.enforce(fake_bot, _prices(BTC=100.0))
    patched.assert_not_called()


# ── F3: trailing high-water mark tracking ─────────────────────────────

def test_update_highs_ratchets_and_resets():
    state = {}
    holdings = {"BTC": {"quantity": 1.0, "avg_cost": 100.0}}
    risk_enforcer._update_highs(state, holdings, _prices(BTC=120.0))
    assert state["highs"]["BTC"] == 120.0
    risk_enforcer._update_highs(state, holdings, _prices(BTC=110.0))   # pullback -> high holds
    assert state["highs"]["BTC"] == 120.0
    risk_enforcer._update_highs(state, holdings, _prices(BTC=150.0))   # new high ratchets up
    assert state["highs"]["BTC"] == 150.0
    # Position closed -> the trail resets so the next entry starts fresh.
    risk_enforcer._update_highs(state, {"BTC": {"quantity": 0.0, "avg_cost": 0.0}}, _prices(BTC=150.0))
    assert "BTC" not in state["highs"]


# ── F4: drawdown auto-de-risk (opt-in) ────────────────────────────────

@pytest.mark.asyncio
async def test_drawdown_auto_derisk_trims_on_trip(fake_bot, patched, monkeypatch):
    # With drawdown_auto_derisk on, a tripping drawdown also sells a fraction of holdings.
    monkeypatch.setitem(risk_enforcer.settings, "risk_limits", {
        "max_drawdown_halt_pct": 15.0, "drawdown_resume_pct": 10.0, "min_curve_points": 24,
        "drawdown_auto_derisk": True, "drawdown_derisk_pct": 25.0,
    })
    monkeypatch.setitem(risk_enforcer.settings, "thresholds", {})   # isolate from the trim path
    monkeypatch.setattr(risk_enforcer.equity, "load_curve", lambda: [{"total_value": 100.0}] * 30)
    monkeypatch.setattr(risk_enforcer.portfolio, "get_total_value", lambda p: 80.0)   # -20% -> trip
    monkeypatch.setattr(risk_enforcer.portfolio, "_state",
                        {"holdings": _holdings(BTC=(1.0, 100.0))})
    # BTC at 95 (vs avg 100) is -5%: no stop-loss, so the only sell is the de-risk.
    await risk_enforcer.enforce(fake_bot, _prices(BTC=95.0))
    sold = [call.args for call in patched.call_args_list]
    assert any(a[0] == "BTC" and abs(a[1] - 0.25) < 1e-9 for a in sold)   # sold 25% of 1.0 BTC
    assert risk_state.load()["halted"] is True
