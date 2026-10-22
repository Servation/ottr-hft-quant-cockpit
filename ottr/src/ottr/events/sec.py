"""
SEC ticker lists and company profiles: which listed symbols are operating companies.

What it does: `Sec.listings()` reads company_tickers_exchange.json (ticker, CIK, name and
exchange for every exchange-listed filer), `Sec.fund_symbols()` reads
company_tickers_mf.json (every fund series and share class with a ticker, which includes
the ETFs organized as funds), and `Sec.company(cik)` reads one filer's profile from the
submissions API. `operating_company` combines them into the screener's test.

Why: the screener (PR 10) may only pick operating companies. Price and volume can't tell
a stock from an ETF, a commodity trust or a SPAC; the SEC's own records can, for free.

Facts this relies on (checked 2026-10-21):
- ETFs organized as funds (QQQ, SGOV, TLT, XLE, PDBC, leveraged ones like TQQQ) are in
  the fund list. Trusts outside it still show up as listed companies: SPY's profile has
  entity type "other"; GLD and SLV are "operating" with SIC 6221 (commodity contracts).
- SIC 6770 is blank-check companies (SPACs).
- The SEC requires a User-Agent naming a person or company and an email, and allows at
  most 10 requests a second; a missing or generic User-Agent gets HTTP 403.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Dict, Optional, Set, Tuple

import httpx

from ottr.market import http
from ottr.market.symbols import normalize

SEC_FILES = "https://www.sec.gov/files"
SEC_DATA = "https://data.sec.gov"
SEC_PER_MINUTE = 480        # the SEC allows 10 a second; 8 leaves headroom
EXCHANGES = ("NYSE", "Nasdaq")
# SIC codes that are never an operating company for the screener, whatever the entity type.
EXCLUDED_SIC = {
    "6221": "commodity trust",           # GLD, SLV and other physical-commodity trusts
    "6770": "blank check company (SPAC)",
    "6189": "asset-backed securities issuer",
}
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}")


class SecError(http.HttpError):
    pass


def user_agent_problem(ua: str) -> Optional[str]:
    """Why `ua` won't satisfy the SEC, or None. It wants "Name email@domain"."""
    ua = (ua or "").strip()
    if not ua:
        return "SEC_USER_AGENT is not set"
    m = _EMAIL.search(ua)
    if not m:
        return "SEC_USER_AGENT needs a contact email (e.g. 'Jane Doe jane@example.com')"
    if not ua.replace(m.group(0), "").strip():
        return "SEC_USER_AGENT needs a name before the email (e.g. 'Jane Doe jane@example.com')"
    return None


@dataclass(frozen=True)
class Listing:
    symbol: str
    cik: int
    name: str
    exchange: Optional[str]     # NYSE, Nasdaq, CBOE, OTC, ... or None


@dataclass(frozen=True)
class Company:
    cik: int
    name: str
    entity_type: str            # "operating", "other", ...
    sic: str                    # "" when the SEC has none (SPY)
    sic_description: str
    tickers: Tuple[str, ...]
    exchanges: Tuple[str, ...]


def _classify(resp: httpx.Response) -> http.Classified:
    if resp.status_code == 403:
        return None, ("HTTP 403: the SEC refused the request (set SEC_USER_AGENT to your name and "
                      "email, e.g. 'Jane Doe jane@example.com')"), False
    return http.classify_json(resp)


def _rows(doc: dict, needed: Tuple[str, ...], what: str):
    """Rows of one of the SEC's {"fields": [...], "data": [[...]]} files, as dicts."""
    try:
        fields = list(doc["fields"])
        data = doc["data"]
    except (KeyError, TypeError):
        raise SecError(f"SEC {what}: not the expected fields/data document")
    if any(f not in fields for f in needed):
        raise SecError(f"SEC {what}: fields {fields} lack {[f for f in needed if f not in fields]}")
    for r in data:
        if isinstance(r, list) and len(r) == len(fields):
            yield dict(zip(fields, r))


