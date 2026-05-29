import logging
import re
import httpx
from typing import Dict, Any, List, Tuple
from app.config import settings, translate
from app.services.market_proxy import market_proxy
from app.services.sopr_provider import sopr_provider
from app.llm_connector import generate_chat_completion, LLMConfigurationError

logger = logging.getLogger(__name__)

class SentimentAnalystAgent:
    async def fetch_fear_and_greed_index(self) -> Tuple[int, str]:
        """
        Fetches the current crypto Fear and Greed index.
        Returns a tuple of (value, classification).
        """
        try:
            url = "https://api.alternative.me/fng/?limit=1"
            async with httpx.AsyncClient(timeout=3.0) as client:
                res = await client.get(url)
                if res.status_code == 200:
                    data = res.json()
                    item = data.get("data", [{}])[0]
                    value = int(item.get("value", 50))
                    fng_class = item.get("value_classification", "Neutral")
                    return value, fng_class
        except Exception as e:
            logger.warning(f"Failed to fetch Fear & Greed index: {e}")
        return 50, "Neutral"

    async def harvest_market_narratives(self, headlines: List[str]) -> str:
        """
        Takes news headlines and prompts the LLM to summarize the 3-5 major trending market narratives (memes/themes)
        and label them as Bullish, Bearish, or Neutral.
        """
        if not headlines:
            return "No active headlines to harvest narratives."
        
        headlines_str = "\n".join([f"- {h}" for h in headlines[:20]])
        prompt = f"""
You are the Sentiment Analyst Agent. Given these recent crypto news headlines:
{headlines_str}

Identify the top 3-5 active market narratives or "memes" currently driving price action.
For each narrative, state if it is a Bullish Catalyst, a Bearish FUD narrative, or Neutral, and provide a 1-sentence description.
Be concise.
"""
        messages = [
            {"role": "system", "content": "You are a quantitative trading assistant. Extract market narratives and format as a concise list."},
            {"role": "user", "content": prompt}
        ]
        
        try:
            narratives, _ = await generate_chat_completion(messages, timeout=4.0)
            return narratives.strip()
        except Exception as e:
            logger.warning(f"Failed to harvest market narratives via LLM: {e}. Using fallback.")
            return (
                "- ETF Flow Momentum (Bullish Catalyst: Strong institutional demand)\n"
                "- Regulatory Uncertainty (Bearish FUD: Ongoing litigation and rules)\n"
                "- Macro Interest Rate Expectations (Neutral: Focus on central bank rate cuts)"
            )

    async def analyze(self, symbol: str, performance_history: str = "") -> Dict[str, Any]:
        """
        Performs sentiment analysis using SOPR, Fear & Greed index, and harvested headlines.
        """
        # Fetch SOPR, news, and Fear & Greed Index
        sopr = await sopr_provider.get_sopr()
        news = await market_proxy.get_news()
        fng_value, fng_class = await self.fetch_fear_and_greed_index()
        
        # Compile headlines
        headlines = [item["title"] for item in news]
        headlines_str = "\n".join([f"- {h}" for h in headlines]) if headlines else "- No recent news available"

        # Harvest active narratives
        narratives = await self.harvest_market_narratives(headlines)

        # Rule-based fallback signal
        fallback_signal = "HOLD"
        if sopr < 0.995 or fng_value < 30:
            fallback_signal = "BUY"
        elif sopr > 1.015 or fng_value > 80:
            fallback_signal = "SELL"

        # Compile LLM prompt
        prompt = f"""
You are the Sentiment Analyst Agent in a multi-agent crypto trading system.
Analyze the sentiment for the cryptocurrency market (specifically {symbol}).

Market Psychology Indicators:
- Daily Fear & Greed Index: {fng_value} ({fng_class})
- SOPR (Spent Output Profit Ratio): {sopr:.5f}
(A SOPR > 1 means coins spent are profitable on average. SOPR < 1 suggests panic/capitulation)

Current Market Memes & Narratives (Dynamically Harvested):
{narratives}

Recent News Headlines for context:
{headlines_str}

Recent Journal History for {symbol}:
{{performance_history}}

First, think step-by-step about this data (news sentiment trends, panic/capitulation cues, on-chain indicators, how these dynamic memes impact {symbol}, and outcomes of past trade decisions to avoid repeating errors). Provide your detailed quantitative reasoning and chain of thought.
Then, conclude your analysis. You must end your response with exactly:
SIGNAL: [BUY/SELL/HOLD]
CONFIDENCE: [value between 0.0 and 1.0]
""".format(performance_history=performance_history)

        messages = [
            {"role": "system", "content": "You are a crypto sentiment analyst assistant. Follow formatting rules precisely and think step-by-step before concluding."},
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
            logger.warning(f"LLM sentiment analysis skipped or failed: {e}. Defaulting to rule-based analysis.")
            analysis_text = f"Rule-based fallback due to LLM offline: {e}"
            signal = fallback_signal
            confidence = 0.6
            inference_time = 0.0

        result = {
            "agent": "Sentiment Analyst",
            "symbol": symbol,
            "sopr": sopr,
            "headlines": headlines,
            "fear_and_greed_index": fng_value,
            "fear_and_greed_class": fng_class,
            "extracted_narratives": narratives,
            "analysis": analysis_text,
            "signal": signal,
            "confidence": confidence,
            "inference_time_ms": int(inference_time * 1000)
        }
        
        logger.info(translate("signal_generated", agent="SentimentAnalyst", signal=f"{signal} (conf: {confidence})"))
        return result

sentiment_analyst = SentimentAnalystAgent()

