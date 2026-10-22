"""
FakeAlpaca: Alpaca's paper trading + market data APIs, in memory, behind httpx.MockTransport.

What it does: serves /v2/clock, /v2/calendar, /v2/assets, /v2/stocks/snapshots and
/v2/stocks/bars from scripted data, and enforces the rules the real free plan enforces:
auth headers, GET only, SIP data must be older than 15 minutes, bars sorted by symbol then
date with `limit` counted across ALL symbols and `next_page_token` paging. It records every
request so tests can prove what the client did (and didn't) send, and can inject 429s.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional
from urllib.parse import parse_qs

import httpx

from ottr.market.alpaca import DATA, TRADING_PAPER, Credentials, parse_ts
from ottr.market.calendar import ET
from tests.nyse import sessions_2026

CREDS = Credentials("PKTESTKEY0000", "secret-xyz")


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")


class FakeAlpaca:
    def __init__(self, now: float) -> None:
        self.now = now
        self.requests: List[httpx.Request] = []
        self.sessions = sessions_2026()
        self.assets = {}            # symbol -> asset row
        self.snaps: Dict[str, dict] = {}
        self.bars: Dict[str, List[dict]] = {}
        self.page_size = 10000
        self.fail_429: Dict[str, int] = {}   # path -> how many 429s to send first
        self.reset_in = 2.0
        self.clock_says_open = None          # force /v2/clock's is_open (to fake a disagreement)

    # -- scripting helpers ---------------------------------------------------------------
    def add_asset(self, symbol: str, fractionable: bool = True, tradable: bool = True, name: str = "") -> None:
        self.assets[symbol] = {"symbol": symbol, "name": name or f"{symbol} Fund", "exchange": "ARCA",
                               "class": "us_equity", "status": "active", "tradable": tradable,
                               "fractionable": fractionable}

    def add_quote(self, symbol: str, bid: float, ask: float, age: float = 1.0, last: Optional[float] = None,
                  trade_age: float = 5.0, prev_close: Optional[float] = None) -> None:
        q = {"bp": bid, "ap": ask, "bs": 1, "as": 1, "bx": "V", "ax": "V", "c": ["R"], "z": "B",
             "t": iso(self.now - age)}
        t = {"p": last if last is not None else (bid + ask) / 2, "s": 10, "t": iso(self.now - trade_age), "x": "V"}
        pc = prev_close if prev_close is not None else (bid + ask) / 2
        self.snaps[symbol] = {"latestQuote": q, "latestTrade": t,
                              "prevDailyBar": {"t": "2026-01-01T05:00:00Z", "o": pc, "h": pc, "l": pc, "c": pc, "v": 1}}

    def add_daily_bars(self, symbol: str, closes: Dict[date, float]) -> None:
        rows = []
        for d, c in sorted(closes.items()):
            midnight = datetime(d.year, d.month, d.day, tzinfo=ET).astimezone(timezone.utc)
            rows.append({"t": midnight.strftime("%Y-%m-%dT%H:%M:%SZ"), "o": c, "h": c * 1.01, "l": c * 0.99,
                         "c": c, "v": 1000, "n": 10, "vw": c})
        self.bars[symbol] = rows

    # -- transport -------------------------------------------------------------------------
    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    def paths(self) -> List[str]:
        return [r.url.path for r in self.requests]

    def handle(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        if req.method != "GET":
            return httpx.Response(405, json={"message": "fake: only GET is modeled"})
        if req.headers.get("APCA-API-KEY-ID") != CREDS.key_id or req.headers.get("APCA-API-SECRET-KEY") != CREDS.secret:
            return httpx.Response(401, json={"message": "unauthorized."})
        path = req.url.path
        if self.fail_429.get(path, 0) > 0:
            self.fail_429[path] -= 1
            return httpx.Response(429, json={"message": "too many requests."},
                                  headers={"X-RateLimit-Reset": str(int(self.now + self.reset_in))})
        q = {k: v[0] for k, v in parse_qs(req.url.query.decode()).items()}
        base = f"{req.url.scheme}://{req.url.host}"
        if base == TRADING_PAPER:
            return self._trading(path, q)
        if base == DATA:
            return self._data(path, q)
        return httpx.Response(404, json={"message": f"fake: unknown host {base}"})

    def _trading(self, path: str, q: dict) -> httpx.Response:
        if path == "/v2/clock":
            nxt = next(s for s in self.sessions if s.open_ts > self.now)
            cur = next((s for s in self.sessions if s.contains(self.now)), None)
            close = (cur or nxt).close_ts
            is_open = cur is not None if self.clock_says_open is None else self.clock_says_open
            return httpx.Response(200, json={"timestamp": iso(self.now), "is_open": is_open,
                                             "next_open": iso(nxt.open_ts), "next_close": iso(close)})
        if path == "/v2/calendar":
            lo, hi = date.fromisoformat(q["start"]), date.fromisoformat(q["end"])
            return httpx.Response(200, json=[
                {"date": s.day.isoformat(), "open": s.open.strftime("%H:%M"), "close": s.close.strftime("%H:%M"),
                 "session_open": "0400", "session_close": "2000",
                 "settlement_date": (s.day + timedelta(days=1)).isoformat()}
                for s in self.sessions if lo <= s.day <= hi])
        if path == "/v2/assets":
            return httpx.Response(200, json=list(self.assets.values()))
        if path.startswith("/v2/assets/"):
            sym = path.rsplit("/", 1)[1]
            if sym in self.assets:
                return httpx.Response(200, json=self.assets[sym])
            return httpx.Response(404, json={"message": "asset not found"})
        return httpx.Response(404, json={"message": f"fake: no route {path}"})

    def _data(self, path: str, q: dict) -> httpx.Response:
        if path == "/v2/stocks/snapshots":
            syms = q["symbols"].split(",")
            return httpx.Response(200, json={s: self.snaps[s] for s in syms if s in self.snaps})
        if path == "/v2/stocks/bars":
            if q.get("feed", "sip") == "sip" and parse_ts(q["end"]) > self.now - 15 * 60:
                return httpx.Response(403, json={"message": "subscription does not permit querying recent SIP data"})
            start = datetime.fromisoformat(q["start"]).date() if "T" in q["start"] else date.fromisoformat(q["start"])
            end_ts = parse_ts(q["end"])
            rows = []
            for sym in sorted(q["symbols"].split(",")):
                for r in self.bars.get(sym, []):
                    t = parse_ts(r["t"])
                    if datetime.fromtimestamp(t, tz=ET).date() >= start and t <= end_ts:
                        rows.append((sym, r))
            offset = int(q.get("page_token") or 0)
            limit = min(int(q.get("limit", 1000)), self.page_size)
            page = rows[offset:offset + limit]
            out: Dict[str, list] = {}
            for sym, r in page:
                out.setdefault(sym, []).append(r)
            nxt = str(offset + limit) if offset + limit < len(rows) else None
            return httpx.Response(200, json={"bars": out, "next_page_token": nxt, "currency": "USD"})
        return httpx.Response(404, json={"message": f"fake: no route {path}"})
