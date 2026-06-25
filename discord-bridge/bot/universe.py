"""
Tradeable universe (a.k.a. the watchlist) — the single source of truth.

WHY this exists: the codebase used to conflate two distinct ideas — what we *hold*
(holdings, qty > 0, derived from executed trades) and what we *may trade* (the fixed
set the desk analyzes, scores, and can open/close positions in). Holdings should only
ever reflect current ownership; the tradeable set is configured here.

The universe is exactly the assets that have Kraken-mapped daily OHLC, so every member
gets technical indicators + deterministic signals (price_feed._KRAKEN_PAIR_MAP). Keeping
it in one place lets the price feed, meeting context, risk caps, and the portfolio all
agree by construction, and prevents a "fetched but never reasoned about" asset (or a
"reasoned about but unfetchable" one, like BNB with no Kraken USD pair) from drifting in.
"""

from __future__ import annotations

from typing import List

from bot import settings

# Default if config is missing the key: the 8 Kraken-listed coins with indicators.
_DEFAULT_UNIVERSE: List[str] = ["BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "LINK", "AVAX"]


def tradeable_universe() -> List[str]:
    """The configured tradeable universe (upper-cased tickers), in declared order.

    Reads ``universe`` from settings.yaml, falling back to the 8-coin default. De-dupes
    while preserving order so a stray duplicate in config can't double-count an asset.
    """
    raw = settings.get("universe") or _DEFAULT_UNIVERSE
    seen = set()
    out: List[str] = []
    for sym in raw:
        s = str(sym).upper().strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


# Module-level snapshot for cheap imports; call tradeable_universe() if config may change.
TRADEABLE_UNIVERSE: List[str] = tradeable_universe()
