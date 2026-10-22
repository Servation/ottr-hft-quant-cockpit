"""
Corporate actions applied to the book (ledger/corporate.py): dividends owed from the
ex-date and paid on the pay date, forward and reverse splits with cash in lieu, cash
mergers, actions the book can't model (exited the session before, alarmed if held
through), late runs, failures that wait for a person, restated marks and split-aware quote
checks, and the ledger replay catching tampering. The last test runs the real engine over
FakeAlpaca through a split and a dividend overnight and checks equity never jumps.
"""

from datetime import date
from decimal import Decimal

import pytest

from ottr import health
from ottr.engine.engine import Engine
from ottr.events import db
from ottr.events.corporate import parse_actions
from ottr.events.refresh import Sources
from ottr.ledger import corporate
from ottr.ledger.book import Position, Valuation
from ottr.market.alpaca import check_quote
from ottr.notify import format_event
from ottr.risk import monitor
from ottr.risk.monitor import RiskState
from ottr.risk.types import Intent
from tests.conftest import quote
from tests.fakes import OPEN, buy_council, et, market_rig, no_corporate_actions
from tests.nyse import calendar_2026

CAL = calendar_2026()
SYMBOLS = ("SPY", "GLD", "PDBC", "TLT", "XYZ")


def store_actions(store, *rows, now=et(2026, 10, 15, 8)):
    """One corporate-actions fetch for SYMBOLS: rows of (Alpaca array name, fields). An
    action stored earlier and missing now is marked withdrawn, as a real refresh would."""
    payload = {}
    for array, row in rows:
        payload.setdefault(array, []).append(row)
    actions, problems = parse_actions(payload)
    assert problems == []
    db.save_corporate_actions(store, actions, SYMBOLS, date(2026, 1, 1), date(2026, 12, 31), now)


def dividend(symbol="SPY", rate=1.888834, ex="2026-10-19", pay="2026-10-30", id="div"):
    return "cash_dividends", {"id": id, "symbol": symbol, "rate": rate, "special": False, "foreign": False,
                              "ex_date": ex, "record_date": ex, "payable_date": pay, "process_date": pay}


def split(symbol="GLD", new=4, old=1, ex="2026-10-22", id="split"):
    kind = "forward_splits" if new > old else "reverse_splits"
    return kind, {"id": id, "symbol": symbol, "new_rate": new, "old_rate": old, "ex_date": ex,
                  "record_date": ex, "payable_date": ex, "process_date": ex}


def merger(symbol="XYZ", rate=33.5, effective="2026-10-22", id="cash"):
    return "cash_mergers", {"id": id, "acquirer_symbol": "BIGCO", "acquiree_symbol": symbol, "rate": rate,
                            "effective_date": effective, "process_date": effective, "payable_date": effective}


def stock_merger(symbol="TLT", effective="2026-10-26", id="stock"):
    row = {"id": id, "acquirer_symbol": "BIGCO", "acquiree_symbol": symbol, "acquirer_rate": 1.2,
           "acquiree_rate": 1, "process_date": effective or "2026-11-30"}
    if effective:
        row["effective_date"] = effective
    return "stock_mergers", row


def trade(lg, side, asset, q, price, key, when):
    with lg.store.tx():
        return lg.apply_fill(idem_key=key, asset=asset, side=side, quantity=q, price=price, quote_source="test",
                             origin="test", now=when)


def apply(store, when, fractionable=lambda s: True, closes=None):
    closes = closes or {}
    return corporate.apply_due(store, when, fractionable, lambda s, d: closes.get(s))


def marks(**prices):
    return {s: quote(s, p, spread_pct=0.0) for s, p in prices.items()}


# -- dividends ---------------------------------------------------------------------------
def test_a_dividend_is_owed_from_the_ex_date_and_paid_on_the_pay_date(store, ledger):
    trade(ledger, "BUY", "SPY", "10", "500", "b1", et(2026, 10, 15, 11))
    store_actions(store, dividend())
    cash = ledger.cash()
    assert apply(store, et(2026, 10, 18, 23)).recorded == 0          # the day before: nothing
    res = apply(store, et(2026, 10, 19, 0, 30))
    assert res.recorded == 1 and "$18.89 owed, paid 2026-10-30" in res.notes[0][1]
    assert ledger.receivable() == Decimal("18.89") and ledger.cash() == cash
    v = ledger.value(marks(SPY=498.11))
    assert v.receivable == pytest.approx(18.89) and v.equity == pytest.approx(float(cash) + 4981.10 + 18.89)
    assert apply(store, et(2026, 10, 19, 10)).recorded == 0            # once only
    res = apply(store, et(2026, 10, 30, 0, 10))
    assert res.notes == [("SPY", "cash dividend paid: $18.89")]
    assert ledger.cash() == cash + Decimal("18.89") and ledger.receivable() == 0
    (event,) = store.conn.execute("SELECT day, kind, asset, amount FROM cash_events WHERE kind='dividend'")
    assert tuple(event) == ("2026-10-30", "dividend", "SPY", "18.89")
    assert ledger.check() == []


