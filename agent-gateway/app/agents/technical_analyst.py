import logging
import re
import httpx
from typing import Dict, Any, List, Tuple
from app.config import settings, translate
from app.llm_connector import generate_chat_completion, LLMConfigurationError

logger = logging.getLogger(__name__)

def compute_ema(prices: List[float], period: int) -> float:
    if not prices:
        return 0.0
    if len(prices) < period:
        return prices[-1]
    multiplier = 2 / (period + 1)
    ema = sum(prices[:period]) / period  # Start with SMA
    for price in prices[period:]:
        ema = (price - ema) * multiplier + ema
    return ema

def compute_rsi(prices: List[float], period: int = 14) -> float:
    if len(prices) < period + 1:
        return 50.0
    
    gains = []
    losses = []
    for i in range(1, len(prices)):
        diff = prices[i] - prices[i-1]
        if diff > 0:
            gains.append(diff)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(diff))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

def compute_macd(prices: List[float]) -> Tuple[float, float, float]:
    if len(prices) < 26:
        return 0.0, 0.0, 0.0
    
    # Calculate fast (12) and slow (26) EMAs
    ema12_list = []
    ema26_list = []
    
    # Initialize
    ema12 = prices[0]
    ema26 = prices[0]
    m12 = 2 / 13
    m26 = 2 / 27
    
    macd_line_list = []
    for p in prices:
        ema12 = (p - ema12) * m12 + ema12
        ema26 = (p - ema26) * m26 + ema26
        macd_line_list.append(ema12 - ema26)
        
    # Signal Line (EMA 9 of MACD line)
    signal_line = macd_line_list[0]
    m9 = 2 / 10
    for m in macd_line_list:
        signal_line = (m - signal_line) * m9 + signal_line
        
    macd_line = macd_line_list[-1]
    macd_histogram = macd_line - signal_line
    return macd_line, signal_line, macd_histogram

