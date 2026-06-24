import logging
import time
import math
import asyncio
from typing import Dict, Any, List, Optional
from collections import deque
import pandas as pd
import pandas_ta as ta

import httpx

from bot import settings

logger = logging.getLogger(__name__)

# API endpoints
COINGECKO_URL = (
    "https://api.coingecko.com/api/v3/simple/price"
    "?ids=bitcoin,ethereum,solana,binancecoin,ripple,cardano,dogecoin,chainlink,avalanche-2"
    "&vs_currencies=usd&include_24hr_change=true"
)
COINCAP_URL = "https://api.coincap.io/v2/assets?ids=bitcoin,ethereum,solana,binance-coin,xrp,cardano,dogecoin,chainlink,avalanche"
DEFILLAMA_YIELDS_URL = "https://yields.llama.fi/pools"
COINGECKO_DERIVATIVES_URL = "https://api.coingecko.com/api/v3/derivatives"

# Map CoinGecko ids to our ticker symbols
_GECKO_ID_MAP = {
    "bitcoin": "BTC",
    "ethereum": "ETH",
    "solana": "SOL",
    "binancecoin": "BNB",
    "ripple": "XRP",
    "cardano": "ADA",
    "dogecoin": "DOGE",
    "chainlink": "LINK",
    "avalanche-2": "AVAX",
}

# Ticker -> Kraken USD pair, for daily-OHLC technical indicators. Kraken is used
# to dodge US geoblocking; not every asset is listed (BNB has no Kraken USD pair),
# so unmapped assets simply get no indicators rather than fabricated ones.
_KRAKEN_PAIR_MAP = {
    "BTC": "XBTUSD",
    "ETH": "ETHUSD",
    "SOL": "SOLUSD",
    "XRP": "XRPUSD",
    "ADA": "ADAUSD",
    "DOGE": "XDGUSD",
    "LINK": "LINKUSD",
    "AVAX": "AVAXUSD",
}


