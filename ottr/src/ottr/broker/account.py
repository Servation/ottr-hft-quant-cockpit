"""
Account model: how much a new BUY may spend, for the account type the desk runs as.

What it does: `buying_power()` starts from ledger cash and holds back what a broker would:
regulatory fees accrued today but not yet charged, and, in a cash account, sale proceeds
that haven't settled yet (T+1, by the exchange calendar's settlement dates).

Why the two types: `margin_1x` is an Alpaca margin account with max_margin_multiplier = 1
and shorting off, the real-life way to get "no leverage" there. Sale proceeds are usable at
once and there is no day-trade limit. `cash` spends only settled funds, which makes a
good-faith violation (buying with unsettled proceeds, then selling before they settle)
impossible by construction. Neither type can borrow, so buying power never exceeds cash.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from ottr.ledger.book import D, Ledger, trade_day
from ottr.market.calendar import TradingCalendar

ZERO = Decimal(0)
# Settlement is T+1, so only the last few days' sales can still be unsettled; looking back
# a week covers a long weekend plus a bank holiday with room to spare.
UNSETTLED_LOOKBACK_DAYS = 7


@dataclass(frozen=True)
class BuyingPower:
    cash: Decimal               # ledger cash
    pending_fees: Decimal       # accrued today, charged at the close
    unsettled: Decimal          # cash account: sale proceeds not settled yet
    available: Decimal          # what a new BUY may spend

    def describe(self) -> str:
        parts = [f"cash {self.cash}"]
        if self.pending_fees:
            parts.append(f"- pending fees {self.pending_fees}")
        if self.unsettled:
            parts.append(f"- unsettled sales {self.unsettled}")
        return " ".join(parts) + f" = {self.available}"


def buying_power(account_type: str, ledger: Ledger, cal: TradingCalendar, now: float) -> BuyingPower:
    cash = ledger.cash()
    pending = ledger.pending_fees()
    unsettled = ZERO
    if account_type == "cash":
        today = trade_day(now)
        since = (today - timedelta(days=UNSETTLED_LOOKBACK_DAYS)).isoformat()
        for day, notional, fee in ledger.store.conn.execute(
                "SELECT day, notional, fee FROM fills WHERE side='SELL' AND day>=?", (since,)):
            if cal.settlement_date(date.fromisoformat(day)) > today:
                unsettled += D(notional) - D(fee)
    elif account_type != "margin_1x":
        raise ValueError(f"unknown account type {account_type!r}")
    available = max(ZERO, cash - pending - unsettled)
    return BuyingPower(cash, pending, unsettled, available)
