"""
Risk tests, pure: the gate's sizing and limits for US ETFs and stocks, and the protective
monitor's rules. (The gate through the real executor and desk is in test_execution.)
"""

from decimal import Decimal

import pytest

from ottr.broker.base import round_against_us
from ottr.ledger.book import Position, Valuation
from ottr.risk import gate, monitor
from ottr.risk.types import Intent
from ottr.universe import SUNSET, Member
from tests.conftest import quote
from tests.fakes import OPEN, et
from tests.nyse import calendar_2026

CAL = calendar_2026()


def val(equity=10000.0, cash=None, values=None):
    values = values or {}
    inv = sum(values.values())
    cash = equity - inv if cash is None else cash
    return Valuation(equity=cash + inv, cash=cash, invested=inv,
                     weights={a: v / (cash + inv) for a, v in values.items()}, values=values)


def ctx(cfg, asset="TLT", price=90.0, side="BUY", cls=None, vol=0.10, regime="TRENDING", valuation=None,
        position=None, bp=None, now=OPEN, member="default", fractionable=True, **kw):
    q = quote(asset, price, spread_pct=0.02, now=now)
    cls = cls or cfg.asset_class(asset) or "stock"
    if member == "default":
        member = Member(asset, cls, fractionable=fractionable)
    v = valuation or val()
    limit = round_against_us(Decimal(str(q.ask if side == "BUY" else q.bid)), side)
    return gate.GateContext(cfg=cfg, now=now, market=CAL.state_at(now), member=member, quote=q, quote_problem="",
                            limit_price=limit, vol=vol, regime=regime, valuation=v, position=position,
                            buying_power=Decimal(str(v.cash if bp is None else bp)), **kw)


def buy(asset="TLT", conviction=1.0, created=OPEN, origin="council"):
    return Intent(origin=origin, asset=asset, side="BUY", conviction=conviction, idem_key="k", created_ts=created)


def sell(asset="TLT", fraction=1.0, origin="council"):
    return Intent(origin=origin, asset=asset, side="SELL", fraction=fraction, idem_key="k", created_ts=OPEN)


def pos(asset="TLT", q="10", cost="900", hwm=90.0, opened=OPEN - 86400):
    return Position(asset, Decimal(q), Decimal(cost), hwm, opened)


# ---- sizing --------------------------------------------------------------------------------
def test_a_buy_is_sized_by_code_to_the_vol_target(cfg):
    v = gate.evaluate(buy(), ctx(cfg, vol=0.10))
    assert v.approved and v.notional == pytest.approx(1500.0, rel=0.01)   # 1.5% risk / 10% vol = 15%
    assert "binding: target" in v.notes


def test_vol_floors_unknown_vol_and_regime(cfg):
    known = gate.target_weight(cfg, "TLT", "bond_etf", 0.10, "TRENDING", 1.0)
    assert gate.target_weight(cfg, "TLT", "bond_etf", 0.001, "TRENDING", 1.0) == pytest.approx(0.25)  # floor, then cap
    for missing in (None, 0.0):
        assert gate.target_weight(cfg, "TLT", "bond_etf", missing, "TRENDING", 1.0) < known       # sizes small
    for regime in ("CHOPPY", "UNKNOWN"):
        assert gate.target_weight(cfg, "TLT", "bond_etf", 0.10, regime, 1.0) == pytest.approx(known * 0.25)
    assert gate.target_weight(cfg, "TLT", "bond_etf", 0.10, "TRENDING", 0.5) == pytest.approx(known / 2)


def test_caps_by_class_and_symbol(cfg):
    assert gate.target_weight(cfg, "EEM", "equity_etf", 0.05, "TRENDING", 1.0) == pytest.approx(0.15)  # symbol cap
    assert gate.target_weight(cfg, "SLV", "commodity_etf", 0.12, "TRENDING", 1.0) == pytest.approx(0.10)
    assert gate.target_weight(cfg, "NVDA", "stock", 0.10, "TRENDING", 1.0) == pytest.approx(0.08)    # stock cap


def test_a_buy_trades_only_the_gap_to_target(cfg):
    v = gate.evaluate(buy(), ctx(cfg, valuation=val(values={"TLT": 1000.0})))
    assert v.approved and v.notional == pytest.approx(500.0, rel=0.02)
    full = val(values={"TLT": 1450.0})
    assert gate.evaluate(buy(), ctx(cfg, valuation=full)).reason == "at_or_above_target"


