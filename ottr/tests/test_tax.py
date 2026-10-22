"""
Tax estimates (ledger/tax.py): holding periods, wash sales (after and before the loss, partial,
the sold lot itself, a replacement already sold, chains carrying the holding period),
collectibles, Schedule D netting with the $3,000 deduction and carryforward, dividends by
class, splits, after-tax equity, and the `ottr tax` report. Checked by hand against the
rules in IRS Publication 550; every number below is worked out in its comment.
"""

import dataclasses
from datetime import date
from decimal import Decimal

import pytest

from ottr.cli import tax_report
from ottr.ledger import tax
from tests.conftest import quote
from tests.fakes import et

D = Decimal


def trade(lg, side, asset, q, price, key, when):
    with lg.store.tx():
        return lg.apply_fill(idem_key=key, asset=asset, side=side, quantity=q, price=price, quote_source="test",
                             origin="test", now=when)


def dividend(store, asset, amount, day):
    with store.tx() as c:
        c.execute("INSERT INTO cash_events(ts,day,kind,asset,qty,amount,note) VALUES(?,?,?,?,?,?,?)",
                  (0.0, day, "dividend", asset, "1", amount, "test"))
        c.execute("UPDATE account SET cash=CAST(cash AS REAL)+? WHERE id=1", (float(amount),))


def rep(store, cfg):
    return tax.replay(store, cfg.tax.collectibles)


def year(store, cfg, y, through=None):
    r = rep(store, cfg)
    divs = tax.dividends_by_year(store, tax.classes_of(cfg, store), cfg.tax.qualified_dividend_classes)
    return {t.year: t for t in tax.years(r, divs, cfg.tax, through or y)}[y]


def test_short_and_long_term(cfg, store, ledger):
    trade(ledger, "BUY", "SPY", "10", "100", "b1", et(2026, 10, 15, 11))
    trade(ledger, "SELL", "SPY", "4", "110", "s1", et(2026, 11, 2, 11))       # +40 short term
    trade(ledger, "SELL", "SPY", "6", "130", "s2", et(2027, 10, 18, 11))      # +180 long term (a year and 3 days)
    d1, d2 = rep(store, cfg).dispositions
    assert (d1.term, d1.gain, d2.term, d2.gain) == ("short", D("40.00"), "long", D("180.00"))
    assert year(store, cfg, 2026).federal == D("9.60")                       # 40 x 24%
    assert year(store, cfg, 2027).federal == D("27.00")                      # 180 x 15%


def test_a_loss_replaced_within_30_days_is_deferred_into_the_new_shares(cfg, store, ledger):
    trade(ledger, "BUY", "SPY", "10", "100", "b1", et(2026, 10, 15, 11))
    trade(ledger, "SELL", "SPY", "10", "90", "s1", et(2026, 10, 26, 11))     # a $100 loss...
    trade(ledger, "BUY", "SPY", "10", "92", "b2", et(2026, 11, 5, 11))       # ...replaced 10 days later
    trade(ledger, "SELL", "SPY", "10", "95", "s2", et(2026, 12, 30, 11))
    r = rep(store, cfg)
    sale, later = r.dispositions
    # The loss is disallowed now: reported gain 900 - 1000 + 100 = 0.
    assert (sale.disallowed, sale.gain) == (D("100.00"), D("0.00"))
    (w,) = r.washes
    assert (w.qty, w.disallowed) == (D(10), D("100.00"))
    # It comes back on the replacement: basis 920 + 100, held since Nov 5 less the 11 days
    # the first shares were held (Oct 25); 950 - 1020 = a $70 loss.
    assert (later.adjustment, later.basis, later.acquired, later.gain) == (
        D("100.00"), D("1020.00"), date(2026, 10, 25), D("-70.00"))
    t = year(store, cfg, 2026)
    assert (t.short_term, t.disallowed, t.deduction, t.federal) == (D("-70.00"), D("100.00"), D("70.00"), D("-16.80"))


def test_a_partial_replacement_defers_only_its_share_of_the_loss(cfg, store, ledger):
    trade(ledger, "BUY", "SPY", "10", "100", "b1", et(2026, 10, 15, 11))
    trade(ledger, "BUY", "SPY", "4", "95", "b2", et(2026, 10, 20, 11))        # bought 6 days before the sale
    trade(ledger, "SELL", "SPY", "10", "90", "s1", et(2026, 10, 26, 11))      # FIFO: the first lot, -$100
    d = rep(store, cfg).dispositions[0]
    assert (d.disallowed, d.gain) == (D("40.00"), D("-60.00"))              # 4 of 10 shares replaced


