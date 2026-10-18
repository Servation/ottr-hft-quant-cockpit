"""
Kraken public REST client (no keys): spot ticker + daily OHLC.

What it does: one request for every pair's spot price, one request per pair for daily
candles, with bounded retries on rate limits / 5xx. Why Kraken only: v1 mixed CoinGecko
(prices, 429-prone history), CoinCap (dead) and Kraken (candles), so prices and
indicators could come from different venues. One venue keeps them consistent and matches
the candles the v1 backtests were run on.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List, Optional, Tuple

import httpx

from ottr.market.types import Quote

logger = logging.getLogger(__name__)

BASE = "https://api.kraken.com/0/public"

# Ticker -> Kraken pair. Kraken answers some pairs under legacy names (XBTUSD comes back
# as XXBTZUSD), so responses are decoded with symbol_for_key(), not by request order.
PAIRS: Dict[str, str] = {
    "BTC": "XBTUSD", "ETH": "ETHUSD", "SOL": "SOLUSD", "XRP": "XRPUSD",
    "ADA": "ADAUSD", "DOGE": "XDGUSD", "LINK": "LINKUSD", "AVAX": "AVAXUSD",
}


class KrakenError(RuntimeError):
    pass


def symbol_for_key(key: str, pairs: Dict[str, str] = PAIRS) -> Optional[str]:
    for symbol, pair in pairs.items():
        legacy = f"X{pair[:-3]}Z{pair[-3:]}" if len(pair) == 6 else None
        if key in (pair, legacy):
            return symbol
    return None


class Kraken:
    def __init__(self, client: Optional[httpx.AsyncClient] = None, retries: int = 3,
                 backoff: float = 1.0) -> None:
        self._client = client
        self._retries = retries
        self._backoff = backoff

    async def _once(self, client: httpx.AsyncClient, path: str, params: dict) -> Tuple[Optional[dict], Optional[str], bool]:
        """One attempt -> (result, error, retryable)."""
        try:
            resp = await client.get(f"{BASE}/{path}", params=params)
        except httpx.TransportError as e:
            return None, f"transport: {e}", True
        if resp.status_code == 429 or resp.status_code >= 500:
            return None, f"HTTP {resp.status_code}", True
        if resp.status_code >= 400:
            return None, f"HTTP {resp.status_code}", False
        try:
            body = resp.json()
        except ValueError:  # an HTML error page from a proxy/CDN: treat as an outage, not a crash
            return None, "non-JSON response", True
        errs = body.get("error") or []
        if errs:
            msg = "; ".join(errs)
            return None, msg, any(t in msg for t in ("Rate limit", "Unavailable", "Busy"))
        return body.get("result") or {}, None, False

    async def _get(self, path: str, params: dict) -> dict:
        client = self._client or httpx.AsyncClient(timeout=10.0)
        try:
            delay = self._backoff
            for attempt in range(self._retries + 1):
                result, err, retryable = await self._once(client, path, params)
                if err is None:
                    return result
                if not retryable or attempt >= self._retries:
                    raise KrakenError(f"{path} failed: {err}")
                logger.warning("Kraken %s attempt %d failed (%s); retrying", path, attempt + 1, err)
                await asyncio.sleep(delay)
                delay *= 2
            raise KrakenError(f"{path} failed")  # unreachable
        finally:
            if self._client is None:
                await client.aclose()

    async def quotes(self, pairs: Dict[str, str] = PAIRS) -> Dict[str, Quote]:
        """Bid/ask/last plus rolling 24h high/low for every pair, in one request."""
        result = await self._get("Ticker", {"pair": ",".join(pairs.values())})
        now = time.time()
        out: Dict[str, Quote] = {}
        for key, e in result.items():
            sym = symbol_for_key(key, pairs)
            if sym is None:
                continue
            q = Quote(
                asset=sym, bid=float(e["b"][0]), ask=float(e["a"][0]), last=float(e["c"][0]),
                ts=now, source="kraken",
                high_24h=float(e["h"][1]), low_24h=float(e["l"][1]),  # [today, last 24h]
            )
            if q.valid():
                out[sym] = q
        return out

    async def daily_ohlc(self, pair: str) -> List[Tuple[int, float, float]]:
        """Closed daily candles as [(open_ts, close, usd_volume)], oldest first.

        Kraken's last row is the still-forming day; it is dropped so indicators don't
        drift intraday (the spot price carries the live information instead). USD volume
        is the day's base volume times its VWAP (the close when Kraken reports no VWAP).
        """
        result = await self._get("OHLC", {"pair": pair, "interval": 1440})
        rows = next((v for k, v in result.items() if k != "last"), None)
        if not rows:
            raise KrakenError(f"no candles for {pair}")
        out = []
        for r in rows[:-1]:
            close, vwap, volume = float(r[4]), float(r[5]), float(r[6])
            if close > 0:
                out.append((int(r[0]), close, volume * (vwap if vwap > 0 else close)))
        return out

    async def daily_closes(self, pair: str) -> List[Tuple[int, float]]:
        """Closed daily candles as [(open_ts, close)], oldest first."""
        return [(ts, close) for ts, close, _ in await self.daily_ohlc(pair)]

    async def asset_pairs(self) -> Dict[str, dict]:
        """Every tradable pair Kraken lists (the screener's universe of candidates)."""
        return await self._get("AssetPairs", {})

    async def tickers(self) -> Dict[str, dict]:
        """The raw ticker of every pair, in one request (no `pair` = all of them)."""
        return await self._get("Ticker", {})
