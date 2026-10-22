"""
The events feeds as the desk reads them: earnings and corporate actions for the gate, and
the macro block for the council packet.

What it does:
- `StoredEvents` implements the desk's `Events` protocol from the database. The sessions
  to a stock's next report come from the stored calendar and the trading calendar; a
  pending corporate action that blocks a BUY comes back as a one-line description.
- `macro_readings` gives each configured FRED series' latest value, its change over a
  week and a month, and whether it has gone stale.

Why fail closed here: these readers are the last step before the gate. A feed that hasn't
succeeded within `max_age` (or a symbol whose corporate actions were never fetched) says
"unknown", which blocks the BUYs it guards. Missing data never reads as "nothing due".
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

FEED_MACRO = "macro"
FEED_EARNINGS = "earnings"
FEED_CORPORATE = "corporate_actions"
FEED_SEC = "sec_lists"


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

    def _fresh(self, name: str, now: float) -> bool:
        f = db.feed(self.store, name)
        return f is not None and f.ok_ts is not None and now - f.ok_ts <= self.max_age_sec

    def earnings_sessions(self, symbol: str, now: float) -> Optional[int]:
        """Sessions to `symbol`'s next report, or None when unknown (no date, or a stale
        calendar)."""
        if not self._fresh(FEED_EARNINGS, now):
            return None
        today = trade_day(now)
        nxt = db.next_earnings(self.store, symbol, today)
        return None if nxt is None else sessions_until(self.calendar(), today, nxt.day)

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
