"""
Clock: the one source of "now", injectable so time-dependent logic is testable.

What it does: `Clock.time()` returns epoch seconds. `SYSTEM` reads the real clock;
`FakeClock` is set and advanced by tests. Why: a US-markets desk branches on sessions,
holidays, early closes and DST. Code that reads `time.time()` inline can only be tested
on the day the test happens to run; code that takes a Clock can be put at 9:29 ET on the
day after Thanksgiving. New modules take a Clock; older call sites move over as the
PRs that rework them land.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def time(self) -> float:
        """Epoch seconds, like time.time()."""


class SystemClock:
    def time(self) -> float:
        return time.time()


class FakeClock:
    """A clock that only moves when told to. Starts at `start` (epoch seconds)."""

    def __init__(self, start: float) -> None:
        self._now = float(start)

    def time(self) -> float:
        return self._now

    def set(self, ts: float) -> None:
        self._now = float(ts)

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("a clock cannot run backwards")
        self._now += seconds


SYSTEM: Clock = SystemClock()


def utc(ts: float) -> datetime:
    """Aware UTC datetime for an epoch timestamp."""
    return datetime.fromtimestamp(ts, tz=timezone.utc)
