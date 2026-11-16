"""
The shadow book: the live book's council decisions, sized by the formula, kept beside it
so the chair's sizing can be judged against the code it replaced.

What it does: under `sizing.sizer = "model"` the chair sizes the live book's BUYs. The
shadow is a second paper book in its own database (`shadow.db`, next to `ottr.db`). It
starts as an exact copy of the live book (`fork`, made just before the first BUY the chair
sizes) and from then on the engine gives it everything the live book gets:
- every council trade, with the chair's weight taken off, so the formula sizes each BUY
  from the same conviction (a SELL is the same fraction of the shadow's own holding);
- everything the desk does by itself: corporate actions (`sync_actions` copies the live
  book's announcements over first), the close-of-day fees, the cash sweep, deferred-order
  retries, and the risk monitor's stops, trims and breaker, decided on the shadow's own
  holdings.
It goes through the same gate, executor and SimBroker at the same moments and quotes, and
gets an equity point with the same marks every time the live book does. So the two books
differ only in who sized the BUYs, and `compare` measures that difference.

The shadow is quiet: nothing about it reaches Discord or the council, which never sees it.
Its holdings can drift from the live book's (a BUY one book takes and the other refuses),
and a SELL the chair aims at a live holding the shadow doesn't have is refused there. Its
own audit log and orders say what it did.

Why a separate database rather than rows in the live one: the live book's tables, code
and checks stay as they are, and the shadow reuses all of them unchanged.

Why it lives only while model sizing does: a shadow that missed a day would compare a
different history. An engine that starts with code sizing retires any shadow, and one
that finds a shadow forked from another book (an archived one) retires it and forks
afresh when the chair next sizes a BUY. Retired files are renamed, never deleted.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import statistics
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from ottr.ledger.store import Store
from ottr.market.calendar import ET

logger = logging.getLogger(__name__)

SHADOW_DB = "shadow.db"
BOOK_ID = "book_id"                  # meta: which book a database is (set on the live one at its first fork)
SHADOW_OF = "shadow_of"              # meta on the shadow: the live book it was forked from
FORKED_TS = "shadow_forked_ts"       # meta on the shadow: when
FORKED_EQUITY = "shadow_forked_equity"  # meta on the shadow: both books' equity at that moment
EARLY_SESSIONS = 20                  # fewer closes than this: say it's too early to judge


def path_for(live: Store) -> Path:
    """Where the live book's shadow lives: beside it, so a scratch copy's shadow (a
    propose-only meeting) never touches the real one."""
    return live.path.parent / SHADOW_DB


def retire(live: Store, why: str, now: Optional[float] = None) -> Optional[Path]:
    """Rename the shadow (and its WAL files) out of the way; None when there is none."""
    path = path_for(live)
    if not path.exists():
        return None
    stamp = datetime.fromtimestamp(now or time.time(), tz=ET).strftime("%Y-%m-%dT%H%M%S")
    dest = path.with_name(f"{SHADOW_DB}.retired-{stamp}")
    for suffix in ("", "-wal", "-shm"):
        part = path.with_name(SHADOW_DB + suffix)
        if part.exists():
            part.rename(path.with_name(f"{dest.name}{suffix}"))
    logger.warning("shadow book retired to %s: %s", dest.name, why)
    live.audit("shadow_retired", file=dest.name, why=why)
    return dest


def open_existing(live: Store) -> Optional[Store]:
    """This live book's shadow, open for writing, or None. A shadow of another book, or
    one whose fork never finished, is retired."""
    path = path_for(live)
    if not path.exists():
        return None
    shadow = Store(path, writer=True)
    book = live.meta(BOOK_ID)
    if book is not None and shadow.meta(SHADOW_OF) == book and shadow.meta(FORKED_TS):
        return shadow
    shadow.close()
    retire(live, "it was forked from another book, or its fork didn't finish")
    return None


def fork(live: Store, now: float, equity: Optional[float]) -> Store:
    """Copy the live book into a new shadow and mark it as this book's. The copy is written
    to a temporary file and renamed, so a crash leaves either no shadow or a whole one
    (and one that never got its marks is retired by `open_existing`)."""
    path = path_for(live)
    retire(live, "replaced by a new fork", now)
    book = live.meta(BOOK_ID)
    if book is None:
        book = uuid.uuid4().hex
        live.set_meta(BOOK_ID, book)
    tmp = path.with_name(SHADOW_DB + ".forking")
    for suffix in ("", "-wal", "-shm"):
        part = tmp.with_name(tmp.name + suffix)
        if part.exists():
            part.unlink()
    dst = sqlite3.connect(tmp)
    try:
        live.conn.backup(dst)
    finally:
        dst.close()
    os.replace(tmp, path)
    shadow = Store(path, writer=True)
    shadow.set_meta(SHADOW_OF, book)
    if equity is not None:
        shadow.set_meta(FORKED_EQUITY, repr(float(equity)))
    shadow.set_meta(FORKED_TS, repr(float(now)))   # last: its presence says the fork finished
    live.audit("shadow_forked", file=path.name, equity=equity)
    shadow.audit("shadow_forked", of=book, equity=equity)
    return shadow


def sync_actions(live: Store, shadow: Store) -> int:
    """Copy the live book's corporate-action announcements (the events feed writes them
    there only) into the shadow, so its ledger books the same dividends and splits. Only
    what changed since the last copy: every write stamps `updated_ts`."""
    cols = [r[1] for r in live.conn.execute("PRAGMA table_info(corporate_actions)")]
    have = shadow.conn.execute("SELECT MAX(updated_ts) FROM corporate_actions").fetchone()[0]
    rows = live.conn.execute(f"SELECT {', '.join(cols)} FROM corporate_actions WHERE updated_ts>?",
                             (have if have is not None else float("-inf"),)).fetchall()
    if not rows:
        return 0
    with shadow.tx() as c:
        c.executemany(f"INSERT OR REPLACE INTO corporate_actions({', '.join(cols)}) "
                      f"VALUES({', '.join('?' * len(cols))})", [tuple(r) for r in rows])
    return len(rows)


# -- the comparison ------------------------------------------------------------------------
def _points(store: Store, since: float) -> Dict[float, Dict[str, Any]]:
    out = {}
    for ts, equity, invested, detail in store.conn.execute(
            "SELECT ts, equity, invested, detail FROM equity WHERE ts>=? ORDER BY ts", (since,)):
        d = json.loads(detail or "{}")
        basis = (d.get("basis") or {}).values()
        out[ts] = {"equity": equity, "invested": invested, "weights": d.get("weights") or {},
                   "close": bool(basis) and all(b == "close" for b in basis)}
    return out


def _book_stats(pts: List[Dict[str, Any]], base: float, cash_symbol: str) -> Dict[str, Any]:
    eq = [p["equity"] for p in pts]
    peak, worst = base, 0.0
    for e in eq:
        peak = max(peak, e)
        worst = min(worst, e / peak - 1)
    closes = [p["equity"] for p in pts if p["close"]]
    daily = [b / a - 1 for a, b in zip(closes, closes[1:])]
    # Cash and the T-bill ETF: what the book holds instead of risk.
    idle = [1 - sum(w for s, w in p["weights"].items() if s != cash_symbol) for p in pts]
    return {"equity": eq[-1], "return_pct": (eq[-1] / base - 1) * 100, "max_drawdown_pct": worst * 100,
            "daily_vol_pct": statistics.pstdev(daily) * 100 if len(daily) >= 2 else None,
            "idle_pct": sum(idle) / len(idle) * 100}


def _council_buys(store: Store, since: float) -> Dict[str, Any]:
    rows = store.conn.execute(
        "SELECT notional FROM fills WHERE origin='council' AND side='BUY' AND ts>=?", (since,)).fetchall()
    return {"count": len(rows), "usd": sum(float(r[0]) for r in rows)}


def _chair_sizing(live: Store, since: float) -> Dict[str, Any]:
    """How the chair's BUY targets compared with the formula's, from the live verdicts."""
    above = below = same = 0
    ratios = []
    for (verdict,) in live.conn.execute(
            "SELECT verdict FROM orders WHERE origin='council' AND side='BUY' AND ts>=? "
            "AND status IN ('filled','dry_run')", (since,)):
        v = json.loads(verdict or "{}")
        if v.get("sized_by") != "model":
            continue
        mine, formula = float(v.get("target_pct") or 0), float(v.get("code_target_pct") or 0)
        if abs(mine - formula) < 0.005:
            same += 1
        elif mine > formula:
            above += 1
        else:
            below += 1
        if formula > 0:
            ratios.append(mine / formula)
    return {"buys": above + below + same, "above": above, "below": below, "same": same,
            "median_ratio": statistics.median(ratios) if ratios else None}


def compare(live: Store, shadow: Store, cash_symbol: str) -> Optional[Dict[str, Any]]:
    """The two books since the fork, at the equity points both have. None before the
    first shared point."""
    since = shadow.meta(FORKED_TS)
    if since is None:
        return None
    since = float(since)
    lp, sp = _points(live, since), _points(shadow, since)
    shared = sorted(set(lp) & set(sp))
    if not shared:
        return None
    base = shadow.meta(FORKED_EQUITY)
    if base is None:  # the fork's own valuation is missing: the last point both share before it
        r = shadow.conn.execute("SELECT equity FROM equity WHERE ts<? ORDER BY ts DESC LIMIT 1", (since,)).fetchone()
        base = r[0] if r else lp[shared[0]]["equity"]
    base = float(base)
    lives, shadows = [lp[t] for t in shared], [sp[t] for t in shared]
    out = {"since": since, "base_equity": base, "points": len(shared),
           "sessions": sum(1 for p in lives if p["close"]),
           "live": {**_book_stats(lives, base, cash_symbol), "council_buys": _council_buys(live, since)},
           "shadow": {**_book_stats(shadows, base, cash_symbol), "council_buys": _council_buys(shadow, since)},
           "chair_sizing": _chair_sizing(live, since)}
    out["difference_usd"] = out["live"]["equity"] - out["shadow"]["equity"]
    out["difference_pts"] = out["live"]["return_pct"] - out["shadow"]["return_pct"]
    return out


def compare_files(live: Store, cash_symbol: str) -> Optional[Dict[str, Any]]:
    """`compare` for a reader: opens the live book's shadow read-only, if there is one."""
    path = path_for(live)
    if not path.exists():
        return None
    shadow = Store(path, writer=False)
    try:
        return compare(live, shadow, cash_symbol)
    finally:
        shadow.close()


