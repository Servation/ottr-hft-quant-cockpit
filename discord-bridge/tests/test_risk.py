"""Unit tests for the Tier 3 (R0) pure risk policies + the persisted latch.

These cover the decision math only — stop / drawdown / trim — with no I/O and no live
wiring (the enforcer arrives in R1+). The risk-state latch is isolated to a temp file by
the autouse `_isolate_risk_state` conftest fixture, so these never touch live data/.
"""

from bot import risk, risk_state


def _prices(**kv):
    """Build a price-feed-shaped dict: _prices(BTC=88.0) -> {'BTC': {'price': 88.0}}."""
    return {asset: {"price": price} for asset, price in kv.items()}


# ── stop_loss_breaches ────────────────────────────────────────────────

def test_stop_loss_trips_past_threshold():
    holdings = {"BTC": {"quantity": 1.0, "avg_cost": 100.0}}
    # 12% below avg cost, stop at 10% -> one full-liquidation action.
    actions = risk.stop_loss_breaches(holdings, _prices(BTC=88.0), stop_pct=10.0)
    assert len(actions) == 1
    action = actions[0]
    assert action.kind == "STOP_LOSS" and action.asset == "BTC"
    assert action.sell_qty == 1.0                  # full position
    assert action.detail["loss_pct"] == -12.0


def test_stop_loss_not_triggered_above_threshold():
    holdings = {"BTC": {"quantity": 1.0, "avg_cost": 100.0}}
    # 5% down, stop at 10% -> nothing.
    assert risk.stop_loss_breaches(holdings, _prices(BTC=95.0), stop_pct=10.0) == []


def test_stop_loss_exactly_at_threshold_trips():
    holdings = {"ETH": {"quantity": 2.0, "avg_cost": 100.0}}
    # exactly -10% is at the stop (<=), so it trips.
    actions = risk.stop_loss_breaches(holdings, _prices(ETH=90.0), stop_pct=10.0)
    assert len(actions) == 1 and actions[0].sell_qty == 2.0


def test_stop_loss_skips_missing_or_bad_inputs():
    holdings = {
        "BTC": {"quantity": 1.0, "avg_cost": 100.0},   # no price in feed -> skip
        "ETH": {"quantity": 0.0, "avg_cost": 100.0},   # no position -> skip
        "SOL": {"quantity": 1.0, "avg_cost": 0.0},     # no cost basis -> skip
    }
    # A missing price must never read as a 100% loss, so nothing fires.
    actions = risk.stop_loss_breaches(holdings, _prices(ETH=10.0, SOL=10.0), stop_pct=10.0)
    assert actions == []


def test_stop_loss_trailing_from_high():
    holdings = {"BTC": {"quantity": 1.0, "avg_cost": 100.0}}
    # Price 135, high 150: +35% vs the 100 avg cost (avg_cost stop does NOT fire), but 10%
    # below the 150 high -> the trailing stop fires.
    prices = _prices(BTC=135.0)
    assert risk.stop_loss_breaches(holdings, prices, stop_pct=10.0, mode="avg_cost") == []
    actions = risk.stop_loss_breaches(holdings, prices, stop_pct=10.0, mode="trailing",
                                      highs={"BTC": 150.0})
    assert len(actions) == 1 and actions[0].sell_qty == 1.0
    assert actions[0].detail["mode"] == "trailing"


def test_stop_loss_trailing_falls_back_to_avg_cost():
    holdings = {"ETH": {"quantity": 2.0, "avg_cost": 100.0}}
    # No high recorded yet -> trailing uses the avg cost; 88 is -12% -> stop.
    actions = risk.stop_loss_breaches(holdings, _prices(ETH=88.0), stop_pct=10.0,
                                      mode="trailing", highs={})
    assert len(actions) == 1


# ── drawdown_state (hysteresis breaker) ───────────────────────────────

def test_drawdown_trips_at_halt_threshold():
    state = risk.drawdown_state(peak=100.0, current_value=84.0, halt_pct=15.0,
                                resume_pct=10.0, was_halted=False)
    assert state.halt is True and state.tripped is True and state.recovered is False
    assert round(state.drawdown, 4) == 0.16


def test_drawdown_does_not_trip_below_halt():
    state = risk.drawdown_state(peak=100.0, current_value=88.0, halt_pct=15.0,
                                resume_pct=10.0, was_halted=False)
    assert state.halt is False and state.tripped is False


def test_drawdown_hysteresis_holds_between_bands():
    # Already halted, recovered to -12% (between resume 10% and halt 15%): stays halted.
    state = risk.drawdown_state(peak=100.0, current_value=88.0, halt_pct=15.0,
                                resume_pct=10.0, was_halted=True)
    assert state.halt is True and state.tripped is False and state.recovered is False


