"""
Scorecard: grade every analyst stance and chair trade by what the asset did next.

What it does: for each call (an analyst's BUY / SELL / HOLD on an asset, or a trade the
chair kept), the asset's total return from the meeting day's adjusted close to the close 1
and 5 sessions later, and that return minus SPY's over the same sessions. Results are
grouped by who made the call, the model (and reasoning effort) behind it, and the stance:
count, mean return, mean excess over SPY, hit rate. Why: the council's calls are the
desk's only source of edge, and a model change (PR #81) should be judged on evidence, so
every group is split by model and a change shows up as a before and an after.

How it grades, and why:
- From the meeting day's adjusted close, not the stance's own price. A stance's price is
  a raw intraday mid (sometimes missing), and mixing it with adjusted closes would book
  any dividend in the window as a loss. The cost: the rest of the meeting day's move is
  not graded, so the morning and afternoon meetings of one day share one base.
- Sessions, not calendar days (the exchange calendar in the book).
- A call whose target close isn't in yet is "pending", never zero; one whose session has
  closed but whose asset has no close (a satellite no longer refreshed) is "no price".
  Neither enters a mean.
- Hit rate: a BUY hits when the asset rose, a SELL when it fell. HOLD has no direction,
  so no hit rate.

How much evidence: the same asset is rated at every meeting, twice a session, so
consecutive calls overlap (two calls on one day have identical +1 returns; calls on
consecutive days share four of their five +5 sessions). Each group therefore reports, next
to the number of calls, `independent`: the most calls on any one asset whose windows don't
overlap, summed over assets (a greedy count; at +1 it is the distinct asset-days). The
rough standard error of the mean excess divides by that count, not by the number of
calls. Calls on different assets on the same day still share the market's move, so even
that count overstates the evidence; `sessions` says how many distinct days it rests on.

The record of a meeting carries each analyst's model id and effort (`models`, and
`model`/`effort` on `chair`); meetings recorded before it did show as "not recorded".

Pure functions (`grade`, `independent`, `summarize`) work on plain data; `load` reads a
Store (read-only is enough) and `scorecard` ties them together for the CLI and the API.
"""

from __future__ import annotations

import bisect
import json
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ottr.ledger.book import trade_day
from ottr.ledger.store import Store

HORIZONS = (1, 5)
BENCHMARK = "SPY"
ACTIONS = ("BUY", "SELL", "HOLD")
NOT_RECORDED = "not recorded"     # meetings from before the record kept model ids

PENDING, NO_PRICE, GRADED = "pending", "no price", "graded"


@dataclass(frozen=True)
class Call:
    """One call to grade: an analyst's stance, or a trade the chair kept."""
    who: str          # analyst name, or the name of whoever chaired
    model: str        # "model/effort", or NOT_RECORDED
    action: str       # BUY | SELL | HOLD
    asset: str
    day: date         # the meeting's trade date
    meeting_id: str


@dataclass(frozen=True)
class Grade:
    status: str                        # GRADED | PENDING | NO_PRICE
    ret: Optional[float] = None        # fraction, e.g. 0.012 = +1.2%
    excess: Optional[float] = None     # ret minus the benchmark's return over the same sessions
    base: Optional[int] = None         # index of the base session in `sessions`


def model_label(info: Optional[Mapping[str, Any]]) -> str:
    """'claude-opus-5-5/high' from a record's {model, effort}; NOT_RECORDED without one."""
    if not info or not info.get("model"):
        return NOT_RECORDED
    return f"{info['model']}/{info['effort']}" if info.get("effort") else str(info["model"])


def grade(asset: str, day: date, horizon: int, sessions: Sequence[date],
          closes: Mapping[str, Mapping[date, float]], as_of: Optional[date],
          benchmark: str = BENCHMARK) -> Grade:
    """The call's return from `day`'s close to the close `horizon` sessions later.

    `sessions` is the exchange calendar (sorted), `closes` adjusted closes by symbol and
    day, `as_of` the newest day the closes speak for (the benchmark's last close). The base
    is the last session on or before `day` (a meeting always runs in session, so that is
    its own day)."""
    i = bisect.bisect_right(sessions, day) - 1
    if i < 0:
        return Grade(NO_PRICE)                        # before the calendar we hold
    if i + horizon >= len(sessions) or as_of is None or sessions[i + horizon] > as_of:
        return Grade(PENDING, base=i)
    d0, d1 = sessions[i], sessions[i + horizon]
    a, b = closes.get(asset, {}), closes.get(benchmark, {})
    if a.get(d0) is None or a.get(d1) is None or b.get(d0) is None or b.get(d1) is None:
        return Grade(NO_PRICE, base=i)
    ret = a[d1] / a[d0] - 1
    return Grade(GRADED, ret=ret, excess=ret - (b[d1] / b[d0] - 1), base=i)


