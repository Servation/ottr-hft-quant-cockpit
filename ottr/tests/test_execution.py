"""
US execution tests: ticks and the SimBroker, the account model, the US gate through the
real Executor on a Desk (session, quotes, members, earnings, caps, buying power), funding
a BUY from the cash ETF, the daily sweep and fee charge, and ports of the core invariants
(no double fill, crash safety, dry run, protective exits never blocked).

Runs on the fake 2026 NYSE calendar with a fake clock and fake quotes; the broker and fee
schedule are the real ones from config.toml.
"""

import dataclasses
from datetime import date, datetime
from decimal import Decimal

import pytest

from ottr.broker.account import buying_power
from ottr.broker.base import Order, round_against_us
from ottr.broker.sim import SimBroker
from ottr.clock import FakeClock
from ottr.config import load
from ottr.engine.desks import Desk
from ottr.engine.executor import Executor
from ottr.engine.jobs import close_of_day, sweep_intent, sweep_moment
from ottr.ledger.book import Ledger
from ottr.market.calendar import ET
from ottr.market.types import Quote
from ottr.risk.monitor import RiskState
from ottr.risk.types import Intent
from ottr.universe import SUNSET, Member, core_members
from tests.nyse import calendar_2026

D = Decimal
CAL = calendar_2026()
PRICES = {"SPY": 600.0, "QQQ": 500.0, "IWM": 220.0, "SGOV": 100.50, "TLT": 90.0, "PDBC": 14.0,
          "GLD": 300.0, "NVDA": 180.0, "OLD": 50.0}


def et(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=ET).timestamp()


OPEN = et(2026, 10, 21, 11, 0)        # a Wednesday, mid-session, after CAT's 2026-10-12 start


class FakeUsMarket:
    def __init__(self, clock, prices=PRICES, spread_pct=0.02):
        self.calendar, self.clock = CAL, clock
        self.prices, self.spread = dict(prices), spread_pct
        self.problem, self.vol, self.regime = {}, {}, {}

    def _q(self, s):
        p = self.prices[s]
        h = p * self.spread / 200
        return Quote(s, p - h, p + h, p, self.clock.time(), "alpaca")

    async def quote(self, s):
        if s in self.problem:
            return None, self.problem[s]
        return (self._q(s), "ok") if s in self.prices else (None, "no_snapshot")

    async def marks(self):
        return {s: self._q(s) for s in self.prices}

    async def vol_regime(self, s):
        return self.vol.get(s, 0.15), self.regime.get(s, "TRENDING")


class Events:
    def __init__(self, earnings=None, actions=()):
        self.earnings, self.actions = earnings or {}, set(actions)

    def earnings_sessions(self, s, now):
        return self.earnings.get(s)

    def corp_action(self, s, now):
        return "stock merger effective 2026-11-02" if s in self.actions else None


@pytest.fixture
def ucfg(tmp_path):
    return load(data_dir=tmp_path)


@pytest.fixture
def rig(ucfg, store):
    clock = FakeClock(OPEN)
    lg = Ledger(store)
    lg.initialize(10000, now=OPEN - 86400 * 3)
    market = FakeUsMarket(clock)
    members = core_members(ucfg, {s: s != "PDBC" for s in ucfg.core})
    members["NVDA"] = Member("NVDA", "stock")
    desk = Desk(ucfg, store, lg, market, SimBroker(ucfg.fees), members, Events(), clock)
    ex = Executor(ucfg, store, lg, desk=desk)
    return dataclasses.make_dataclass("Rig", ["cfg", "store", "ledger", "market", "desk", "ex", "clock"])(
        ucfg, store, lg, market, desk, ex, clock)


def buy(asset="SPY", key="b", conviction=1.0, created=None, origin="council", usd=0.0):
    return Intent(origin=origin, asset=asset, side="BUY", conviction=conviction, idem_key=key,
                  created_ts=created if created is not None else OPEN, usd=usd)


def sell(asset="SPY", key="s", fraction=1.0, origin="council", created=None):
    return Intent(origin=origin, asset=asset, side="SELL", fraction=fraction, idem_key=key,
                  created_ts=created if created is not None else OPEN)


def status(rig, key):
    return rig.store.conn.execute("SELECT status FROM orders WHERE idem_key=?", (key,)).fetchone()[0]


# -- ticks and the SimBroker --------------------------------------------------------------

@pytest.mark.parametrize("price,side,expect", [
    ("100.004", "BUY", "100.01"), ("100.006", "SELL", "100.00"), ("100.00", "BUY", "100.00"),
    ("0.12341", "BUY", "0.1235"), ("0.12349", "SELL", "0.1234"),
])
def test_prices_round_to_a_legal_tick_against_us(price, side, expect):
    assert round_against_us(price, side) == D(expect)


