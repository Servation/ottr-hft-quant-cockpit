"""
Fee tests: the effective-dated schedule, per-fill accrual, the daily round-up charge in the
ledger (one charge per trading day, rounded up), check() on fees, and the shipped rates.
"""

from datetime import date, datetime
from decimal import Decimal

import pytest

from ottr import config as cfgmod
from ottr.broker.fees import FeeRate, FeeSchedule, FeeScheduleError, charge_for_day, from_rows
from ottr.config import ConfigError, load
from ottr.ledger.book import LedgerError
from ottr.market.calendar import ET

D = Decimal


def at(y, m, d, h=12):
    return datetime(y, m, d, h, tzinfo=ET).timestamp()


@pytest.fixture(scope="module")
def shipped():
    return load().fees


# -- schedule -----------------------------------------------------------------------------

def test_rates_apply_from_their_trade_date_until_the_next(shipped):
    assert shipped.rate_on("taf", date(2026, 9, 30)).rate == D("0.000195")
    assert shipped.rate_on("taf", date(2026, 10, 1)).rate == 0          # Q4 2026 pause
    assert shipped.rate_on("taf", date(2026, 12, 31)).rate == 0
    assert shipped.rate_on("taf", date(2027, 1, 1)).rate == D("0.000195")
    assert shipped.rate_on("sec", date(2026, 10, 21)).rate == D("0.0000206")


def test_a_date_before_any_rate_is_an_error_not_a_guess(shipped):
    with pytest.raises(FeeScheduleError, match="no sec rate on file"):
        shipped.rate_on("sec", date(2026, 4, 1))
    with pytest.raises(FeeScheduleError, match="no fee called"):
        shipped.rate_on("orf", date(2026, 10, 21))


def test_sells_pay_sec_taf_and_cat_buys_pay_only_cat(shipped):
    day = date(2027, 1, 4)
    sell = shipped.accrue("SELL", 100, D("50000"), day)
    assert sell == {"sec": D("1.03"), "taf": D("0.0195"), "cat": D("0.0003")}
    assert shipped.accrue("buy", 10, D("5000"), day) == {"cat": D("0.00003")}


def test_a_paused_fee_is_recorded_as_zero_not_dropped(shipped):
    assert shipped.accrue("SELL", 100, D("50000"), date(2026, 10, 21))["taf"] == 0


def test_taf_is_capped_per_trade(shipped):
    big = shipped.accrue("SELL", 100_000, D("1000000"), date(2027, 1, 4))
    assert big["taf"] == D("9.79")                  # 100k x 0.000195 = 19.50, capped


@pytest.mark.parametrize("kw,needle", [
    (dict(basis="weight"), "basis"),
    (dict(rate=D("-1")), "negative"),
    (dict(sides=("HOLD",)), "sides"),
])
def test_bad_rates_are_rejected(kw, needle):
    base = dict(fee="sec", effective=date(2026, 1, 1), basis="notional", rate=D("0.00002"), sides=("SELL",))
    with pytest.raises(FeeScheduleError, match=needle):
        FeeRate(**{**base, **kw})


def test_two_rates_on_one_day_are_ambiguous():
    r = FeeRate("cat", date(2026, 1, 1), "share", D("0.000003"), ("BUY",))
    with pytest.raises(FeeScheduleError, match="same day"):
        FeeSchedule([r, r])


def test_rows_with_unknown_or_missing_keys_are_rejected():
    with pytest.raises(FeeScheduleError, match="unknown keys"):
        from_rows([{"fee": "sec", "effective": "2026-01-01", "basis": "notional", "rate": 1,
                    "sides": ["SELL"], "per": "x"}])
    with pytest.raises(FeeScheduleError, match="missing 'rate'"):
        from_rows([{"fee": "sec", "effective": "2026-01-01", "basis": "notional", "sides": ["SELL"]}])


# -- rounding -------------------------------------------------------------------------------

@pytest.mark.parametrize("amounts,charged", [
    ([D("0.00083")], D("0.01")),                    # Alpaca's own example
    ([D("1.03"), D("0.0003")], D("1.04")),          # up, never to the nearest
    ([D("2.00")], D("2.00")),
    ([D("0")], D("0.00")),
    ([], D("0.00")),
])
def test_a_days_total_is_rounded_up_to_the_cent(amounts, charged):
    assert charge_for_day(amounts) == charged


# -- ledger: accrue per fill, charge per day --------------------------------------------------

def trade(lg, side, q, px, key, when, accrued):
    with lg.store.tx():
        return lg.apply_fill(idem_key=key, asset="SPY", side=side, quantity=q, price=px,
                             quote_source="test", origin="test", accrued=accrued, now=when)


def test_fills_accrue_without_touching_cash_until_the_close(ledger, shipped):
    day = date(2026, 10, 21)
    f = trade(ledger, "BUY", "10", "500", "b", at(2026, 10, 21, 10), shipped.accrue("BUY", 10, D("5000"), day))
    assert f.accrued == {"cat": D("0.00003")}
    assert ledger.cash() == D("5000.00")                 # the fee isn't charged yet
    assert ledger.pending_fees() == D("0.01")            # but buying power must hold it back
    assert ledger.charge_fees(day, now=at(2026, 10, 21, 16)) == [(day, D("0.01"))]
    assert ledger.cash() == D("4999.99") and ledger.account()["fees"] == D("0.01")
    assert ledger.pending_fees() == 0
    assert ledger.charge_fees(day) == []                 # idempotent: nothing left to charge
    assert ledger.check() == []