def independent(windows: Iterable[Tuple[str, int]], horizon: int) -> int:
    """How many (asset, base session) windows of `horizon` sessions can be picked so that
    no two on the same asset share a session of return: greedy from the earliest, which is
    the maximum. At horizon 1 it is the number of distinct asset-days."""
    by_asset: Dict[str, List[int]] = defaultdict(list)
    for asset, i in windows:
        by_asset[asset].append(i)
    n = 0
    for starts in by_asset.values():
        last = None
        for i in sorted(starts):
            if last is None or i >= last + horizon:
                n, last = n + 1, i
    return n


def hit(action: str, ret: float) -> Optional[bool]:
    if action == "BUY":
        return ret > 0
    if action == "SELL":
        return ret < 0
    return None


def summarize(calls: Iterable[Call], sessions: Sequence[date], closes: Mapping[str, Mapping[date, float]],
              as_of: Optional[date], horizons: Sequence[int] = HORIZONS,
              benchmark: str = BENCHMARK) -> List[Dict[str, Any]]:
    """One row per (who, model, action, horizon), in that order. Means are fractions;
    None where nothing has been graded yet (never zero)."""
    groups: Dict[Tuple[str, str, str], List[Call]] = defaultdict(list)
    for c in calls:
        groups[(c.who, c.model, c.action)].append(c)
    order = {a: k for k, a in enumerate(ACTIONS)}
    rows = []
    for (who, model, action) in sorted(groups, key=lambda k: (k[0], k[1], order.get(k[2], 9), k[2])):
        group = groups[(who, model, action)]
        for h in horizons:
            grades = [(c, grade(c.asset, c.day, h, sessions, closes, as_of, benchmark)) for c in group]
            done = [(c, g) for c, g in grades if g.status == GRADED]
            rets = [g.ret for _, g in done]
            excess = [g.excess for _, g in done]
            hits = [hit(action, r) for r in rets]
            indep = independent(((c.asset, g.base) for c, g in done), h)
            rows.append({
                "who": who, "model": model, "action": action, "horizon": h,
                "calls": len(group), "graded": len(done),
                "pending": sum(g.status == PENDING for _, g in grades),
                "no_price": sum(g.status == NO_PRICE for _, g in grades),
                "independent": indep,
                "sessions": len({g.base for _, g in done}),
                "mean_return": statistics.fmean(rets) if rets else None,
                "mean_excess": statistics.fmean(excess) if excess else None,
                # Rough: the spread of all graded calls over the root of the independent
                # count, since overlapping calls are not separate evidence.
                "excess_se": (statistics.stdev(excess) / math.sqrt(indep)) if len(excess) >= 2 and indep >= 2 else None,
                "hit_rate": (sum(hits) / len(hits)) if hits and hits[0] is not None else None,
            })
    return rows


# -- reading the book ----------------------------------------------------------------------
@dataclass
class Book:
    """What the scorecard reads from a book, as plain data."""
    meetings: int
    stances: List[Call]
    trades: List[Call]
    sessions: List[date]
    closes: Dict[str, Dict[date, float]]
    as_of: Optional[date]


def load(store: Store, benchmark: str = BENCHMARK) -> Book:
    """Read stances, the chair's kept trades, the calendar and adjusted closes in one
    snapshot (a read-only store) so closes from two refreshes are never mixed. Only the
    record's small fields are pulled out of its JSON: the packet stays in the database."""
    conn = store.conn
    if store.writer:
        return _load(conn, benchmark)
    with store.snapshot():
        return _load(conn, benchmark)


