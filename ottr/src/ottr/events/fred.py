"""
FRED macro series through the keyless CSV download (fredgraph.csv). GET only, no key.

What it does: `Fred.series(id, start)` returns the daily observations of one FRED series
from `start` on, as (day, value). Days with no value are left out, never filled in.

Why keyless CSV: it is the same data as FRED's JSON API without a key to manage. One
request per series, so one renamed or retired series fails alone.

Format facts (checked live 2026-10-21): the header is `observation_date,<SERIES ID>`; a
day with no value is an empty cell (DGS10 on Columbus Day, 2026-10-12, when the bond
market is shut); and a site-wide maintenance window is answered with an HTML page and
HTTP 200, which is treated as an outage and retried, never parsed. Older downloads used a
`DATE` header and "." for no value, so both are still read.
"""

from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass
from datetime import date
from typing import List, Optional

import httpx

from ottr.market import http

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"
DATE_HEADERS = ("observation_date", "date")
MISSING = ("", ".")


class FredError(http.HttpError):
    pass


@dataclass(frozen=True)
class Observation:
    day: date
    value: float


def parse_fredgraph(text: str, series_id: str) -> List[Observation]:
    """Observations from one fredgraph.csv, oldest first. FredError if it isn't one."""
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    if not rows:
        raise FredError(f"FRED {series_id}: empty response")
    header = [h.strip().lower() for h in rows[0]]
    if len(header) != 2 or header[0] not in DATE_HEADERS or header[1] != series_id.lower():
        raise FredError(f"FRED {series_id}: unexpected header {rows[0][:3]}")
    out: List[Observation] = []
    for row in rows[1:]:
        if not row:
            continue
        if len(row) != 2:
            raise FredError(f"FRED {series_id}: malformed row {row[:3]}")
        try:
            day = date.fromisoformat(row[0].strip())
        except ValueError:
            raise FredError(f"FRED {series_id}: bad date {row[0][:20]!r}")
        cell = row[1].strip()
        if cell in MISSING:
            continue
        try:
            value = float(cell)
        except ValueError:
            raise FredError(f"FRED {series_id}: bad value {cell[:20]!r} on {day}")
        if not math.isfinite(value):
            raise FredError(f"FRED {series_id}: non-finite value on {day}")
        out.append(Observation(day, value))
    out.sort(key=lambda o: o.day)
    return out


class Fred:
    def __init__(self, client: Optional[httpx.AsyncClient] = None, retries: int = 2,
                 backoff: float = 2.0, base: str = FRED_CSV) -> None:
        self._client, self._retries, self._backoff, self.base = client, retries, backoff, base

    async def series(self, series_id: str, start: date) -> List[Observation]:
        client = self._client or httpx.AsyncClient(timeout=30.0, follow_redirects=True)
        try:
            text = await http.get_json(client, self.base, {"id": series_id, "cosd": start.isoformat()},
                                       classify=http.classify_text, retries=self._retries,
                                       backoff=self._backoff, name=f"FRED {series_id}", error=FredError)
        finally:
            if self._client is None:
                await client.aclose()
        return [o for o in parse_fredgraph(text, series_id) if o.day >= start]