def test_each_day_is_rounded_separately_when_catching_up(ledger):
    # Two days of tiny fees, charged together after a missed close: 0.01 + 0.01, not 0.01.
    trade(ledger, "BUY", "1", "10", "b1", at(2026, 10, 20), {"cat": D("0.004")})
    trade(ledger, "BUY", "1", "10", "b2", at(2026, 10, 21), {"cat": D("0.004")})
    charged = ledger.charge_fees(date(2026, 10, 21))
    assert charged == [(date(2026, 10, 20), D("0.01")), (date(2026, 10, 21), D("0.01"))]
    assert ledger.account()["fees"] == D("0.02")
    assert ledger.check() == []


def test_charging_stops_at_the_given_day(ledger):
    trade(ledger, "BUY", "1", "10", "b1", at(2026, 10, 20), {"cat": D("0.004")})
    trade(ledger, "BUY", "1", "10", "b2", at(2026, 10, 21), {"cat": D("0.004")})
    assert [d for d, _ in ledger.charge_fees(date(2026, 10, 20))] == [date(2026, 10, 20)]
    assert ledger.pending_fees() == D("0.01")


def test_a_zero_fee_day_is_marked_charged_without_a_cash_event(ledger):
    trade(ledger, "BUY", "1", "10", "b", at(2026, 10, 21), {"taf": D("0")})
    assert ledger.charge_fees(date(2026, 10, 21)) == [(date(2026, 10, 21), D("0.00"))]
    assert ledger.store.conn.execute("SELECT COUNT(*) FROM cash_events WHERE kind='fee'").fetchone()[0] == 0
    assert ledger.pending_fees() == 0 and ledger.check() == []


def test_regulatory_fees_stay_out_of_lot_basis_and_proceeds(ledger):
    trade(ledger, "BUY", "1", "100", "b", at(2026, 10, 20), {"cat": D("0.000003")})
    f = trade(ledger, "SELL", "1", "110", "s", at(2026, 10, 21), {"sec": D("0.002266"), "cat": D("0.000003")})
    assert ledger.lots(open_only=False)[0].cost == D("100.00")
    assert f.realized_pnl == D("10.00")                  # gross of the regulatory fees
    ledger.charge_fees(date(2026, 10, 21))
    assert ledger.cash() == D("10009.98")                # 10000 + 10 - 0.01 - 0.01
    assert ledger.check() == []


def test_negative_accruals_are_refused(ledger):
    with pytest.raises(LedgerError, match="negative"):
        trade(ledger, "BUY", "1", "10", "b", at(2026, 10, 21), {"cat": D("-0.01")})


def test_a_charge_larger_than_cash_is_refused_and_rolled_back(ledger):
    trade(ledger, "BUY", "1000", "10", "b", at(2026, 10, 21), {"cat": D("0.003")})  # all the cash
    with pytest.raises(LedgerError, match="exceed cash"):
        ledger.charge_fees(date(2026, 10, 21))
    assert ledger.pending_fees() == D("0.01") and ledger.check() == []


@pytest.mark.parametrize("sql,needle", [
    ("UPDATE cash_events SET amount='-0.05' WHERE kind='fee'", "round up to"),
    ("UPDATE fee_accruals SET day='2026-10-19'", "its fill's day"),
    ("UPDATE fee_accruals SET charge_event_id=NULL", "marked charged without a charge"),
    ("UPDATE account SET fees='0.00'", "fees"),
])
def test_check_catches_fee_tampering(ledger, sql, needle):
    trade(ledger, "BUY", "1", "10", "b", at(2026, 10, 21), {"cat": D("0.004")})
    ledger.charge_fees(date(2026, 10, 21))
    with ledger.store.tx() as c:
        c.execute(sql)
    assert any(needle in p for p in ledger.check()), ledger.check()


# -- config ---------------------------------------------------------------------------------

def test_a_required_fee_missing_from_config_is_rejected(tmp_path):
    text = cfgmod.DEFAULT_CONFIG.read_text(encoding="utf-8")
    start = text.index('[[fees.rate]]\nfee = "cat"')
    end = text.index("[risk]")
    p = tmp_path / "c.toml"
    p.write_text(text[:start] + text[end:], encoding="utf-8")
    with pytest.raises(ConfigError, match=r"no \[\[fees.rate\]\] for \['cat'\]"):
        load(p, data_dir=tmp_path)


def test_a_bad_fee_row_is_a_config_error(tmp_path):
    text = cfgmod.DEFAULT_CONFIG.read_text(encoding="utf-8").replace('basis = "notional"', 'basis = "weight"', 1)
    p = tmp_path / "c.toml"
    p.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match="fees: .*basis"):
        load(p, data_dir=tmp_path)
