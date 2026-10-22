"""
Alpaca market-data client: clock, calendar, assets, snapshots, daily bars and corporate
actions. GET only.

What it does: reads the US market through Alpaca's free plan. The trading API (paper host
only) gives the clock, the exchange calendar and the asset list. The data API gives live
IEX quotes (snapshots), SIP daily bars and corporate actions (read by
`ottr.events.corporate`). `check_quote` turns a snapshot into a Quote only
when the quote is sane; otherwise it says why, so the caller defers instead of guessing.

Why GET only, paper host only: OTTR simulates its own fills, so it never needs to send an
order. A client that can only GET, and only from the paper trading host, can't place a
real trade even if a live key is pasted into ottr/.env by mistake.

Free-plan facts this relies on (docs.alpaca.markets, checked 2026-10-21):
- Snapshots are keyed by symbol; real time only on the IEX feed (a few percent of volume,
  so spreads can look wider than the national best).
- Historical bars default to SIP, but the latest ~15 minutes of SIP data need a paid plan,
  so `daily_bars` ends its request 16 minutes before now.
- `limit` on bars counts bars across ALL symbols; results come sorted by symbol, so pages
  must be followed to the end (`next_page_token`).
- 200 requests a minute: one shared RateLimiter keeps the client under that.
- Daily bars cover the regular session only, closing at the official close (checked live
  2026-10-21: SPY's daily close 773.09 matched the 15:59 bar, not the after-hours 770.92,
  and PDBC's after-hours high/low fell outside its daily range). Daily bars are stamped at
  midnight New York time, written in UTC.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import httpx

from ottr.clock import SYSTEM, Clock
from ottr.market import http
from ottr.market.calendar import ET, TradingCalendar
from ottr.market.symbols import normalize, to_alpaca
from ottr.market.types import BASIS_LIVE, Bar, Quote

logger = logging.getLogger(__name__)

TRADING_PAPER = "https://paper-api.alpaca.markets"
DATA = "https://data.alpaca.markets"
FREE_PLAN_PER_MINUTE = 200
SIP_DELAY = timedelta(minutes=16)   # free plan: SIP data newer than ~15 min is refused
SNAPSHOT_CHUNK = 100                # symbols per snapshot request (keeps URLs short)
BARS_PAGE_LIMIT = 10000             # Alpaca's maximum, counted across all symbols
CORP_ACTIONS_PAGE_LIMIT = 1000      # Alpaca's maximum, counted across all symbols and types


class AlpacaError(http.HttpError):
    pass


@dataclass(frozen=True)
class Credentials:
    key_id: str
    secret: str

    @classmethod
    def from_env(cls) -> Optional["Credentials"]:
        k, s = os.getenv("APCA_API_KEY_ID", "").strip(), os.getenv("APCA_API_SECRET_KEY", "").strip()
        return cls(k, s) if k and s else None

    @property
    def kind(self) -> str:
        """Alpaca key IDs start with PK (paper) or AK (live)."""
        return {"PK": "paper", "AK": "live"}.get(self.key_id[:2].upper(), "unknown")

    def headers(self) -> Dict[str, str]:
        return {"APCA-API-KEY-ID": self.key_id, "APCA-API-SECRET-KEY": self.secret}

    def __repr__(self) -> str:  # never print the secret
        return f"Credentials(key_id={self.key_id[:4]}..., kind={self.kind})"


@dataclass(frozen=True)
class MarketClock:
    ts: float
    is_open: bool
    next_open: float
    next_close: float


@dataclass(frozen=True)
class Asset:
    symbol: str
    name: str
    exchange: str
    tradable: bool
    fractionable: bool
    status: str


# -- parsing -------------------------------------------------------------------------------
def parse_ts(s: str) -> float:
    """RFC-3339 with Alpaca's nanosecond fractions ("...59.123456789Z") -> epoch seconds."""
    s = s.strip().replace("Z", "+00:00")
    main, sep, rest = s.partition(".")
    if sep:  # trim the fraction to microseconds, keeping any offset after it
        n = len(rest) - len(rest.lstrip("0123456789"))
        digits, offset = rest[:n], rest[n:]
        s = f"{main}.{digits[:6].ljust(6, '0')}{offset}"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def bar_day(t: str) -> date:
    """The session date of a daily bar, read in New York time (Alpaca stamps daily bars at
    the session's midnight ET, written in UTC)."""
    return datetime.fromtimestamp(parse_ts(t), tz=ET).date()