def test_the_dividend_goes_to_the_shares_held_into_the_ex_date_even_when_booked_late(store, ledger):
    trade(ledger, "BUY", "SPY", "10", "500", "b1", et(2026, 10, 15, 11))
    trade(ledger, "SELL", "SPY", "4", "499", "s1", et(2026, 10, 19, 10))   # sold on the ex-date: still paid
    trade(ledger, "BUY", "SPY", "5", "497", "b2", et(2026, 10, 19, 11))    # bought on it: not paid
    store_actions(store, dividend(rate=2))
    apply(store, et(2026, 10, 21, 9))                                       # the engine was down until Wed
    (d,) = ledger.dividends()
    assert (d["qty"], d["amount"], d["status"]) == (Decimal("10"), Decimal("20.00"), "owed")
    assert ledger.check() == []


def test_a_holding_sold_before_the_ex_date_gets_nothing(store, ledger):
    trade(ledger, "BUY", "SPY", "10", "500", "b1", et(2026, 10, 15, 11))
    trade(ledger, "SELL", "SPY", "10", "501", "s1", et(2026, 10, 16, 11))
    store_actions(store, dividend())
    res = apply(store, et(2026, 10, 19, 1))
    assert res.recorded == 1 and res.notes == [] and ledger.dividends() == []
    (e,) = ledger.corporate_entries()
    assert (e["outcome"], e["detail"]) == ("not_held", {"rate": "1.888834"})


def test_a_corrected_rate_is_followed_and_a_withdrawn_dividend_is_cancelled(store, ledger):
    trade(ledger, "BUY", "SPY", "10", "500", "b1", et(2026, 10, 15, 11))
    store_actions(store, dividend(rate=1.5))
    apply(store, et(2026, 10, 19, 1))
    store_actions(store, dividend(rate=1.75), now=et(2026, 10, 20, 8))
    res = apply(store, et(2026, 10, 20, 9))
    assert res.notes == [("SPY", "dividend div corrected to $1.75/share: $15.00 -> $17.50 owed")]
    store_actions(store, now=et(2026, 10, 21, 8))                         # gone from the feed
    res = apply(store, et(2026, 10, 21, 9))
    assert "withdrawn by the source: cancelled" in res.notes[0][1]
    assert ledger.receivable() == 0 and ledger.dividends()[0]["status"] == "cancelled"
    assert ledger.check() == []


# -- splits ------------------------------------------------------------------------------
def test_a_forward_split_multiplies_each_lot_and_restates_the_stop(store, ledger):
    trade(ledger, "BUY", "GLD", "3", "400", "b1", et(2026, 10, 15, 11))
    trade(ledger, "BUY", "GLD", "2", "410", "b2", et(2026, 10, 16, 11))
    ledger.set_high_water("GLD", 420.0)
    ledger.set_stop("GLD", 0.08)
    store_actions(store, split())
    res = apply(store, et(2026, 10, 22, 0, 30))
    assert res.notes == [("GLD", "4-for-1 forward split effective 2026-10-22: 5 -> 20 shares "
                                 "(cost basis and holding period unchanged, stop restated)")]
    p = ledger.positions()["GLD"]
    assert (p.qty, p.cost, p.high_water, p.stop_pct) == (Decimal(20), Decimal("2020.00"), 105.0, 0.08)
    assert [(lot.qty_remaining, lot.cost_remaining, str(lot.opened_day)) for lot in ledger.lots("GLD")] == [
        (Decimal(12), Decimal("1200.00"), "2026-10-15"), (Decimal(8), Decimal("820.00"), "2026-10-16")]
    assert ledger.check() == []
    # A sale after the split consumes the adjusted lots, oldest first.
    fill = trade(ledger, "SELL", "GLD", "13", "101", "s1", et(2026, 10, 22, 11))
    assert [(c.qty, c.cost) for c in fill.closures] == [(Decimal(12), Decimal("1200.00")),
                                                        (Decimal(1), Decimal("102.50"))]
    assert ledger.held_before("GLD", date(2026, 10, 23)) == Decimal(7)
    assert ledger.check() == []