def _pct(x: Optional[float], signed: bool = True) -> str:
    return "n/a" if x is None else f"{x:+.2f}%" if signed else f"{x:.2f}%"


def report(c: Optional[Dict[str, Any]]) -> List[str]:
    """The comparison as text, for `ottr scorecard`."""
    if c is None:
        return ["Chair's sizing vs the formula: no shadow book yet (it starts at the first BUY the chair sizes)."]
    since = datetime.fromtimestamp(c["since"], tz=ET).strftime("%a %Y-%m-%d %H:%M ET")
    early = c["sessions"] < EARLY_SESSIONS
    lines = [f"Chair's sizing vs the formula, since {since} ({c['sessions']} closes"
             + (f"; too early to judge before {EARLY_SESSIONS})" if early else ")") + ":",
             "  the same decisions; the live book's BUYs sized by the chair, the shadow's by the formula.",
             f"  {'':20}{'equity':>12}{'return':>9}{'max drawdown':>14}{'daily vol':>11}{'in cash/T-bills':>17}"
             f"{'council BUYs':>20}"]
    for label, b in (("chair (live)", c["live"]), ("formula (shadow)", c["shadow"])):
        equity = f"${b['equity']:,.2f}"
        buys = f"{b['council_buys']['count']} (${b['council_buys']['usd']:,.0f})"
        lines.append(f"  {label:20}{equity:>12}{_pct(b['return_pct']):>9}"
                     f"{_pct(b['max_drawdown_pct']):>14}{_pct(b['daily_vol_pct'], False):>11}"
                     f"{_pct(b['idle_pct'], False):>17}{buys:>20}")
    d = c["difference_usd"]
    lines.append(f"  the chair's sizing is {'ahead' if d >= 0 else 'behind'} by ${abs(d):,.2f} "
                 f"({c['difference_pts']:+.2f} points)")
    s = c["chair_sizing"]
    if s["buys"]:
        ratio = f" (median {s['median_ratio']:.1f}x the formula)" if s["median_ratio"] is not None else ""
        lines.append(f"  the chair sized {s['buys']} BUY(s): {s['above']} above the formula's target, "
                     f"{s['below']} below, {s['same']} the same{ratio}")
    return lines


def status_line(c: Optional[Dict[str, Any]]) -> Optional[str]:
    """One line for `ottr status`."""
    if c is None:
        return None
    d = c["difference_usd"]
    since = datetime.fromtimestamp(c["since"], tz=ET).strftime("%m-%d")
    return (f"sizing: chair ${c['live']['equity']:,.2f} vs formula ${c['shadow']['equity']:,.2f} "
            f"since {since} (chair {'ahead' if d >= 0 else 'behind'} ${abs(d):,.2f}; `ottr scorecard`)")
