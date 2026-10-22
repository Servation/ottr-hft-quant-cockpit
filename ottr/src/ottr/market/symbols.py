"""
US ticker symbols: one canonical spelling, and conversions to each provider's.

What it does: `normalize` turns any common spelling of a ticker into OTTR's canonical form
(upper case, share class after a dot: "brk-b", "BRK/B" and "BRK B" all become "BRK.B") and
rejects anything that isn't a plain US listing. `to_alpaca` and `to_sec` spell it the way
each source expects.

Why: share classes are written differently everywhere (Alpaca "BRK.B", SEC "BRK-B", some
feeds "BRK/B"). If two spellings of one stock reached the ledger they would be two
positions, with two cost bases and two concentration checks. Every symbol is normalized at
the edge, once, so the rest of the system compares plain strings.
"""

from __future__ import annotations

import re

# 1-5 letters, optionally a share class of 1-2 letters (BRK.B, BF.B, GOOG has none).
_CANONICAL = re.compile(r"^[A-Z]{1,5}(\.[A-Z]{1,2})?$")
_SEPARATORS = re.compile(r"[-/ _]")


def normalize(raw: str) -> str:
    """Canonical form of a US ticker, or ValueError if it can't be one."""
    if not isinstance(raw, str):
        raise ValueError(f"not a ticker: {raw!r}")
    s = _SEPARATORS.sub(".", raw.strip().upper())
    if not _CANONICAL.match(s):
        raise ValueError(f"not a US ticker: {raw!r}")
    return s


def is_valid(raw: str) -> bool:
    try:
        normalize(raw)
        return True
    except ValueError:
        return False


def to_alpaca(symbol: str) -> str:
    """Alpaca spells share classes with a dot, like the canonical form."""
    return normalize(symbol)


def to_sec(symbol: str) -> str:
    """The SEC's company_tickers files spell share classes with a dash (BRK-B)."""
    return normalize(symbol).replace(".", "-")
