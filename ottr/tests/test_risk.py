"""
Risk tests: gate sizing/limits and the protective monitor rules (pure functions).
"""

from decimal import Decimal

import pytest

from ottr.ledger.book import Position, Valuation
from ottr.risk import gate, monitor
from ottr.risk.types import Intent
from tests.conftest import quote

NOW = 1_800_000_000.0


def val(equity=10000.0, cash=None, values=None):
    values = values or {}
    inv = sum(values.values())
    cash = equity - inv if cash is None else cash
    return Valuation(equity=cash + inv, cash=cash, invested=inv,
                     weights={a: v / (cash + inv) for a, v in values.items()}, values=values)


def ctx(cfg, asset="ETH", price=2000.0, vol=0.6, regime="TRENDING", valuation=None,
        position=None, age=0.0, **kw):
    q = quote(asset, price, age=age)
    q = type(q)(**{**q.__dict__, "ts": NOW - age})
    return gate.GateContext(cfg=cfg, now=NOW, quote=q, vol=vol, regime=regime,
                            valuation=valuation or val(), position=position, **kw)


def buy(asset="ETH", conviction=1.0, created=NOW, origin="council"):
    return Intent(origin=origin, asset=asset, side="BUY", conviction=conviction,
                  idem_key="k", created_ts=created)


def sell(asset="ETH", fraction=1.0, origin="council"):
    return Intent(origin=origin, asset=asset, side="SELL", fraction=fraction, idem_key="k",
                  created_ts=NOW)


def pos(asset="ETH", q="1", cost="2000", hwm=2000.0, opened=NOW - 10):
    return Position(asset, Decimal(q), Decimal(cost), hwm, opened)


# ---- gate: BUY -----------------------------------------------------------------------

def test_buy_is_sized_by_code_to_vol_target(cfg):
    v = gate.evaluate(buy(conviction=1.0), ctx(cfg, vol=0.5))
    assert v.approved
    assert v.notional == pytest.approx(400.0, rel=0.01)  # 2% / 0.5 vol = 4% of $10k


def test_buy_trades_only_the_gap_to_target(cfg):
    held = val(values={"ETH": 300.0})
    v = gate.evaluate(buy(), ctx(cfg, vol=0.5, valuation=held))
    assert v.approved and v.notional == pytest.approx(100.0, rel=0.02)
    full = val(values={"ETH": 400.0})
    assert gate.evaluate(buy(), ctx(cfg, vol=0.5, valuation=full)).reason == "at_or_above_target"


def test_unknown_vol_sizes_small_not_large(cfg):
    known = gate.target_weight(cfg, "ETH", 0.6, "TRENDING", 1.0)
    for missing in (None, 0.0):
        assert gate.target_weight(cfg, "ETH", missing, "TRENDING", 1.0) < known


def test_choppy_and_unknown_regime_shrink_size(cfg):
    t = gate.target_weight(cfg, "ETH", 0.6, "TRENDING", 1.0)
    assert gate.target_weight(cfg, "ETH", 0.6, "CHOPPY", 1.0) == pytest.approx(t * 0.25)
    assert gate.target_weight(cfg, "ETH", 0.6, "UNKNOWN", 1.0) == pytest.approx(t * 0.25)


def test_buy_respects_asset_cap_and_max_trade(cfg):
    # Very low vol would ask for 50% of the book; DOGE's 15% cap binds.
    w = gate.target_weight(cfg, "DOGE", 0.05, "TRENDING", 1.0)
    assert w == pytest.approx(0.15)
    v = gate.evaluate(buy("BTC"), ctx(cfg, asset="BTC", vol=0.05, valuation=val(equity=100000)))
    assert v.approved and v.notional <= cfg.trading.max_trade_usd + 1e-6


@pytest.mark.parametrize("kw,reason", [
    ({"halted": True}, "halted"),
    ({"cooldown_until": NOW + 60}, "cooldown"),
    ({"risk_event_since_intent": True}, "risk_event_during_decision"),
    ({"age": 999}, "stale_quote"),
])
def test_buy_blocks(cfg, kw, reason):
    assert gate.evaluate(buy(), ctx(cfg, **kw)).reason == reason


def test_expired_council_decision_is_dropped(cfg):
    assert gate.evaluate(buy(created=NOW - 3600), ctx(cfg)).reason == "decision_expired"


def test_buy_clamped_by_cash_reserve(cfg):
    poor = val(equity=10000, cash=250, values={"BTC": 9750.0})
    v = gate.evaluate(buy("SOL"), ctx(cfg, asset="SOL", price=100, vol=0.5, valuation=poor))
    assert v.reason.startswith("sized_below_min_by_")  # 250 - 2% reserve leaves < $100


# ---- gate: SELL ----------------------------------------------------------------------

