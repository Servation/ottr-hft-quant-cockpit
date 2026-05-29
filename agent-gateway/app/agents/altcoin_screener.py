import logging
import httpx
from typing import List
from app.config import translate

logger = logging.getLogger(__name__)

class AltcoinScreenerAgent:
    def __init__(self):
        self.candidates = [
            "SOLUSDT", "ADAUSDT", "XRPUSDT", "DOTUSDT", "DOGEUSDT",
            "AVAXUSDT", "LINKUSDT", "LTCUSDT", "UNIUSDT", "NEARUSDT"
        ]

    async def screen_altcoins(self) -> List[str]:
        """
        Query Binance ticker 24hr statistics, filter candidate altcoins,
        sort them by 24h price change percentage, and return the top 2.
        """
        try:
            url = "https://api.binance.com/api/v3/ticker/24hr"
            async with httpx.AsyncClient(timeout=5.0) as client:
                res = await client.get(url)
                if res.status_code == 200:
                    data = res.json()
                    # Filter and map
                    screened = []
                    for item in data:
                        symbol = item.get("symbol")
                        if symbol in self.candidates:
                            try:
                                change_pct = float(item.get("priceChangePercent", 0.0))
                                screened.append((symbol, change_pct))
                            except ValueError:
                                continue
                    
                    # Sort by change percentage descending
                    screened.sort(key=lambda x: x[1], reverse=True)
                    top_coins = [x[0] for x in screened[:2]]
                    logger.info(f"Altcoin Screener screened candidates: {screened}. Top 2 selected: {top_coins}")
                    return top_coins
                else:
                    logger.warning(f"Binance 24hr ticker API returned status code {res.status_code}. Using fallback.")
        except Exception as e:
            logger.warning(f"Failed to screen altcoins via Binance API: {e}. Using fallback.")

        # Fallback list if API fails
        fallback_coins = ["SOLUSDT", "AVAXUSDT"]
        logger.info(f"Altcoin Screener returning fallback altcoins: {fallback_coins}")
        return fallback_coins

altcoin_screener = AltcoinScreenerAgent()