def _classify(resp: httpx.Response) -> http.Classified:
    if resp.status_code == 401:
        return None, "HTTP 401: Alpaca rejected the keys (check APCA_API_KEY_ID / APCA_API_SECRET_KEY)", False
    # 403 is NOT always bad keys: Alpaca also uses it for "your plan can't query recent SIP
    # data", so its own message is passed through below.
    result, err, retryable = http.classify_json(resp)
    if err and not retryable:
        try:  # Alpaca explains 4xx errors in {"message": ...}
            err = f"{err}: {resp.json().get('message', '')}".rstrip(": ")
        except ValueError:
            pass
    return result, err, retryable


class Alpaca:
    def __init__(self, creds: Credentials, client: Optional[httpx.AsyncClient] = None,
                 limiter: Optional[http.RateLimiter] = None, retries: int = 3, backoff: float = 1.0,
                 trading_base: str = TRADING_PAPER, data_base: str = DATA, clock: Clock = SYSTEM) -> None:
        # Safety: the only trading host this client will ever talk to is the paper one.
        if trading_base != TRADING_PAPER:
            raise ValueError(f"OTTR reads from Alpaca's paper host only, not {trading_base}")
        self.creds = creds
        self._client = client
        # 10 below the free plan's 200/min, to leave room for clock skew on Alpaca's side.
        self.limiter = limiter or http.RateLimiter(per_minute=FREE_PLAN_PER_MINUTE - 10, capacity=20)
        self._retries, self._backoff = retries, backoff
        self.trading_base, self.data_base = trading_base, data_base
        self._clock = clock

    async def _get(self, base: str, path: str, params: Optional[dict] = None):
        client = self._client or httpx.AsyncClient(timeout=15.0)
        try:
            return await http.get_json(client, f"{base}{path}", params, classify=_classify,
                                       retries=self._retries, backoff=self._backoff, limiter=self.limiter,
                                       headers=self.creds.headers(), name=f"Alpaca {path}", error=AlpacaError)
        finally:
            if self._client is None:
                await client.aclose()

    # -- trading API (paper host) ---------------------------------------------------------
    async def clock(self) -> MarketClock:
        d = await self._get(self.trading_base, "/v2/clock")
        return MarketClock(parse_ts(d["timestamp"]), bool(d["is_open"]),
                           parse_ts(d["next_open"]), parse_ts(d["next_close"]))

    async def calendar(self, start: date, end: date) -> TradingCalendar:
        rows = await self._get(self.trading_base, "/v2/calendar",
                               {"start": start.isoformat(), "end": end.isoformat()})
        if not rows:
            raise AlpacaError(f"Alpaca returned no sessions between {start} and {end}")
        return TradingCalendar.from_rows(rows, first_day=start, last_day=end)

    async def asset(self, symbol: str) -> Asset:
        return _asset(await self._get(self.trading_base, f"/v2/assets/{to_alpaca(symbol)}"))

    async def assets(self) -> Dict[str, Asset]:
        """Every active US equity (thousands; one request)."""
        rows = await self._get(self.trading_base, "/v2/assets", {"status": "active", "asset_class": "us_equity"})
        out = {}
        for r in rows:
            try:
                a = _asset(r)
            except ValueError:  # warrants, units and other symbols OTTR never trades
                continue
            out[a.symbol] = a
        return out

    # -- data API ---------------------------------------------------------------------------
    async def snapshots(self, symbols: Sequence[str], feed: str = "iex") -> Dict[str, dict]:
        """Raw snapshots keyed by canonical symbol. Symbols Alpaca doesn't know are absent."""
        syms = [to_alpaca(s) for s in symbols]
        out: Dict[str, dict] = {}
        for i in range(0, len(syms), SNAPSHOT_CHUNK):
            chunk = syms[i:i + SNAPSHOT_CHUNK]
            d = await self._get(self.data_base, "/v2/stocks/snapshots", {"symbols": ",".join(chunk), "feed": feed})
            for k, v in (d or {}).items():
                if v:
                    out[normalize(k)] = v
        return out

    async def daily_bars(self, symbols: Sequence[str], start: date, end: Optional[date] = None,
                         adjustment: str = "raw", feed: str = "sip",
                         now: Optional[float] = None) -> Dict[str, List[Bar]]:
        """Daily bars per symbol, oldest first, following every page.

        The request ends 16 minutes before `now` (free-plan SIP delay), so today's bar can
        be partial while the market is open: callers keep only sessions that have closed
        (see `completed_bars`).
        """
        now = self._clock.time() if now is None else now
        limit_end = datetime.fromtimestamp(now, tz=timezone.utc) - SIP_DELAY
        end_dt = limit_end if end is None else min(limit_end, datetime(end.year, end.month, end.day, 23, 59, 59, tzinfo=ET))
        params = {"symbols": ",".join(to_alpaca(s) for s in symbols), "timeframe": "1Day",
                  "start": start.isoformat(), "end": end_dt.isoformat(timespec="seconds"),
                  "adjustment": adjustment, "feed": feed}
        out: Dict[str, List[Bar]] = {normalize(s): [] for s in symbols}
        for sym, rows in (await self._bar_rows(params)).items():
            dst = out.setdefault(sym, [])
            for r in rows:
                try:
                    dst.append(Bar(bar_day(r["t"]), float(r["o"]), float(r["h"]), float(r["l"]),
                                   float(r["c"]), float(r["v"])))
                except (KeyError, ValueError) as e:  # one bad bar is dropped and logged, not fatal
                    logger.warning("dropping malformed %s bar %s: %s", sym, r.get("t"), e)
        return {s: sorted(_dedupe(b), key=lambda x: x.day) for s, b in out.items()}

    async def latest_minute_closes(self, symbols: Sequence[str], now: Optional[float] = None,
                                   lookback: timedelta = timedelta(minutes=45)) -> Dict[str, Tuple[float, float]]:
        """symbol -> (bar end time, close) of the newest SIP minute bar the free plan may read
        (16 minutes old or more), from the `lookback` before that. A symbol with no trade in
        the window is absent. Marks a holding whose live IEX quote isn't usable."""
        now = self._clock.time() if now is None else now
        end = datetime.fromtimestamp(now, tz=timezone.utc) - SIP_DELAY
        params = {"symbols": ",".join(to_alpaca(s) for s in symbols), "timeframe": "1Min",
                  "start": (end - lookback).isoformat(timespec="seconds"),
                  "end": end.isoformat(timespec="seconds"), "adjustment": "raw", "feed": "sip"}
        out: Dict[str, Tuple[float, float]] = {}
        for sym, rows in (await self._bar_rows(params)).items():
            for r in rows:
                try:
                    ts, close = parse_ts(r["t"]) + 60.0, float(r["c"])  # a minute bar is stamped at its start
                except (KeyError, ValueError):
                    continue
                if close > 0 and (sym not in out or ts > out[sym][0]):
                    out[sym] = (ts, close)
        return out

    async def _bar_rows(self, params: dict) -> Dict[str, List[dict]]:
        """Raw bar rows per canonical symbol, following every page (`limit` counts across
        all symbols, so a page can end mid-symbol)."""
        params = {**params, "limit": BARS_PAGE_LIMIT, "sort": "asc"}
        out: Dict[str, List[dict]] = {}
        token: Optional[str] = None
        for _ in range(1000):  # a hard stop if the API ever repeats a token
            page = await self._get(self.data_base, "/v2/stocks/bars", {**params, **({"page_token": token} if token else {})})
            for sym, rows in ((page or {}).get("bars") or {}).items():
                out.setdefault(normalize(sym), []).extend(rows or ())
            token = (page or {}).get("next_page_token")
            if not token:
                return out
        raise AlpacaError("bars pagination did not end")

    async def corporate_actions(self, symbols: Sequence[str], start: date, end: date,
                                types: Optional[Sequence[str]] = None) -> Dict[str, List[dict]]:
        """Raw corporate actions touching `symbols` whose process_date is in [start, end],
        as Alpaca's arrays (type name -> rows), merged across symbol chunks and pages.
        `ottr.events.corporate.parse_actions` reads them."""
        out: Dict[str, List[dict]] = {}
        seen: set = set()
        syms = [to_alpaca(s) for s in symbols]
        for i in range(0, len(syms), SNAPSHOT_CHUNK):
            params = {"symbols": ",".join(syms[i:i + SNAPSHOT_CHUNK]), "start": start.isoformat(),
                      "end": end.isoformat(), "limit": CORP_ACTIONS_PAGE_LIMIT}
            if types:
                params["types"] = ",".join(types)
            token: Optional[str] = None
            for _ in range(1000):  # a hard stop if the API ever repeats a token
                page = await self._get(self.data_base, "/v1/corporate-actions",
                                       {**params, **({"page_token": token} if token else {})})
                for kind, rows in ((page or {}).get("corporate_actions") or {}).items():
                    for r in rows or ():
                        # An action touching two requested symbols (a merger of two of
                        # them) comes back in both chunks: keep it once.
                        rid = r.get("id") if isinstance(r, dict) else None
                        if rid is None or rid not in seen:
                            seen.add(rid)
                            out.setdefault(kind, []).append(r)
                token = (page or {}).get("next_page_token")
                if not token:
                    break
            else:
                raise AlpacaError("corporate actions pagination did not end")
        return out


