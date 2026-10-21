"""
A fake NYSE calendar for tests: the 2026 regular sessions with real holidays, early closes
and DST switches. Production loads sessions from Alpaca's /v2/calendar instead.

Covers what the calendar code must get right: DST (Mar 8 and Nov 1, 2026), weekday
holidays (Good Friday, Juneteenth on a Friday, Independence Day observed on Fri Jul 3,
Thanksgiving), and the 13:00 early closes on Black Friday and Christmas Eve.
"""

from datetime import date, timedelta

from ottr.market.calendar import Session, TradingCalendar

HOLIDAYS_2026 = {
    date(2026, 1, 1),    # New Year's Day
    date(2026, 1, 19),   # Martin Luther King Jr. Day
    date(2026, 2, 16),   # Washington's Birthday
    date(2026, 4, 3),    # Good Friday
    date(2026, 5, 25),   # Memorial Day
    date(2026, 6, 19),   # Juneteenth
    date(2026, 7, 3),    # Independence Day (observed; Jul 4 is a Saturday)
    date(2026, 9, 7),    # Labor Day
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 12, 25),  # Christmas
}
EARLY_CLOSES_2026 = {date(2026, 11, 27), date(2026, 12, 24)}  # 13:00 ET


def sessions_2026():
    d, out = date(2026, 1, 1), []
    while d.year == 2026:
        if d.weekday() < 5 and d not in HOLIDAYS_2026:
            out.append(Session.at(d, "09:30", "13:00" if d in EARLY_CLOSES_2026 else "16:00"))
        d += timedelta(days=1)
    return out


def calendar_2026() -> TradingCalendar:
    return TradingCalendar(sessions_2026(), first_day=date(2026, 1, 1), last_day=date(2026, 12, 31))
