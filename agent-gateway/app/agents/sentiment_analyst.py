import logging
import re
from typing import Dict, Any, List
from app.config import settings, translate
from app.services.market_proxy import market_proxy
from app.services.sopr_provider import sopr_provider
from app.llm_connector import generate_chat_completion, LLMConfigurationError

logger = logging.getLogger(__name__)

class SentimentAnalystAgent:
    async def analyze(self, symbol: str, performance_history: str = "") -> Dict[str, Any]:
        """
        Performs sentiment analysis using SOPR and recent news feeds.
        """
        # Fetch SOPR and news feeds
        sopr = await sopr_provider.get_sopr()
        news = await market_proxy.get_news()
        
        # Compile headlines
        headlines = [item["title"] for item in news]
        headlines_str = "\n".join([f"- {h}" for h in headlines]) if headlines else "- No recent news available"

        # Rule-based fallback signal
        # Economic theory:
        # SOPR < 1 indicates capitulation (underpriced / buying opportunity).
        # SOPR > 1 indicates profit taking / potential resistance.
        fallback_signal = "HOLD"
        if sopr < 0.995:
            fallback_signal = "BUY"
        elif sopr > 1.015:
            fallback_signal = "SELL"

        # Compile LLM prompt
        prompt = f"""
You are the Sentiment Analyst Agent in a multi-agent crypto trading system.
Analyze the sentiment for the cryptocurrency market (specifically {symbol}).

Market Indicators:
- SOPR (Spent Output Profit Ratio): {sopr:.5f}
(A SOPR > 1 means coin spent on-chain are profitable on average. SOPR < 1 means coins are spent at a loss, suggesting panic/capitulation)

Recent News Headlines:
{headlines_str}

Recent Journal History for {symbol}:
{performance_history}

First, think step-by-step about this data (news sentiment trends, panic/capitulation cues, on-chain indicators, and outcomes of past trade decisions to avoid repeating errors). Provide your detailed qualitative reasoning and chain of thought.
Then, conclude your analysis. You must end your response with exactly:
SIGNAL: [BUY/SELL/HOLD]
CONFIDENCE: [value between 0.0 and 1.0]
"""

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
            "analysis": analysis_text,
            "signal": signal,
            "confidence": confidence,
            "inference_time_ms": int(inference_time * 1000)
        }
        
        logger.info(translate("signal_generated", agent="SentimentAnalyst", signal=f"{signal} (conf: {confidence})"))
        return result

sentiment_analyst = SentimentAnalystAgent()
