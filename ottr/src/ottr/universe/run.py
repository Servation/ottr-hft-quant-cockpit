"""
One screen: from Alpaca's tradable stocks to a ranking and a membership decision.

What it does, in stages, counting what each one keeps (so a thin result says where the
pool shrank):
1. listed: Alpaca's active, tradable US equities on NYSE or Nasdaq that the SEC lists as
   an exchange-listed company and not as a fund, less the core and reference ETFs and
   names that say ETF, ETN, leveraged or inverse.
2. liquid: a month of daily bars: the price and the 20-session median dollar volume at
   the floors; the `pool_max` most traded go on.
3. measured: 13+ months of adjusted daily bars, measured against the hard gates
   (`screen.py`). The satellites are measured whatever stage they would have dropped at,
   so the decision can say why.
4. operating: for the stocks that passed the gates, the SEC profile (cached 90 days)
   must say an operating company, not a trust, a SPAC or an asset-backed issuer
   (`events/sec.operating_company`). Last because it is the slow stage: a profile is one
   request of up to a few megabytes, at most 8 a second, so it is only fetched for a
   stock that could actually be picked. Those are ranked together.
5. decided: `select.decide` against the current membership.
`run_screen` changes nothing; `apply` books its decision (the engine does that weekly,
`ottr screen` only shows what it would do).

Why it refuses rather than guesses: a screen on half the data would demote good
satellites for "missing sessions" or rank a thin pool as if it were the market, so
missing SEC lists, no SPY history or bars missing for most stocks fail the run instead.
"""

from __future__ import annotations

import asyncio
import logging
import re
import statistics
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable, Dict, List, Mapping, Optional, Set

from ottr.events import db as events_db
from ottr.events.refresh import COMPANY_TTL_DAYS
from ottr.events.sec import Company, Sec, SecError, operating_company
from ottr.events.view import StoredEvents
from ottr.ledger.book import trade_day
from ottr.ledger.store import Store
from ottr.market import history
from ottr.market import indicators as ind
from ottr.market.alpaca import Alpaca, completed_bars
from ottr.market.calendar import TradingCalendar
from ottr.universe import db as udb
from ottr.universe.screen import CORR_WINDOW, LIQUIDITY_WINDOW, MOM_LOOKBACK, RECENT_SESSIONS, Row, measure, score
from ottr.universe.select import Decision, decide

logger = logging.getLogger(__name__)

SPY = "SPY"
ALPACA_EXCHANGES = ("NYSE", "NASDAQ")
RECENT_DAYS = 45                    # calendar days of bars for the liquidity stage
SHOWN = 25                          # ranked rows kept in a stored screen
# Names of exchange-traded products that aren't companies. ("Trust" is left out: most
# REITs are trusts, and the SEC profile catches the commodity trusts.)
NOT_A_STOCK = re.compile(r"\b(ETF|ETN|ETP|fund|[23]x|leveraged|inverse|ultrapro|ultrashort)\b", re.I)
MAX_PROFILE_ERRORS_SHOWN = 5
PROFILE_CONCURRENCY = 8            # SEC profile requests in flight at once


class ScreenError(RuntimeError):
    """The screen can't be trusted with the data it has: nothing is decided."""


@dataclass
class ScreenResult:
    id: str
    ts: float
    day: date
    kind: str
    counts: Dict[str, int]
    ranked: List[Row]
    rows: Dict[str, Row]                      # every measured stock
    decision: Decision
    current: List[str]                        # the active satellites going in
    errors: List[str] = field(default_factory=list)

    def summary(self) -> str:
        d = self.decision
        parts = [f"{self.counts.get('measured', 0)} measured, {len(self.ranked)} ranked"]
        if d.promote:
            parts.append("promoted " + ", ".join(s for s, _ in d.promote))
        if d.demote:
            parts.append("demoted " + ", ".join(s for s, _ in d.demote))
        if not (d.promote or d.demote):
            parts.append("no changes")
        return "; ".join(parts)

    def to_dict(self, shown: int = SHOWN) -> dict:
        keep = {s for s, _ in self.decision.promote + self.decision.demote + self.decision.keep +
                self.decision.passed} | set(self.current)
        return {"id": self.id, "ts": self.ts, "day": self.day.isoformat(), "kind": self.kind,
                "counts": self.counts, "summary": self.summary(), "decision": self.decision.to_dict(),
                "current": self.current, "errors": self.errors,
                "top": [r.to_dict() for r in self.ranked[:shown]],
                "rows": {s: r.to_dict() for s, r in sorted(self.rows.items()) if s in keep}}


