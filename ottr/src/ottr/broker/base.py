"""
The broker seam: the one interface between "the gate approved an order" and "it filled".

What it does: an Order is a marketable limit order (side, quantity, limit price, the
idempotency key that doubles as the broker's client order id). A Broker decides what
happened to it and returns an Execution: filled (quantity, price, the fees the fill
accrued), accepted (a live broker will report the fill later), or rejected with a reason.
Tick helpers round prices to a legal increment, against us.

Why a seam: today the SimBroker fills synchronously inside the executor's transaction.
Later an Alpaca adapter can implement the same interface and return "accepted", with the
fill booked through the same executor path when Alpaca reports it, keyed by the same
client order id. Nothing above this interface has to change for that.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Mapping, Protocol

from ottr.market.types import Quote

PENNY = Decimal("0.01")
SUBPENNY = Decimal("0.0001")


def tick(price: Decimal) -> Decimal:
    """The minimum price increment (Reg NMS Rule 612): $0.01 at $1.00 and up, $0.0001 below."""
    return PENNY if price >= 1 else SUBPENNY


def round_against_us(price, side: str) -> Decimal:
    """A fill price on a legal tick, rounded the costly way: buys up, sells down."""
    p = Decimal(str(price))
    step = tick(p)
    mode = ROUND_CEILING if side.upper() == "BUY" else ROUND_FLOOR
    return (p / step).to_integral_value(rounding=mode) * step


@dataclass(frozen=True)
class Order:
    idem_key: str                 # also the broker's client order id
    asset: str
    side: str                     # BUY | SELL
    qty: Decimal
    limit_price: Decimal          # marketable: at or through the touch
    origin: str


@dataclass(frozen=True)
class Execution:
    status: str                   # filled | accepted | rejected
    qty: Decimal = Decimal(0)
    price: Decimal = Decimal(0)
    accrued: Mapping[str, Decimal] = field(default_factory=dict)
    reason: str = ""
    venue: str = ""

    @property
    def filled(self) -> bool:
        return self.status == "filled"


class Broker(Protocol):
    name: str

    def execute(self, order: Order, quote: Quote, now: float,
                fractionable: bool = True) -> Execution:
        """Decide the order against `quote` at `now`. Must not touch the ledger."""


def whole_or_fractional(qty: Decimal, fractionable: bool) -> Decimal:
    """Fractional shares to 1e-9 where the asset allows them, whole shares otherwise."""
    q = qty.quantize(Decimal("0.000000001"), rounding=ROUND_FLOOR)
    return q if fractionable else q.to_integral_value(rounding=ROUND_FLOOR)
