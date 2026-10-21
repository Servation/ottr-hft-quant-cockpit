"""
Ledger + store tests: cents and 1e-9 precision, FIFO tax lots, holding terms, fee
breakdowns, the rebuilt-from-the-log check(), atomicity, idempotency and the writer lock.
"""

from datetime import date, datetime
from decimal import Decimal

import pytest

from ottr.ledger.book import Ledger, LedgerError, anniversary, holding_term
from ottr.ledger.store import LockError, Store
from ottr.market.calendar import ET
from tests.conftest import quote


def at(y, m, d, h=12):
    return datetime(y, m, d, h, tzinfo=ET).timestamp()


def fill(lg, side, asset, q, price, key, fee=0.4, **kw):
    with lg.store.tx():
        return lg.apply_fill(idem_key=key, asset=asset, side=side, quantity=q, price=price,
                             fee_pct=fee, quote_source="test", origin="test", **kw)


def buy(lg, asset, q, price, key, fee=0.4, **kw):
    return fill(lg, "BUY", asset, q, price, key, fee, **kw)


def sell(lg, asset, q, price, key, fee=0.4, **kw):
    return fill(lg, "SELL", asset, q, price, key, fee, **kw)


# -- cash, cost, precision ----------------------------------------------------------------

def test_buy_capitalizes_fee_and_sell_realizes_net(ledger):
    buy(ledger, "ETH", "1", "2000", "b1")
    p = ledger.positions()["ETH"]
    assert str(p.cost) == "2008.00"            # 2000 + 0.4% fee, in cents
    assert str(ledger.cash()) == "7992.00"
    f = sell(ledger, "ETH", "0.5", "2200", "s1")
    # proceeds 1100, fee 4.40, cost out 1004 -> realized 91.60
    assert str(f.realized_pnl) == "91.60"
    assert str(ledger.cash()) == "9087.60"
    assert str(ledger.positions()["ETH"].cost) == "1004.00"
    assert ledger.check() == []


def test_amounts_are_cents_and_quantities_nine_decimals(ledger):
    f = buy(ledger, "SPY", "0.1234567891234", "500", "b", fee=0)
    assert str(f.qty) == "0.123456789"         # Alpaca's fractional precision
    assert str(f.notional) == "61.73"          # 61.72839455 rounded to the cent
    assert str(ledger.positions()["SPY"].qty) == "0.123456789"
    assert ledger.check() == []


def test_full_exit_removes_position_and_books_balance(ledger):
    buy(ledger, "SOL", "10", "100", "b")
    sell(ledger, "SOL", "10", "90", "s")
    assert "SOL" not in ledger.positions()
    assert ledger.lots("SOL") == []                          # no open lots
    assert ledger.lots("SOL", open_only=False)[0].closed_ts is not None
    assert ledger.account()["realized_pnl"] == Decimal("-107.60")  # 896.40 - 1004.00
    assert ledger.check() == []


def test_overspend_and_oversell_roll_back(ledger):
    with pytest.raises(LedgerError):
        buy(ledger, "BTC", "1", "100000", "too-big")
    buy(ledger, "ADA", "100", "1", "b")
    with pytest.raises(LedgerError):
        sell(ledger, "ADA", "101", "1", "too-many")
    assert ledger.cash() == Decimal("9899.60")
    assert ledger.store.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1
    assert len(ledger.lots("ADA")) == 1
    assert ledger.check() == []


def test_fees_larger_than_the_sale_are_refused(ledger):
    buy(ledger, "X", "1", "1", "b", fee=0)
    with pytest.raises(LedgerError, match="exceed"):
        sell(ledger, "X", "1", "0.01", "s", fee=0, fees={"sec": "0.05"})


# -- FIFO lots ---------------------------------------------------------------------------

