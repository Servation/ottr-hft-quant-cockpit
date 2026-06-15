import logging
import time
from typing import Dict, Any, List, Optional
from collections import deque

import httpx

from bot import settings

logger = logging.getLogger(__name__)

# API endpoints
COINGECKO_URL = (
    "https://api.coingecko.com/api/v3/simple/price"
    "?ids=bitcoin,ethereum&vs_currencies=usd&include_24hr_change=true"
)
COINCAP_URL = "https://api.coincap.io/v2/assets?ids=bitcoin,ethereum"

# Map CoinGecko ids to our ticker symbols
_GECKO_ID_MAP = {"bitcoin": "BTC", "ethereum": "ETH"}


class PriceFeed:
    """
    Fetches BTC and ETH prices from CoinGecko with CoinCap fallback.
    Caches results for a configurable TTL and maintains a rolling
    price history for emergency alert calculations.
    """

    def __init__(self) -> None:
        price_feed_cfg = settings.get("price_feed", {})
        self._cache_ttl: float = float(price_feed_cfg.get("cache_ttl_seconds", 60))

        self._cached_prices: Optional[Dict[str, Dict[str, float]]] = None
        self._cache_timestamp: float = 0.0

        # Rolling history: deque of (timestamp, prices_dict) tuples
        self._history: deque = deque(maxlen=60)

    async def _fetch_coingecko(self, client: httpx.AsyncClient) -> Dict[str, Dict[str, float]]:
        """Fetch prices from CoinGecko free API."""
        response = await client.get(COINGECKO_URL, timeout=10.0)
        response.raise_for_status()
        data = response.json()

        prices: Dict[str, Dict[str, float]] = {}
        for gecko_id, symbol in _GECKO_ID_MAP.items():
            entry = data.get(gecko_id, {})
            prices[symbol] = {
                "price": float(entry.get("usd", 0.0)),
                "change_24h": float(entry.get("usd_24h_change", 0.0)),
            }
        return prices

    async def _fetch_coincap(self, client: httpx.AsyncClient) -> Dict[str, Dict[str, float]]:
        """Fallback: fetch prices from CoinCap."""
        response = await client.get(COINCAP_URL, timeout=10.0)
        response.raise_for_status()
        data = response.json()

        prices: Dict[str, Dict[str, float]] = {}
        for asset in data.get("data", []):
            asset_id = asset.get("id", "")
            symbol = _GECKO_ID_MAP.get(asset_id)
            if symbol is None:
                continue
            price_usd = float(asset.get("priceUsd", 0.0))
            change_pct = float(asset.get("changePercent24Hr", 0.0))
            prices[symbol] = {
                "price": price_usd,
                "change_24h": change_pct,
            }
        return prices

    async def get_prices(self) -> Dict[str, Dict[str, float]]:
        """
        Returns current prices, using the cache if still fresh.
        Tries CoinGecko first, falls back to CoinCap on failure.
        """
        now = time.time()
        if self._cached_prices and (now - self._cache_timestamp) < self._cache_ttl:
            return self._cached_prices

        async with httpx.AsyncClient() as client:
            try:
                prices = await self._fetch_coingecko(client)
                logger.debug("Fetched prices from CoinGecko")
            except Exception as e:
                logger.warning("CoinGecko fetch failed (%s), falling back to CoinCap", e)
                try:
                    prices = await self._fetch_coincap(client)
                    logger.debug("Fetched prices from CoinCap")
                except Exception as fallback_err:
                    logger.error("CoinCap fallback also failed: %s", fallback_err)
                    if self._cached_prices:
                        logger.warning("Returning stale cached prices")
                        return self._cached_prices
                    return {
                        "BTC": {"price": 0.0, "change_24h": 0.0},
                        "ETH": {"price": 0.0, "change_24h": 0.0},
                    }

        self._cached_prices = prices
        self._cache_timestamp = now
        self._history.append((now, prices))
        return prices

    def get_price_history(self) -> List[Dict[str, Any]]:
        """
        Returns the rolling price history as a list of dicts with
        timestamp and per-asset price snapshots.
        """
        return [
            {"timestamp": ts, "prices": px}
            for ts, px in self._history
        ]

    async def get_formatted_summary(self) -> str:
        """
        Returns a human-readable price summary string for use in
        Discord messages or LLM context injection.
        """
        prices = await self.get_prices()
        lines: List[str] = []
        for symbol in ("BTC", "ETH"):
            entry = prices.get(symbol, {})
            price = entry.get("price", 0.0)
            change = entry.get("change_24h", 0.0)
            direction = "▲" if change >= 0 else "▼"
            lines.append(f"{symbol}: ${price:,.2f} ({direction} {abs(change):.2f}%)")
        return " | ".join(lines)


price_feed = PriceFeed()