def test_the_per_trade_ceiling_and_gross_exposure_bind(cfg):
    big = gate.evaluate(buy(), ctx(cfg, vol=0.04, valuation=val(equity=100000)))
    assert big.approved and big.notional <= cfg.trading.max_trade_usd and "binding: max_trade_usd" in big.notes
    nearly_full = val(values={"SPY": 8950.0})                          # $50 of room under the 90% limit
    assert gate.evaluate(buy(), ctx(cfg, valuation=nearly_full)).reason == "sized_below_min_by_max_gross_pct"
    some_room = val(values={"SPY": 8700.0})
    v = gate.evaluate(buy(), ctx(cfg, valuation=some_room))
    assert v.approved and v.notional <= 300.0 and "binding: max_gross_pct" in v.notes


def test_swept_cash_funds_a_buy_short_of_buying_power(cfg):
    book = val(cash=200, values={"SGOV": 9800.0})
    v = gate.evaluate(buy(), ctx(cfg, valuation=book, cash_etf_value=9800.0))
    assert v.reason == "needs_cash" and v.funding_usd == pytest.approx(1500.0, rel=0.01)
    unsettled = val(cash=5000, values={"IEF": 5000.0})                  # cash, but only $150 spendable today
    v = gate.evaluate(buy(), ctx(cfg, valuation=unsettled, bp=150))
    assert v.reason == "sized_below_min_by_buying_power"               # and nothing swept to sell


def test_whole_share_assets_buy_whole_shares(cfg):
    v = gate.evaluate(buy("PDBC"), ctx(cfg, asset="PDBC", price=20.0, fractionable=False))
    assert v.approved and v.quantity == v.quantity.to_integral_value()


# ---- BUY blocks ------------------------------------------------------------------------------
@pytest.mark.parametrize("kw,reason", [
    ({"halted": True}, "halted"),
    ({"cooldown_until": OPEN + 60}, "cooldown"),
    ({"risk_event_since_intent": True}, "risk_event_during_decision"),
    ({"corp_action": "stock merger effective 2026-11-02"}, "corporate_action_pending"),
    ({"member": None}, "not_in_universe"),
    ({"member": Member("TLT", "bond_etf", SUNSET)}, "sunset_member"),
    ({"now": et(2026, 10, 21, 15, 55)}, "closing_soon"),
    ({"now": et(2026, 10, 24, 12)}, "market_closed"),
])
def test_buy_blocks(cfg, kw, reason):
    created = kw.get("now", OPEN)
    assert gate.evaluate(buy(created=created), ctx(cfg, **kw)).reason == reason


@pytest.mark.parametrize("sessions,reason", [(None, "earnings_unknown"), (5, "earnings_blackout"), (6, "approved")])
def test_a_stock_needs_a_known_report_date_outside_the_blackout(cfg, sessions, reason):
    v = gate.evaluate(buy("NVDA"), ctx(cfg, asset="NVDA", price=180.0, vol=0.40, earnings_sessions=sessions))
    assert v.reason == reason


def test_an_expired_decision_or_a_caller_sized_order_is_refused(cfg):
    assert gate.evaluate(buy(created=OPEN - 3600), ctx(cfg)).reason == "decision_expired"
    sized = Intent(origin="council", asset="TLT", side="BUY", conviction=1.0, usd=5000, idem_key="k", created_ts=OPEN)
    assert gate.evaluate(sized, ctx(cfg)).reason == "sized_by_caller"


# ---- SELL ------------------------------------------------------------------------------------
@pytest.mark.parametrize("kw", [{"halted": True}, {"cooldown_until": OPEN + 999}, {"risk_event_since_intent": True},
                                {"member": None}, {"member": Member("TLT", "bond_etf", SUNSET)},
                                {"corp_action": "spin off effective 2026-11-02"}])
def test_reductions_are_never_blocked_by_buy_limits(cfg, kw):
    v = gate.evaluate(sell(), ctx(cfg, side="SELL", position=pos(), **kw))
    assert v.approved and v.quantity == Decimal("10")


def test_a_sell_fills_at_the_bid_on_a_tick_and_rounds_a_stub_to_a_full_exit(cfg):
    c = ctx(cfg, side="SELL", position=pos())
    v = gate.evaluate(sell(), c)
    assert Decimal(str(v.price)) == c.limit_price and c.limit_price <= Decimal("90")
    v = gate.evaluate(sell(fraction=0.95), ctx(cfg, side="SELL", position=pos()))
    assert v.quantity == Decimal("10") and "rounded_to_full_exit" in v.notes   # $45 left is below the minimum


def test_a_sell_needs_a_position(cfg):
    assert gate.evaluate(sell(), ctx(cfg, side="SELL")).reason == "no_position"


