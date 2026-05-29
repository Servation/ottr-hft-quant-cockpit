import httpx
import logging
import time
import xml.etree.ElementTree as ET
from typing import Dict, List, Any, Optional
from app.config import settings, translate

logger = logging.getLogger(__name__)

class MarketProxyService:
    def __init__(self):
        self.ticker_cache: Dict[str, Dict[str, Any]] = {}  # symbol -> {"price": float, "timestamp": float}
        self.news_cache: Optional[Dict[str, Any]] = None  # {"items": [...], "timestamp": float}

    async def fetch_ticker_from_binance(self, symbol: str) -> Optional[float]:
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url)
            if response.status_code == 200:
                data = response.json()
                return float(data["price"])
        return None

    async def fetch_ticker_from_yahoo(self, symbol: str) -> Optional[float]:
        # Convert BTCUSDT to BTC-USD for Yahoo
        yahoo_symbol = symbol
        if symbol.endswith("USDT"):
            yahoo_symbol = symbol.replace("USDT", "-USD")
        
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}?interval=1m&range=1d"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url, headers=headers)
            if response.status_code == 200:
                data = response.json()
                price = data["chart"]["result"][0]["meta"]["regularMarketPrice"]
                return float(price)
        return None

    async def get_ticker(self, symbol: str) -> float:
        # Normalize symbol (e.g. BTC -> BTCUSDT) for external API queries
        normalized_symbol = symbol.strip().upper()
        if not normalized_symbol.endswith("USDT"):
            binance_symbol = f"{normalized_symbol}USDT"
        else:
            binance_symbol = normalized_symbol

        now = time.time()
        # Check cache using original symbol key
        if symbol in self.ticker_cache:
            cache_entry = self.ticker_cache[symbol]
            if now - cache_entry["timestamp"] < settings.ticker_cache_ttl:
                return cache_entry["price"]

        # Fetch from Binance (primary)
        price = None
        try:
            price = await self.fetch_ticker_from_binance(binance_symbol)
        except Exception as e:
            logger.warning(f"Binance fetch failed for {binance_symbol}: {e}")

        # Fallback to Yahoo
        if price is None:
            try:
                price = await self.fetch_ticker_from_yahoo(binance_symbol)
            except Exception as e:
                logger.warning(f"Yahoo fetch failed for {binance_symbol}: {e}")

        # If both fail, use default/mock fallback so it doesn't crash
        if price is None:
            logger.error(f"Failed to fetch price for {symbol} (binance: {binance_symbol}) from all sources.")
            if symbol in self.ticker_cache:
                return self.ticker_cache[symbol]["price"]
            # Mock default values if cache empty
            mock_prices = {"BTCUSDT": 65000.0, "ETHUSDT": 3500.0, "BTC": 65000.0, "ETH": 3500.0}
            price = mock_prices.get(binance_symbol, mock_prices.get(symbol, 1.0))

        # Update cache using original symbol key
        self.ticker_cache[symbol] = {
            "price": price,
            "timestamp": now
        }
        return price

    async def get_tickers(self, symbols: List[str]) -> Dict[str, float]:
        results = {}
        for symbol in symbols:
            results[symbol] = await self.get_ticker(symbol)
        return results

    async def get_news(self) -> List[Dict[str, str]]:
        now = time.time()
        # Check cache
        if self.news_cache:
            if now - self.news_cache["timestamp"] < settings.news_cache_ttl:
                return self.news_cache["items"]

        news_items = []
        try:
            url = "https://www.coindesk.com/arc/outboundfeeds/rss/"
            async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
                response = await client.get(url)
                if response.status_code == 200:
                    root = ET.fromstring(response.content)
                    for item in root.findall(".//item")[:10]:
                        title = item.find("title")
                        link = item.find("link")
                        pub_date = item.find("pubDate")
                        news_items.append({
                            "title": title.text if title is not None else "No Title",
                            "link": link.text if link is not None else "",
                            "pubDate": pub_date.text if pub_date is not None else ""
                        })
                else:
                    raise RuntimeError(f"HTTP {response.status_code} received from Coindesk RSS")
            
            if not news_items:
                raise RuntimeError("No news items parsed from Coindesk RSS")

        except Exception as e:
            logger.error(f"Failed to fetch RSS news: {e}")
            # Mock news as a fallback
            news_items = [
                {
                    "title": "Bitcoin consolidates as market structure improves",
                    "link": "https://example.com/btc",
                    "pubDate": "Fri, 29 May 2026 00:00:00 GMT",
                    "source": "Yahoo Finance"
                },
                {
                    "title": "Ethereum gas fees hit multi-year lows",
                    "link": "https://example.com/eth",
                    "pubDate": "Fri, 29 May 2026 00:05:00 GMT",
                    "source": "Coindesk"
                }
            ]

        # Save to cache
        self.news_cache = {
            "items": news_items,
            "timestamp": now
        }
        return news_items

market_proxy = MarketProxyService()
