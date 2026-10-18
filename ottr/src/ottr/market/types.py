"""
Market value types shared by the data layer, risk and the broker.

Quote carries bid/ask (not just a last price) so paper fills cross the spread like a
real taker order would: buys at the ask, sells at the bid.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Quote:
    asset: str
    bid: float
    ask: float
    last: float
    ts: float                      # when we fetched it (epoch seconds)
    source: str                    # "kraken" | "coinbase"
    high_24h: Optional[float] = None
    low_24h: Optional[float] = None

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    def age(self, now: Optional[float] = None) -> float:
        return (now if now is not None else time.time()) - self.ts

    def valid(self) -> bool:
        return 0 < self.bid <= self.ask and self.last > 0


class MarketDataError(RuntimeError):
    """No trustworthy market data is available. Callers must not trade or mark on it."""
