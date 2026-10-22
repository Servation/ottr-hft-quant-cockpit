"""
Market history in the book's database: the exchange calendar and daily bars.

What it does: saves and loads the trading calendar (sessions, early closes, settlement
dates, and the range of days the rows speak for), raw daily bars (as traded: the official
closes the book is marked to overnight) and adjusted closes (splits and dividends folded
in: a total-return series for indicators and benchmarks).

Why stored: the engine restarts, and `ottr status` runs read-only, without waiting on
Alpaca; a day of Alpaca trouble leaves the last calendar and closes in place. Raw bars
only ever gain rows (an archive). Adjusted closes are replaced whole per symbol, because
every new dividend rewrites the adjusted history before it.
"""

from __future__ import annotations

from datetime import date
from typing import Dict, Iterable, List, Optional, Tuple

from ottr.ledger.store import Store
from ottr.market.calendar import Session, TradingCalendar
from ottr.market.types import Bar

CALENDAR_RANGE = "calendar_range"


# -- calendar ------------------------------------------------------------------------------
def save_calendar(store: Store, cal: TradingCalendar) -> None:
    """Store `cal` for the days it covers. A range that overlaps (or touches) the stored one
    extends it; one that doesn't replaces it, so the stored range never has a hole."""
    first, last = cal.first_day, cal.last_day
    old = _range(store)
    if old and old[0] <= last and first <= old[1]:
        first, last = min(first, old[0]), max(last, old[1])
        wipe = (cal.first_day.isoformat(), cal.last_day.isoformat())
    else:
        wipe = ("0000-00-00", "9999-99-99")
    with store.tx() as c:
        c.execute("DELETE FROM calendar WHERE day BETWEEN ? AND ?", wipe)
        c.executemany("INSERT INTO calendar(day,open,close,settles) VALUES(?,?,?,?)",
                      [(s.day.isoformat(), s.open.strftime("%H:%M"), s.close.strftime("%H:%M"),
                        s.settles.isoformat() if s.settles else None) for s in cal.sessions])
        c.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (CALENDAR_RANGE, f"{first.isoformat()},{last.isoformat()}"))


def _range(store: Store) -> Optional[Tuple[date, date]]:
    raw = store.meta(CALENDAR_RANGE)
    if not raw:
        return None
    a, b = raw.split(",")
    return date.fromisoformat(a), date.fromisoformat(b)


def load_calendar(store: Store) -> Optional[TradingCalendar]:
    """The stored calendar, or None if there is none yet."""
    rng = _range(store)
    rows = store.conn.execute("SELECT day,open,close,settles FROM calendar ORDER BY day").fetchall()
    if rng is None or not rows:
        return None
    sessions = [Session.at(date.fromisoformat(d), o, c, date.fromisoformat(s) if s else None) for d, o, c, s in rows]
    return TradingCalendar(sessions, first_day=rng[0], last_day=rng[1])


# -- bars ----------------------------------------------------------------------------------
def save_bars(store: Store, bars: Dict[str, Iterable[Bar]]) -> int:
    """Upsert raw bars (a corrected bar replaces the old one). Returns rows written."""
    rows = [(sym, b.day.isoformat(), b.open, b.high, b.low, b.close, b.volume)
            for sym, bs in bars.items() for b in bs]
    with store.tx() as c:
        c.executemany("INSERT INTO bars(symbol,day,open,high,low,close,volume) VALUES(?,?,?,?,?,?,?) "
                      "ON CONFLICT(symbol,day) DO UPDATE SET open=excluded.open, high=excluded.high, "
                      "low=excluded.low, close=excluded.close, volume=excluded.volume", rows)
    return len(rows)


def raw_bars(store: Store, symbol: str, since: Optional[date] = None) -> List[Bar]:
    rows = store.conn.execute("SELECT day,open,high,low,close,volume FROM bars WHERE symbol=? AND day>=? ORDER BY day",
                              (symbol, (since or date.min).isoformat()))
    return [Bar(date.fromisoformat(d), o, h, lo, c, v) for d, o, h, lo, c, v in rows]


def replace_adjusted(store: Store, adjusted: Dict[str, Iterable[Bar]]) -> int:
    """Replace each symbol's adjusted closes with this refresh's. A symbol that came back
    empty keeps its old rows: an outage must not erase history."""
    n = 0
    with store.tx() as c:
        for sym, bs in adjusted.items():
            rows = [(sym, b.day.isoformat(), b.close) for b in bs]
            if not rows:
                continue
            c.execute("DELETE FROM adj_closes WHERE symbol=?", (sym,))
            c.executemany("INSERT INTO adj_closes(symbol,day,close) VALUES(?,?,?)", rows)
            n += len(rows)
    return n


def adjusted_closes(store: Store, symbol: str) -> List[Tuple[date, float]]:
    return [(date.fromisoformat(d), c) for d, c in store.conn.execute(
        "SELECT day,close FROM adj_closes WHERE symbol=? ORDER BY day", (symbol,))]
