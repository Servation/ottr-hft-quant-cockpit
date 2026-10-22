"""
SimBroker: OTTR's own paper fills, modeling what Alpaca's paper account doesn't.

What it does: fills a marketable limit order immediately against the quote it was priced
on: a BUY at the ask, a SELL at the bid, each rounded to a legal tick against us, plus
optional slippage. It refuses an order whose limit no longer crosses the quote (the market
moved) and a quantity that isn't a legal size (fractional only where the asset allows),
and it accrues the regulatory fees the fill owes on its New York trade date.

Why our own simulator: Alpaca's paper fills ignore dividends, splits, regulatory fees and
slippage, so a paper record there flatters the strategy. Filling here, with the same
fee schedule and tick rules a live account faces, keeps the paper book honest.
"""

from __future__ import annotations

from decimal import Decimal

from ottr.broker.base import Execution, Order, round_against_us, whole_or_fractional
from ottr.broker.fees import FeeSchedule, FeeScheduleError
from ottr.ledger.book import trade_day, usd
from ottr.market.types import Quote


class SimBroker:
    name = "sim"

    def __init__(self, fees: FeeSchedule, slippage_pct: float = 0.0) -> None:
        self.fees = fees
        self.slippage = Decimal(str(slippage_pct)) / 100

    def price_for(self, side: str, quote: Quote) -> Decimal:
        """Where a marketable order on `quote` would fill: the touch, plus slippage, on a tick."""
        if side == "BUY":
            return round_against_us(Decimal(str(quote.ask)) * (1 + self.slippage), "BUY")
        return round_against_us(Decimal(str(quote.bid)) * (1 - self.slippage), "SELL")

    def execute(self, order: Order, quote: Quote, now: float, fractionable: bool = True) -> Execution:
        side = order.side.upper()
        if side not in ("BUY", "SELL"):
            return Execution("rejected", reason=f"bad side {side}")
        if not quote.valid():
            return Execution("rejected", reason="no_quote")
        qty = whole_or_fractional(order.qty, fractionable)
        if qty <= 0:
            return Execution("rejected", reason="below_one_share" if not fractionable else "zero_quantity")
        if qty != order.qty:
            return Execution("rejected", reason="illegal_quantity")
        price = self.price_for(side, quote)
        crosses = price <= order.limit_price if side == "BUY" else price >= order.limit_price
        if not crosses:
            # The quote moved past our limit between pricing and filling: no fill, retry later.
            return Execution("rejected", reason="limit_not_marketable")
        try:
            accrued = self.fees.accrue(side, qty, usd(qty * price), trade_day(now))
        except FeeScheduleError as e:
            return Execution("rejected", reason=f"no_fee_rate: {e}")
        return Execution("filled", qty=qty, price=price, accrued=accrued, venue=self.name)