def test_sim_fills_at_the_touch_and_accrues_fees(ucfg):
    b = SimBroker(ucfg.fees)
    q = Quote("SPY", 599.99, 600.013, 600.0, OPEN, "alpaca")
    ex = b.execute(Order("k", "SPY", "SELL", D("2"), D("599.99"), "council"), q, OPEN)
    assert ex.filled and ex.price == D("599.99")
    assert set(ex.accrued) == {"sec", "taf", "cat"} and ex.accrued["taf"] == 0  # TAF paused in Q4 2026
    assert b.execute(Order("k", "SPY", "BUY", D("1"), D("600.02"), "council"), q, OPEN).price == D("600.02")


def test_sim_refuses_a_limit_the_market_moved_past(ucfg):
    q = Quote("SPY", 600.5, 600.6, 600.55, OPEN, "alpaca")
    ex = SimBroker(ucfg.fees).execute(Order("k", "SPY", "BUY", D("1"), D("600.02"), "council"), q, OPEN)
    assert ex.status == "rejected" and ex.reason == "limit_not_marketable"


def test_sim_refuses_fractions_where_the_asset_has_none(ucfg):
    q = Quote("PDBC", 13.99, 14.01, 14.0, OPEN, "alpaca")
    o = Order("k", "PDBC", "BUY", D("1.5"), D("14.01"), "council")
    assert SimBroker(ucfg.fees).execute(o, q, OPEN, fractionable=False).reason == "illegal_quantity"


def test_sim_refuses_a_date_with_no_fee_rate(ucfg):
    before_cat = et(2026, 10, 9, 11)
    q = Quote("SPY", 599.9, 600.1, 600, before_cat, "alpaca")
    ex = SimBroker(ucfg.fees).execute(Order("k", "SPY", "BUY", D("1"), D("600.10"), "c"), q, before_cat)
    assert ex.status == "rejected" and ex.reason.startswith("no_fee_rate")


# -- account model ---------------------------------------------------------------------------

def _raw_fill(lg, side, q, px, key, when):
    with lg.store.tx():
        lg.apply_fill(idem_key=key, asset="SPY", side=side, quantity=q, price=px,
                      quote_source="test", origin="test", now=when)


def test_margin_1x_spends_cash_less_pending_fees(store):
    lg = Ledger(store)
    lg.initialize(1000)
    with store.tx():
        lg.apply_fill(idem_key="b", asset="SPY", side="BUY", quantity=1, price=100, quote_source="t",
                      origin="t", accrued={"cat": D("0.004")}, now=OPEN)
    bp = buying_power("margin_1x", lg, CAL, OPEN)
    assert bp.available == D("899.99") and bp.pending_fees == D("0.01")


def test_cash_account_waits_for_settlement_across_columbus_day(store):
    lg = Ledger(store)
    lg.initialize(1000, now=et(2026, 10, 1, 10))
    _raw_fill(lg, "BUY", "5", "100", "b", et(2026, 10, 2, 10))
    _raw_fill(lg, "SELL", "5", "110", "s", et(2026, 10, 9, 10))        # Friday: settles Tue Oct 13
    mon = buying_power("cash", lg, CAL, et(2026, 10, 12, 11))          # Columbus Day: market open, banks shut
    assert mon.unsettled == D("550.00") and mon.available == D("500.00")
    assert buying_power("cash", lg, CAL, et(2026, 10, 13, 11)).available == D("1050.00")
    assert buying_power("margin_1x", lg, CAL, et(2026, 10, 12, 11)).available == D("1050.00")


# -- the US gate through the executor ----------------------------------------------------------

async def test_a_council_buy_is_sized_ticked_filled_and_booked(rig):
    v = await rig.ex.process(buy())
    assert v.approved, v
    lot = rig.ledger.lots("SPY")[0]
    assert lot.qty_remaining > 0 and rig.ledger.positions()["SPY"].cost < D("1000.01")  # 1.5% / 0.15 vol = 10%
    fill = rig.ledger.fills(1)[0]
    assert D(fill["price"]) == D("600.06")                # the ask, on a tick
    assert rig.ledger.pending_fees() == D("0.01")         # CAT accrued, charged at the close
    assert rig.ledger.check() == []


async def test_closed_market_defers_then_fills_when_open(rig):
    rig.clock.set(et(2026, 10, 21, 8, 0))
    assert (await rig.ex.process(buy(created=et(2026, 10, 21, 8, 0)))).reason == "market_closed"
    assert status(rig, "b") == "deferred"
    rig.clock.set(et(2026, 10, 21, 9, 45))
    assert (await rig.ex.process(buy(created=et(2026, 10, 21, 9, 40)))).approved


