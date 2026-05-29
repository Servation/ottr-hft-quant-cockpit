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
    async def fetch_historical_prices(self, symbol: str) -> List[float]:
        """
        Fetches the last 50 close prices from Binance or Yahoo Finance as a fallback.
        """
        # Primary: Binance Klines
        try:
            url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1m&limit=50"
            async with httpx.AsyncClient(timeout=5.0) as client:
                res = await client.get(url)
                if res.status_code == 200:
                    data = res.json()
                    # index 4 is the close price
                    return [float(candle[4]) for candle in data]
        except Exception as e:
            logger.warning(f"Failed to fetch historical klines from Binance: {e}")

        # Fallback: Yahoo Finance
        try:
            yahoo_symbol = symbol.replace("USDT", "-USD") if symbol.endswith("USDT") else symbol
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}?interval=1m&range=1d"
            headers = {"User-Agent": "Mozilla/5.0"}
            async with httpx.AsyncClient(timeout=5.0) as client:
                res = await client.get(url, headers=headers)
                if res.status_code == 200:
                    data = res.json()
                    closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
                    # Filter out None values
                    valid_closes = [float(c) for c in closes if c is not None]
                    if valid_closes:
                        return valid_closes[-50:]
        except Exception as e:
            logger.warning(f"Failed to fetch historical klines from Yahoo: {e}")

        # Final fallback: Mock prices
        logger.error(f"Using mock historical prices for {symbol}")
        base_price = 65000.0 if "BTC" in symbol else 3500.0
        import random
        prices = [base_price]
        for _ in range(49):
            prices.append(prices[-1] * (1 + random.uniform(-0.002, 0.002)))
        return prices

    async def analyze(self, symbol: str, current_price: float, performance_history: str = "") -> Dict[str, Any]:
        """
        Performs technical analysis. Computes indicators, queries LLM, and parses signal.
        """
        prices = await self.fetch_historical_prices(symbol)
        
        # Calculate technical indicators
        ema20 = compute_ema(prices, 20)
        rsi = compute_rsi(prices, 14)
        macd_line, macd_signal, macd_hist = compute_macd(prices)
        
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
            "macd_hist": macd_hist
        }

        # Query LLM
        prompt = f"""
You are the Technical Analyst Agent in a multi-agent crypto trading system.
Analyze the following parameters for {symbol}:
- Current Price: {current_price}
- 20-period EMA: {ema20:.4f}
- 14-period RSI: {rsi:.2f}
- MACD Line: {macd_line:.4f}
- MACD Signal Line: {macd_signal:.4f}
- MACD Histogram: {macd_hist:.4f}

Recent Journal History for {symbol}:
{performance_history}

First, think step-by-step about this data (trend direction, momentum strength, indicator alignment, and performance of past trade decisions to avoid repeating errors). Provide your detailed quantitative reasoning and chain of thought.
Then, conclude your analysis. You must end your response with exactly:
SIGNAL: [BUY/SELL/HOLD]
CONFIDENCE: [value between 0.0 and 1.0]
"""

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
