"""
Test doubles: a scripted market and scripted model adapters.

FakeMarket implements the MarketData surface the engine/executor use (quotes, candles,
snapshot) from in-memory prices, so pipeline tests run offline and deterministically.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Dict, List, Optional, Tuple

from ottr.council.council import Council
from ottr.council.models import ModelAdapter
from ottr.market.service import MarketSnapshot, build_view
from ottr.market.types import MarketDataError, Quote

DAY = 86400
PRICES = {"BTC": 80000.0, "ETH": 2400.0, "SOL": 100.0, "XRP": 1.3, "ADA": 0.22,
          "DOGE": 0.08, "LINK": 12.0, "AVAX": 9.8}


def trending_closes(start: float, n: int = 120, step: float = 0.004) -> List[Tuple[int, float]]:
    # Ends at yesterday's 00:00 UTC open, like Kraken's newest closed daily candle, so
    # the engine's market heartbeat sees fresh closes.
    now = int(time.time())
    t0 = now - now % DAY - n * DAY
    out, px = [], start
    for i in range(n):
        px *= 1 + step + (0.01 if i % 2 else -0.01)
        out.append((t0 + i * DAY, px))
    return out


class FakeMarket:
    def __init__(self, cfg, prices: Dict[str, float], spread_pct: float = 0.1) -> None:
        self.cfg = cfg
        self.prices = dict(prices)
        self.spread_pct = spread_pct
        self.fail = False
        self.age = 0.0
        self.delay = 0.0  # seconds a quotes() call takes (a slow exchange)
        self.candle_rows = {a: trending_closes(p / 1.6) for a, p in prices.items()}
        self.quote_calls = 0

    def _q(self, a: str, p: float) -> Quote:
        half = p * self.spread_pct / 200
        return Quote(a, p - half, p + half, p, time.time() - self.age, "kraken", high_24h=p, low_24h=p)

    async def quotes(self, force: bool = False) -> Dict[str, Quote]:
        self.quote_calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise MarketDataError("scripted outage")
        return {a: self._q(a, p) for a, p in self.prices.items()}

    async def candles(self, force: bool = False):
        return dict(self.candle_rows)

    async def snapshot(self) -> MarketSnapshot:
        q = await self.quotes()
        btc = [c for _, c in self.candle_rows.get("BTC", [])]
        views = {a: build_view(a, q.get(a), self.candle_rows.get(a, []), btc, self.cfg)
                 for a in self.cfg.universe}
        return MarketSnapshot(as_of=time.time(), assets=views)


class ScriptedModel(ModelAdapter):
    def __init__(self, name: str, replies) -> None:
        self.name, self.replies, self.prompts = name, list(replies), []

    async def complete(self, prompt: str, timeout: float = 240.0) -> str:
        self.prompts.append(prompt)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r if isinstance(r, str) else json.dumps(r)


def analyst_reply(universe, overrides: Optional[Dict[str, Tuple[str, float]]] = None) -> dict:
    overrides = overrides or {}
    return {"overall_view": "test view", "assets": [
        {"asset": a, "stance": overrides.get(a, ("HOLD", 0.5))[0],
         "conviction": overrides.get(a, ("HOLD", 0.5))[1], "thesis": "t", "key_risk": "r"}
        for a in universe]}


def council_for(cfg, analyst_replies, chair_reply):
    analysts = {n: ScriptedModel(n, [r]) for n, r in zip(("claude", "gpt", "gemini"), analyst_replies)}
    return Council(analysts, ScriptedModel("chair", [chair_reply]), cfg.universe,
                   min_analysts=2, max_trades=3)


def eth_buy_council(cfg):
    """A council whose analysts back ETH and whose chair buys it."""
    u = cfg.universe
    reps = [analyst_reply(u, {"ETH": ("BUY", 0.8)}), analyst_reply(u, {"ETH": ("BUY", 0.6)}), analyst_reply(u)]
    chair = {"summary": "buy eth", "dissent_notes": "",
             "trades": [{"action": "BUY", "asset": "ETH", "conviction": 0.7, "reasoning": "trend"}]}
    return council_for(cfg, reps, chair)