@pytest.mark.parametrize("fractionable, shares, lieu", [(False, "3", "14.00"), (True, "3.333333333", None)])
def test_a_reverse_split_pays_a_fraction_as_cash_in_lieu_on_whole_share_assets(store, ledger, fractionable,
                                                                                 shares, lieu):
    trade(ledger, "BUY", "PDBC", "10", "15", "b1", et(2026, 10, 15, 11))
    store_actions(store, split("PDBC", new=1, old=3))
    cash = ledger.cash()
    apply(store, et(2026, 10, 22, 1), fractionable=lambda s: fractionable, closes={"PDBC": 14.0})
    assert ledger.positions()["PDBC"].qty == Decimal(shares)
    rows = store.conn.execute("SELECT side, qty, price, notional, origin, quote_source, day, realized_pnl "
                              "FROM fills WHERE origin='corporate_action'").fetchall()
    if lieu is None:
        assert rows == [] and ledger.cash() == cash
    else:
        # 0.333333333 share at the last close restated to the new shares ($14 x 3).
        assert [tuple(r) for r in rows] == [("SELL", "0.333333333", "42.0000", lieu, "corporate_action",
                                             "cash_in_lieu", "2026-10-22", "-1.00")]
        assert ledger.cash() == cash + Decimal(lieu)
    assert ledger.check() == []


def test_cash_in_lieu_without_a_close_waits_and_says_so(store, ledger):
    trade(ledger, "BUY", "PDBC", "10", "15", "b1", et(2026, 10, 15, 11))
    store_actions(store, split("PDBC", new=1, old=3))
    when = et(2026, 10, 22, 1)
    res = apply(store, when, fractionable=lambda s: False)
    assert "no close before 2026-10-22" in res.failed["split"]
    assert corporate.unapplied(store, date(2026, 10, 22)) == {
        "PDBC": "reverse split effective 2026-10-22 not applied to the book yet"}
    assert ledger.positions()["PDBC"].qty == Decimal(10) and ledger.corporate_entries() == []
    (alarm,) = health.corporate_alarms(store, when)
    assert alarm.key == "corporate:split" and alarm.severity == health.CRITICAL and "no close" in alarm.message
    apply(store, when + 60, fractionable=lambda s: False, closes={"PDBC": 14.0})
    assert health.corporate_alarms(store, when + 60) == [] and store.get_state(corporate.FAILURES) == {}
    assert ledger.check() == []


def test_trades_after_a_split_took_effect_need_a_person(store, ledger):
    trade(ledger, "BUY", "GLD", "5", "400", "b1", et(2026, 10, 15, 11))
    trade(ledger, "BUY", "GLD", "1", "100", "b2", et(2026, 10, 22, 10))   # a fill the split never saw
    store_actions(store, split())
    res = apply(store, et(2026, 10, 22, 11))
    assert "traded on or after 2026-10-22 before this was applied (5 shares going in, 6 now)" in res.failed["split"]
    assert ledger.positions()["GLD"].qty == Decimal(6)


def test_an_unknown_ratio_is_a_failure_not_a_guess(store, ledger):
    trade(ledger, "BUY", "GLD", "5", "400", "b1", et(2026, 10, 15, 11))
    kind, row = split()
    del row["new_rate"]
    store_actions(store, (kind, row))
    res = apply(store, et(2026, 10, 22, 1))
    assert "has no usable 'new_rate'" in res.failed["split"] and ledger.corporate_entries() == []


# -- cash mergers and actions the book can't model -----------------------------------------------
def test_a_cash_merger_sells_the_holding_at_the_deal_price(store, ledger):
    trade(ledger, "BUY", "XYZ", "10", "30", "b1", et(2026, 10, 15, 11))
    store_actions(store, merger())
    cash = ledger.cash()
    res = apply(store, et(2026, 10, 23, 1))                               # booked a day late
    assert res.notes == [("XYZ", "cash merger effective 2026-10-22: 10 shares became $335.00 ($33.5/share), "
                                 "realized $+35.00")]
    assert "XYZ" not in ledger.positions() and ledger.cash() == cash + Decimal("335.00")
    (row,) = store.conn.execute("SELECT idem_key, day, quote_source, origin FROM fills WHERE side='SELL'")
    assert tuple(row) == ("ca:cash", "2026-10-22", "cash_merger", "corporate_action")
    assert ledger.check() == []


