"""
Pass-through regulatory fees for US equities, effective-dated, accrued per fill.

What it does: a FeeSchedule holds every rate with the first trade date it applies to,
read from [fees] in the US config. `accrue()` returns the exact (unrounded) fees one fill
owes on its trade date. `charge_for_day()` turns a day's accrued total into what the
broker debits: the total rounded UP to the cent.

Why this shape: the rates change on dates set by regulators, sometimes mid-year and
sometimes for a few months only (FINRA paused its TAF for Q4 2026), so a fill must be
priced with the rate in force on ITS trade date, and a date with no rate on file is an
error rather than a guess. Alpaca accrues these fees through the day and charges the
day's total once at the close, rounded up, so a paper book that charged per fill (or
rounded half-even) would drift from what a real account would show.

Sources (checked 2026-10-21):
- SEC Section 31: $20.60 per $1M of sales from April 2026 (SEC fee rate advisory, FY2026;
  Nasdaq applies it to equity trades from trade date 2026-04-02).
- FINRA TAF: $0.000195 per share sold, max $9.79 per trade; paused to $0 for trades from
  2026-10-01 through 2026-12-31, resuming 2027-01-01 (FINRA SR-FINRA-2026-021).
- CAT: $0.000003 per executed equivalent share, buys and sells; one NMS share is one
  executed equivalent share (Alpaca brokerage fee schedule, Oct 12 2026).
- Alpaca: fees accrue intraday; "at EOD, we charge each account the fees for that trading
  day", with the total rounded up to the cent (docs.alpaca.markets, Regulatory Fees).
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_CEILING, Decimal
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

CENT = Decimal("0.01")
SIDES = ("BUY", "SELL")
BASES = ("notional", "share")


class FeeScheduleError(ValueError):
    """No rate is on file for a fee on the date asked, or the schedule itself is invalid."""


@dataclass(frozen=True)
class FeeRate:
    fee: str                         # "sec", "taf", "cat", ...
    effective: date                  # first trade date this rate applies to
    basis: str                       # "notional": rate per $ of trade value; "share": per share
    rate: Decimal
    sides: Tuple[str, ...]           # which sides pay it
    max_per_trade: Optional[Decimal] = None
    source: str = ""

    def __post_init__(self) -> None:
        if self.basis not in BASES:
            raise FeeScheduleError(f"{self.fee} {self.effective}: basis must be one of {BASES}")
        if self.rate < 0 or (self.max_per_trade is not None and self.max_per_trade < 0):
            raise FeeScheduleError(f"{self.fee} {self.effective}: rate and max_per_trade can't be negative")
        if not self.sides or set(self.sides) - set(SIDES):
            raise FeeScheduleError(f"{self.fee} {self.effective}: sides must be from {SIDES}")


class FeeSchedule:
    def __init__(self, rates: Iterable[FeeRate]) -> None:
        by_fee: Dict[str, List[FeeRate]] = {}
        for r in rates:
            by_fee.setdefault(r.fee, []).append(r)
        for fee, rs in by_fee.items():
            rs.sort(key=lambda r: r.effective)
            days = [r.effective for r in rs]
            if len(set(days)) != len(days):
                raise FeeScheduleError(f"{fee}: two rates start on the same day")
        if not by_fee:
            raise FeeScheduleError("the fee schedule is empty")
        self._rates = by_fee

    @property
    def fees(self) -> Tuple[str, ...]:
        return tuple(sorted(self._rates))

    def rate_on(self, fee: str, day: date) -> FeeRate:
        rs = self._rates.get(fee)
        if not rs:
            raise FeeScheduleError(f"no fee called {fee!r}")
        i = bisect.bisect_right([r.effective for r in rs], day) - 1
        if i < 0:
            raise FeeScheduleError(f"no {fee} rate on file for {day} (earliest is {rs[0].effective})")
        return rs[i]

    def accrue(self, side: str, quantity, notional, day: date) -> Dict[str, Decimal]:
        """Exact fees one fill owes, by fee name. Fees that don't apply to `side` are left
        out; a fee whose current rate is zero is kept (as 0) so the record shows it was
        checked, not forgotten."""
        side = side.upper()
        if side not in SIDES:
            raise FeeScheduleError(f"bad side {side!r}")
        q, n = Decimal(str(quantity)), Decimal(str(notional))
        out: Dict[str, Decimal] = {}
        for fee in self.fees:
            r = self.rate_on(fee, day)
            if side not in r.sides:
                continue
            amount = r.rate * (n if r.basis == "notional" else q)
            if r.max_per_trade is not None:
                amount = min(amount, r.max_per_trade)
            out[fee] = amount
        return out


def charge_for_day(accrued: Iterable[Decimal]) -> Decimal:
    """What the broker debits for a day's accruals: their total rounded UP to the cent."""
    total = sum((Decimal(str(a)) for a in accrued), Decimal(0))
    if total < 0:
        raise FeeScheduleError("accrued fees can't be negative")
    return total.quantize(CENT, rounding=ROUND_CEILING)


def from_rows(rows: Iterable[Mapping]) -> FeeSchedule:
    """A schedule from config rows: {fee, effective, basis, rate, sides, max_per_trade?, source?}."""
    rates = []
    for i, r in enumerate(rows):
        unknown = set(r) - {"fee", "effective", "basis", "rate", "sides", "max_per_trade", "source"}
        if unknown:
            raise FeeScheduleError(f"fees.rate[{i}]: unknown keys {sorted(unknown)}")
        try:
            eff = r["effective"]
            if not isinstance(eff, date):
                eff = date.fromisoformat(str(eff))
            rates.append(FeeRate(
                fee=str(r["fee"]), effective=eff, basis=str(r["basis"]),
                rate=Decimal(str(r["rate"])),
                sides=tuple(str(s).upper() for s in r["sides"]),
                max_per_trade=Decimal(str(r["max_per_trade"])) if r.get("max_per_trade") is not None else None,
                source=str(r.get("source", ""))))
        except KeyError as e:
            raise FeeScheduleError(f"fees.rate[{i}]: missing {e}")
        except ValueError as e:
            raise FeeScheduleError(f"fees.rate[{i}]: {e}")
    return FeeSchedule(rates)
