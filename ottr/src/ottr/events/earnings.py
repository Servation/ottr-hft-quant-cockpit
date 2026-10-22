"""
Earnings calendar from Alpha Vantage: every US company's expected report dates, one call.

What it does: `AlphaVantage.earnings_calendar(horizon)` downloads EARNINGS_CALENDAR (CSV)
and returns one EarningsDate per (symbol, report day), with the time of day when Alpha
Vantage gives it ("pre-market", "post-market", or "" for unknown).

Why one call a day: the free key allows 25 requests a day, and the whole market's calendar
is a single request. The desk only needs the next report per held or wanted stock; the
gate blocks a single stock's BUY within 5 sessions of it, or when no date is known.

Why the 6-month horizon by default: a company that has just reported is next due about
13 weeks later, right at the edge of a 3-month window. With 3 months it would drop out
of the calendar (and so be blocked as "unknown") exactly when buying it is safest.

Format facts (checked 2026-10-21 with the documented demo key): columns symbol, name,
reportDate, fiscalDateEnding, estimate, currency, timeOfTheDay; share classes are written
with a dot (BRK.B). Errors come back with HTTP 200: as JSON ({"Information": ...}), or as
the CSV header followed by the message's first letters split into cells ("I,n,f,o,r,m,a").
Both are refused here, and the rate-limit message quotes the API key, so every message is
redacted before it can reach a log.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import math
import os
from dataclasses import dataclass
from datetime import date
from typing import List, Optional

import httpx

from ottr.market import http
from ottr.market.symbols import normalize

logger = logging.getLogger(__name__)

ALPHA_VANTAGE = "https://www.alphavantage.co/query"
HORIZONS = ("3month", "6month", "12month")
TIMES_OF_DAY = ("pre-market", "post-market")
REQUIRED_COLUMNS = ("symbol", "reportDate")


class AlphaVantageError(http.HttpError):
    pass


@dataclass(frozen=True)
class EarningsDate:
    symbol: str
    day: date
    time_of_day: str = ""              # pre-market, post-market, or "" (not given)
    fiscal_end: Optional[date] = None
    estimate: Optional[float] = None   # consensus EPS, in the report's currency


def _date(raw: str) -> Optional[date]:
    try:
        return date.fromisoformat(raw.strip())
    except (ValueError, AttributeError):
        return None


def _float(raw: str) -> Optional[float]:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def parse_calendar(text: str) -> List[EarningsDate]:
    """EarningsDates from an EARNINGS_CALENDAR CSV. AlphaVantageError if it isn't one.

    Rows that aren't a US ticker with a report date are skipped (a few foreign or odd
    symbols are normal); a calendar with no usable row at all is an error message in
    disguise, never an empty calendar.
    """
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    cols = reader.fieldnames or []
    if any(c not in cols for c in REQUIRED_COLUMNS):
        raise AlphaVantageError(f"Alpha Vantage: unexpected calendar columns {cols[:8]}")
    out: List[EarningsDate] = []
    skipped = 0
    for row in reader:
        day = _date(row.get("reportDate") or "")
        try:
            symbol = normalize(row.get("symbol") or "")
        except ValueError:
            symbol = None
        if day is None or symbol is None:
            skipped += 1
            continue
        tod = (row.get("timeOfTheDay") or "").strip().lower()
        out.append(EarningsDate(symbol, day, tod if tod in TIMES_OF_DAY else "",
                                _date(row.get("fiscalDateEnding") or ""), _float(row.get("estimate"))))
    if not out:
        raise AlphaVantageError("Alpha Vantage sent no usable calendar rows (most likely an error "
                                "message in place of the CSV: the daily limit, or a bad key)")
    if skipped:
        logger.info("earnings calendar: skipped %d rows that weren't a US ticker with a date", skipped)
    return out


class AlphaVantage:
    def __init__(self, api_key: str, client: Optional[httpx.AsyncClient] = None, retries: int = 2,
                 backoff: float = 5.0, base: str = ALPHA_VANTAGE) -> None:
        if not api_key:
            raise ValueError("an Alpha Vantage API key is required")
        self._key = api_key
        self._client, self._retries, self._backoff, self.base = client, retries, backoff, base

    @classmethod
    def from_env(cls, **kw) -> Optional["AlphaVantage"]:
        key = os.getenv("ALPHAVANTAGE_API_KEY", "").strip()
        return cls(key, **kw) if key else None

    def __repr__(self) -> str:  # never print the key
        return "AlphaVantage(key=***)"

    def _redact(self, text: str) -> str:
        return text.replace(self._key, "***")

    def _classify(self, resp: httpx.Response) -> http.Classified:
        text, err, retryable = http.classify_text(resp)
        if err is None and text.lstrip().startswith("{"):
            # A JSON body from a CSV endpoint is always a message: the daily limit, a bad
            # key or a bad call. Not retried: a retry spends another of the 25 daily calls.
            try:
                msg = " ".join(str(v) for v in json.loads(text).values())
            except (ValueError, AttributeError):
                msg = text
            return None, f"Alpha Vantage refused: {self._redact(msg)[:300]}", False
        return text, err, retryable

    async def earnings_calendar(self, horizon: str = "6month") -> List[EarningsDate]:
        if horizon not in HORIZONS:
            raise ValueError(f"horizon must be one of {HORIZONS}")
        client = self._client or httpx.AsyncClient(timeout=60.0)
        try:
            # The key travels as a query parameter (Alpha Vantage has no header auth). get_json
            # never puts URLs in its errors, and the CLI keeps httpx's request log at WARNING.
            text = await http.get_json(client, self.base,
                                       {"function": "EARNINGS_CALENDAR", "horizon": horizon, "apikey": self._key},
                                       classify=self._classify, retries=self._retries, backoff=self._backoff,
                                       name="Alpha Vantage earnings calendar", error=AlphaVantageError)
        finally:
            if self._client is None:
                await client.aclose()
        return parse_calendar(text)