@pytest.mark.parametrize("kw", [{"halted": True}, {"cooldown_until": NOW + 999},
                                {"risk_event_since_intent": True}])
def test_reductions_are_never_blocked_by_buy_limits(cfg, kw):
    v = gate.evaluate(sell(), ctx(cfg, position=pos(), **kw))
    assert v.approved and v.quantity == Decimal("1")


def test_sell_fills_at_bid_minus_slippage(cfg):
    v = gate.evaluate(sell(), ctx(cfg, price=2000, position=pos()))
    q = quote("ETH", 2000)
    assert v.price == pytest.approx(q.bid * (1 - cfg.trading.slippage_pct / 100))


def test_sell_rounds_tiny_remainder_to_full_exit(cfg):
    v = gate.evaluate(sell(fraction=0.97), ctx(cfg, price=2000, position=pos()))
    assert v.quantity == Decimal("1") and "rounded_to_full_exit" in v.notes


def test_sell_needs_a_position_and_fresh_quote(cfg):
    assert gate.evaluate(sell(), ctx(cfg)).reason == "no_position"
    assert gate.evaluate(sell(), ctx(cfg, position=pos(), age=999)).reason == "stale_quote"


# ---- monitor -------------------------------------------------------------------------

def run(cfg, positions, quotes, valuation=None, state=None, points=100, now=NOW):
    return monitor.check(cfg, now, positions, valuation, quotes, state or monitor.RiskState(), points)


def fresh(asset, price, high=None):
    q = quote(asset, price, high=high)
    return type(q)(**{**q.__dict__, "ts": NOW})


def test_trailing_stop_fires_off_high_water(cfg):
    r = run(cfg, {"ETH": pos(hwm=2500.0)}, {"ETH": fresh("ETH", 2200)})
    assert [(i.origin, i.fraction) for i in r.intents] == [("stop", 1.0)]
    r = run(cfg, {"ETH": pos(hwm=2400.0)}, {"ETH": fresh("ETH", 2200)})
    assert r.intents == []


def test_high_water_ignores_24h_high_until_held_a_day(cfg):
    young = pos(hwm=2000.0, opened=NOW - 3600)
    r = run(cfg, {"ETH": young}, {"ETH": fresh("ETH", 2000, high=3000)})
    assert r.high_water.get("ETH", 2000.0) < 3000
    old = pos(hwm=2000.0, opened=NOW - 2 * 86400)
    assert run(cfg, {"ETH": old}, {"ETH": fresh("ETH", 2000, high=3000)}).high_water["ETH"] == 3000


def test_concentration_trim_only_beyond_band(cfg):
    v = val(values={"ADA": 2160.0})  # 21.6% vs 15% cap + 5% band
    r = run(cfg, {"ADA": pos("ADA", "10000", "1500", 0.2)}, {"ADA": fresh("ADA", 0.216)}, v)
    trims = [i for i in r.intents if i.origin == "trim"]
    assert len(trims) == 1 and trims[0].fraction == pytest.approx((0.216 - 0.15) / 0.216, rel=1e-3)
    v = val(values={"DOGE": 1690.0})  # 16.9%: inside the band
    r = run(cfg, {"DOGE": pos("DOGE", "1", "1", 1)}, {"DOGE": fresh("DOGE", 1)}, v)
    assert not [i for i in r.intents if i.origin == "trim"]


def test_drawdown_halts_derisks_and_resumes_by_timeout(cfg):
    st = monitor.RiskState(peak_equity=10000.0)
    p = {"ETH": pos(hwm=1000.0)}
    q = {"ETH": fresh("ETH", 1000)}
    r = run(cfg, p, q, val(equity=8400, values={"ETH": 1000.0}), st)
    assert r.state.halted and any(i.origin == "derisk" for i in r.intents)
    # Still down 16% three days later: v1 would stay halted forever. v2 re-baselines.
    later = NOW + 73 * 3600
    q2 = {"ETH": type(q["ETH"])(**{**q["ETH"].__dict__, "ts": later})}
    r2 = monitor.check(cfg, later, p, val(equity=8400, values={"ETH": 1000.0}), q2, r.state, 100)
    assert not r2.state.halted and r2.state.peak_equity == pytest.approx(8400)
    assert ("resume", {"why": "timeout_rebaseline", "new_peak": pytest.approx(8400)}) in r2.events


def test_drawdown_needs_enough_history(cfg):
    st = monitor.RiskState(peak_equity=10000.0)
    r = run(cfg, {}, {}, val(equity=8000), st, points=3)
    assert not r.state.halted


def test_stale_quote_makes_risk_blind_not_silent(cfg):
    stale = quote("ETH", 1)
    stale = type(stale)(**{**stale.__dict__, "ts": NOW - 9999})
    r = run(cfg, {"ETH": pos()}, {"ETH": stale})
    assert r.events and r.events[0][0] == "risk_blind"
