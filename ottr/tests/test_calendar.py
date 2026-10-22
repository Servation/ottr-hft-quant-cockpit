"""
Calendar tests: session state through DST, holidays and early closes; session windows and
gap detection; the council's meeting schedule; and the injectable clock.
"""

from datetime import date, datetime, timezone

import pytest

from ottr.clock import FakeClock
from ottr.market.calendar import (ET, CalendarError, Session, SessionState, TradingCalendar,
                                  meeting_slots, next_meeting_at)
from tests.nyse import calendar_2026


def et(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=ET).timestamp()


@pytest.fixture(scope="module")
def cal():
    return calendar_2026()


def test_open_follows_new_york_wall_time_across_dst(cal):
    # 9:30 ET is 14:30 UTC in winter and 13:30 UTC in summer (DST began Sun Mar 8, 2026).
    fri, mon = cal.session_on(date(2026, 3, 6)), cal.session_on(date(2026, 3, 9))
    assert datetime.fromtimestamp(fri.open_ts, tz=timezone.utc).hour == 14
    assert datetime.fromtimestamp(mon.open_ts, tz=timezone.utc).hour == 13
    # And back again after Nov 1.
    assert datetime.fromtimestamp(cal.session_on(date(2026, 11, 2)).open_ts, tz=timezone.utc).hour == 14


@pytest.mark.parametrize("ts,state", [
    (et(2026, 10, 21, 9, 29), SessionState.PRE_OPEN),
    (et(2026, 10, 21, 9, 30), SessionState.OPEN),        # the bell is inclusive
    (et(2026, 10, 21, 15, 59), SessionState.OPEN),
    (et(2026, 10, 21, 16, 0), SessionState.POST_CLOSE),  # the close is not
    (et(2026, 10, 24, 12, 0), SessionState.CLOSED),      # Saturday
    (et(2026, 11, 26, 12, 0), SessionState.CLOSED),      # Thanksgiving
    (et(2026, 11, 27, 12, 59), SessionState.OPEN),       # Black Friday, early close
    (et(2026, 11, 27, 13, 0), SessionState.POST_CLOSE),
])
def test_state_at(cal, ts, state):
    assert cal.state_at(ts).state is state


def test_neighbors_skip_weekends_and_holidays(cal):
    # Wednesday night before Thanksgiving: next open is Friday, last close Wednesday.
    mt = cal.state_at(et(2026, 11, 25, 20, 0))
    assert mt.next_open.day == date(2026, 11, 27) and mt.next_open.early_close
    assert mt.last_close.day == date(2026, 11, 25)
    # Friday Jul 3 (observed holiday): next open is Monday Jul 6.
    assert cal.state_at(et(2026, 7, 2, 17, 0)).next_open.day == date(2026, 7, 6)
    # During a session, "next open" is tomorrow's, not today's.
    assert cal.state_at(et(2026, 10, 21, 11, 0)).next_open.day == date(2026, 10, 22)


def test_seconds_to_close_only_while_open(cal):
    assert cal.state_at(et(2026, 11, 27, 12, 0)).seconds_to_close() == 3600
    assert cal.state_at(et(2026, 11, 27, 14, 0)).seconds_to_close() is None


def test_windows_count_sessions_not_calendar_days(cal):
    # The 5 sessions completed by the Monday after Thanksgiving week's close.
    days = [s.day for s in cal.completed_sessions(et(2026, 11, 30, 16, 0), 5)]
    assert days == [date(2026, 11, 23), date(2026, 11, 24), date(2026, 11, 25),
                    date(2026, 11, 27), date(2026, 11, 30)]
    # Mid-session, today's session isn't complete yet.
    assert cal.completed_sessions(et(2026, 11, 30, 12, 0), 1)[0].day == date(2026, 11, 27)


def test_gaps_are_measured_against_the_calendar(cal):
    have = [date(2026, 11, 23), date(2026, 11, 24), date(2026, 11, 27)]
    # Thanksgiving is not a gap; the 25th is.
    assert cal.missing_days(have, date(2026, 11, 23), date(2026, 11, 27)) == [date(2026, 11, 25)]


def test_outside_the_calendar_is_an_error_not_a_guess(cal):
    with pytest.raises(CalendarError):
        cal.state_at(et(2027, 1, 4, 10, 0))
    with pytest.raises(CalendarError):
        cal.session_on(date(2025, 12, 31))


def test_from_rows_matches_the_exchange_feed_shape():
    cal = TradingCalendar.from_rows([{"date": "2026-11-27", "open": "09:30", "close": "13:00"},
                                     {"date": "2026-11-25", "open": "09:30", "close": "16:00"}],
                                    first_day=date(2026, 11, 25), last_day=date(2026, 11, 27))
    assert [s.day for s in cal.sessions] == [date(2026, 11, 25), date(2026, 11, 27)]
    assert cal.state_at(et(2026, 11, 26, 12, 0)).state is SessionState.CLOSED  # covered, no session


def test_bad_sessions_are_refused():
    d = date(2026, 1, 2)
    with pytest.raises(ValueError):
        TradingCalendar([Session.at(d), Session.at(d)])
    with pytest.raises(ValueError):
        Session.at(d, "16:00", "09:30")


# -- meeting schedule -----------------------------------------------------------------------

def test_two_meetings_on_a_normal_day(cal):
    slots = meeting_slots(cal.session_on(date(2026, 10, 21)))
    assert [s.strftime("%H:%M") for s in slots] == ["10:15", "15:15"]


def test_afternoon_meeting_moves_earlier_on_an_early_close(cal):
    slots = meeting_slots(cal.session_on(date(2026, 11, 27)))
    assert [s.strftime("%H:%M") for s in slots] == ["10:15", "12:15"]  # 45 min before 13:00


def test_next_meeting_rolls_over_nights_weekends_and_holidays(cal):
    def nxt(ts):
        return datetime.fromtimestamp(next_meeting_at(cal, ts), tz=ET).strftime("%Y-%m-%d %H:%M")

    assert nxt(et(2026, 10, 21, 8, 0)) == "2026-10-21 10:15"
    assert nxt(et(2026, 10, 21, 10, 15)) == "2026-10-21 15:15"  # strictly after
    assert nxt(et(2026, 10, 23, 16, 30)) == "2026-10-26 10:15"  # Friday evening -> Monday
    assert nxt(et(2026, 11, 25, 15, 30)) == "2026-11-27 10:15"  # over Thanksgiving
    assert nxt(et(2026, 11, 27, 11, 0)) == "2026-11-27 12:15"   # early close


def test_next_meeting_past_the_calendar_end_is_an_error(cal):
    with pytest.raises(CalendarError):
        next_meeting_at(cal, et(2026, 12, 31, 16, 30))


def test_fake_clock_drives_calendar_questions(cal):
    clock = FakeClock(et(2026, 11, 27, 12, 50))
    assert cal.is_open(clock.time())
    clock.advance(10 * 60)
    assert not cal.is_open(clock.time())
    with pytest.raises(ValueError):
        clock.advance(-1)