def test_the_shares_sold_and_a_replacement_already_gone_are_not_replacements(cfg, store, ledger):
    trade(ledger, "BUY", "SPY", "5", "100", "b1", et(2026, 10, 15, 11))
    trade(ledger, "BUY", "SPY", "5", "100", "b2", et(2026, 10, 16, 11))
    trade(ledger, "SELL", "SPY", "5", "101", "s1", et(2026, 10, 19, 11))      # the first lot, at a gain
    trade(ledger, "SELL", "SPY", "5", "90", "s2", et(2026, 10, 22, 11))       # the second, at a loss
    r = rep(store, cfg)
    assert r.washes == [] and r.dispositions[1].gain == D("-50.00")


def test_wash_sales_chain_and_carry_the_holding_period_into_long_term(cfg, store, ledger):
    trade(ledger, "BUY", "TLT", "10", "100", "b1", et(2026, 10, 15, 11))
    trade(ledger, "SELL", "TLT", "10", "80", "s1", et(2027, 9, 1, 11))        # -200 after 321 days
    trade(ledger, "BUY", "TLT", "10", "81", "b2", et(2027, 9, 20, 11))
    trade(ledger, "SELL", "TLT", "10", "100", "s2", et(2027, 10, 1, 11))      # 11 days later...
    last = rep(store, cfg).dispositions[-1]
    # ...but it carries the 321 days: acquired 2026-11-03, sold 2027-10-01 is still short; the
    # gain is 1000 - (810 + 200) = -10.
    assert (last.acquired, last.term, last.gain) == (date(2026, 11, 3), "short", D("-10.00"))
    trade(ledger, "BUY", "TLT", "10", "99", "b3", et(2027, 10, 5, 11))
    trade(ledger, "SELL", "TLT", "10", "120", "s3", et(2027, 11, 20, 11))
    final = rep(store, cfg).dispositions[-1]
    # The $10 loss moved on with the 332 days held (2026-11-03 to 2027-10-01): acquired
    # 2027-10-05 less 332 days = 2026-11-07, sold 2027-11-20: long.
    assert (final.acquired, final.term, final.gain) == (date(2026, 11, 7), "long", D("200.00"))


def test_collectibles_and_the_netting_of_short_against_long(cfg, store, ledger):
    trade(ledger, "BUY", "GLD", "10", "200", "b1", et(2026, 10, 15, 11))
    trade(ledger, "BUY", "SPY", "10", "100", "b2", et(2026, 10, 15, 11))
    trade(ledger, "BUY", "QQQ", "10", "100", "b3", et(2027, 9, 1, 11))
    trade(ledger, "SELL", "GLD", "10", "250", "s1", et(2027, 11, 1, 11))      # +500 long, collectible
    trade(ledger, "SELL", "SPY", "10", "130", "s2", et(2027, 11, 1, 11))      # +300 long
    trade(ledger, "SELL", "QQQ", "10", "70", "s3", et(2027, 11, 1, 11))       # -300 short
    t = year(store, cfg, 2027)
    assert (t.short_term, t.long_term, t.collectibles) == (D("-300.00"), D("300.00"), D("500.00"))
    # The short-term loss comes off the collectibles first: 200 at min(28%, 24%) + 300 at 15%.
    assert t.federal == D("93.00")
    rates = dataclasses.replace(cfg.tax, federal_ordinary_pct=35.0, state_pct=5.0, niit_pct=3.8)
    est = tax.estimate(2027, rep(store, cfg).dispositions, {}, D(0), rates)
    assert (est.federal, est.state, est.niit) == (D("101.00"), D("25.00"), D("19.00"))   # 200 x 28% + 300 x 15%


def test_a_net_loss_deducts_3000_and_carries_the_rest(cfg, store, ledger):
    trade(ledger, "BUY", "QQQ", "100", "100", "b1", et(2026, 10, 15, 11))
    trade(ledger, "SELL", "QQQ", "100", "50", "s1", et(2026, 12, 1, 11))      # -5000
    trade(ledger, "BUY", "SPY", "10", "100", "b2", et(2027, 1, 5, 11))
    trade(ledger, "SELL", "SPY", "10", "500", "s2", et(2027, 3, 1, 11))       # +4000 short term
    t26, t27 = year(store, cfg, 2026, 2027), year(store, cfg, 2027)
    assert (t26.deduction, t26.carry_out, t26.federal) == (D("3000"), D("2000"), D("-720.00"))
    assert (t27.carry_in, t27.federal) == (D("2000"), D("480.00"))           # (4000 - 2000) x 24%


