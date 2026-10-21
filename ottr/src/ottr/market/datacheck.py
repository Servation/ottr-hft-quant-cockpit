"""
`ottr data --check`: is Alpaca usable for OTTR right now? Reads only; stores nothing.

What it does: with the keys from ottr/.env it checks, in order, the key type, Alpaca's
clock (and our own clock's skew against it), the calendar (and that it agrees with the
clock), each symbol's asset record (tradable, fractionable), live IEX quotes through the
same `check_quote` the desk will use, and a year of SIP daily bars checked for gaps against
the calendar. It prints one line per finding and returns an exit code.

Why: the US desk can only be as right as its feed. This is the command to run after adding
keys, before a cutover, or when a quote alarm fires, and it reports the same verdicts the
engine will act on rather than raw JSON.

Exit codes: 0 all good, 1 degraded (some symbol has a problem), 2 a feed-level failure
(auth, clock, calendar), 3 no keys.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, List, Sequence

from ottr.market.alpaca import Alpaca, AlpacaError, check_quote, completed_bars
from ottr.market.calendar import ET, CalendarError

# The plan's 16 core ETFs, the default check list until the universe moves into config
# (US plan, PR 3). SGOV is the cash sweep; the rest span equity, rates, credit, real assets.
CORE_ETFS = ("SPY", "QQQ", "IWM", "EFA", "EEM", "SGOV", "IEF", "TLT", "TIP", "LQD", "HYG",
             "GLD", "SLV", "PDBC", "XLE", "VNQ")
HISTORY_DAYS = 400        # calendar days of bars: covers the screener's 273-session minimum
MAX_CLOCK_SKEW_SEC = 5.0  # quote ages are measured on our clock against Alpaca's timestamps


@dataclass
class Report:
    lines: List[str] = field(default_factory=list)
    degraded: bool = False
    failed: bool = False

    def ok(self, msg: str) -> None:
        self.lines.append(f"  ok    {msg}")

    def warn(self, msg: str) -> None:
        self.lines.append(f"  WARN  {msg}")
        self.degraded = True

    def fail(self, msg: str) -> None:
        self.lines.append(f"  FAIL  {msg}")
        self.failed = True

    def note(self, msg: str) -> None:
        self.lines.append(f"        {msg}")

    @property
    def exit_code(self) -> int:
        return 2 if self.failed else 1 if self.degraded else 0


def _et(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=ET).strftime("%a %Y-%m-%d %H:%M ET")


async def run(alpaca: Alpaca, symbols: Sequence[str], now: float,
              out: Callable[[str], None] = print) -> int:
    r = Report()
    kind = alpaca.creds.kind
    if kind == "paper":
        r.ok("keys: paper account")
    elif kind == "live":
        r.warn("keys: a LIVE account key. OTTR only reads data with it, but use a paper key")
    else:
        r.warn("keys: unrecognized key ID prefix (expected PK... for paper)")

    today = datetime.fromtimestamp(now, tz=ET).date()  # New York's date, not this PC's
    try:
        clk = await alpaca.clock()
        cal = await alpaca.calendar(today - timedelta(days=HISTORY_DAYS), today + timedelta(days=14))
    except AlpacaError as e:
        r.fail(str(e))
        return _finish(r, out)

    skew = now - clk.ts
    (r.ok if abs(skew) <= MAX_CLOCK_SKEW_SEC else r.warn)(
        f"clock: market {'OPEN' if clk.is_open else 'closed'}; our clock is {skew:+.1f}s vs Alpaca's")
    try:
        mt = cal.state_at(clk.ts)
    except CalendarError as e:
        r.fail(f"calendar does not cover Alpaca's clock: {e}")
        return _finish(r, out)
    if mt.is_open != clk.is_open:
        r.fail(f"clock says {'open' if clk.is_open else 'closed'} but the calendar says {mt.state.value}: "
               "the desk would treat the market as closed")
    else:
        nxt = mt.next_open
        r.ok(f"calendar agrees ({mt.state.value}); next open {_et(nxt.open_ts) if nxt else 'beyond range'}")
    upcoming = [s for s in cal.sessions if s.open_ts > now][:10]
    early = [s.day.isoformat() for s in upcoming if s.early_close]
    r.note(f"next {len(upcoming)} sessions loaded" + (f"; early closes: {', '.join(early)}" if early else ""))

    # Assets: one request per symbol keeps this light (the full list is thousands of rows).
    for sym in symbols:
        try:
            a = await alpaca.asset(sym)
        except AlpacaError as e:
            r.warn(f"{sym}: asset lookup failed ({e})")
            continue
        if not a.tradable or a.status != "active":
            r.warn(f"{sym}: not tradable on Alpaca (status {a.status})")
        else:
            r.ok(f"{sym}: {a.name[:40]} on {a.exchange}, {'fractional' if a.fractionable else 'whole shares only'}")

    # Quotes: the same verdicts the desk will act on. Outside a session every IEX quote is
    # old, so that is informational there, a problem only while the market is open.
    try:
        snaps = await alpaca.snapshots(symbols)
    except AlpacaError as e:
        r.fail(f"snapshots: {e}")
        snaps = {}
    for sym in symbols:
        q, why = check_quote(sym, snaps.get(sym), now)
        if q is not None:
            spread = (q.ask - q.bid) / q.mid * 100
            r.ok(f"{sym}: IEX {q.bid:,.2f} x {q.ask:,.2f} (spread {spread:.3f}%, {now - q.ts:.0f}s old)")
        elif clk.is_open:
            r.warn(f"{sym}: no usable quote ({why}); orders would defer")
        else:
            r.note(f"{sym}: {why} (expected while the market is closed)")

    # Daily bars: a year of completed sessions, gaps measured against the calendar.
    start = today - timedelta(days=HISTORY_DAYS)
    try:
        bars = await alpaca.daily_bars(symbols, start, now=now)
    except AlpacaError as e:
        r.fail(f"daily bars: {e}")
        return _finish(r, out)
    for sym in symbols:
        done = completed_bars(bars.get(sym, []), cal, now)
        if not done:
            r.warn(f"{sym}: no daily bars")
            continue
        missing = cal.missing_days([b.day for b in done], done[0].day, done[-1].day)
        msg = f"{sym}: {len(done)} sessions {done[0].day} to {done[-1].day}"
        if missing:
            r.warn(f"{msg}, {len(missing)} missing ({', '.join(d.isoformat() for d in missing[:3])}"
                   f"{'...' if len(missing) > 3 else ''})")
        else:
            r.ok(msg)
    return _finish(r, out)


def _finish(r: Report, out: Callable[[str], None]) -> int:
    for line in r.lines:
        out(line)
    verdict = {0: "Alpaca data OK", 1: "Alpaca data usable, with problems above",
               2: "Alpaca data NOT usable"}[r.exit_code]
    out(f"{verdict} (exit {r.exit_code})")
    return r.exit_code
