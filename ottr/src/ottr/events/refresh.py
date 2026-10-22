"""
Refresh jobs: fetch each events feed and store it, recording how it went.

What it does: one coroutine per feed (macro, earnings, corporate actions, SEC lists) that
fetches, sanity-checks and stores in one transaction, then records the attempt in the
`feeds` table. A failure never raises out of a job and never touches the stored data: the
last good data stays, and its age (which the readers turn into "unknown") keeps growing.
`refresh_due` runs whichever feeds are due, so the engine can call it on any tick; a
failed feed waits a per-feed retry interval before it is tried again.

Why sanity checks before storing: a snapshot feed (the whole market's earnings calendar,
the SEC's lists) replaces what is stored, so a short or garbled response would erase good
data. One that is far smaller than the real thing is refused instead.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, List, Mapping, Optional, Sequence

from ottr.events import db
from ottr.events.corporate import parse_actions
from ottr.events.earnings import AlphaVantage, AlphaVantageError
from ottr.events.fred import Fred, FredError
from ottr.events.sec import Company, Sec, SecError
from ottr.events.view import FEED_CORPORATE, FEED_EARNINGS, FEED_MACRO, FEED_SEC
from ottr.ledger.book import trade_day
from ottr.ledger.store import Store
from ottr.market.alpaca import Alpaca, AlpacaError

logger = logging.getLogger(__name__)

# The whole market's calendar runs to thousands of rows (4,503 for 3 months on
# 2026-10-21) and the SEC lists to tens of thousands; far fewer means a bad response.
MIN_EARNINGS_ROWS = 500
MIN_SEC_LISTINGS = 5000
MIN_SEC_FUNDS = 10000
COMPANY_TTL_DAYS = 90
# After a failure, wait this long before trying the feed again. Alpha Vantage's is long:
# each try spends one of the free key's 25 daily calls.
RETRY_SEC = {FEED_MACRO: 1800, FEED_EARNINGS: 7200, FEED_CORPORATE: 900, FEED_SEC: 21600}


@dataclass(frozen=True)
class FeedResult:
    feed: str
    ok: bool
    detail: str
    rows: int = 0


@dataclass
class Sources:
    """The feed clients. A None one isn't configured (no key): its feed records why."""
    fred: Optional[Fred] = None
    alpha_vantage: Optional[AlphaVantage] = None
    sec: Optional[Sec] = None
    alpaca: Optional[Alpaca] = None


def _done(store: Store, feed: str, ok: bool, detail: str, now: float, rows: int = 0) -> FeedResult:
    db.record_feed(store, feed, ok, detail, now, rows if ok else None)
    (logger.info if ok else logger.warning)("events feed %s: %s", feed, detail)
    return FeedResult(feed, ok, detail, rows)


async def refresh_macro(store: Store, fred: Fred, series: Mapping[str, str], today: date,
                        history_days: int, now: float) -> FeedResult:
    """Every series, one request each; a failing series doesn't stop the others, but the
    feed only counts as ok when all of them came back. An outage (FRED down for
    maintenance) stops the run at the first series instead of retrying each one."""
    start = today - timedelta(days=history_days)
    ids = list(series)
    good, rows, bad = 0, 0, []
    for i, sid in enumerate(ids):
        try:
            obs = await fred.series(sid, start)
        except FredError as e:
            bad.append(f"{sid}: {e}")
            if e.transient and ids[i + 1:]:
                bad.append(f"skipped {', '.join(ids[i + 1:])} (FRED unavailable)")
                break
            continue
        if not obs:
            bad.append(f"{sid}: no observations since {start}")
            continue
        db.save_macro(store, sid, obs, start)
        good, rows = good + 1, rows + len(obs)
    detail = f"{good}/{len(ids)} series, {rows} observations" + (f"; {'; '.join(bad)}" if bad else "")
    return _done(store, FEED_MACRO, not bad, detail, now, rows)


async def refresh_earnings(store: Store, av: Optional[AlphaVantage], horizon: str, today: date,
                           now: float, min_rows: int = MIN_EARNINGS_ROWS) -> FeedResult:
    if av is None:
        return _done(store, FEED_EARNINGS, False, "ALPHAVANTAGE_API_KEY is not set", now)
    try:
        dates = await av.earnings_calendar(horizon)
    except AlphaVantageError as e:
        return _done(store, FEED_EARNINGS, False, str(e), now)
    upcoming = [d for d in dates if d.day >= today]
    if len(upcoming) < min_rows:
        return _done(store, FEED_EARNINGS, False, f"only {len(upcoming)} upcoming report dates (expected thousands); "
                     "kept the previous calendar", now)
    n = db.save_earnings(store, upcoming, today, now)
    last = max(d.day for d in upcoming)
    return _done(store, FEED_EARNINGS, True, f"{n} report dates through {last} ({horizon})", now, n)