async def test_no_new_buys_in_the_last_minutes(rig):
    rig.clock.set(et(2026, 10, 21, 15, 55))
    v = await rig.ex.process(buy(created=et(2026, 10, 21, 15, 55)))
    assert v.reason == "closing_soon" and status(rig, "b") == "rejected"


async def test_a_bad_quote_defers_with_its_reason(rig):
    rig.market.problem["SPY"] = "too_wide (1.40%)"
    v = await rig.ex.process(buy())
    assert v.reason == "no_quote" and "too_wide" in v.notes[0] and status(rig, "b") == "deferred"


async def test_sunset_members_can_be_sold_not_bought(rig):
    await rig.ex.process(buy("QQQ", "b1"))
    rig.desk.members["QQQ"] = Member("QQQ", "equity_etf", SUNSET)
    assert (await rig.ex.process(buy("QQQ", "b2"))).reason == "sunset_member"
    assert (await rig.ex.process(sell("QQQ", "s"))).approved


async def test_a_position_outside_the_universe_can_always_be_sold(rig):
    with rig.store.tx():
        rig.ledger.apply_fill(idem_key="x", asset="OLD", side="BUY", quantity=4, price=50, quote_source="t",
                              origin="t", now=OPEN)
    assert (await rig.ex.process(buy("OLD", "b"))).reason == "not_in_universe"
    assert (await rig.ex.process(sell("OLD", "s"))).approved and "OLD" not in rig.ledger.positions()


@pytest.mark.parametrize("earnings,reason", [(None, "earnings_unknown"), (3, "earnings_blackout"), (10, "approved")])
async def test_single_stocks_respect_the_earnings_blackout(rig, earnings, reason):
    rig.desk.events = Events({"NVDA": earnings} if earnings is not None else {})
    assert (await rig.ex.process(buy("NVDA"))).reason == reason


async def test_pending_corporate_action_blocks_buys(rig):
    rig.desk.events = Events(actions={"GLD"})
    v = await rig.ex.process(buy("GLD"))
    assert v.reason == "corporate_action_pending" and "stock merger" in v.notes[0]


async def test_the_stored_events_gate_buys_until_actions_are_known(rig):
    """The real reader: unknown corporate actions block an ETF BUY, a clean check allows it,
    and a stock needs a stored report date outside the blackout. Exits are never blocked."""
    from ottr.events import db
    from ottr.events.earnings import EarningsDate
    from ottr.events.view import FEED_EARNINGS, StoredEvents
    rig.desk.events = StoredEvents(rig.store, lambda: CAL, 72 * 3600)
    v = await rig.ex.process(buy("SPY", "b1"))
    assert v.reason == "corporate_action_pending" and "never checked" in v.notes[0]
    db.save_corporate_actions(rig.store, [], ["SPY", "NVDA"], date(2026, 8, 1), date(2027, 1, 1), OPEN)
    assert (await rig.ex.process(buy("SPY", "b2"))).approved
    assert (await rig.ex.process(buy("NVDA", "b3"))).reason == "earnings_unknown"
    db.save_earnings(rig.store, [EarningsDate("NVDA", date(2026, 11, 18))], date(2026, 10, 21), OPEN)
    db.record_feed(rig.store, FEED_EARNINGS, True, "test", OPEN)
    assert (await rig.ex.process(buy("NVDA", "b4"))).approved          # 20 sessions away
    monday = et(2026, 10, 26, 11)                                       # no refresh since: stale, unknown
    rig.clock.set(monday)
    assert (await rig.ex.process(buy("SPY", "b5", created=monday))).reason == "corporate_action_pending"
    assert (await rig.ex.process(sell("SPY", "s1", created=monday))).approved


async def test_stocks_are_capped_tighter_than_etfs(rig):
    rig.desk.events = Events({"NVDA": 30})
    rig.market.vol["NVDA"] = 0.05                      # would ask for a huge weight; stock cap is 8%
    v = await rig.ex.process(buy("NVDA"))
    assert v.approved and v.notional <= 0.08 * 10000 + 1


async def test_whole_share_assets_buy_whole_shares(rig):
    v = await rig.ex.process(buy("PDBC"))
    assert v.approved and v.quantity == v.quantity.to_integral_value()


async def test_council_orders_cannot_carry_a_size(rig):
    assert (await rig.ex.process(buy(usd=5000))).reason == "sized_by_caller"


async def test_expired_decisions_are_dropped(rig):
    assert (await rig.ex.process(buy(created=OPEN - 3600))).reason == "decision_expired"


# -- cash sweep and funding --------------------------------------------------------------------