def test_sells_consume_the_oldest_lots_first(ledger):
    buy(ledger, "SPY", "2", "100", "b1", fee=0, now=at(2026, 1, 5))
    buy(ledger, "SPY", "3", "110", "b2", fee=0, now=at(2026, 2, 5))
    buy(ledger, "SPY", "1", "120", "b3", fee=0, now=at(2026, 3, 5))
    f = sell(ledger, "SPY", "4", "130", "s", fee=0, now=at(2026, 4, 1))
    # All of lot 1 (2 @ 100) and 2 of lot 2's 3 shares (@ 110).
    assert [(c.qty, c.cost, c.proceeds, c.realized) for c in f.closures] == [
        (Decimal("2"), Decimal("200.00"), Decimal("260.00"), Decimal("60.00")),
        (Decimal("2"), Decimal("220.00"), Decimal("260.00"), Decimal("40.00")),
    ]
    assert f.realized_pnl == Decimal("100.00")
    open_lots = ledger.lots("SPY")
    assert [(lot.qty_remaining, lot.cost_remaining) for lot in open_lots] == [
        (Decimal("1"), Decimal("110.00")), (Decimal("1"), Decimal("120.00"))]
    p = ledger.positions()["SPY"]
    assert (p.qty, p.cost) == (Decimal("2"), Decimal("230.00"))
    assert ledger.check() == []


def test_awkward_splits_always_add_up_to_the_cent(ledger):
    # A lot whose cost doesn't divide evenly, sold in thirds with a fee on each sale.
    buy(ledger, "QQQ", "3", "33.333", "b", fee=0)          # cost 100.00 (99.999 rounded)
    for i in range(3):
        sell(ledger, "QQQ", "1", "35.017", f"s{i}", fee=0.4)
    lot = ledger.lots("QQQ", open_only=False)[0]
    assert (lot.qty_remaining, lot.cost_remaining) == (0, 0)
    assert sum(c.cost for c in ledger.closures()) == Decimal("100.00")  # nothing lost to rounding
    assert ledger.check() == []


def test_one_sale_across_lots_splits_proceeds_exactly(ledger):
    for i, px in enumerate(("10.01", "10.02", "10.03")):
        buy(ledger, "IWM", "1", px, f"b{i}", fee=0)
    f = sell(ledger, "IWM", "3", "10.00", "s", fee=0.4)    # net 29.88 split 3 ways
    assert sum(c.proceeds for c in f.closures) == f.notional - f.fee == Decimal("29.88")
    assert ledger.check() == []


def test_init_holdings_are_lots_consumed_before_later_buys(store):
    lg = Ledger(store)
    lg.initialize(1000, holdings={"GLD": (Decimal("2"), 200.0)}, now=at(2026, 1, 2))
    buy(lg, "GLD", "1", "250", "b", fee=0, now=at(2026, 2, 2))
    f = sell(lg, "GLD", "1", "260", "s", fee=0, now=at(2026, 3, 2))
    assert f.closures[0].lot_id == lg.lots("GLD", open_only=False)[0].id
    assert lg.lots("GLD", open_only=False)[0].open_fill_id is None
    assert f.realized_pnl == Decimal("60.00")              # vs the 200 import mark, not 250
    assert lg.check() == []


def test_import_marks_holdings_at_day0(store):
    lg = Ledger(store)
    lg.initialize(5898.76, note="v1 import", holdings={"ADA": (Decimal("10081.9"), 0.2215)})
    p = lg.positions()["ADA"]
    assert p.cost == Decimal("2233.14")       # qty * mark in cents: no invented history
    assert p.high_water == 0.2215
    assert lg.check() == []
    with pytest.raises(LedgerError):
        lg.initialize(1)


# -- holding period -----------------------------------------------------------------------

@pytest.mark.parametrize("opened,sold,term", [
    (date(2025, 3, 1), date(2026, 3, 1), "short"),   # on the anniversary: not more than a year
    (date(2025, 3, 1), date(2026, 3, 2), "long"),
    (date(2024, 2, 29), date(2025, 2, 28), "short"),
    (date(2024, 2, 29), date(2025, 3, 1), "long"),
])
def test_holding_term(opened, sold, term):
    assert holding_term(opened, sold) == term


def test_closures_carry_short_and_long_terms(ledger):
    buy(ledger, "TLT", "1", "90", "b1", fee=0, now=at(2025, 3, 2))
    buy(ledger, "TLT", "1", "91", "b2", fee=0, now=at(2026, 1, 5))
    f = sell(ledger, "TLT", "2", "95", "s", fee=0, now=at(2026, 3, 3))
    assert [c.term for c in f.closures] == ["long", "short"]
    assert anniversary(date(2025, 3, 2)) == date(2026, 3, 2)


