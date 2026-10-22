"""
The events feeds' tables: writes for the refresh jobs, reads for the gate and the packet.

What it does: one function per question or update, over the tables in `ledger/store.py`
(feeds, macro_obs, earnings, corporate_actions, corporate_action_checks, sec_*). Writes
need the writer store and each runs in one transaction, so a refresh lands whole or not
at all; reads work on a read-only store too.

Why its own module: the Store keeps the ledger's core helpers; these tables belong to the
events package and change with it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from ottr.events.corporate import CorporateAction
from ottr.events.earnings import EarningsDate
from ottr.events.fred import Observation
from ottr.events.sec import Company, Listing
from ottr.ledger.store import Store


# -- feed status -------------------------------------------------------------------------
@dataclass(frozen=True)
class FeedStatus:
    name: str
    ts: float                 # last attempt
    ok: bool                  # did the last attempt succeed
    detail: str
    ok_ts: Optional[float]    # last success (None: never)
    rows: Optional[int]


def record_feed(store: Store, name: str, ok: bool, detail: str, now: float, rows: Optional[int] = None) -> None:
    with store.tx() as c:
        c.execute("INSERT INTO feeds(name,ts,ok,detail,ok_ts,rows) VALUES(?,?,?,?,?,?) "
                  "ON CONFLICT(name) DO UPDATE SET ts=excluded.ts, ok=excluded.ok, detail=excluded.detail, "
                  "ok_ts=COALESCE(excluded.ok_ts, feeds.ok_ts), rows=COALESCE(excluded.rows, feeds.rows)",
                  (name, now, int(ok), detail[:500], now if ok else None, rows))


def feed(store: Store, name: str) -> Optional[FeedStatus]:
    r = store.conn.execute("SELECT name,ts,ok,detail,ok_ts,rows FROM feeds WHERE name=?", (name,)).fetchone()
    return FeedStatus(r[0], r[1], bool(r[2]), r[3] or "", r[4], r[5]) if r else None


def feeds(store: Store) -> Dict[str, FeedStatus]:
    return {r[0]: FeedStatus(r[0], r[1], bool(r[2]), r[3] or "", r[4], r[5])
            for r in store.conn.execute("SELECT name,ts,ok,detail,ok_ts,rows FROM feeds")}


# -- macro -------------------------------------------------------------------------------
def save_macro(store: Store, series: str, obs: Sequence[Observation], start: date) -> None:
    """Replace the series from `start` on with `obs` (FRED revises; a merge would keep a
    value FRED has since withdrawn)."""
    with store.tx() as c:
        c.execute("DELETE FROM macro_obs WHERE series=? AND day>=?", (series, start.isoformat()))
        c.executemany("INSERT INTO macro_obs(series,day,value) VALUES(?,?,?)",
                      [(series, o.day.isoformat(), o.value) for o in obs])


def macro_series(store: Store, series: str, since: Optional[date] = None) -> List[Observation]:
    rows = store.conn.execute("SELECT day,value FROM macro_obs WHERE series=? AND day>=? ORDER BY day",
                              (series, (since or date.min).isoformat()))
    return [Observation(date.fromisoformat(d), v) for d, v in rows]


# -- earnings ----------------------------------------------------------------------------
def save_earnings(store: Store, dates: Iterable[EarningsDate], today: date, now: float) -> int:
    """Replace every report date from `today` on with this snapshot. Returns rows stored."""
    rows = {(d.symbol, d.day): d for d in dates if d.day >= today}
    with store.tx() as c:
        c.execute("DELETE FROM earnings WHERE day>=?", (today.isoformat(),))
        c.executemany("INSERT INTO earnings(symbol,day,time_of_day,fiscal_end,estimate,fetched_ts) "
                      "VALUES(?,?,?,?,?,?)",
                      [(d.symbol, d.day.isoformat(), d.time_of_day,
                        d.fiscal_end.isoformat() if d.fiscal_end else None, d.estimate, now)
                       for d in rows.values()])
    return len(rows)


def next_earnings(store: Store, symbol: str, today: date) -> Optional[EarningsDate]:
    r = store.conn.execute("SELECT day,time_of_day,fiscal_end,estimate FROM earnings "
                           "WHERE symbol=? AND day>=? ORDER BY day LIMIT 1", (symbol, today.isoformat())).fetchone()
    if r is None:
        return None
    return EarningsDate(symbol, date.fromisoformat(r[0]), r[1], date.fromisoformat(r[2]) if r[2] else None, r[3])


# -- corporate actions -------------------------------------------------------------------
def _iso(d: Optional[date]) -> Optional[str]:
    return d.isoformat() if d else None


@dataclass(frozen=True)
class ActionChanges:
    new: List[CorporateAction]
    changed: List[CorporateAction]
    withdrawn: List[str]           # ids

    def __bool__(self) -> bool:
        return bool(self.new or self.changed or self.withdrawn)


def save_corporate_actions(store: Store, actions: Iterable[CorporateAction], symbols: Iterable[str],
                           start: date, end: date, now: float) -> ActionChanges:
    """Store one fetch: `actions` for `symbols` with process dates in [start, end].

    Actions for other symbols (an acquirer named in a requested target's merger) are not
    stored: the fetch only vouches for the symbols it asked about. A stored action for one
    of those symbols in that window that the fetch no longer returns is marked withdrawn."""
    syms = sorted(set(symbols))
    wanted = set(syms)
    mine = {a.id: a for a in actions if a.symbol in wanted}
    new: List[CorporateAction] = []
    changed: List[CorporateAction] = []
    stale: List[str] = []
    with store.tx() as c:
        if syms:
            marks = ",".join("?" * len(syms))
            stale = [r[0] for r in c.execute(
                f"SELECT id FROM corporate_actions WHERE withdrawn_ts IS NULL AND symbol IN ({marks}) "
                "AND process_day BETWEEN ? AND ?", (*syms, start.isoformat(), end.isoformat()))
                if r[0] not in mine]
            c.executemany("UPDATE corporate_actions SET withdrawn_ts=?, updated_ts=? WHERE id=?",
                          [(now, now, i) for i in stale])
        for a in mine.values():
            row = (a.kind, a.symbol, _iso(a.effective), _iso(a.process), _iso(a.ex), _iso(a.record),
                   _iso(a.pay), a.detail_json())
            old = c.execute("SELECT kind,symbol,effective_day,process_day,ex_day,record_day,pay_day,detail,"
                            "withdrawn_ts FROM corporate_actions WHERE id=?", (a.id,)).fetchone()
            if old is None:
                c.execute("INSERT INTO corporate_actions(id,kind,symbol,effective_day,process_day,ex_day,"
                          "record_day,pay_day,detail,first_seen_ts,updated_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                          (a.id, *row, now, now))
                new.append(a)
            elif tuple(old[:8]) != row or old[8] is not None:
                c.execute("UPDATE corporate_actions SET kind=?,symbol=?,effective_day=?,process_day=?,ex_day=?,"
                          "record_day=?,pay_day=?,detail=?,updated_ts=?,withdrawn_ts=NULL WHERE id=?",
                          (*row, now, a.id))
                changed.append(a)
        c.executemany("INSERT INTO corporate_action_checks(symbol,ts) VALUES(?,?) "
                      "ON CONFLICT(symbol) DO UPDATE SET ts=excluded.ts", [(s, now) for s in syms])
    return ActionChanges(new, changed, stale)


def _day(s: Optional[str]) -> Optional[date]:
    return date.fromisoformat(s) if s else None


def _action(r) -> CorporateAction:
    return CorporateAction(id=r[0], kind=r[1], symbol=r[2], effective=_day(r[3]), process=_day(r[4]),
                           ex=_day(r[5]), record=_day(r[6]), pay=_day(r[7]), detail=json.loads(r[8]))


_ACTION_COLS = "id,kind,symbol,effective_day,process_day,ex_day,record_day,pay_day,detail"


def actions_for(store: Store, symbol: str, since: Optional[date] = None) -> List[CorporateAction]:
    """Live (not withdrawn) actions for `symbol` booked on or after `since`, or with no
    process date, ordered by effective date."""
    rows = store.conn.execute(
        f"SELECT {_ACTION_COLS} FROM corporate_actions WHERE symbol=? AND withdrawn_ts IS NULL "
        "AND (process_day IS NULL OR process_day>=?) ORDER BY effective_day, id",
        (symbol, (since or date.min).isoformat()))
    return [_action(r) for r in rows]


def pending_actions(store: Store, symbol: str, today: date) -> List[CorporateAction]:
    return [a for a in actions_for(store, symbol) if a.pending(today)]


def corporate_actions_checked(store: Store, symbol: str) -> Optional[float]:
    r = store.conn.execute("SELECT ts FROM corporate_action_checks WHERE symbol=?", (symbol,)).fetchone()
    return r[0] if r else None


# -- SEC ---------------------------------------------------------------------------------
def save_sec_lists(store: Store, listings: Dict[str, Listing], funds: Set[str]) -> None:
    with store.tx() as c:
        c.execute("DELETE FROM sec_listings")
        c.executemany("INSERT INTO sec_listings(symbol,cik,name,exchange) VALUES(?,?,?,?)",
                      [(x.symbol, x.cik, x.name, x.exchange) for x in listings.values()])
        c.execute("DELETE FROM sec_funds")
        c.executemany("INSERT INTO sec_funds(symbol) VALUES(?)", [(s,) for s in sorted(funds)])


def sec_listing(store: Store, symbol: str) -> Optional[Listing]:
    r = store.conn.execute("SELECT symbol,cik,name,exchange FROM sec_listings WHERE symbol=?", (symbol,)).fetchone()
    return Listing(r[0], r[1], r[2], r[3]) if r else None


def sec_funds(store: Store) -> Set[str]:
    return {r[0] for r in store.conn.execute("SELECT symbol FROM sec_funds")}


def save_company(store: Store, co: Company, now: float) -> None:
    with store.tx() as c:
        c.execute("INSERT INTO sec_companies(cik,name,entity_type,sic,sic_description,tickers,exchanges,fetched_ts) "
                  "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(cik) DO UPDATE SET name=excluded.name, "
                  "entity_type=excluded.entity_type, sic=excluded.sic, sic_description=excluded.sic_description, "
                  "tickers=excluded.tickers, exchanges=excluded.exchanges, fetched_ts=excluded.fetched_ts",
                  (co.cik, co.name, co.entity_type, co.sic, co.sic_description, json.dumps(list(co.tickers)),
                   json.dumps(list(co.exchanges)), now))


def sec_company(store: Store, cik: int) -> Optional[Tuple[Company, float]]:
    """A cached profile and when it was fetched, or None."""
    r = store.conn.execute("SELECT cik,name,entity_type,sic,sic_description,tickers,exchanges,fetched_ts "
                           "FROM sec_companies WHERE cik=?", (cik,)).fetchone()
    if r is None:
        return None
    return Company(r[0], r[1], r[2], r[3], r[4], tuple(json.loads(r[5])), tuple(json.loads(r[6]))), r[7]
