"""
How often each symbol's live IEX quote is good enough to trade on, per trade day.

What it does: `record` counts, per symbol and trade day, the samples taken in session and
how many passed `check_quote` (two-sided, not crossed, a spread under
`data.max_spread_pct`, fresh, near the last trade and the previous close). `usable` sums
them over a window of days. The engine samples every tracked symbol each minute it checks
quotes, and the latest screen's best-ranked candidates every 15 minutes.

Why: the free feed is IEX's own book, a few percent of US volume. A thinly quoted stock
can show a 4% or a 29% spread there for hours while the national market is a penny wide,
and the desk can only trade on the quote it sees: its orders defer and its stop is blind.
So the screener skips (and demotes) stocks whose IEX quote is rarely usable
(`screen.min_quote_usable_pct`).
"""

from __future__ import annotations

from datetime import date
from typing import Dict, Iterable, Mapping, Tuple

from ottr.ledger.store import Store


def record(store: Store, usable: Mapping[str, bool], day: date) -> None:
    """One sample per symbol: whether its quote was usable."""
    if not usable:
        return
    with store.tx() as c:
        c.executemany(
            "INSERT INTO quote_quality(symbol,day,samples,usable) VALUES(?,?,1,?) ON CONFLICT(symbol,day) "
            "DO UPDATE SET samples=samples+1, usable=usable+excluded.usable",
            [(s, day.isoformat(), int(ok)) for s, ok in sorted(usable.items())])


def usable(store: Store, symbols: Iterable[str], since: date) -> Dict[str, Tuple[int, int]]:
    """symbol -> (samples, usable) from `since` on; a symbol never sampled is absent."""
    want = set(symbols)
    out: Dict[str, Tuple[int, int]] = {}
    for s, n, ok in store.conn.execute(
            "SELECT symbol, SUM(samples), SUM(usable) FROM quote_quality WHERE day>=? GROUP BY symbol",
            (since.isoformat(),)):
        if s in want:
            out[s] = (int(n), int(ok))
    return out


def prune(store: Store, before: date) -> None:
    with store.tx() as c:
        c.execute("DELETE FROM quote_quality WHERE day<?", (before.isoformat(),))
