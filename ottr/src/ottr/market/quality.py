"""
How often each symbol's live IEX quote is good enough to trade on, per trade day.

What it does: `record` counts, per symbol and trade day, the samples taken in session and
how many passed `check_quote` (two-sided, not crossed, a spread under
`data.max_spread_pct`, fresh, near the last trade and the previous close). `usable` sums
them over a range of days; `window` is the range the rules read (the last
`screen.quote_sessions` completed sessions) and `unreliable` names the symbols measured
over it whose quote was usable too rarely. The engine samples every tracked symbol each
minute it checks quotes, and the latest screen's best-ranked candidates every 15 minutes.

Why: the free feed is IEX's own book, a few percent of US volume. A thinly quoted stock
can show a 4% or a 29% spread there for hours while the national market is a penny wide,
and the desk can only trade on the quote it sees: its orders defer and its stop is blind.
So the screener skips (and demotes) stocks whose IEX quote is rarely usable
(`screen.min_quote_usable_pct`), and between screens the gate refuses to BUY a satellite
that has fallen under it (a liquidity provider can leave IEX mid-week: RPRX went from
51,000 IEX quote updates a day in October to 4,300 on 2026-11-13, and the council added
to it that morning with its week at 72.6%).

Why completed sessions only: a window that also counted today's partial session would
change from minute to minute, and a morning's few samples would weigh like a full day.
"""

from __future__ import annotations

from datetime import date
from typing import Dict, Iterable, Mapping, Optional, Tuple

from ottr.ledger.book import trade_day
from ottr.ledger.store import Store
from ottr.market.calendar import TradingCalendar


def record(store: Store, usable: Mapping[str, bool], day: date) -> None:
    """One sample per symbol: whether its quote was usable."""
    if not usable:
        return
    with store.tx() as c:
        c.executemany(
            "INSERT INTO quote_quality(symbol,day,samples,usable) VALUES(?,?,1,?) ON CONFLICT(symbol,day) "
            "DO UPDATE SET samples=samples+1, usable=usable+excluded.usable",
            [(s, day.isoformat(), int(ok)) for s, ok in sorted(usable.items())])


def usable(store: Store, symbols: Iterable[str], since: date,
           until: Optional[date] = None) -> Dict[str, Tuple[int, int]]:
    """symbol -> (samples, usable) from `since` through `until` (no end when None); a
    symbol never sampled is absent."""
    want = set(symbols)
    out: Dict[str, Tuple[int, int]] = {}
    for s, n, ok in store.conn.execute(
            "SELECT symbol, SUM(samples), SUM(usable) FROM quote_quality WHERE day>=? AND day<=? GROUP BY symbol",
            (since.isoformat(), (until or date.max).isoformat())):
        if s in want:
            out[s] = (int(n), int(ok))
    return out


def window(cal: TradingCalendar, now: float, sessions: int) -> Tuple[date, Optional[date]]:
    """The first and last day of the last `sessions` completed sessions; (today, None)
    when the calendar holds none yet."""
    done = cal.completed_sessions(now, sessions)
    return (done[0].day, done[-1].day) if done else (trade_day(now), None)


def too_rare(n: int, ok: int, since: date, sc) -> Optional[str]:
    """Why a quote seen usable `ok` times in `n` samples since `since` can't be relied on,
    or None: fine, or measured too little to say (`screen.min_quote_samples`)."""
    if n < sc.min_quote_samples or ok / n * 100 >= sc.min_quote_usable_pct:
        return None
    return (f"IEX quote usable only {ok / n * 100:.0f}% of the time since {since} (needs "
            f"{sc.min_quote_usable_pct:g}%)")


def unreliable(store: Store, cal: TradingCalendar, symbols: Iterable[str], now: float, sc) -> Dict[str, str]:
    """symbol -> why its IEX quote can't be relied on, over the last `sc.quote_sessions`
    completed sessions. A symbol measured too little there is absent: a member is sampled
    every minute in session, so that only follows days the engine was down, and the
    order's own quote check still guards every fill."""
    since, until = window(cal, now, sc.quote_sessions)
    return {s: why for s, (n, ok) in sorted(usable(store, symbols, since, until).items())
            if (why := too_rare(n, ok, since, sc))}


def prune(store: Store, before: date) -> None:
    with store.tx() as c:
        c.execute("DELETE FROM quote_quality WHERE day<?", (before.isoformat(),))