class Sec:
    def __init__(self, user_agent: str, client: Optional[httpx.AsyncClient] = None,
                 limiter: Optional[http.RateLimiter] = None, retries: int = 3, backoff: float = 1.0) -> None:
        problem = user_agent_problem(user_agent)
        if problem:
            raise ValueError(problem)
        self.user_agent = user_agent.strip()
        self._client = client
        self.limiter = limiter or http.RateLimiter(per_minute=SEC_PER_MINUTE, capacity=8)
        self._retries, self._backoff = retries, backoff

    @classmethod
    def from_env(cls, **kw) -> Optional["Sec"]:
        ua = os.getenv("SEC_USER_AGENT", "")
        return None if user_agent_problem(ua) else cls(ua, **kw)

    async def _get(self, url: str, what: str):
        client = self._client or httpx.AsyncClient(timeout=60.0)
        try:
            return await http.get_json(client, url, None, classify=_classify, retries=self._retries,
                                       backoff=self._backoff, limiter=self.limiter,
                                       headers={"User-Agent": self.user_agent}, name=f"SEC {what}", error=SecError)
        finally:
            if self._client is None:
                await client.aclose()

    async def listings(self) -> Dict[str, Listing]:
        """Exchange-listed filers by canonical symbol. Symbols that aren't plain US tickers
        (warrants, units, foreign lines) are left out."""
        doc = await self._get(f"{SEC_FILES}/company_tickers_exchange.json", "company tickers")
        out: Dict[str, Listing] = {}
        for r in _rows(doc, ("cik", "name", "ticker", "exchange"), "company tickers"):
            try:
                sym = normalize(str(r["ticker"]))
                cik = int(r["cik"])
            except (ValueError, TypeError):
                continue
            # No ticker appeared twice on 2026-10-21; if one ever does, the first row wins
            # (the file lists the largest companies first).
            out.setdefault(sym, Listing(sym, cik, str(r["name"] or ""), r["exchange"] or None))
        if not out:
            raise SecError("SEC company tickers: no usable rows")
        return out

    async def fund_symbols(self) -> Set[str]:
        """Every ticker of a fund series or share class (ETFs organized as funds included)."""
        doc = await self._get(f"{SEC_FILES}/company_tickers_mf.json", "fund tickers")
        out: Set[str] = set()
        for r in _rows(doc, ("cik", "seriesId", "classId", "symbol"), "fund tickers"):
            try:
                out.add(normalize(str(r["symbol"])))
            except ValueError:
                continue
        if not out:
            raise SecError("SEC fund tickers: no usable rows")
        return out

    async def company(self, cik: int) -> Company:
        d = await self._get(f"{SEC_DATA}/submissions/CIK{int(cik):010d}.json", f"profile {cik}")
        try:
            return Company(cik=int(d["cik"]), name=str(d.get("name") or ""),
                           entity_type=str(d.get("entityType") or ""), sic=str(d.get("sic") or ""),
                           sic_description=str(d.get("sicDescription") or ""),
                           tickers=tuple(str(t) for t in d.get("tickers") or ()),
                           exchanges=tuple(str(e) for e in d.get("exchanges") or () if e))
        except (KeyError, TypeError, ValueError):
            raise SecError(f"SEC profile {cik}: unexpected document")


def operating_company(symbol: str, listing: Optional[Listing], funds: Set[str],
                      company: Optional[Company]) -> Tuple[bool, str]:
    """(True, why) when `symbol` is an operating company listed on NYSE or Nasdaq, else
    (False, the first reason it isn't). Anything unknown fails: no listing or no profile
    means no pick, never a guess."""
    if symbol in funds:
        return False, "a fund series (ETF or mutual fund)"
    if listing is None:
        return False, "not in the SEC's exchange-listed companies"
    if listing.exchange not in EXCHANGES:
        return False, f"listed on {listing.exchange or 'no exchange'}, not NYSE or Nasdaq"
    if company is None:
        return False, "no SEC company profile"
    if company.cik != listing.cik:
        return False, f"profile is CIK {company.cik}, the listing CIK {listing.cik}"
    if company.entity_type != "operating":
        return False, f"SEC entity type {company.entity_type or 'unknown'}"
    if company.sic in EXCLUDED_SIC:
        return False, f"{EXCLUDED_SIC[company.sic]} (SIC {company.sic})"
    return True, f"operating company ({company.sic_description or 'SIC ' + (company.sic or 'none')})"
