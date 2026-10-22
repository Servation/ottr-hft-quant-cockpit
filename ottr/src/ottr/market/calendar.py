"""
US market calendar: sessions, session state, and the council's meeting schedule.

What it does: `TradingCalendar` holds the regular sessions (date, open, close in New York
time) and answers "is the market open at t?", "when is the next open?", "which sessions
lie between these dates?", and "which sessions are missing from this bar series?".
`meeting_slots` / `next_meeting_at` place the council's meetings (10:15 and 15:15 ET by
default) on trading days, moving a slot earlier on an early-close day.

Why its own module: crypto never closed, so nothing in OTTR knew about time of day. US
markets close nights, weekends and holidays, close early around holidays, and shift by an
hour across DST. Every rule here works in America/New_York wall time and converts to UTC
only at the edges, so DST and early closes fall out of the data instead of special cases.
Sessions come from a source of truth (Alpaca's /v2/calendar) rather than a hand-kept
holiday list. A timestamp outside the loaded range is an error, never a guess: unknown
must not read as "open".
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from enum import Enum
from typing import Iterable, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
REGULAR_CLOSE = dtime(16, 0)


class CalendarError(RuntimeError):
    """The calendar can't answer: the time is outside the loaded sessions."""


@dataclass(frozen=True)
class Session:
    """One regular trading session. `open` and `close` are aware New York datetimes."""

    day: date
    open: datetime
    close: datetime
    # When a trade made in this session settles (T+1 business day). It comes from the
    # exchange calendar rather than "the next session" because banks close on days the
    # market is open (Columbus Day, Veterans Day), and trades don't settle on those.
    settles: Optional[date] = None

    def __post_init__(self) -> None:
        if self.open.tzinfo is None or self.close.tzinfo is None:
            raise ValueError("session times must be timezone-aware")
        if not self.open < self.close:
            raise ValueError(f"{self.day}: open must be before close")
        if self.settles is not None and self.settles <= self.day:
            raise ValueError(f"{self.day}: settlement must come after the trade date")

    @property
    def open_ts(self) -> float:
        return self.open.timestamp()

    @property
    def close_ts(self) -> float:
        return self.close.timestamp()

    @property
    def early_close(self) -> bool:
        return self.close.astimezone(ET).time() < REGULAR_CLOSE

    def contains(self, ts: float) -> bool:
        """Open from the opening bell up to (not including) the close."""
        return self.open_ts <= ts < self.close_ts

    @classmethod
    def at(cls, day: date, open_hm: str = "09:30", close_hm: str = "16:00",
           settles: Optional[date] = None) -> "Session":
        """A session from New York wall-clock times ("HH:MM"), as exchange calendars list them."""
        return cls(day, _et(day, open_hm), _et(day, close_hm), settles)


def _et(day: date, hm: str) -> datetime:
    h, m = (int(x) for x in hm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=ET)


class SessionState(Enum):
    PRE_OPEN = "pre_open"      # a trading day, before the bell
    OPEN = "open"
    POST_CLOSE = "post_close"  # a trading day, after the close
    CLOSED = "closed"          # a weekend or holiday


@dataclass(frozen=True)
class MarketTime:
    """Where `ts` falls: the state, today's session if any, and the neighbors."""

    ts: float
    state: SessionState
    session: Optional[Session]        # today's session (None on weekends and holidays)
    next_open: Optional[Session]      # the next session that hasn't opened yet
    last_close: Optional[Session]     # the most recent session that has closed

    @property
    def is_open(self) -> bool:
        return self.state is SessionState.OPEN

    def seconds_to_close(self) -> Optional[float]:
        return self.session.close_ts - self.ts if self.is_open and self.session else None