def test_an_action_the_book_cant_model_is_exited_the_session_before_and_alarmed(store, ledger):
    trade(ledger, "BUY", "TLT", "10", "90", "b1", et(2026, 10, 15, 11))
    store_actions(store, stock_merger(effective="2026-10-26"))            # a Monday
    held = ledger.positions()
    assert corporate.exits_due(store, CAL, date(2026, 10, 22), held) == {}  # Thu: two sessions to go
    (aid, why), = corporate.exits_due(store, CAL, date(2026, 10, 23), held).values()   # Fri: the session before
    assert aid == "stock" and why == "stock merger effective 2026-10-26, which the book can't model: " \
                                     "exiting the session before"
    (warn,) = health.corporate_alarms(store, et(2026, 10, 23, 10))
    assert warn.severity == health.WARNING and "BUYs are blocked" in warn.message
    # Held through it anyway (the exit never filled): recorded, and critical while held.
    res = apply(store, et(2026, 10, 26, 1))
    assert "held 10 shares through an action the book can't model" in res.notes[0][1]
    assert ledger.corporate_entries()[0]["outcome"] == "unmodeled"
    crit = [a for a in health.corporate_alarms(store, et(2026, 10, 26, 10)) if a.severity == health.CRITICAL]
    assert len(crit) == 1 and "was held through a stock merger on 2026-10-26" in crit[0].message
    trade(ledger, "SELL", "TLT", "10", "91", "s1", et(2026, 10, 26, 11))
    assert [a for a in health.corporate_alarms(store, et(2026, 10, 26, 12)) if a.severity == health.CRITICAL] == []


def test_an_unmodeled_action_with_no_date_is_exited_at_once(store, ledger):
    trade(ledger, "BUY", "TLT", "10", "90", "b1", et(2026, 10, 15, 11))
    store_actions(store, stock_merger(effective=None))
    assert set(corporate.exits_due(store, CAL, date(2026, 10, 15), ledger.positions())) == {"TLT"}


def test_the_monitor_sells_a_holding_ahead_of_an_unmodeled_action(cfg):
    p = Position("TLT", Decimal(10), Decimal(900), 90.0, OPEN - 86400, 0.05)
    v = Valuation(equity=10000.0, cash=9100.0, invested=900.0, weights={"TLT": 0.09}, values={"TLT": 900.0})
    exits = {"TLT": ("stock", "stock merger effective 2026-10-26, which the book can't model")}
    for quotes in ({"TLT": quote("TLT", 90.0, now=OPEN)}, {}):            # with or without a quote
        res = monitor.check(cfg, OPEN, {"TLT": p}, v, quotes, RiskState(), 100, {"TLT": "rates"}, exits=exits)
        (i,) = res.intents
        assert (i.origin, i.side, i.fraction, i.idem_key, i.ref) == ("corp_exit", "SELL", 1.0, "corp_exit:TLT:stock",
                                                                     "stock")
    # A stop already selling it is enough: no second order.
    res = monitor.check(cfg, OPEN, {"TLT": p}, v, {"TLT": quote("TLT", 80.0, now=OPEN)}, RiskState(), 100,
                        {"TLT": "rates"}, exits=exits)
    assert [i.origin for i in res.intents] == ["stop"]


def test_an_applied_split_the_source_withdraws_is_alarmed(store, ledger):
    trade(ledger, "BUY", "GLD", "5", "400", "b1", et(2026, 10, 15, 11))
    store_actions(store, split())
    apply(store, et(2026, 10, 22, 1))
    assert health.corporate_alarms(store, et(2026, 10, 22, 2)) == []
    store_actions(store, now=et(2026, 10, 22, 8))
    (alarm,) = health.corporate_alarms(store, et(2026, 10, 22, 9))
    assert alarm.severity == health.CRITICAL and "applied to the book, then withdrawn" in alarm.message


def test_an_action_left_unapplied_too_long_is_alarmed_even_without_a_failure(store, ledger):
    store_actions(store, split(), now=et(2026, 10, 20, 8))
    assert health.corporate_alarms(store, et(2026, 10, 22, 0, 30)) == []     # just became due
    (alarm,) = health.corporate_alarms(store, et(2026, 10, 22, 2))
    assert "in effect but not applied to the book (is the engine running?)" in alarm.message


