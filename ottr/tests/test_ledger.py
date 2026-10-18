"""
Ledger + store tests: exact accounting, atomicity, idempotency and the single-writer lock.
"""

from decimal import Decimal

import pytest

from ottr.ledger.book import Ledger, LedgerError
from ottr.ledger.store import LockError, Store
from tests.conftest import quote


def buy(lg, asset, q, price, key, fee=0.4):
    with lg.store.tx():
        return lg.apply_fill(idem_key=key, asset=asset, side="BUY", quantity=q, price=price,
                             fee_pct=fee, quote_source="kraken", origin="test")


def sell(lg, asset, q, price, key, fee=0.4):
    with lg.store.tx():
        return lg.apply_fill(idem_key=key, asset=asset, side="SELL", quantity=q, price=price,
                             fee_pct=fee, quote_source="kraken", origin="test")


def test_buy_capitalizes_fee_and_sell_realizes_net(ledger):
    buy(ledger, "ETH", "1", "2000", "b1")
    p = ledger.positions()["ETH"]
    assert p.cost == Decimal("2008.00000000")  # 2000 + 0.4% fee
    assert ledger.cash() == Decimal("7992.00000000")
    f = sell(ledger, "ETH", "0.5", "2200", "s1")
    # proceeds 1100, fee 4.40, cost out 1004 -> realized 91.60
    assert f.realized_pnl == Decimal("91.60000000")
    assert ledger.cash() == Decimal("9087.60000000")
    assert ledger.positions()["ETH"].cost == Decimal("1004.00000000")
    assert ledger.check() == []


def test_full_exit_removes_position_and_books_balance(ledger):
    buy(ledger, "SOL", "10", "100", "b")
    sell(ledger, "SOL", "10", "90", "s")
    assert "SOL" not in ledger.positions()
    assert ledger.check() == []
    assert ledger.account()["realized_pnl"] == Decimal("-107.60000000")  # 896.4 - 1004


def test_overspend_and_oversell_roll_back(ledger):
    with pytest.raises(LedgerError):
        buy(ledger, "BTC", "1", "100000", "too-big")
    buy(ledger, "ADA", "100", "1", "b")
    with pytest.raises(LedgerError):
        sell(ledger, "ADA", "101", "1", "too-many")
    assert ledger.cash() == Decimal("9899.60000000")
    assert ledger.store.conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1
    assert ledger.check() == []


def test_same_idempotency_key_cannot_fill_twice(ledger):
    buy(ledger, "ETH", "1", "100", "dup")
    with pytest.raises(Exception):
        buy(ledger, "ETH", "1", "100", "dup")
    assert ledger.positions()["ETH"].qty == Decimal("1.000000000000")
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


def test_import_marks_holdings_at_day0(store):
    lg = Ledger(store)
    lg.initialize(5898.76, note="v1 import", holdings={"ADA": (Decimal("10081.9"), 0.2215)})
    p = lg.positions()["ADA"]
    assert p.cost == Decimal("2233.14085000")  # qty * mark: no invented history
    assert p.high_water == 0.2215
    assert lg.check() == []
    with pytest.raises(LedgerError):
        lg.initialize(1)


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
    assert ledger.positions() == {}
    assert ledger.cash() == Decimal("10000.00000000")
    assert ledger.check() == []