def _dedupe(bars: Iterable[Bar]) -> List[Bar]:
    by_day: Dict[date, Bar] = {}
    for b in bars:
        by_day[b.day] = b
    return list(by_day.values())


def _asset(r: dict) -> Asset:
    return Asset(symbol=normalize(r["symbol"]), name=str(r.get("name") or ""), exchange=str(r.get("exchange") or ""),
                 tradable=bool(r.get("tradable")), fractionable=bool(r.get("fractionable")),
                 status=str(r.get("status") or ""))


def completed_bars(bars: Sequence[Bar], cal: TradingCalendar, now: float) -> List[Bar]:
    """Drop today's bar until it is final: an in-progress day's bar is partial and would
    drift every indicator intraday. It counts as final once the session has closed AND the
    free plan's SIP delay has passed (before that, the request window ends mid-session).
    Older bars are kept whether or not the calendar's loaded range reaches back to them."""
    today = datetime.fromtimestamp(now, tz=ET).date()
    session = cal.session_on(today)
    final_today = session is None or now >= session.close_ts + SIP_DELAY.total_seconds()
    return [b for b in bars if b.day < today or (b.day == today and final_today)]


# -- quote sanity -----------------------------------------------------------------------
@dataclass(frozen=True)
class QuoteLimits:
    max_spread_pct: float = 1.0       # wider than this (of mid) is not a fillable market
    max_age_sec: float = 120.0        # an older quote no longer says what the market is
    max_trade_gap_pct: float = 2.0    # mid vs a recent (<5 min) last trade
    max_close_gap_pct: float = 25.0   # mid vs the previous close: catches a stale or bad print
    recent_trade_sec: float = 300.0