def _unlisted(sym: str, assets: Mapping, listings: Mapping, funds: Set[str]) -> str:
    """Why `sym` isn't a listed stock the desk could hold ("" = it is)."""
    a = assets.get(sym)
    if a is None or not (a.tradable and a.status == "active"):
        return "not tradable at Alpaca"
    if a.exchange not in ALPACA_EXCHANGES:
        return f"trades on {a.exchange or 'no exchange'}, not NYSE or Nasdaq"
    if sym in funds:
        return "a fund series (ETF or mutual fund)"
    if sym not in listings:
        return "not in the SEC's exchange-listed companies"
    if NOT_A_STOCK.search(a.name or ""):
        return f"named like a fund or a note ({a.name})"
    return ""


async def _profile(store: Store, sec: Optional[Sec], cik: int, now: float, errors: List[str]) -> Optional[Company]:
    """The SEC profile, from the cache while fresh. A failed fetch falls back to a stale
    cached one, so an SEC outage can't make a satellite look like a non-company."""
    cached = events_db.sec_company(store, cik)
    if cached and now - cached[1] < COMPANY_TTL_DAYS * 86400:
        return cached[0]
    if sec is None:
        return cached[0] if cached else None
    try:
        co = await sec.company(cik)
    except SecError as e:
        errors.append(f"SEC profile {cik}: {e}")
        return cached[0] if cached else None
    if store.writer:
        events_db.save_company(store, co, now)
    return co


def _liquidity(bars) -> Optional[tuple]:
    if len(bars) < LIQUIDITY_WINDOW:
        return None
    return bars[-1].close, statistics.median(b.close * b.volume for b in bars[-LIQUIDITY_WINDOW:])