# ---- the protective monitor ---------------------------------------------------------------------
CLASSES = {"TLT": "bond_etf", "SPY": "equity_etf", "SGOV": "cash_etf"}


def run(cfg, positions, quotes, valuation=None, state=None, points=100, now=OPEN, classes=CLASSES):
    return monitor.check(cfg, now, positions, valuation, quotes, state or monitor.RiskState(), points, classes)


def live(asset, price, now=OPEN):
    return quote(asset, price, spread_pct=0.02, now=now)


def test_the_trailing_stop_fires_off_the_high_water_mark(cfg):
    r = run(cfg, {"TLT": pos(hwm=100.0)}, {"TLT": live("TLT", 89.0)})
    assert [(i.origin, i.fraction) for i in r.intents] == [("stop", 1.0)]
    r = run(cfg, {"TLT": pos(hwm=95.0)}, {"TLT": live("TLT", 89.0)})
    assert r.intents == []
    assert run(cfg, {"TLT": pos(hwm=90.0)}, {"TLT": live("TLT", 92.0)}).high_water["TLT"] == pytest.approx(92.0, rel=1e-3)


def test_swept_cash_is_never_stopped_trimmed_or_blind(cfg):
    sgov = Position("SGOV", Decimal("99"), Decimal("9950"), 101.0, OPEN - 86400)
    r = run(cfg, {"SGOV": sgov}, {}, val(values={"SGOV": 9900.0}))
    assert r.intents == [] and r.events == []


def test_a_concentration_trim_only_beyond_the_band(cfg):
    r = run(cfg, {"SPY": pos("SPY", "4", "2400", 600.0)}, {"SPY": live("SPY", 775.0)}, val(values={"SPY": 3100.0}))
    trims = [i for i in r.intents if i.origin == "trim"]
    assert len(trims) == 1 and trims[0].fraction == pytest.approx((0.31 - 0.25) / 0.31, rel=1e-3)
    r = run(cfg, {"SPY": pos("SPY", "4", "2400", 600.0)}, {"SPY": live("SPY", 725.0)}, val(values={"SPY": 2900.0}))
    assert not [i for i in r.intents if i.origin == "trim"]


def test_a_holding_that_left_the_universe_is_capped_as_a_stock(cfg):
    r = run(cfg, {"OLD": pos("OLD", "10", "1000", 120.0)}, {"OLD": live("OLD", 120.0)}, val(values={"OLD": 1400.0}))
    assert [i.origin for i in r.intents] == ["trim"]                    # 14% vs the 8% stock cap + band


def test_drawdown_halts_derisks_and_resumes_by_timeout(cfg):
    st = monitor.RiskState(peak_equity=10000.0)
    p = {"TLT": pos(hwm=90.0), "SGOV": Position("SGOV", Decimal("10"), Decimal("1000"), 100.0, OPEN)}
    q = {"TLT": live("TLT", 90.0)}
    r = run(cfg, p, q, val(equity=8400, values={"TLT": 900.0, "SGOV": 1000.0}), st)
    assert r.state.halted and [i.asset for i in r.intents if i.origin == "derisk"] == ["TLT"]
    # Still down 16% three days later: v1 would stay halted forever. v2 re-baselines.
    later = OPEN + 73 * 3600
    r2 = run(cfg, p, {"TLT": live("TLT", 90.0, now=later)}, val(equity=8400, values={"TLT": 900.0}), r.state,
             now=later)
    assert not r2.state.halted and r2.state.peak_equity == pytest.approx(8400)
    assert ("resume", {"why": "timeout_rebaseline", "new_peak": pytest.approx(8400)}) in r2.events


def test_the_breaker_needs_enough_history(cfg):
    st = monitor.RiskState(peak_equity=10000.0)
    assert not run(cfg, {}, {}, val(equity=8000), st, points=3).state.halted


def test_no_live_quote_makes_the_monitor_blind_not_silent(cfg):
    r = run(cfg, {"TLT": pos()}, {})
    assert r.events == [("risk_blind", {"asset": "TLT", "why": "no live quote"})]
    assert r.summary() == "blind on TLT"
    assert run(cfg, {"TLT": pos(hwm=100.0)}, {"TLT": live("TLT", 89.0)}).summary() == "stop TLT"
    assert run(cfg, {"TLT": pos()}, {"TLT": live("TLT", 90.0)}).summary() == "all clear"
    stale = live("TLT", 90.0, now=OPEN - 999)
    assert run(cfg, {"TLT": pos()}, {"TLT": stale}).events[0][0] == "risk_blind"
