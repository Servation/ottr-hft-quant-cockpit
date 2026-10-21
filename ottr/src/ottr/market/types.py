"""
Market value types shared by the data layer, risk and the broker.

Quote carries bid/ask (not just a last price) so paper fills cross the spread like a
real taker order would: buys at the ask, sells at the bid. Its `basis` says how current
the price is, because a US desk marks its book at night from the last close, and the
equity curve has to say so. Bar is one daily OHLCV bar keyed by its session date.
AssetClass groups instruments for caps, vol floors and the packet.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Optional

# How current a price is: a live quote, a delayed print (still today), or a session close.
BASIS_LIVE, BASIS_DELAYED, BASIS_CLOSE = "live", "delayed", "close"


@dataclass(frozen=True)
class Quote:
    asset: str
    bid: float
    ask: float
    last: float
    ts: float                      # when we fetched it (epoch seconds)
    source: str                    # "kraken" | "coinbase" | "alpaca"
    high_24h: Optional[float] = None
    low_24h: Optional[float] = None
    basis: str = BASIS_LIVE

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    def age(self, now: Optional[float] = None) -> float:
        return (now if now is not None else time.time()) - self.ts

    def valid(self) -> bool:
        return 0 < self.bid <= self.ask and self.last > 0


class MarketDataError(RuntimeError):
    """No trustworthy market data is available. Callers must not trade or mark on it."""


class AssetClass(str, Enum):
    """What an instrument is, for caps, vol floors and how the packet groups it."""

    EQUITY_ETF = "equity_etf"        # SPY, QQQ, IWM, EFA, EEM, XLE
    BOND_ETF = "bond_etf"            # IEF, TLT, TIP, LQD, HYG
    CASH_ETF = "cash_etf"            # SGOV: the sweep target, cash-like
    COMMODITY_ETF = "commodity_etf"  # GLD, SLV, PDBC
    REAL_ESTATE_ETF = "real_estate_etf"  # VNQ
    STOCK = "stock"                  # screener-picked single names
    CRYPTO = "crypto"                # the current desk, until the cutover


@dataclass(frozen=True)
class Bar:
    """One daily bar for one session. Prices are as traded (unadjusted)."""

    day: date
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self) -> None:
        if not (0 < self.low <= min(self.open, self.close) and max(self.open, self.close) <= self.high):
            raise ValueError(f"{self.day}: inconsistent bar o={self.open} h={self.high} l={self.low} c={self.close}")
        if self.volume < 0:
            raise ValueError(f"{self.day}: negative volume")
