"""
The events feeds as the desk reads them: earnings and corporate actions for the gate, and
the macro block for the council packet.

What it does:
- `StoredEvents` implements the desk's `Events` protocol from the database. The sessions
  to a stock's next report come from the stored calendar and the trading calendar (a
  floor on them, when the calendar lists no date yet but the stock has just reported); a
  pending corporate action that blocks a BUY comes back as a one-line description.
- `macro_readings` gives each configured FRED series' latest value, its change over a
  week and a month, and whether it has gone stale.

Why fail closed here: these readers are the last step before the gate. A feed that hasn't
succeeded within `max_age` (or a symbol whose corporate actions were never fetched) says
"unknown", which blocks the BUYs it guards. Missing data never reads as "nothing due".

Why a floor from the last report: Alpha Vantage lists most report dates only a few weeks
ahead, so for most of a quarter a stock that has just reported has no listed date. That
isn't "unknown": the next report covers a quarter that hasn't ended yet, and no company
reports a quarter before it ends. The floor is the earliest day that quarter can end (12
weeks after the last one, the shortest fiscal quarter in use). It only counts when the
last report is one we can vouch for; see `StoredEvents.next_report` for the conditions
and the measurements behind them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable, List, Mapping, Optional

from ottr.events import db
from ottr.events.fred import Observation
from ottr.ledger.book import trade_day
from ottr.ledger.store import Store
from ottr.market.calendar import TradingCalendar
from ottr.risk.gate import EARNINGS_BLACKOUT_SESSIONS

FEED_MACRO = "macro"
FEED_EARNINGS = "earnings"
FEED_CORPORATE = "corporate_actions"
FEED_SEC = "sec_lists"

# The fewest days from one fiscal quarter's end to the next: 12 weeks (companies on a
# 52-week year with a 16-week quarter have three of 12). Alpha Vantage rounds a period end
# to the nearest month end, so the days between two it lists are 89 to 92; 84 leaves room
# for that rounding. Measured on the live calendar (2026-11-11, 4,885 dates): every
# pair of consecutive period ends was 91 or 92 days apart, and no report came less than 8
# days after its own period end, so none came less than 99 days after the one before.
MIN_QUARTER_DAYS = 84


@dataclass(frozen=True)
class NextReport:
    """When a stock next reports, as far as the calendar can say."""
    sessions: int            # sessions to the listed date, or the fewest there can be
    floor: bool = False      # True: no date is listed yet; the report is at least `sessions` away


def sessions_until(cal: TradingCalendar, today: date, day: date) -> int:
    """Sessions after `today` up to and including `day`: 0 for a report today, 1 for one
    on the next session. Past the calendar's range the count is a lower bound (one more
    session at least), so a short calendar errs toward the blackout, never away from it."""
    if day <= today:
        return 0
    n = len(cal.sessions_between(today + timedelta(days=1), min(day, cal.last_day)))
    return n + 1 if day > cal.last_day else n


class StoredEvents:
    """The desk's Events, read from the book (`ottr.engine.desks.Events`)."""

    def __init__(self, store: Store, calendar: Callable[[], TradingCalendar], max_age_sec: float) -> None:
        # `calendar` is a callable so the engine can refresh its calendar without
        # rebuilding this object.
        self.store, self.calendar, self.max_age_sec = store, calendar, max_age_sec

    @classmethod
    def from_config(cls, store: Store, calendar: Callable[[], TradingCalendar], cfg) -> "StoredEvents":
        return cls(store, calendar, cfg.events.max_age_hours * 3600)

    def next_report(self, symbol: str, now: float) -> Optional[NextReport]:
        """`symbol`'s next report, or None when unknown.

        A listed date gives the exact count. With no listed date, the last report gives a
        floor: the next one covers the quarter after it, which can't end sooner than
        MIN_QUARTER_DAYS after the reported one, and a quarter isn't reported before it
        ends. The floor needs all of:
        - a calendar fetched within `max_age` (as for a listed date);
        - a last report whose date carried a time of day. Alpha Vantage adds the time once
          a date is confirmed, a few weeks ahead; without one the date is its estimate,
          and when an estimate passes with no report it lists a new one about a week
          later. On the live calendar, of the dates a week or more past, 161 of 337
          estimates had been listed again like that, against 4 of 1,616 with a time;
        - a calendar fetched after that report's day, so the snapshot we hold had the
          chance to list a postponed date and didn't;
        - a floor still more than the blackout away. Closer than that, all it says is "not
          yet", which is unknown again until the date is listed.
        A stock with no report on record, or only an estimate, stays unknown.
        """
        f = db.feed(self.store, FEED_EARNINGS)
        if f is None or f.ok_ts is None or now - f.ok_ts > self.max_age_sec:
            return None
        today, cal = trade_day(now), self.calendar()
        nxt = db.next_earnings(self.store, symbol, today)
        if nxt is not None:
            return NextReport(sessions_until(cal, today, nxt.day))
        last = db.last_earnings(self.store, symbol, today)
        if last is None or not last.time_of_day or last.fiscal_end is None or trade_day(f.ok_ts) <= last.day:
            return None
        n = sessions_until(cal, today, last.fiscal_end + timedelta(days=MIN_QUARTER_DAYS))
        return NextReport(n, floor=True) if n > EARNINGS_BLACKOUT_SESSIONS else None

    def earnings_sessions(self, symbol: str, now: float) -> Optional[int]:
        """Sessions to `symbol`'s next report (the fewest there can be, when no date is
        listed yet), or None when unknown. The number the gate and the screener test
        against the blackout; `next_report` also says which kind it is."""
        r = self.next_report(symbol, now)
        return None if r is None else r.sessions

    def corp_action(self, symbol: str, now: float) -> Optional[str]:
        """What blocks a BUY of `symbol` (a pending action, or actions unknown), or None."""
        checked = db.corporate_actions_checked(self.store, symbol)
        if checked is None:
            return "corporate actions unknown: never checked"
        if now - checked > self.max_age_sec:
            return f"corporate actions unknown: last checked {(now - checked) / 3600:.0f}h ago"
        for a in db.pending_actions(self.store, symbol, trade_day(now)):
            if a.blocks_buy:
                return a.describe()
        return None


@dataclass(frozen=True)
class MacroReading:
    series: str
    label: str
    value: Optional[float]           # None: no observation at all
    day: Optional[date]              # the observation's date (FRED publishes with a lag)
    change_1w: Optional[float]       # value minus the last value a week before `day`
    change_1m: Optional[float]       # ... a month (30 days) before
    stale: bool                      # no value within the stale window (or none at all)


def _as_of(obs: List[Observation], day: date) -> Optional[Observation]:
    """The last observation on or before `day`."""
    best = None
    for o in obs:
        if o.day > day:
            break
        best = o
    return best


def macro_readings(store: Store, series: Mapping[str, str], today: date, stale_days: int) -> List[MacroReading]:
    out = []
    for sid, label in series.items():
        obs = db.macro_series(store, sid, since=today - timedelta(days=60))
        if not obs:
            out.append(MacroReading(sid, label, None, None, None, None, True))
            continue
        last = obs[-1]
        week, month = _as_of(obs, last.day - timedelta(days=7)), _as_of(obs, last.day - timedelta(days=30))
        out.append(MacroReading(sid, label, last.value, last.day,
                                last.value - week.value if week else None,
                                last.value - month.value if month else None,
                                (today - last.day).days > stale_days))
    return out
