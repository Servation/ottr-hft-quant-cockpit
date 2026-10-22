"""
Fakes for the events feeds, behind httpx.MockTransport: FRED's fredgraph.csv, Alpha
Vantage's EARNINGS_CALENDAR and the SEC's ticker lists and submissions.

Each serves the formats checked against the real services on 2026-10-21, including their
failure modes: FRED's HTTP 200 maintenance page, Alpha Vantage's errors inside a 200
response (JSON, or the CSV header followed by "I,n,f,o,r,m,a"), and the SEC's 403 for a
request without a contact User-Agent. Every request is recorded.
"""

from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs

import httpx

AV_HEADER = "symbol,name,reportDate,fiscalDateEnding,estimate,currency,timeOfTheDay"
MAINTENANCE_PAGE = ("<!doctype html>\n<html lang=\"en\"><head><title>Site Maintenance | St. Louis Fed</title>"
                    "</head><body>We'll be back soon!</body></html>")


class _Fake:
    def __init__(self) -> None:
        self.requests: List[httpx.Request] = []

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _handle(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        return self.handle(req, {k: v[0] for k, v in parse_qs(req.url.query.decode()).items()})

    def handle(self, req: httpx.Request, q: dict) -> httpx.Response:
        raise NotImplementedError


class FakeFred(_Fake):
    def __init__(self) -> None:
        super().__init__()
        self.series: Dict[str, List[Tuple[date, Optional[str]]]] = {}   # id -> (day, cell)
        self.maintenance = 0          # how many requests get the maintenance page first
        self.header = "observation_date"

    def add(self, sid: str, values: Dict[date, Optional[float]]) -> None:
        self.series[sid] = sorted((d, "" if v is None else repr(v)) for d, v in values.items())

    def handle(self, req, q):
        if self.maintenance > 0:
            self.maintenance -= 1
            return httpx.Response(200, text=MAINTENANCE_PAGE, headers={"content-type": "text/html"})
        sid = q["id"]
        if sid not in self.series:
            return httpx.Response(404, text="Series not found")
        start = q.get("cosd", "0000")
        lines = [f"{self.header},{sid}"] + [f"{d.isoformat()},{v}" for d, v in self.series[sid] if d.isoformat() >= start]
        return httpx.Response(200, text="\n".join(lines) + "\n", headers={"content-type": "text/csv"})


AV_KEY = "AVTESTKEY123"


class FakeAlphaVantage(_Fake):
    def __init__(self) -> None:
        super().__init__()
        self.rows: List[str] = []
        self.fail: Optional[str] = None     # "json": the rate-limit JSON; "garbled": the split CSV

    def add(self, symbol: str, day: date, tod: str = "post-market", name: str = "SOME COMPANY INC",
            estimate: str = "1.0", fiscal: str = "2026-09-30") -> None:
        self.rows.append(f"{symbol},{name},{day.isoformat()},{fiscal},{estimate},USD,{tod}")

    def many(self, n: int, start: date) -> None:
        """`n` filler companies reporting on `start` (a realistic calendar is thousands)."""
        for i in range(n):
            letters = "".join(chr(65 + (i // 26 ** k) % 26) for k in range(3))
            self.add(f"Z{letters}", start, name=f"FILLER {i} INC")

    def handle(self, req, q):
        if q.get("apikey") != AV_KEY:
            return httpx.Response(200, json={"Error Message": "the parameter apikey is invalid or missing."})
        if self.fail == "json":
            return httpx.Response(200, json={"Information": f"We have detected your API key as {AV_KEY} and our "
                                                            "standard API rate limit is 25 requests per day."})
        if self.fail == "garbled":
            return httpx.Response(200, text=f"{AV_HEADER}\r\nI,n,f,o,r,m,a\r\n")
        return httpx.Response(200, text="\r\n".join([AV_HEADER, *self.rows]) + "\r\n",
                              headers={"content-type": "application/x-download"})


class FakeSec(_Fake):
    def __init__(self) -> None:
        super().__init__()
        self.listings: List[list] = []        # [cik, name, ticker, exchange]
        self.funds: List[list] = []           # [cik, seriesId, classId, symbol]
        self.companies: Dict[int, dict] = {}

    def add_company(self, ticker: str, cik: int, name: str, exchange: Optional[str] = "Nasdaq",
                    entity_type: str = "operating", sic: str = "3571", sic_desc: str = "Electronic Computers") -> None:
        self.listings.append([cik, name, ticker, exchange])
        self.companies[cik] = {"cik": str(cik), "entityType": entity_type, "sic": sic, "sicDescription": sic_desc,
                               "name": name, "tickers": [ticker], "exchanges": [exchange] if exchange else [],
                               "filings": {"recent": {}}}

    def add_fund(self, symbol: str, cik: int = 1100663) -> None:
        self.funds.append([cik, f"S{len(self.funds):09d}", f"C{len(self.funds):09d}", symbol])

    def handle(self, req, q):
        ua = req.headers.get("user-agent", "")
        if "@" not in ua:
            return httpx.Response(403, text="<html>Your Request Originates from an Undeclared Automated Tool</html>")
        path = req.url.path
        if path == "/files/company_tickers_exchange.json":
            return httpx.Response(200, json={"fields": ["cik", "name", "ticker", "exchange"], "data": self.listings})
        if path == "/files/company_tickers_mf.json":
            return httpx.Response(200, json={"fields": ["cik", "seriesId", "classId", "symbol"], "data": self.funds})
        if path.startswith("/submissions/CIK"):
            cik = int(path[len("/submissions/CIK"):-len(".json")])
            if cik in self.companies:
                return httpx.Response(200, json=self.companies[cik])
        return httpx.Response(404, text="not found")


def standard_sec() -> FakeSec:
    """The issuers the classifier was checked against, with their real SEC attributes."""
    s = FakeSec()
    s.add_company("AAPL", 320193, "Apple Inc.")
    s.add_company("BRK-B", 1067983, "BERKSHIRE HATHAWAY INC", "NYSE", sic="6331",
                  sic_desc="Fire, Marine & Casualty Insurance")
    s.add_company("SPY", 884394, "SPDR S&P 500 ETF TRUST", "NYSE", entity_type="other", sic="", sic_desc="")
    s.add_company("GLD", 1222333, "SPDR GOLD TRUST", "NYSE", sic="6221",
                  sic_desc="Commodity Contracts Brokers & Dealers")
    s.add_company("SPAC", 1999001, "EXAMPLE ACQUISITION CORP", "Nasdaq", sic="6770", sic_desc="Blank Checks")
    s.add_company("PINK", 1999002, "SOME OTC CO", "OTC")
    s.add_company("NONE.", 1999003, "BAD TICKER CO", None)
    for sym in ("QQQ", "SGOV", "TLT", "TQQQ"):
        s.add_fund(sym)
    return s