async def run_screen(cfg, store: Store, alpaca: Alpaca, sec: Optional[Sec], cal: TradingCalendar, now: float,
                     kind: str = "weekly", progress: Callable[[str], None] = logger.info) -> ScreenResult:
    """`progress` hears one line per stage (a run takes minutes; the first one, with no
    cached SEC profiles, longer)."""
    sc = cfg.screen
    today = trade_day(now)
    errors: List[str] = []
    counts: Dict[str, int] = {}
    current = udb.satellites(store, (udb.ACTIVE,))
    cooling = {s: x for s, x in udb.satellites(store, (udb.SUNSET, udb.REMOVED)).items()
               if x.cooling(today, sc.cooldown_days)}
    listings, funds = events_db.sec_listings_all(store), events_db.sec_funds(store)
    if not listings or not funds:
        raise ScreenError("the SEC's company and fund lists aren't loaded yet (the sec_lists events feed loads them)")

    # 1. listed
    assets = await alpaca.assets()
    exclude = set(cfg.universe.core) | set(cfg.universe.reference)
    listed = sorted(s for s in assets if s not in exclude and not _unlisted(s, assets, listings, funds))
    unfit = {s: why for s in current if (why := _unlisted(s, assets, listings, funds))}
    counts["alpaca_active"], counts["listed"] = len(assets), len(listed)
    progress(f"{len(listed):,} listed stocks; reading a month of daily bars for their liquidity")

    # 2. liquid
    recent = await alpaca.daily_bars(sorted(set(listed) | set(current)), today - timedelta(days=RECENT_DAYS),
                                     now=now, adjustment="all")
    liquid = []
    for s in listed:
        lq = _liquidity(completed_bars(recent.get(s, []), cal, now))
        if lq and lq[0] >= sc.min_price and lq[1] >= sc.min_dollar_volume:
            liquid.append((-lq[1], s))
    pool = [s for _, s in sorted(liquid)[:sc.pool_max]]
    counts["liquid"], counts["pool"] = len(liquid), len(pool)
    progress(f"{len(liquid):,} liquid, {len(pool):,} kept; reading {sc.history_days} days of bars for each")

    # 3. measured
    bars = await alpaca.daily_bars(sorted(set(pool) | set(current) | {SPY}), today - timedelta(days=sc.history_days),
                                   now=now, adjustment="all")
    bars = {s: completed_bars(b, cal, now) for s, b in bars.items()}
    spy = [(b.day, b.close) for b in bars.get(SPY, [])]
    if len(spy) < MOM_LOOKBACK + 1:
        raise ScreenError(f"only {len(spy)} sessions of SPY history to measure against")
    recent_sessions = [x.day for x in cal.completed_sessions(now, RECENT_SESSIONS)]
    events = StoredEvents.from_config(store, lambda: cal, cfg)
    rows = {s: measure(cfg, s, [(b.day, b.close, b.volume) for b in bars.get(s, [])], spy, recent_sessions,
                       events.earnings_sessions(s, now), assets[s].name if s in assets else "")
            for s in sorted(set(pool) | (set(current) - set(unfit)))}
    gapped = sum(1 for r in rows.values() if r.gate.startswith("no bar for"))
    if len(rows) >= 20 and gapped > len(rows) / 2:
        raise ScreenError(f"{gapped} of {len(rows)} stocks miss recent sessions: the bars look incomplete")
    counts["measured"] = len(rows)
    counts["passed"] = sum(1 for r in rows.values() if r.passed)
    progress(f"{counts['passed']:,} of {len(rows):,} passed the gates; checking their SEC company profiles")

    # 4. operating companies, among those that could be picked. Fetched several at a
    # time: each is about 150 KB and a third of a second, so one by one the stage is bound
    # by latency, not by the SEC's limit (the client's limiter keeps to that either way).
    gate = asyncio.Semaphore(PROFILE_CONCURRENCY)

    async def profile(s: str) -> Optional[Company]:
        async with gate:
            listing = listings.get(s)
            return await _profile(store, sec, listing.cik, now, errors) if listing else None

    picked = sorted(s for s, r in rows.items() if r.passed or s in current)
    for s, co in zip(picked, await asyncio.gather(*(profile(s) for s in picked))):
        ok, why = operating_company(s, listings.get(s), funds, co)
        if not ok:
            del rows[s]
            if s in current:
                unfit[s] = why
    counts["operating"] = sum(1 for r in rows.values() if r.passed)
    if counts["passed"] and not counts["operating"]:
        why = errors[0] if errors else "SEC_USER_AGENT is not set and no profiles are cached"
        raise ScreenError(f"no operating company confirmed among {counts['passed']} stocks that passed the gates "
                          f"({why})")
    if len(errors) > MAX_PROFILE_ERRORS_SHOWN:
        errors[MAX_PROFILE_ERRORS_SHOWN:] = [f"... and {len(errors) - MAX_PROFILE_ERRORS_SHOWN} more SEC errors"]
    ranked = score(list(rows.values()))
    rows.update((r.symbol, r) for r in ranked)
    counts["ranked"] = len(ranked)

    # 5. decided
    closes = {s: [(b.day, b.close) for b in bars.get(s, [])] for s in rows}
    for s in cfg.universe.core:
        closes[s] = history.adjusted_closes(store, s)
    memo: Dict[tuple, Optional[float]] = {}

    def correlation(a: str, b: str) -> Optional[float]:
        if (a, b) not in memo:
            memo[(a, b)] = ind.aligned_return_correlation(closes.get(a, []), closes.get(b, []), CORR_WINDOW)
        return memo[(a, b)]

    decision = decide(cfg, ranked, rows, current, cooling, today, correlation, list(cfg.universe.core), unfit)
    return ScreenResult(id=f"screen-{today.isoformat()}-{uuid.uuid4().hex[:6]}", ts=now, day=today, kind=kind,
                        counts=counts, ranked=ranked, rows=rows, decision=decision, current=sorted(current),
                        errors=errors)


def apply(store: Store, result: ScreenResult, held: Set[str], now: float) -> None:
    """Book a screen's decision: demotions (sunset while held), then promotions."""
    for sym, why in result.decision.demote:
        udb.demote(store, sym, why, sym in held, now, result.id)
    for sym, why in result.decision.promote:
        r = result.rows.get(sym)
        udb.promote(store, sym, r.score if r else None, why, now, result.id)


def digest(result: ScreenResult) -> dict:
    """What Discord shows: the changes with each stock's numbers, and the best passed over."""
    def line(sym: str) -> str:
        r = result.rows.get(sym)
        if r is None or r.momentum_return is None:
            return sym
        parts = [f"12-1 {r.momentum_return * 100:+.0f}%"]
        if r.vol:
            parts.append(f"vol {r.vol * 100:.0f}%")
        if r.near_high:
            parts.append(f"{r.near_high * 100:.0f}% of its 52w high")
        return f"{sym} ({', '.join(parts)})"
    d = result.decision
    return {"id": result.id, "day": result.day.isoformat(), "kind": result.kind, "counts": result.counts,
            "promote": [[line(s), why] for s, why in d.promote], "demote": [[s, why] for s, why in d.demote],
            "keep": [[s, why] for s, why in d.keep], "passed": [[s, why] for s, why in d.passed[:5]],
            "errors": len(result.errors)}
