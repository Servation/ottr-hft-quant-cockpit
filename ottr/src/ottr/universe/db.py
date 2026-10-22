"""
The satellites' tables: which stocks the screener picked, every membership change, and
each screen's result.

What it does: reads and writes `satellites`, `universe_events` and `screens` (schema in
`ledger/store.py`). A satellite is active (buyable), sunset (demoted while held: SELL or
HOLD until flat) or removed (gone; its row stays for the re-promotion cooldown and the
history). Writes need the writer store; each runs in the caller's transaction or its own.

Why its own module: membership changes come from the screener and the engine, and are
read by the market service, the API and the CLI; one place keeps the states consistent.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Dict, Iterable, List, Optional

from ottr.ledger.book import trade_day
from ottr.ledger.store import Store, dumps

ACTIVE, SUNSET, REMOVED = "active", "sunset", "removed"
KEEP_SCREENS = 60           # stored screens kept (about a year of weekly runs plus dry runs)


@dataclass(frozen=True)
class Satellite:
    symbol: str
    state: str
    promoted_ts: float
    promoted_day: date
    demoted_ts: Optional[float]
    demoted_day: Optional[date]
    score: Optional[float]
    why: str

    def cooling(self, today: date, days: int) -> bool:
        """Demoted less than `days` ago: not eligible to come back yet."""
        return self.demoted_day is not None and (today - self.demoted_day).days < days


_COLS = "symbol,state,promoted_ts,promoted_day,demoted_ts,demoted_day,score,why"


def _sat(r) -> Satellite:
    return Satellite(r[0], r[1], r[2], date.fromisoformat(r[3]), r[4],
                     date.fromisoformat(r[5]) if r[5] else None, r[6], r[7])


def satellites(store: Store, states: Iterable[str] = (ACTIVE, SUNSET)) -> Dict[str, Satellite]:
    """symbol -> Satellite in any of `states` (by default the members: active and sunset)."""
    want = tuple(states)
    marks = ",".join("?" * len(want))
    return {r[0]: _sat(r) for r in store.conn.execute(
        f"SELECT {_COLS} FROM satellites WHERE state IN ({marks}) ORDER BY symbol", want)}


def member_symbols(store: Store) -> List[str]:
    """Active and sunset satellites: the stocks the market data must track."""
    return sorted(satellites(store))


def _event(store: Store, symbol: str, change: str, why: str, now: float, screen_id: Optional[str]) -> None:
    store.conn.execute("INSERT INTO universe_events(ts,day,symbol,change,why,screen_id) VALUES(?,?,?,?,?,?)",
                       (now, trade_day(now).isoformat(), symbol, change, why[:500], screen_id))


def promote(store: Store, symbol: str, score: Optional[float], why: str, now: float,
            screen_id: Optional[str] = None) -> None:
    with store.tx() as c:
        c.execute("INSERT INTO satellites(symbol,state,promoted_ts,promoted_day,score,why) VALUES(?,?,?,?,?,?) "
                  "ON CONFLICT(symbol) DO UPDATE SET state=excluded.state, promoted_ts=excluded.promoted_ts, "
                  "promoted_day=excluded.promoted_day, demoted_ts=NULL, demoted_day=NULL, score=excluded.score, "
                  "why=excluded.why", (symbol, ACTIVE, now, trade_day(now).isoformat(), score, why[:500]))
        _event(store, symbol, "promoted", why, now, screen_id)


def demote(store: Store, symbol: str, why: str, held: bool, now: float, screen_id: Optional[str] = None) -> str:
    """Demote an active satellite: sunset while it is held, else removed. Returns the state."""
    state = SUNSET if held else REMOVED
    with store.tx() as c:
        c.execute("UPDATE satellites SET state=?, demoted_ts=?, demoted_day=?, why=? WHERE symbol=?",
                  (state, now, trade_day(now).isoformat(), why[:500], symbol))
        _event(store, symbol, "demoted", why, now, screen_id)
    return state


def retire(store: Store, held: Iterable[str], now: float) -> List[str]:
    """Sunset satellites no longer held become removed. Returns their symbols."""
    held = set(held)
    gone = [s for s, sat in satellites(store, (SUNSET,)).items() if s not in held]
    if gone:
        with store.tx() as c:
            for s in gone:
                c.execute("UPDATE satellites SET state=? WHERE symbol=?", (REMOVED, s))
                _event(store, s, "removed", "sold off: no longer held", now, None)
    return gone


def events(store: Store, limit: int = 50) -> List[Dict[str, object]]:
    cols = ("ts", "day", "symbol", "change", "why", "screen_id")
    return [dict(zip(cols, r)) for r in store.conn.execute(
        f"SELECT {','.join(cols)} FROM universe_events ORDER BY id DESC LIMIT ?", (limit,))]


# -- screens ----------------------------------------------------------------------------
def save_screen(store: Store, screen_id: str, kind: str, applied: bool, summary: str, result: dict,
                now: float, keep: int = KEEP_SCREENS) -> None:
    with store.tx() as c:
        c.execute("INSERT INTO screens(id,ts,kind,applied,summary,result) VALUES(?,?,?,?,?,?)",
                  (screen_id, now, kind, int(applied), summary[:1000], dumps(result)))
        c.execute("DELETE FROM screens WHERE id NOT IN (SELECT id FROM screens ORDER BY ts DESC LIMIT ?)", (keep,))


def last_screen(store: Store, applied_only: bool = False) -> Optional[Dict[str, object]]:
    q = "SELECT id,ts,kind,applied,summary,result FROM screens"
    r = store.conn.execute(q + (" WHERE applied=1" if applied_only else "") + " ORDER BY ts DESC LIMIT 1").fetchone()
    if r is None:
        return None
    return {"id": r[0], "ts": r[1], "kind": r[2], "applied": bool(r[3]), "summary": r[4], "result": json.loads(r[5])}


def last_applied_ts(store: Store) -> Optional[float]:
    r = store.conn.execute("SELECT MAX(ts) FROM screens WHERE applied=1").fetchone()
    return r[0] if r else None
