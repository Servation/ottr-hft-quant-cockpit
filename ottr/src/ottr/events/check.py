"""
The events half of `ottr data --check`: are earnings dates, corporate actions, macro series
and the SEC lists available right now? Reads only; stores nothing.

What it does: fetches each feed once, the way the refresh jobs will, and reports what the
desk would conclude: how far the earnings calendar reaches (and a few sample dates), which
corporate actions are pending for the checked symbols (and whether any blocks a BUY),
each macro series' latest value, and how the SEC records classify a few known issuers.
A missing key is reported with what it costs and how to add it.

Exit codes, like the Alpaca half: 0 all good; 1 degraded (macro, earnings or SEC missing:
the desk runs, with stock BUYs or the screener held back); 2 corporate actions
unavailable (every BUY would be blocked as "corporate actions unknown").
"""

from __future__ import annotations

import os
from collections import Counter
from datetime import timedelta
from typing import Callable, Sequence

from ottr.events.corporate import parse_actions
from ottr.events.earnings import AlphaVantageError
from ottr.events.fred import FredError
from ottr.events.refresh import MIN_EARNINGS_ROWS, MIN_SEC_FUNDS, MIN_SEC_LISTINGS, Sources
from ottr.events.sec import SecError, operating_company, user_agent_problem
from ottr.ledger.book import trade_day
from ottr.market.alpaca import AlpacaError
from ottr.market.datacheck import Report

EARNINGS_SAMPLES = ("AAPL", "MSFT", "NVDA")
# Known answers for the SEC classifier: a company, an ETF trust, a commodity trust, a fund.
SEC_SAMPLES = (("AAPL", True), ("SPY", False), ("GLD", False), ("QQQ", False))
AV_KEY_URL = "https://www.alphavantage.co/support/#api-key"


async def run(sources: Sources, events_cfg, symbols: Sequence[str], now: float,
              out: Callable[[str], None] = print) -> int:
    r = Report()
    today = trade_day(now)

    # Macro (FRED, keyless): advisory context for the council.
    if sources.fred is not None:
        ids = list(events_cfg.macro)
        for i, (sid, label) in enumerate(events_cfg.macro.items()):
            try:
                obs = await sources.fred.series(sid, today - timedelta(days=30))
            except FredError as e:
                r.warn(f"macro {sid}: {e}")
                if e.transient and ids[i + 1:]:   # FRED itself is down: don't wait out each series
                    r.note(f"skipped {', '.join(ids[i + 1:])}: FRED is unavailable; try again later")
                    break
                continue
            if not obs:
                r.warn(f"macro {sid}: no value in the last 30 days")
                continue
            last = obs[-1]
            age = (today - last.day).days
            (r.ok if age <= events_cfg.macro_stale_days else r.warn)(
                f"macro {label}: {last.value:g} on {last.day} ({sid})")

    # Earnings (Alpha Vantage): no calendar means no BUY of a single stock.
    av = sources.alpha_vantage
    if av is None:
        r.warn("earnings: ALPHAVANTAGE_API_KEY is not set, so every stock's report date is unknown "
               f"and single-stock BUYs are blocked (free key: {AV_KEY_URL})")
    else:
        try:
            dates = await av.earnings_calendar(events_cfg.earnings_horizon)
        except AlphaVantageError as e:
            r.warn(f"earnings: {e}")
        else:
            upcoming = sorted((d for d in dates if d.day >= today), key=lambda d: d.day)
            msg = (f"earnings: {len(upcoming)} report dates, {upcoming[0].day} to {upcoming[-1].day} "
                   f"({events_cfg.earnings_horizon})" if upcoming else "earnings: no upcoming report dates")
            (r.ok if len(upcoming) >= MIN_EARNINGS_ROWS else r.warn)(msg)
            first = {}
            for d in upcoming:
                first.setdefault(d.symbol, d)
            samples = [f"{s} {first[s].day} {first[s].time_of_day or 'time n/a'}" for s in EARNINGS_SAMPLES if s in first]
            if samples:
                r.note("next reports: " + ", ".join(samples))
            r.note("(each check spends one of the free key's 25 daily calls)")

    # SEC lists: the screener's operating-company test.
    sec = sources.sec
    if sec is None:
        r.warn(f"SEC: {user_agent_problem(os.getenv('SEC_USER_AGENT', ''))}, so the screener can't tell "
               "companies from funds (set it in ottr/.env to your name and email)")
    else:
        try:
            listings = await sec.listings()
            funds = await sec.fund_symbols()
        except SecError as e:
            r.warn(f"SEC: {e}")
        else:
            (r.ok if len(listings) >= MIN_SEC_LISTINGS and len(funds) >= MIN_SEC_FUNDS else r.warn)(
                f"SEC: {len(listings)} listed companies, {len(funds)} fund tickers")
            for sym, expected in SEC_SAMPLES:
                listing = listings.get(sym)
                try:
                    co = await sec.company(listing.cik) if listing and sym not in funds else None
                except SecError as e:
                    r.warn(f"SEC {sym}: {e}")
                    continue
                ok, why = operating_company(sym, listing, funds, co)
                (r.ok if ok == expected else r.warn)(f"SEC {sym}: {why}" if ok else f"SEC {sym}: not a pick: {why}")

    # Corporate actions (Alpaca): unknown blocks every BUY.
    if sources.alpaca is None:
        r.fail("corporate actions: no Alpaca keys, so every BUY would be blocked")
    else:
        start = today - timedelta(days=events_cfg.corporate_actions_lookback_days)
        end = today + timedelta(days=events_cfg.corporate_actions_ahead_days)
        try:
            payload = await sources.alpaca.corporate_actions(symbols, start, end)
        except AlpacaError as e:
            r.fail(f"corporate actions: {e} (every BUY would be blocked)")
        else:
            actions, problems = parse_actions(payload)
            wanted = set(symbols)
            mine = [a for a in actions if a.symbol in wanted]
            kinds = Counter(a.kind for a in mine)
            summary = ", ".join(f"{n} {k.replace('_', ' ')}{'' if n == 1 else 's'}"
                                for k, n in sorted(kinds.items())) or "none"
            r.ok(f"corporate actions: {len(symbols)} symbols, {start} to {end} by process date: {summary}")
            for p in problems:
                r.warn(f"corporate actions: {p}")
            pending = [a for a in mine if a.pending(today)]
            for a in sorted(pending, key=lambda a: (a.symbol, a.effective or today)):
                if a.blocks_buy:
                    r.note(f"{a.symbol}: {a.describe()}: BUYs blocked until it is done")
                elif a.kind == "cash_dividend" and a.ex and a.ex <= today:
                    r.note(f"{a.symbol}: dividend {a.detail.get('rate')}/share, ex {a.ex}, paid {a.pay or 'date n/a'}")
                else:
                    r.note(f"{a.symbol}: {a.describe()}")
    return r.finish(out, "Events feeds")