class TechnicalAnalystAgent:
    async def fetch_historical_prices(self, symbol: str, interval: str = "1m", limit: int = 50) -> List[float]:
        """
        Fetches close prices from Binance or Yahoo Finance as a fallback for the specified timeframe and limit.
        """
        # Primary: Binance Klines
        try:
            url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval={interval}&limit={limit}"
            async with httpx.AsyncClient(timeout=5.0) as client:
                res = await client.get(url)
                if res.status_code == 200:
                    data = res.json()
                    # index 4 is the close price
                    return [float(candle[4]) for candle in data]
        except Exception as e:
            logger.warning(f"Failed to fetch historical klines from Binance ({interval}): {e}")

        # Fallback: Yahoo Finance
        try:
            yahoo_symbol = symbol.replace("USDT", "-USD") if symbol.endswith("USDT") else symbol
            y_interval = interval
            # Map Binance interval to Yahoo interval
            if interval == "1m":
                y_interval = "1m"
            elif interval == "5m":
                y_interval = "5m"
            elif interval == "1h":
                y_interval = "1h"
            elif interval == "1d":
                y_interval = "1d"
            elif interval == "1w":
                y_interval = "1wk"
            
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}?interval={y_interval}&range=5d"
            headers = {"User-Agent": "Mozilla/5.0"}
            async with httpx.AsyncClient(timeout=5.0) as client:
                res = await client.get(url, headers=headers)
                if res.status_code == 200:
                    data = res.json()
                    closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
                    # Filter out None values
                    valid_closes = [float(c) for c in closes if c is not None]
                    if valid_closes:
                        return valid_closes[-limit:]
        except Exception as e:
            logger.warning(f"Failed to fetch historical klines from Yahoo ({interval}): {e}")

        # Final fallback: Mock prices
        logger.error(f"Using mock historical prices for {symbol} ({interval})")
        base_price = 65000.0 if "BTC" in symbol else 3500.0
        import random
        prices = [base_price]
        for _ in range(limit - 1):
            prices.append(prices[-1] * (1 + random.uniform(-0.002, 0.002)))
        return prices

    async def fetch_order_book_imbalance(self, symbol: str) -> float:
        """
        Calculates the volume imbalance in the order book:
        (bid_volume - ask_volume) / (bid_volume + ask_volume) within a 1% price threshold.
        Positive indicates buy pressure; negative indicates sell pressure.
        """
        try:
            url = f"{settings.java_engine_url}/api/v1/orderbook/{symbol}"
            async with httpx.AsyncClient(timeout=2.0) as client:
                res = await client.get(url)
                if res.status_code == 200:
                    data = res.json()
                    bids = data.get("bids", [])
                    asks = data.get("asks", [])
                    
                    if bids and asks:
                        top_bid = float(bids[0].get("price", 0))
                        top_ask = float(asks[0].get("price", 0))
                        mid_price = (top_bid + top_ask) / 2.0
                        
                        bid_vol = sum(float(b.get("quantity", 0)) for b in bids if abs(float(b.get("price", 0)) - mid_price) / mid_price <= 0.01)
                        ask_vol = sum(float(a.get("quantity", 0)) for a in asks if abs(float(a.get("price", 0)) - mid_price) / mid_price <= 0.01)
                        
                        total_vol = bid_vol + ask_vol
                        if total_vol > 0:
                            return (bid_vol - ask_vol) / total_vol
        except Exception as e:
            logger.warning(f"Failed to fetch order book from Java matching engine: {e}. Trying public Binance fallback.")

        # Public Binance API fallback
        try:
            url = f"https://api.binance.com/api/v3/depth?symbol={symbol}&limit=100"
            async with httpx.AsyncClient(timeout=3.0) as client:
                res = await client.get(url)
                if res.status_code == 200:
                    data = res.json()
                    bids = data.get("bids", [])
                    asks = data.get("asks", [])
                    if bids and asks:
                        top_bid = float(bids[0][0])
                        top_ask = float(asks[0][0])
                        mid_price = (top_bid + top_ask) / 2.0
                        
                        bid_vol = sum(float(b[1]) for b in bids if abs(float(b[0]) - mid_price) / mid_price <= 0.01)
                        ask_vol = sum(float(a[1]) for a in asks if abs(float(a[0]) - mid_price) / mid_price <= 0.01)
                        
                        total_vol = bid_vol + ask_vol
                        if total_vol > 0:
                            return (bid_vol - ask_vol) / total_vol
        except Exception as e:
            logger.warning(f"Failed to fetch public Binance order book depth: {e}")

        return 0.0

    async def analyze(self, symbol: str, current_price: float, performance_history: str = "") -> Dict[str, Any]:
        """
        Performs technical analysis. Computes indicators, queries LLM, and parses signal.
        """
        # Fetch 1m prices for indicators (EMA20, RSI, MACD)
        prices_1m = await self.fetch_historical_prices(symbol, interval="1m", limit=50)
        
        # Fetch 1h prices for Macro EMA20
        prices_1h = await self.fetch_historical_prices(symbol, interval="1h", limit=50)
        
        # Fetch book imbalance
        book_imbalance = await self.fetch_order_book_imbalance(symbol)

        # Calculate technical indicators
        ema20 = compute_ema(prices_1m, 20)
        rsi = compute_rsi(prices_1m, 14)
        macd_line, macd_signal, macd_hist = compute_macd(prices_1m)
        macro_ema20 = compute_ema(prices_1h, 20)
        
        # Rule-based fallback signal
        fallback_signal = "HOLD"
        if rsi < 30 or (macd_hist > 0 and current_price > ema20):
            fallback_signal = "BUY"
        elif rsi > 70 or (macd_hist < 0 and current_price < ema20):
            fallback_signal = "SELL"
            
        indicators = {
            "ema20": ema20,
            "rsi": rsi,
            "macd_line": macd_line,
            "macd_signal": macd_signal,
            "macd_hist": macd_hist,
            "macro_ema20": macro_ema20,
            "book_imbalance": book_imbalance
        }

        # Query LLM
        prompt = f"""
You are the Technical Analyst Agent in a multi-agent crypto trading system.
Analyze the following parameters for {symbol}:
- Current Price: {current_price}
- Micro Trend (1m interval, 20-period EMA): {ema20:.4f}
- Micro RSI (1m interval, 14-period): {rsi:.2f}
- Micro MACD Line: {macd_line:.4f}
- Micro MACD Signal Line: {macd_signal:.4f}
- Micro MACD Histogram: {macd_hist:.4f}
- Macro Trend (1h interval, 20-period EMA): {macro_ema20:.4f} (Used to verify overall market direction)
- Order Book Imbalance: {book_imbalance:+.2%} (Positive indicates bids/buy pressure, negative indicates asks/sell pressure within a 1% price window)

Recent Journal History for {symbol}:
{{performance_history}}

First, think step-by-step about this data (trend direction, momentum strength, indicator alignment, order book bid/ask pressure, and performance of past trade decisions to avoid repeating errors). Provide your detailed quantitative reasoning and chain of thought.
Then, conclude your analysis. You must end your response with exactly:
SIGNAL: [BUY/SELL/HOLD]
CONFIDENCE: [value between 0.0 and 1.0]
""".format(performance_history=performance_history)

        messages = [
            {"role": "system", "content": "You are a quantitative trading assistant. Follow formatting rules precisely and think step-by-step before concluding."},
            {"role": "user", "content": prompt}
        ]

        try:
            analysis_text, inference_time = await generate_chat_completion(messages, timeout=4.0)
            
            # Parse signal
            signal_match = re.search(r"SIGNAL:\s*(BUY|SELL|HOLD)", analysis_text, re.IGNORECASE)
            confidence_match = re.search(r"CONFIDENCE:\s*([0-9.]+)", analysis_text)
            
            signal = signal_match.group(1).upper() if signal_match else fallback_signal
            confidence = float(confidence_match.group(1)) if confidence_match else 0.5
            
        except (LLMConfigurationError, Exception) as e:
            logger.warning(f"LLM analysis skipped or failed: {e}. Defaulting to rule-based analysis.")
            analysis_text = f"Rule-based fallback due to LLM offline: {e}"
            signal = fallback_signal
            confidence = 0.6
            inference_time = 0.0

        result = {
            "agent": "Technical Analyst",
            "symbol": symbol,
            "indicators": indicators,
            "analysis": analysis_text,
            "signal": signal,
            "confidence": confidence,
            "inference_time_ms": int(inference_time * 1000)
        }
        
        logger.info(translate("signal_generated", agent="TechnicalAnalyst", signal=f"{signal} (conf: {confidence})"))
        return result

technical_analyst = TechnicalAnalystAgent()