def check_quote(symbol: str, snap: Optional[dict], now: float,
                limits: QuoteLimits = QuoteLimits()) -> Tuple[Optional[Quote], str]:
    """A tradeable Quote from a snapshot, or (None, reason). Never repairs a bad quote.

    Reasons: no_snapshot, no_quote, one_sided, crossed, too_wide, stale, off_market. The
    Quote's `ts` is the exchange's quote time (not when we fetched it), so staleness is
    measured against the market, not against our own poll.
    """
    if not snap:
        return None, "no_snapshot"
    q = snap.get("latestQuote") or {}
    if not q or "t" not in q:
        return None, "no_quote"
    bid, ask = float(q.get("bp") or 0), float(q.get("ap") or 0)
    if bid <= 0 or ask <= 0:
        return None, "one_sided"
    if ask < bid:
        return None, "crossed"
    mid = (bid + ask) / 2
    spread = (ask - bid) / mid * 100
    if spread > limits.max_spread_pct:
        return None, f"too_wide ({spread:.2f}%)"
    qts = parse_ts(q["t"])
    if now - qts > limits.max_age_sec:
        return None, f"stale ({now - qts:.0f}s)"
    trade = snap.get("latestTrade") or {}
    last = float(trade.get("p") or 0)
    if last > 0 and "t" in trade and now - parse_ts(trade["t"]) <= limits.recent_trade_sec:
        gap = abs(mid / last - 1) * 100
        if gap > limits.max_trade_gap_pct:
            return None, f"off_market ({gap:.1f}% from last trade)"
    prev = float((snap.get("prevDailyBar") or {}).get("c") or 0)
    if prev > 0:
        gap = abs(mid / prev - 1) * 100
        if gap > limits.max_close_gap_pct:
            return None, f"off_market ({gap:.1f}% from previous close)"
    return Quote(asset=normalize(symbol), bid=bid, ask=ask, last=last or mid, ts=qts, source="alpaca",
                 basis=BASIS_LIVE), "ok"
