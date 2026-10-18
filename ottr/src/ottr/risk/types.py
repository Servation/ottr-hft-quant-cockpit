"""
Intent and Verdict: the only way anything asks to trade, and the only answer it gets.

An Intent says WHAT is wanted (asset, side, conviction or fraction) and WHY (origin,
reason, ref); it never carries a dollar size, so no model output can size a trade.
A Verdict is the gate's deterministic answer, including the exact fill quantity/price.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Tuple

PROTECTIVE = ("stop", "trim", "derisk")
# Lower runs first: protective exits always jump ahead of new entries.
PRIORITY = {"stop": 0, "derisk": 1, "trim": 1, "operator": 2, "council_sell": 3, "council_buy": 5}


@dataclass(frozen=True)
class Intent:
    origin: str                 # council | stop | trim | derisk | operator
    asset: str
    side: str                   # BUY | SELL
    idem_key: str
    conviction: float = 0.0     # BUY only, 0..1
    fraction: float = 0.0       # SELL only, (0, 1] of the current position
    reason: str = ""
    ref: str = ""               # meeting id / rule name
    created_ts: float = field(default_factory=time.time)

    @property
    def priority(self) -> int:
        if self.origin == "council":
            return PRIORITY["council_buy" if self.side == "BUY" else "council_sell"]
        return PRIORITY.get(self.origin, 4)

    @property
    def reduces_exposure(self) -> bool:
        return self.side == "SELL"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Verdict:
    approved: bool
    asset: str
    side: str
    reason: str                       # why approved/rejected (machine-readable first word)
    quantity: Decimal = Decimal(0)
    price: float = 0.0                # fill price incl. spread + slippage
    notional: float = 0.0
    quote_source: str = ""
    notes: Tuple[str, ...] = ()

    @classmethod
    def reject(cls, intent: Intent, reason: str, *notes: str) -> "Verdict":
        return cls(False, intent.asset, intent.side, reason, notes=tuple(notes))

    def to_dict(self) -> dict:
        d = asdict(self)
        d["quantity"] = str(self.quantity)
        return d