def _load(conn, benchmark: str) -> Book:
    meetings: Dict[str, Dict[str, Any]] = {}
    for mid, ts, models, chair, trades in conn.execute(
            "SELECT id, ts, json_extract(record, '$.models'), json_extract(record, '$.chair'), "
            "json_extract(record, '$.trades') FROM meetings"):
        meetings[mid] = {"day": trade_day(ts), "models": json.loads(models) if models else {},
                         "chair": json.loads(chair) if chair else None,
                         "trades": json.loads(trades) if trades else []}
    stances = []
    for mid, analyst, asset, stance, ts in conn.execute(
            "SELECT meeting_id, analyst, asset, stance, ts FROM stances"):
        m = meetings.get(mid)
        stances.append(Call(who=analyst, model=model_label((m or {}).get("models", {}).get(analyst)),
                            action=stance, asset=asset, day=m["day"] if m else trade_day(ts), meeting_id=mid))
    trades = []
    for mid, m in meetings.items():
        chair = m["chair"] or {}
        for t in m["trades"]:   # the trades the chair kept, filled by the gate or not
            trades.append(Call(who=chair.get("name", NOT_RECORDED), model=model_label(chair), action=t["action"],
                               asset=t["asset"], day=m["day"], meeting_id=mid))
    sessions = [date.fromisoformat(d) for (d,) in conn.execute("SELECT day FROM calendar ORDER BY day")]
    closes: Dict[str, Dict[date, float]] = defaultdict(dict)
    for sym, d, c in conn.execute("SELECT symbol, day, close FROM adj_closes"):
        closes[sym][date.fromisoformat(d)] = c
    bench = closes.get(benchmark)
    return Book(meetings=len(meetings), stances=stances, trades=trades, sessions=sessions,
                closes=dict(closes), as_of=max(bench) if bench else None)


def scorecard(store: Store, horizons: Sequence[int] = HORIZONS, benchmark: str = BENCHMARK) -> Dict[str, Any]:
    """The whole scorecard as JSON-ready data (the CLI prints it, the API serves it)."""
    b = load(store, benchmark)
    return {
        "as_of": b.as_of.isoformat() if b.as_of else None, "benchmark": benchmark,
        "horizons": list(horizons), "meetings": b.meetings, "stances": len(b.stances),
        "trades": len(b.trades),
        "analysts": summarize(b.stances, b.sessions, b.closes, b.as_of, horizons, benchmark),
        "chair": summarize(b.trades, b.sessions, b.closes, b.as_of, horizons, benchmark),
    }


# -- text report ---------------------------------------------------------------------------
def _pct(x: Optional[float], signed: bool = True) -> str:
    if x is None:
        return "-"
    return f"{x * 100:+.2f}%" if signed else f"{x * 100:.0f}%"


def _se(x: Optional[float]) -> str:
    return "-" if x is None else f"{x * 100:.2f}%"


def _table(rows: List[Dict[str, Any]], horizon: int, who: str) -> List[str]:
    rows = [r for r in rows if r["horizon"] == horizon]
    if not rows:
        return ["  (none)"]
    mw = max(len("model"), *(len(r["model"]) for r in rows))
    ww = max(len(who), *(len(r["who"]) for r in rows))
    out = [f"  {who:<{ww}}  {'model':<{mw}}  {'call':<4}  {'n':>4}  {'indep':>5}  {'days':>4}  "
           f"{'pending':>7}  {'return':>7}  {'vs SPY':>7}  {'+/-':>6}  {'hit':>4}"]
    for r in rows:
        extra = f"  ({r['no_price']} no price)" if r["no_price"] else ""
        out.append(f"  {r['who']:<{ww}}  {r['model']:<{mw}}  {r['action']:<4}  {r['graded']:>4}  "
                   f"{r['independent']:>5}  {r['sessions']:>4}  {r['pending']:>7}  "
                   f"{_pct(r['mean_return']):>7}  {_pct(r['mean_excess']):>7}  "
                   f"{_se(r['excess_se']):>6}  "
                   f"{_pct(r['hit_rate'], signed=False):>4}{extra}")
    return out


def report(sc: Dict[str, Any]) -> str:
    """The scorecard as text for `ottr scorecard`."""
    lines = [f"Scorecard: {sc['meetings']} meetings, {sc['stances']:,} analyst stances, {sc['trades']} chair "
             f"trades; adjusted closes through {sc['as_of'] or 'n/a'}.",
             "Each call is graded from its meeting day's close to the close N sessions later; "
             f"'vs SPY' subtracts {sc['benchmark']}'s return over the same sessions.",
             "n: graded calls. indep: calls on one asset whose windows don't overlap (the evidence is "
             "this, not n). days: distinct meeting days. +/-: rough standard error of 'vs SPY'.",
             "hit: BUY rose / SELL fell (HOLD has none). Pending calls (target close not in yet) "
             "count nowhere."]
    for h in sc["horizons"]:
        lines += ["", f"Analysts, +{h} session{'s' if h != 1 else ''}:", *_table(sc["analysts"], h, "analyst")]
    for h in sc["horizons"]:
        lines += ["", f"Chair's trades, +{h} session{'s' if h != 1 else ''}:", *_table(sc["chair"], h, "chair")]
    return "\n".join(lines)