def test_dividends_are_qualified_or_ordinary_by_class(cfg, store, ledger):
    dividend(store, "SPY", "100.00", "2026-10-30")
    dividend(store, "TLT", "50.00", "2026-11-02")
    dividend(store, "NVDA", "10.00", "2026-11-03")                           # a satellite: a stock
    t = year(store, cfg, 2026)
    assert (t.qualified_dividends, t.ordinary_dividends, t.federal) == (D("110.00"), D("50.00"), D("28.50"))


def test_a_split_keeps_the_wash_sale_shares_in_step(cfg, store, ledger):
    from ottr.ledger import corporate
    from tests.test_corporate import split, store_actions
    trade(ledger, "BUY", "GLD", "10", "400", "b1", et(2026, 10, 15, 11))
    store_actions(store, split())                                            # 4-for-1 on Oct 22
    corporate.apply_due(store, et(2026, 10, 22, 1), lambda s: True, lambda s, d: None)
    trade(ledger, "SELL", "GLD", "40", "90", "s1", et(2026, 10, 23, 11))      # 3600 - 4000 = -400
    trade(ledger, "BUY", "GLD", "20", "91", "b2", et(2026, 10, 28, 11))       # half replaced
    d = rep(store, cfg).dispositions[0]
    assert (d.qty, d.disallowed, d.gain) == (D(40), D("200.00"), D("-200.00"))
    assert ledger.check() == []


def test_after_tax_equity_counts_realized_and_unrealized_gains(cfg, store, ledger):
    trade(ledger, "BUY", "SPY", "10", "100", "b1", et(2026, 10, 15, 11))
    trade(ledger, "SELL", "SPY", "5", "120", "s1", et(2026, 11, 2, 11))       # +100 realized
    marks = {"SPY": quote("SPY", 140.0, spread_pct=0.0)}                      # +200 unrealized
    v = ledger.value(marks)
    after = tax.after_tax_equity(cfg, store, marks, v.equity, date(2026, 11, 3))
    assert after == pytest.approx(v.equity - 300 * 0.24)
    assert tax.after_tax_equity(cfg, store, {}, v.equity, date(2026, 11, 3)) is None


def test_the_tax_report(cfg, store, ledger):
    trade(ledger, "BUY", "SPY", "10", "100", "b1", et(2026, 10, 15, 11))
    trade(ledger, "SELL", "SPY", "10", "90", "s1", et(2026, 10, 26, 11))
    trade(ledger, "BUY", "SPY", "10", "92", "b2", et(2026, 11, 5, 11))
    text = tax_report(cfg, store, {"SPY": quote("SPY", 95.0, spread_pct=0.0)}, date(2026, 11, 10), detail=True)
    assert text.startswith("TAX ESTIMATES for the paper book (rates in config.toml [tax]: federal 24% ordinary")
    assert "Not tax advice." in text and "wash sales deferred $100.00" in text
    assert "2026-10-26 SPY    10 acquired 2026-10-15: proceeds $900.00, basis $1,000.00, short term +$0.00, " \
           "wash sale: $100.00 disallowed" in text
    assert "Selling everything today at these prices would add about $-16.80 of tax." in text
    assert "—" not in text


async def test_the_equity_curve_records_after_tax_equity(cfg, store, ledger):
    from ottr.engine.engine import Engine
    from tests.fakes import OPEN, market_rig, no_corporate_actions
    no_corporate_actions(store, cfg.core)
    rig = await market_rig(cfg, store).ready()
    eng = Engine(cfg, store, rig.market, clock=rig.clock)
    trade(ledger, "BUY", "TLT", "10", str(rig.price("TLT") * 0.8), "b1", OPEN - 86400)   # a 25% gain to tax
    assert (await eng.equity_tick()).startswith("equity $")
    equity, after = store.conn.execute("SELECT equity, after_tax FROM equity").fetchone()
    gain = 10 * (ledger.value(await rig.market.marks()).values["TLT"] / 10 - rig.price("TLT") * 0.8)
    assert after == pytest.approx(equity - gain * 0.24, abs=0.02)