def test_drawdown_resumes_below_resume_threshold():
    # Halted, recovered to -8% (below resume 10%): clears, flags the transition.
    state = risk.drawdown_state(peak=100.0, current_value=92.0, halt_pct=15.0,
                                resume_pct=10.0, was_halted=True)
    assert state.halt is False and state.recovered is True


def test_drawdown_bad_peak_preserves_latch():
    state = risk.drawdown_state(peak=0.0, current_value=50.0, halt_pct=15.0,
                                resume_pct=10.0, was_halted=True)
    assert state.halt is True and state.drawdown == 0.0
    assert state.tripped is False and state.recovered is False


def test_drawdown_above_peak_is_zero():
    state = risk.drawdown_state(peak=100.0, current_value=110.0, halt_pct=15.0,
                                resume_pct=10.0, was_halted=False)
    assert state.drawdown == 0.0 and state.halt is False


# ── concentration_breaches (continuous trim) ──────────────────────────

def test_concentration_trims_over_cap_back_to_cap():
    # SOL = $5000 of a $10000 book = 50%, cap 35%, band 0 -> trim to 35%.
    holdings = {"SOL": {"quantity": 50.0, "avg_cost": 80.0}}
    actions = risk.concentration_breaches(
        holdings, _prices(SOL=100.0), total_value=10000.0,
        default_cap_pct=35.0, band_pct=0.0,
    )
    assert len(actions) == 1
    action = actions[0]
    assert action.kind == "CONCENTRATION_TRIM" and action.asset == "SOL"
    # value 5000, target 3500 -> trim 1500 / $100 = 15 coins.
    assert round(action.sell_qty, 6) == 15.0
    assert round(action.detail["trim_notional"], 2) == 1500.0


def test_concentration_within_band_is_ignored():
    # 38% with a 5% band over a 35% cap (cap+band = 40%) -> no trim.
    holdings = {"BTC": {"quantity": 38.0, "avg_cost": 90.0}}
    actions = risk.concentration_breaches(
        holdings, _prices(BTC=100.0), total_value=10000.0,
        default_cap_pct=35.0, band_pct=5.0,
    )
    assert actions == []


def test_concentration_per_asset_override_is_stricter():
    # SOL at 25% with a 20% override beats the 35% default -> trims; band 0.
    holdings = {"SOL": {"quantity": 25.0, "avg_cost": 80.0}}
    actions = risk.concentration_breaches(
        holdings, _prices(SOL=100.0), total_value=10000.0,
        default_cap_pct=35.0, per_asset_caps={"SOL": 20.0}, band_pct=0.0,
    )
    assert len(actions) == 1
    # value 2500, target 2000 -> trim 500 / 100 = 5 coins.
    assert round(actions[0].sell_qty, 6) == 5.0


def test_concentration_skips_zero_total_and_bad_price():
    holdings = {"BTC": {"quantity": 1.0, "avg_cost": 100.0}}
    assert risk.concentration_breaches(holdings, _prices(BTC=100.0), total_value=0.0,
                                       default_cap_pct=35.0) == []
    assert risk.concentration_breaches(holdings, _prices(), total_value=10000.0,
                                       default_cap_pct=35.0) == []


# ── risk_state latch (persisted) ──────────────────────────────────────

def test_risk_state_defaults_when_absent():
    assert risk_state.load() == {"halted": False, "halted_since": None, "last_action_ts": {}, "highs": {}}


def test_risk_state_roundtrip():
    state = risk_state.load()
    state["halted"] = True
    state["halted_since"] = 1234.0
    risk_state.mark_action(state, "SOL", now=1000.0)
    risk_state.save(state)

    reloaded = risk_state.load()
    assert reloaded["halted"] is True
    assert reloaded["halted_since"] == 1234.0
    assert reloaded["last_action_ts"]["SOL"] == 1000.0


def test_risk_state_corrupt_file_heals():
    # A garbage latch file loads as the default rather than raising (fail safe).
    risk_state._STATE_FILE.write_text("{ not json", encoding="utf-8")
    assert risk_state.load() == {"halted": False, "halted_since": None, "last_action_ts": {}, "highs": {}}


def test_cooldown_active_window():
    state = {"last_action_ts": {"SOL": 1000.0}}
    assert risk_state.cooldown_active(state, "SOL", now=1300.0, cooldown_seconds=900) is True
    assert risk_state.cooldown_active(state, "SOL", now=2000.0, cooldown_seconds=900) is False
    assert risk_state.cooldown_active(state, "BTC", now=1300.0, cooldown_seconds=900) is False