# -- prices --------------------------------------------------------------------------------
def test_marks_from_the_last_close_are_restated_by_what_was_applied_since(store, ledger):
    store_actions(store, dividend(rate=2, ex="2026-10-22"), split())
    apply(store, et(2026, 10, 22, 1))                                     # neither held: recorded all the same
    m = corporate.restate_marks(store, marks(SPY=500.0, GLD=400.0, TLT=90.0), date(2026, 10, 21))
    assert (m["SPY"].bid, m["GLD"].bid, m["TLT"].bid) == (498.0, 100.0, 90.0)
    assert corporate.restate_marks(store, marks(GLD=101.0), date(2026, 10, 22))["GLD"].bid == 101.0
    assert corporate.split_ratios(store, date(2026, 10, 21), date(2026, 10, 22)) == {"GLD": 4.0}


def test_a_dividend_on_a_split_day_is_taken_off_before_the_split(store, ledger):
    store_actions(store, dividend("GLD", rate=4, ex="2026-10-22"), split())
    apply(store, et(2026, 10, 22, 1))
    assert corporate.restate_marks(store, marks(GLD=404.0), date(2026, 10, 21))["GLD"].bid == 100.0


def test_a_quote_the_split_explains_is_not_off_market():
    now = OPEN
    snap = {"latestQuote": {"bp": 99.99, "ap": 100.01, "t": "2026-10-21T15:00:00Z"},
            "latestTrade": {"p": 100.0, "t": "2026-10-21T14:59:58Z"}, "prevDailyBar": {"c": 400.0}}
    q, why = check_quote("GLD", snap, now, split=4.0)
    assert q is not None and why == "ok"
    assert check_quote("GLD", snap, now)[1].startswith("off_market")


# -- the replay -------------------------------------------------------------------------
@pytest.mark.parametrize("sql, needle", [
    ("UPDATE lot_adjustments SET qty_after='13'", "position"),
    ("UPDATE lot_adjustments SET qty_before='4'", "a split adjusted 4 shares, the replay holds 3"),
    ("DELETE FROM corporate_entries WHERE action_id='split'", "has no corporate entry"),
    ("UPDATE dividends SET amount='99.00'", "dividend gd: 99.00 isn't"),
    ("UPDATE cash_events SET amount='21.00' WHERE kind='dividend'", "cash"),
    ("UPDATE dividends SET status='owed', cash_event_id=NULL", "pays no dividend"),
])
def test_check_catches_tampering_with_splits_and_dividends(store, ledger, sql, needle):
    trade(ledger, "BUY", "GLD", "3", "400", "b1", et(2026, 10, 15, 11))
    trade(ledger, "BUY", "GLD", "2", "410", "b2", et(2026, 10, 16, 11))
    store_actions(store, dividend("GLD", rate=2, ex="2026-10-22", pay="2026-10-23", id="gd"), split())
    apply(store, et(2026, 10, 23, 1))
    assert ledger.check() == []
    with store.tx() as c:
        c.execute(sql)
    assert any(needle in p for p in ledger.check()), ledger.check()


def test_the_discord_message():
    assert format_event("corporate_action", {"symbol": "SPY", "note": "cash dividend paid: $18.89"}) == \
        "**SPY** corporate action: cash dividend paid: $18.89"