class PriceFeed:
    """
    Fetches BTC and ETH prices from CoinGecko with CoinCap fallback.
    Also fetches Volatility (14d historical), Stablecoin Yields, 
    Funding Rates, and calculates BTC/ETH Correlation.
    """

    def __init__(self) -> None:
        price_feed_cfg = settings.get("price_feed", {})
        self._cache_ttl: float = float(price_feed_cfg.get("cache_ttl_seconds", 60))
        
        # New caches for rate-limited endpoints
        self._volatility_cache_ttl: float = 4 * 3600  # 4 hours
        self._yield_cache_ttl: float = 4 * 3600       # 4 hours
        self._funding_cache_ttl: float = 2 * 3600     # 2 hours

        self._cached_prices: Optional[Dict[str, Dict[str, float]]] = None
        self._cache_timestamp: float = 0.0

        self._cached_volatility: Optional[Dict[str, float]] = None
        self._volatility_timestamp: float = 0.0

        self._cached_historical_prices: Dict[str, List[float]] = {}

        self._cached_yield: Optional[float] = None
        self._yield_timestamp: float = 0.0

        self._cached_funding_rates: Dict[str, float] = {}
        self._funding_timestamp: float = 0.0

        self._cached_tech: Optional[Dict[str, Dict[str, float]]] = None
        self._tech_timestamp: float = 0.0
        self._tech_cache_ttl: float = 3600  # 1 hour

        self._cached_fng: Optional[Dict[str, Any]] = None
        self._fng_timestamp: float = 0.0
        self._fng_cache_ttl: float = 3600  # 1 hour

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
                    raise Exception("Critical Market Data Unavailable: Both primary and fallback APIs failed.")

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

    async def _fetch_historical_data(self, client: httpx.AsyncClient) -> Dict[str, float]:
        """Fetch 14-day historical data to calculate annualized volatility and store prices for correlation."""
        volatility_dict = {}
        for gecko_id, symbol in _GECKO_ID_MAP.items():
            url = f"https://api.coingecko.com/api/v3/coins/{gecko_id}/market_chart?vs_currency=usd&days=14"
            try:
                response = await client.get(url, timeout=10.0)
                response.raise_for_status()
                data = response.json()
                prices = data.get("prices", [])
                
                if len(prices) > 1:
                    raw_prices = [p[1] for p in prices]
                    self._cached_historical_prices[symbol] = raw_prices
                    
                    returns = []
                    for i in range(1, len(prices)):
                        prev_price = prices[i-1][1]
                        curr_price = prices[i][1]
                        if prev_price > 0:
                            returns.append((curr_price - prev_price) / prev_price)
                    
                    if returns:
                        mean_return = sum(returns) / len(returns)
                        variance = sum((r - mean_return) ** 2 for r in returns) / len(returns)
                        std_dev = math.sqrt(variance)
                        # Annualize
                        annualized_volatility = std_dev * math.sqrt(365 * 24)
                        volatility_dict[symbol] = annualized_volatility
            except Exception as e:
                logger.error("Failed to fetch historical data for %s: %s", symbol, e)
                volatility_dict[symbol] = 0.0
                
            # Sleep slightly between requests to respect free tier rate limits
            await asyncio.sleep(1.5)
            
        return volatility_dict

    def get_correlation(self) -> float:
        """Calculates Pearson correlation between BTC and ETH using cached 14-day prices."""
        btc_prices = self._cached_historical_prices.get("BTC", [])
        eth_prices = self._cached_historical_prices.get("ETH", [])
        if not btc_prices or not eth_prices:
            return 0.0
        
        # Align lengths
        min_len = min(len(btc_prices), len(eth_prices))
        x = btc_prices[-min_len:]
        y = eth_prices[-min_len:]
        
        if min_len < 2: return 0.0
        
        mean_x = sum(x) / min_len
        mean_y = sum(y) / min_len
        
        cov = sum((x[i] - mean_x) * (y[i] - mean_y) for i in range(min_len))
        var_x = sum((x[i] - mean_x) ** 2 for i in range(min_len))
        var_y = sum((y[i] - mean_y) ** 2 for i in range(min_len))
        
        if var_x == 0 or var_y == 0: return 0.0
        return cov / math.sqrt(var_x * var_y)

    async def _fetch_defi_yields(self, client: httpx.AsyncClient) -> float:
        """Fetch stablecoin yields from DefiLlama and calculate an average APY."""
        try:
            response = await client.get(DEFILLAMA_YIELDS_URL, timeout=10.0)
            response.raise_for_status()
            data = response.json().get("data", [])
            
            stablecoins = {"USDC", "USDT", "DAI"}
            valid_yields = []
            for pool in data:
                if pool.get("symbol") in stablecoins and pool.get("tvlUsd", 0) > 10_000_000:
                    apy = pool.get("apy", 0)
                    if apy > 0:
                        valid_yields.append(apy)
            
            if valid_yields:
                valid_yields.sort(reverse=True)
                top_yields = valid_yields[:10]
                return sum(top_yields) / len(top_yields)
        except Exception as e:
            logger.error("Failed to fetch Defi yields: %s", e)
        return 0.0

    async def _fetch_derivatives(self, client: httpx.AsyncClient) -> Dict[str, float]:
        """Fetch funding rates from OKX Public API."""
        funding_rates = {"BTC": 0.0, "ETH": 0.0}
        try:
            for sym in ["BTC", "ETH"]:
                url = f"https://www.okx.com/api/v5/public/funding-rate?instId={sym}-USDT-SWAP"
                response = await client.get(url, timeout=5.0)
                response.raise_for_status()
                data = response.json()
                if data.get("code") == "0" and data.get("data"):
                    funding_rates[sym] = float(data["data"][0].get("fundingRate", 0.0))
        except Exception as e:
            logger.error("Failed to fetch derivatives from OKX: %s", e)
        return funding_rates

    async def get_volatility(self) -> Dict[str, float]:
        """Returns annualized volatility, utilizing a long-TTL cache."""
        now = time.time()
        if self._cached_volatility and (now - self._volatility_timestamp) < self._volatility_cache_ttl:
            return self._cached_volatility
            
        async with httpx.AsyncClient() as client:
            vol = await self._fetch_historical_data(client)
            self._cached_volatility = vol
            self._volatility_timestamp = now
            return vol

    async def get_yield(self) -> float:
        """Returns average stablecoin yield, utilizing a long-TTL cache."""
        now = time.time()
        if self._cached_yield is not None and (now - self._yield_timestamp) < self._yield_cache_ttl:
            return self._cached_yield
            
        async with httpx.AsyncClient() as client:
            avg_yield = await self._fetch_defi_yields(client)
            self._cached_yield = avg_yield
            self._yield_timestamp = now
            return avg_yield

    async def get_funding_rates(self) -> Dict[str, float]:
        """Returns perpetual funding rates, utilizing a long-TTL cache."""
        now = time.time()
        if self._cached_funding_rates and (now - self._funding_timestamp) < self._funding_cache_ttl:
            return self._cached_funding_rates
            
        async with httpx.AsyncClient() as client:
            rates = await self._fetch_derivatives(client)
            # Only cache if we actually got valid data (not the default 0.0 fallback from errors)
            if rates.get("BTC") != 0.0 or rates.get("ETH") != 0.0:
                self._cached_funding_rates = rates
                self._funding_timestamp = now
            elif self._cached_funding_rates:
                # If fetch failed but we have a stale cache, keep using it
                rates = self._cached_funding_rates
            return rates

    async def _fetch_klines(self, client: httpx.AsyncClient, symbol: str) -> Optional[pd.DataFrame]:
        """Fetch 100 daily klines from Kraken public API to avoid US geoblocking."""
        kraken_pair = _KRAKEN_PAIR_MAP.get(symbol)
        if not kraken_pair:
            return None
            
        url = f"https://api.kraken.com/0/public/OHLC?pair={kraken_pair}&interval=1440"
        try:
            response = await client.get(url, timeout=5.0)
            response.raise_for_status()
            data = response.json()
            
            if data.get("error"):
                logger.error("Kraken error for %s: %s", symbol, data["error"])
                return None
                
            # Kraken result key might be XXBTZUSD for BTC or XETHZUSD for ETH
            result_keys = list(data.get("result", {}).keys())
            data_key = [k for k in result_keys if k != "last"][0]
            
            klines = data["result"][data_key]
            # Kraken format: [time, open, high, low, close, vwap, volume, count]
            # We only need the last 100 rows
            klines = klines[-100:]
            
            df = pd.DataFrame(klines, columns=[
                "open_time", "open", "high", "low", "close", "vwap", "volume", "count"
            ])
            df["close"] = df["close"].astype(float)
            return df
        except Exception as e:
            logger.error("Failed to fetch klines for %s: %s", symbol, e)
            return None

    async def get_technical_indicators(self) -> Dict[str, Dict[str, float]]:
        """Returns EMA, RSI, and MACD for every Kraken-listed traded asset (1h cache).

        Assets without a Kraken USD pair (or with insufficient history) are simply
        omitted — never faked — so an agent told to cite indicators can't be forced
        to hallucinate them.
        """
        now = time.time()
        if self._cached_tech and (now - self._tech_timestamp) < self._tech_cache_ttl:
            return self._cached_tech

        indicators = {}
        async with httpx.AsyncClient() as client:
            for symbol in _KRAKEN_PAIR_MAP:
                df = await self._fetch_klines(client, symbol)
                if df is not None and len(df) >= 50:
                    try:
                        ema_20 = float(ta.ema(df["close"], length=20).iloc[-1])
                        ema_50 = float(ta.ema(df["close"], length=50).iloc[-1])
                        rsi_14 = float(ta.rsi(df["close"], length=14).iloc[-1])
                        macd = ta.macd(df["close"], fast=12, slow=26, signal=9)
                        macd_val = float(macd["MACD_12_26_9"].iloc[-1])
                        macd_signal = float(macd["MACDs_12_26_9"].iloc[-1])

                        indicators[symbol] = {
                            "EMA_20": ema_20,
                            "EMA_50": ema_50,
                            "RSI_14": rsi_14,
                            "MACD": macd_val,
                            "MACD_signal": macd_signal,
                        }
                    except Exception as e:
                        logger.error("Failed to calculate indicators for %s: %s", symbol, e)
                # Space out calls so eight sequential pairs don't trip Kraken's
                # public rate limit.
                await asyncio.sleep(0.3)

        if indicators:
            self._cached_tech = indicators
            self._tech_timestamp = now
        elif self._cached_tech:
            indicators = self._cached_tech
            
        return indicators

    async def get_fear_and_greed_index(self) -> Optional[Dict[str, Any]]:
        """Fetch Fear & Greed index from alternative.me API with 1-hour cache."""
        now = time.time()
        if self._cached_fng and (now - self._fng_timestamp) < self._fng_cache_ttl:
            return self._cached_fng
            
        url = "https://api.alternative.me/fng/"
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(url, timeout=5.0)
                response.raise_for_status()
                data = response.json()
                if data and data.get("data"):
                    fng_data = data["data"][0]
                    result = {
                        "value": int(fng_data.get("value", 50)),
                        "classification": fng_data.get("value_classification", "Neutral")
                    }
                    self._cached_fng = result
                    self._fng_timestamp = now
                    return result
        except Exception as e:
            logger.error("Failed to fetch Fear & Greed index: %s", e)
            
        return self._cached_fng

    async def get_market_state_summary(self) -> str:
        """
        Returns an enriched Market State Summary string including Spot, Volatility, Yield, and Derivatives.
        Used for LLM context injection.
        """
        prices = await self.get_prices()
        volatility = await self.get_volatility()
        avg_yield = await self.get_yield()
        funding_rates = await self.get_funding_rates()
        tech_indicators = await self.get_technical_indicators()
        correlation = self.get_correlation()
        fng = await self.get_fear_and_greed_index()
        
        lines: List[str] = []
        now = time.time()
        
        if self._cache_timestamp > 0 and (now - self._cache_timestamp) > self._cache_ttl:
            stale_minutes = (now - self._cache_timestamp) / 60
            lines.append(f"⚠️ **WARNING**: Live API is down. Data is stale by {stale_minutes:.1f} minutes. Exercise caution with market orders.")
        # Sort so BTC and ETH appear first, then the rest
        symbols = sorted(prices.keys(), key=lambda s: (s not in ("BTC", "ETH"), s))
        for symbol in symbols:
            entry = prices.get(symbol, {})
            price = entry.get("price", 0.0)
            change = entry.get("change_24h", 0.0)
            vol = volatility.get(symbol, 0.0)
            fund = funding_rates.get(symbol, 0.0)
            
            direction = "🟢" if change >= 0 else "🔴"
            vol_str = f"Vol: {vol*100:.1f}%" if vol > 0 else "Vol: N/A"
            fund_str = f"Fund: {fund*100:+.3f}%"
            
            tech = tech_indicators.get(symbol)
            if tech:
                tech_str = f" | EMA(20/50): {tech['EMA_20']:.0f}/{tech['EMA_50']:.0f} | RSI: {tech['RSI_14']:.1f} | MACD: {tech['MACD']:.1f}"
                lines.append(f"- **{symbol}**: ${price:,.2f} ({direction} {abs(change):.2f}% | {vol_str} | {fund_str}{tech_str})")
            else:
                lines.append(f"- **{symbol}**: ${price:,.2f} ({direction} {abs(change):.2f}% | {vol_str} | {fund_str})")
            
        yield_str = f"- **Stablecoin Yield**: {avg_yield:.1f}% APY"
        corr_str = f"- **BTC/ETH Correlation**: {correlation:.2f}"
        lines.append(corr_str)
        lines.append(yield_str)
        
        if fng:
            lines.append(f"- **Fear & Greed Index**: {fng['value']} ({fng['classification']})")

        # Deterministic signals derived from the indicators above (code, not LLM),
        # so agents reason over an explicit read rather than just raw numbers.
        try:
            from bot import signals as _signals
            sig_map = _signals.signals_for_assets(
                tech_indicators, funding_rates, fng.get("value") if fng else None
            )
            if sig_map:
                lines.append("")
                lines.append("**Deterministic Signals (computed, not LLM):**")
                lines.append(_signals.format_signals(sig_map))
        except Exception:
            logger.exception("Failed to compute deterministic signals")

        return "\n".join(lines)


price_feed = PriceFeed()