async def refresh_corporate_actions(store: Store, alpaca: Optional[Alpaca], symbols: Sequence[str],
                                    today: date, now: float, lookback_days: int, ahead_days: int) -> FeedResult:
    if alpaca is None:
        return _done(store, FEED_CORPORATE, False, "no Alpaca keys (APCA_API_KEY_ID / APCA_API_SECRET_KEY)", now)
    if not symbols:
        return _done(store, FEED_CORPORATE, False, "no symbols to check", now)
    start, end = today - timedelta(days=lookback_days), today + timedelta(days=ahead_days)
    try:
        payload = await alpaca.corporate_actions(symbols, start, end)
    except AlpacaError as e:
        return _done(store, FEED_CORPORATE, False, str(e), now)
    actions, problems = parse_actions(payload)
    changes = db.save_corporate_actions(store, actions, symbols, start, end, now)
    if changes:
        store.audit("corporate_actions_changed",
                    new=[f"{a.symbol} {a.describe()} ({a.id})" for a in changes.new],
                    changed=[f"{a.symbol} {a.describe()} ({a.id})" for a in changes.changed],
                    withdrawn=changes.withdrawn)
    for p in problems:
        logger.warning("corporate actions: %s", p)
    wanted = set(symbols)
    kept = sum(1 for a in actions if a.symbol in wanted)
    detail = (f"{len(symbols)} symbols, {kept} actions ({len(changes.new)} new, {len(changes.changed)} changed, "
              f"{len(changes.withdrawn)} withdrawn)" + (f"; {'; '.join(problems)}" if problems else ""))
    return _done(store, FEED_CORPORATE, True, detail, now, kept)


async def refresh_sec(store: Store, sec: Optional[Sec], now: float, min_listings: int = MIN_SEC_LISTINGS,
                      min_funds: int = MIN_SEC_FUNDS) -> FeedResult:
    if sec is None:
        return _done(store, FEED_SEC, False, "SEC_USER_AGENT is not set (the SEC wants 'Name email')", now)
    try:
        listings = await sec.listings()
        funds = await sec.fund_symbols()
    except SecError as e:
        return _done(store, FEED_SEC, False, str(e), now)
    if len(listings) < min_listings or len(funds) < min_funds:
        return _done(store, FEED_SEC, False, f"only {len(listings)} listings and {len(funds)} fund tickers; "
                     "kept the previous lists", now)
    db.save_sec_lists(store, listings, funds)
    return _done(store, FEED_SEC, True, f"{len(listings)} listed companies, {len(funds)} fund tickers", now,
                 len(listings) + len(funds))


async def company_profile(store: Store, sec: Sec, cik: int, now: float,
                          ttl_days: float = COMPANY_TTL_DAYS) -> Company:
    """A filer's SEC profile, from the cache while it is younger than `ttl_days`. Entity
    types and SIC codes almost never change, so the screener re-reads each one rarely."""
    cached = db.sec_company(store, cik)
    if cached and now - cached[1] < ttl_days * 86400:
        return cached[0]
    co = await sec.company(cik)
    db.save_company(store, co, now)
    return co


def due(store: Store, feed: str, now: float, every_sec: float) -> bool:
    """Whether `feed` should run now: never run, its last success is `every_sec` old, or
    its last attempt failed and the feed's retry wait has passed."""
    f = db.feed(store, feed)
    if f is None:
        return True
    if not f.ok:
        return now - f.ts >= RETRY_SEC.get(feed, 900)
    return f.ok_ts is None or now - f.ok_ts >= every_sec


async def refresh_due(cfg, store: Store, sources: Sources, symbols: Sequence[str], now: float) -> List[FeedResult]:
    """Run every feed that is due. `symbols` are the ones whose corporate actions matter
    (every member and every held asset)."""
    e = cfg.events
    today = trade_day(now)
    out: List[FeedResult] = []
    plan: Dict[str, float] = {FEED_MACRO: e.macro_every_hours * 3600, FEED_EARNINGS: e.earnings_every_hours * 3600,
                              FEED_CORPORATE: e.corporate_actions_every_hours * 3600, FEED_SEC: e.sec_every_days * 86400}
    for feed, every in plan.items():
        run = due(store, feed, now, every)
        if not run and feed == FEED_CORPORATE:
            # A symbol that just joined the universe has unknown actions (its BUYs are
            # blocked) until it is fetched, so don't make it wait for the next interval.
            f = db.feed(store, feed)
            run = (f is None or f.ok) and any(
                (db.corporate_actions_checked(store, s) or 0) < now - every for s in symbols)
        if not run:
            continue
        if feed == FEED_MACRO:
            if sources.fred is None:
                out.append(_done(store, feed, False, "no FRED client", now))
            else:
                out.append(await refresh_macro(store, sources.fred, e.macro, today, e.macro_history_days, now))
        elif feed == FEED_EARNINGS:
            out.append(await refresh_earnings(store, sources.alpha_vantage, e.earnings_horizon, today, now))
        elif feed == FEED_CORPORATE:
            out.append(await refresh_corporate_actions(store, sources.alpaca, symbols, today, now,
                                                       e.corporate_actions_lookback_days, e.corporate_actions_ahead_days))
        else:
            out.append(await refresh_sec(store, sources.sec, now))
    return out
