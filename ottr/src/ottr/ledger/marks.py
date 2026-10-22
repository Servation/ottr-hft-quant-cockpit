"""
Marks: the price each holding is valued at, and how current that price is. Pure.

What it does: `mark` picks one price per symbol by the session:
- Session open: the live IEX quote (bid and ask, already sanity-checked); else the newest
  delayed SIP minute close from this session, labeled `delayed`; else unknown.
- Outside the session: the official close of the last completed session, labeled `close`.
  Until that close is final (the free plan's 16-minute SIP delay after the bell), the
  newest delayed minute close from that same session stands in.
Anything else is unknown (None), never an older price passed off as current.

Why: a US book is marked at night and on weekends from the last close, and the equity
curve has to say which kind of number it is. The delayed fallback keeps one thin ETF
(PDBC, TIP) with a wide IEX quote from making the whole book unmarkable mid-session.
Marks only value the book: an order still needs a live quote (the gate checks that).
"""

from __future__ import annotations

from typing import Optional, Sequence, Tuple

from ottr.market.calendar import MarketTime
from ottr.market.types import BASIS_CLOSE, BASIS_DELAYED, Bar, Quote

SIP_FINAL_AFTER_SEC = 16 * 60   # a session's daily bar is final this long after its close


def _flat(symbol: str, price: float, ts: float, basis: str) -> Quote:
    return Quote(asset=symbol, bid=price, ask=price, last=price, ts=ts, source="alpaca", basis=basis)


def mark(symbol: str, mt: MarketTime, live: Optional[Quote], delayed: Optional[Tuple[float, float]],
         bars: Sequence[Bar]) -> Optional[Quote]:
    """`live`: a quote that passed `check_quote` (or None). `delayed`: (bar end time, close)
    of the newest SIP minute bar. `bars`: raw daily bars, oldest first."""
    if mt.is_open:
        if live is not None:
            return live
        s = mt.session
        if delayed and s.open_ts < delayed[0] <= s.close_ts:
            return _flat(symbol, delayed[1], delayed[0], BASIS_DELAYED)
        return None
    last = mt.last_close
    if last is None:
        return None
    bar = next((b for b in reversed(bars) if b.day == last.day), None)
    if bar is not None and mt.ts >= last.close_ts + SIP_FINAL_AFTER_SEC:
        return _flat(symbol, bar.close, last.close_ts, BASIS_CLOSE)
    if delayed and last.open_ts < delayed[0] <= last.close_ts:
        return _flat(symbol, delayed[1], delayed[0], BASIS_DELAYED)
    return None