def test_trade_date_is_new_york_not_this_pc(ledger):
    late = datetime(2026, 10, 21, 22, 30, tzinfo=ET).timestamp()  # 02:30 UTC on the 22nd
    assert buy(ledger, "SPY", "1", "10", "b", fee=0, now=late).day == date(2026, 10, 21)


# -- fee breakdown and quote snapshot ---------------------------------------------------------

def test_fee_breakdown_is_stored_and_charged_to_the_cent(ledger):
    buy(ledger, "SPY", "10", "500", "b", fee=0)
    q = quote("SPY", 520)
    f = sell(ledger, "SPY", "10", "520", "s", fee=0, fees={"sec": "0.10712", "taf": "0.00195"}, quote=q)
    assert f.fee == Decimal("0.11")
    assert ledger.fee_detail(f.id) == {"sec": Decimal("0.10712"), "taf": Decimal("0.00195")}
    snap = ledger.store.conn.execute("SELECT quote FROM fills WHERE id=?", (f.id,)).fetchone()[0]
    assert '"bid"' in snap and '"basis": "live"' in snap
    assert ledger.check() == []


# -- check() catches tampering ---------------------------------------------------------------

def _two_lot_book(lg):
    buy(lg, "SPY", "1", "100", "b1", fee=0)
    buy(lg, "SPY", "1", "110", "b2", fee=0)
    sell(lg, "SPY", "1", "120", "s", fee=0)
    assert lg.check() == []


@pytest.mark.parametrize("sql,needle", [
    ("UPDATE lots SET qty_remaining='0.5' WHERE id=2", "stored remaining"),
    ("UPDATE lot_closures SET lot_id=2", "broke FIFO"),
    ("UPDATE positions SET qty='3'", "position"),
    ("UPDATE account SET realized_pnl='99.00'", "realized"),
    ("UPDATE account SET cash='1.00'", "cash"),
    ("UPDATE lot_closures SET proceeds='1.00'", "net proceeds"),
    ("INSERT INTO cash_events(ts,day,kind,amount) VALUES(0,'2026-01-01','gift','5')", "unknown cash event"),
])
def test_check_catches_tampering(ledger, sql, needle):
    _two_lot_book(ledger)
    with ledger.store.tx() as c:
        c.execute(sql)
    assert any(needle in p for p in ledger.check()), ledger.check()


# -- store guarantees ------------------------------------------------------------------------

def test_same_idempotency_key_cannot_fill_twice(ledger):
    buy(ledger, "ETH", "1", "100", "dup")
    with pytest.raises(Exception):
        buy(ledger, "ETH", "1", "100", "dup")
    assert ledger.positions()["ETH"].qty == Decimal("1")
    assert len(ledger.lots("ETH")) == 1
    assert ledger.check() == []


def test_fill_outside_transaction_is_refused(ledger):
    with pytest.raises(LedgerError):
        ledger.apply_fill(idem_key="x", asset="ETH", side="BUY", quantity=1, price=1,
                          fee_pct=0, quote_source="t", origin="t")


def test_value_marks_at_bid_and_unknown_quote_is_none(ledger):
    buy(ledger, "ETH", "1", "2000", "b")
    v = ledger.value({"ETH": quote("ETH", 2000, spread_pct=1.0)})
    assert v.values["ETH"] == pytest.approx(1990.0)
    assert ledger.value({}) is None  # held asset with no quote: unknown, never $0


def test_single_writer_lock_and_read_only_reader(tmp_path):
    w = Store(tmp_path / "x.db", writer=True)
    try:
        with pytest.raises(LockError):
            Store(tmp_path / "x.db", writer=True)
        r = Store(tmp_path / "x.db", writer=False)
        with pytest.raises(PermissionError):
            with r.tx():
                pass
        r.close()
    finally:
        w.close()
    Store(tmp_path / "x.db", writer=True).close()  # lock released on close


def test_crash_mid_transaction_leaves_no_partial_fill(ledger):
    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with ledger.store.tx():
            ledger.apply_fill(idem_key="k", asset="ETH", side="BUY", quantity=1, price=100,
                              fee_pct=0.4, quote_source="t", origin="t")
            raise Boom()  # e.g. the process dies before the order is marked filled
    assert ledger.positions() == {} and ledger.lots() == []
    assert ledger.cash() == Decimal("10000.00")
    assert ledger.check() == []