class TradingCalendar:
    """Regular sessions over a date range. Days in the range with no session are closed."""

    def __init__(self, sessions: Iterable[Session], first_day: Optional[date] = None,
                 last_day: Optional[date] = None) -> None:
        ss = sorted(sessions, key=lambda s: s.day)
        days = [s.day for s in ss]
        if len(set(days)) != len(days):
            raise ValueError("duplicate session dates")
        if not ss:
            raise ValueError("a calendar needs at least one session")
        self.sessions: Tuple[Session, ...] = tuple(ss)
        self._days = days
        self._opens = [s.open_ts for s in ss]
        self._closes = [s.close_ts for s in ss]
        # Coverage: the days this calendar can speak for. Defaults to its own sessions;
        # a source that lists a range (with holidays inside it) can say so explicitly.
        self.first_day = min(first_day or days[0], days[0])
        self.last_day = max(last_day or days[-1], days[-1])

    @classmethod
    def from_rows(cls, rows: Iterable[Mapping[str, str]], first_day: Optional[date] = None,
                  last_day: Optional[date] = None) -> "TradingCalendar":
        """From exchange-calendar rows: {"date": "2026-11-27", "open": "09:30", "close": "13:00"}
        (the shape of Alpaca's /v2/calendar)."""
        sessions = [Session.at(date.fromisoformat(r["date"]), r["open"], r["close"],
                               date.fromisoformat(r["settlement_date"]) if r.get("settlement_date") else None)
                    for r in rows]
        return cls(sessions, first_day, last_day)

    # -- lookups ---------------------------------------------------------------------------
    def covers_day(self, day: date) -> bool:
        return self.first_day <= day <= self.last_day

    def _check(self, ts: float) -> date:
        day = datetime.fromtimestamp(ts, tz=ET).date()
        if not self.covers_day(day):
            raise CalendarError(f"{day} is outside the calendar ({self.first_day} to {self.last_day})")
        return day

    def session_on(self, day: date) -> Optional[Session]:
        if not self.covers_day(day):
            raise CalendarError(f"{day} is outside the calendar ({self.first_day} to {self.last_day})")
        i = bisect.bisect_left(self._days, day)
        return self.sessions[i] if i < len(self._days) and self._days[i] == day else None

    def is_trading_day(self, day: date) -> bool:
        return self.session_on(day) is not None

    def state_at(self, ts: float) -> MarketTime:
        day = self._check(ts)
        today = self.session_on(day)
        if today is None:
            state = SessionState.CLOSED
        elif ts < today.open_ts:
            state = SessionState.PRE_OPEN
        elif ts < today.close_ts:
            state = SessionState.OPEN
        else:
            state = SessionState.POST_CLOSE
        i = bisect.bisect_right(self._opens, ts)
        nxt = self.sessions[i] if i < len(self.sessions) else None
        j = bisect.bisect_right(self._closes, ts) - 1
        last = self.sessions[j] if j >= 0 else None
        return MarketTime(ts, state, today, nxt, last)

    def is_open(self, ts: float) -> bool:
        return self.state_at(ts).is_open

    def sessions_between(self, start: date, end: date) -> List[Session]:
        """Sessions with start <= day <= end (both inclusive)."""
        lo = bisect.bisect_left(self._days, start)
        hi = bisect.bisect_right(self._days, end)
        return list(self.sessions[lo:hi])

    def completed_sessions(self, ts: float, n: int) -> List[Session]:
        """The last `n` sessions that have closed by `ts`, oldest first. Windows that say
        "20 sessions" count these, so holidays never shorten a window."""
        self._check(ts)
        j = bisect.bisect_right(self._closes, ts)
        return list(self.sessions[max(0, j - n):j])

    def settlement_date(self, trade_day: date) -> date:
        """When a trade made on `trade_day` settles. Raises if the calendar doesn't say: a
        guessed settlement date would let a cash account spend money it doesn't have yet."""
        s = self.session_on(trade_day)
        if s is None:
            raise CalendarError(f"{trade_day} is not a trading day")
        if s.settles is None:
            raise CalendarError(f"{trade_day}: the calendar has no settlement date")
        return s.settles

    def missing_days(self, have: Iterable[date], start: date, end: date) -> List[date]:
        """Trading days in [start, end] with no entry in `have`: gaps in a daily bar series
        measured against the calendar, so a holiday is never mistaken for a gap."""
        got = set(have)
        return [s.day for s in self.sessions_between(start, end) if s.day not in got]


# -- the council's schedule -----------------------------------------------------------------
DEFAULT_MEETING_TIMES: Tuple[str, ...] = ("10:15", "15:15")


def meeting_slots(session: Session, times: Sequence[str] = DEFAULT_MEETING_TIMES,
                  min_before_close_min: int = 45) -> List[datetime]:
    """When the council meets during `session`, as New York datetimes.

    Each slot must leave `min_before_close_min` before the close, so the chair's trades
    still have time to fill. On an early close (13:00) a slot past that limit moves back to
    it (15:15 becomes 12:15); slots that end up equal collapse into one.
    """
    latest = session.close - timedelta(minutes=min_before_close_min)
    slots = []
    for hm in times:
        t = _et(session.day, hm)
        if t < session.open:
            raise ValueError(f"meeting time {hm} is before the open")
        slots.append(min(t, latest))
    return sorted(set(s for s in slots if s >= session.open))


def next_meeting_at(cal: TradingCalendar, ts: float, times: Sequence[str] = DEFAULT_MEETING_TIMES,
                    min_before_close_min: int = 45) -> float:
    """Epoch time of the first council slot strictly after `ts`.

    Raises CalendarError when the calendar ends before the next slot: the engine must
    refresh its calendar, not skip ahead or meet on a day it can't vouch for.
    """
    cal._check(ts)
    day = datetime.fromtimestamp(ts, tz=ET).date()
    for s in cal.sessions_between(day, cal.last_day):
        for slot in meeting_slots(s, times, min_before_close_min):
            if slot.timestamp() > ts:
                return slot.timestamp()
    raise CalendarError(f"no council slot after {datetime.fromtimestamp(ts, tz=ET)} within the calendar")