async def test_sweep_buys_sgov_once_at_1545_with_cash_above_5_pct(rig):
    marks = await rig.market.marks()
    assert sweep_intent(rig.cfg, rig.ledger, CAL, marks, et(2026, 10, 21, 15, 40)) is None
    t = et(2026, 10, 21, 15, 46)
    it = sweep_intent(rig.cfg, rig.ledger, CAL, marks, t)
    assert it.asset == "SGOV" and it.usd == pytest.approx(9500) and it.idem_key == "sweep:2026-10-21"
    rig.clock.set(t)
    assert (await rig.ex.process(it)).approved
    assert (await rig.ex.process(it)).reason == "duplicate:filled"
    assert sweep_intent(rig.cfg, rig.ledger, CAL, await rig.market.marks(), t + 60) is None  # nothing left


def test_sweep_moves_back_on_an_early_close():
    t = sweep_moment(CAL, et(2026, 11, 27, 10, 0), "15:45")
    assert datetime.fromtimestamp(t, tz=ET).strftime("%H:%M") == "12:45"


async def test_a_buy_short_of_cash_sells_sgov_first(rig):
    rig.clock.set(et(2026, 10, 21, 15, 46))
    await rig.ex.process(sweep_intent(rig.cfg, rig.ledger, CAL, await rig.market.marks(), rig.clock.time()))
    rig.clock.set(et(2026, 10, 22, 10, 30))
    v = await rig.ex.process(buy("SPY", "b", created=rig.clock.time()))
    assert v.approved, v
    assert status(rig, "fund:b") == "filled"
    sides = [(r[0], r[1]) for r in rig.store.conn.execute("SELECT origin, asset FROM fills ORDER BY id")]
    assert sides[-2:] == [("sweep", "SGOV"), ("council", "SPY")]
    assert rig.ledger.check() == []


async def test_gross_exposure_ignores_the_cash_etf(rig):
    rig.clock.set(et(2026, 10, 21, 15, 46))
    await rig.ex.process(sweep_intent(rig.cfg, rig.ledger, CAL, await rig.market.marks(), rig.clock.time()))
    rig.clock.set(et(2026, 10, 22, 10, 30))
    v = await rig.ex.process(buy("TLT", "b", created=rig.clock.time()))  # 95% in SGOV, still room to buy
    assert v.approved, v


# -- daily fee charge ---------------------------------------------------------------------------

async def test_fees_are_charged_once_after_the_close(rig):
    await rig.ex.process(buy())
    assert close_of_day(rig.ledger, CAL, et(2026, 10, 21, 15, 0)) == []        # today still open
    assert close_of_day(rig.ledger, CAL, et(2026, 10, 21, 16, 5)) == [(date(2026, 10, 21), D("0.01"))]
    assert close_of_day(rig.ledger, CAL, et(2026, 10, 22, 9, 0)) == []         # idempotent
    assert rig.ledger.pending_fees() == 0 and rig.ledger.check() == []


# -- invariants, ported -----------------------------------------------------------------------

async def test_duplicate_intent_executes_once(rig):
    assert (await rig.ex.process(buy())).approved
    assert (await rig.ex.process(buy())).reason == "duplicate:filled"
    assert rig.store.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1


async def test_crash_after_booking_rolls_back_and_retries(rig, monkeypatch):
    real = rig.ledger.apply_fill

    def boom(**kw):
        real(**kw)
        raise RuntimeError("power cut before commit")
    monkeypatch.setattr(rig.ledger, "apply_fill", boom)
    with pytest.raises(RuntimeError):
        await rig.ex.process(buy())
    assert rig.ledger.positions() == {} and rig.ledger.pending_fees() == 0 and rig.ledger.check() == []
    monkeypatch.setattr(rig.ledger, "apply_fill", real)
    assert (await rig.ex.process(buy())).approved


async def test_dry_run_records_and_never_fills(rig):
    rig.ex.kill.cfg = dataclasses.replace(rig.cfg, trading=dataclasses.replace(rig.cfg.trading, dry_run=True))
    assert (await rig.ex.process(buy())).approved
    assert rig.ledger.positions() == {} and status(rig, "b") == "dry_run"


@pytest.mark.parametrize("flag", ["halted", "cooldown"])
async def test_exits_are_never_blocked_by_buy_limits(rig, flag):
    await rig.ex.process(buy())
    st = RiskState(halted=flag == "halted", cooldowns={"SPY": OPEN + 9999} if flag == "cooldown" else {})
    rig.store.set_state("risk", st.to_dict())
    assert (await rig.ex.process(buy("SPY", "b2"))).reason in ("halted", "cooldown")
    assert (await rig.ex.process(sell("SPY", "stop", origin="stop"))).approved