# -- the engine, end to end ---------------------------------------------------------------
async def test_a_split_and_a_dividend_overnight_leave_equity_whole(cfg, store, ledger):
    """Hold GLD and TLT on Wednesday; GLD splits 4-for-1 and TLT goes ex a $0.30 dividend
    on Thursday. At no point (Wednesday's close, Thursday before the open, Thursday in
    session) may equity jump, the stop fire on the split, or a quote be refused for it."""
    no_corporate_actions(store, cfg.core)
    rig = await market_rig(cfg, store, through=date(2026, 10, 22)).ready()   # Alpaca has bars through Wed
    eng = Engine(cfg, store, rig.market, council=buy_council(cfg, "TLT"), sources=Sources(alpaca=rig.market.alpaca),
                 clock=rig.clock)
    for s in ("GLD", "TLT"):
        await eng.executor.submit(Intent(origin="council", asset=s, side="BUY", conviction=1.0,
                                         idem_key=f"buy-{s}", created_ts=OPEN))
    await eng.executor.drain()
    before = ledger.positions()
    assert set(before) == {"GLD", "TLT"}
    wed = {s: rig.price(s) for s in before}
    array, row = split()
    rig.fake.add_action(array, **row)

    rig.at(et(2026, 10, 21, 17), requote=False)                            # Wednesday, after the close
    await eng.market_tick()
    await eng.jobs_tick()
    assert {a.id for s in before for a in db.actions_for(store, s)} == {"split"}
    close = ledger.value(await rig.market.marks())
    assert ledger.corporate_entries() == []
    # The dividend only shows up in the feed overnight: it is booked in the tick that fetches it.
    array, row = dividend("TLT", rate=0.30, ex="2026-10-22", pay="2026-10-27")
    rig.fake.add_action(array, **row)

    rig.at(et(2026, 10, 22, 0, 30), requote=False)                         # Thursday, before the open
    done = await eng.jobs_tick()
    assert "corporate_actions refreshed" in done and done.endswith("2 corporate action update(s)")
    night = ledger.value(await rig.market.marks())
    gld = ledger.positions()["GLD"]
    assert gld.qty == before["GLD"].qty * 4 and gld.high_water == pytest.approx(before["GLD"].high_water / 4)
    assert night.receivable == pytest.approx(float(before["TLT"].qty) * 0.30, abs=0.01)
    assert night.equity == pytest.approx(close.equity, abs=0.02)

    rig.at(et(2026, 10, 22, 9, 40))                                        # Thursday, in session
    for s, px in (("GLD", wed["GLD"] / 4), ("TLT", wed["TLT"] - 0.30)):
        # Alpaca's previous daily bar is still Wednesday's, in the old shares for GLD.
        rig.fake.add_quote(s, round(px * 0.9999, 2), round(px * 1.0001, 2), age=2.0, prev_close=wed[s])
    q, why = await rig.market.quote("GLD")
    assert q is not None, why
    res = await eng.risk_tick()
    assert res.intents == [] and res.summary() == "all clear"
    assert (await eng.equity_tick()).startswith("equity $")
    live = ledger.value(await rig.market.marks())
    assert live.equity == pytest.approx(close.equity, rel=0.0005)          # within the quotes' spread

    rig.at(et(2026, 10, 27, 0, 30), requote=False)                         # TLT's pay date
    cash = ledger.cash()
    await eng.jobs_tick()
    assert ledger.receivable() == 0 and ledger.cash() == cash + Decimal(str(night.receivable)).quantize(Decimal("0.01"))
    assert ledger.check() == [] and health.corporate_alarms(store, et(2026, 10, 27, 1)) == []


async def test_trading_and_the_equity_curve_wait_for_an_action_the_book_couldnt_apply(cfg, store, ledger):
    """A held asset's split that can't be applied (no ratio) holds back every BUY (equity
    is wrong until it is), the equity point and the stop on that asset, until it is fixed."""
    no_corporate_actions(store, cfg.core)
    rig = await market_rig(cfg, store).ready()
    eng = Engine(cfg, store, rig.market, council=buy_council(cfg, "TLT"), clock=rig.clock)
    trade(ledger, "BUY", "PDBC", "10", str(rig.price("PDBC")), "b-pdbc", et(2026, 10, 20, 11))
    kind, row = split("PDBC", new=1, old=2, ex="2026-10-21")
    store_actions(store, (kind, {k: v for k, v in row.items() if k != "new_rate"}), now=OPEN)
    ca = eng.apply_corporate_actions()
    assert list(ca.failed) == ["split"]
    assert eng.buy_blocks(OPEN)["PDBC"] == "reverse split effective 2026-10-21 not applied to the book yet"

    intent = Intent(origin="council", asset="TLT", side="BUY", conviction=1.0, idem_key="buy-tlt", created_ts=OPEN)
    await eng.executor.submit(intent)
    await eng.executor.drain()
    status, verdict = store.conn.execute("SELECT status, verdict FROM orders WHERE idem_key='buy-tlt'").fetchone()
    assert status == "deferred" and '"corporate_action_unapplied"' in verdict
    with pytest.raises(Exception, match="PDBC isn't applied yet; equity unknown"):
        await eng.equity_tick()
    rig.at(et(2026, 10, 21, 11, 10))
    res = await eng.risk_tick()
    assert res.intents == []

    store_actions(store, split("PDBC", new=1, old=2, ex="2026-10-21"), now=et(2026, 10, 21, 11, 15))
    assert eng.apply_corporate_actions().failed == {}
    assert ledger.positions()["PDBC"].qty == Decimal(5)
    await eng.executor.submit(intent)                                      # the jobs task's retry
    await eng.executor.drain()
    assert store.conn.execute("SELECT status FROM orders WHERE idem_key='buy-tlt'").fetchone()[0] == "filled"
    assert ledger.check() == []
