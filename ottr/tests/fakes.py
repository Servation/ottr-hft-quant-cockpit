"""
Test doubles: a scripted market and scripted model adapters.

FakeMarket implements the MarketData surface the engine/executor use (quotes, candles,
snapshot) from in-memory prices, so pipeline tests run offline and deterministically.
"""

from __future__ import annotations

import json
import time
from typing import Dict, List, Optional, Tuple

from ottr.council.models import ModelAdapter
from ottr.market.service import MarketSnapshot, build_view
from ottr.market.types import MarketDataError, Quote

DAY = 86400


def trending_closes(start: float, n: int = 120, step: float = 0.004) -> List[Tuple[int, float]]:
    t0 = 1_700_000_000 - (1_700_000_000 % DAY)
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
        self.candle_rows = {a: trending_closes(p / 1.6) for a, p in prices.items()}
        self.quote_calls = 0

    def _q(self, a: str, p: float) -> Quote:
        half = p * self.spread_pct / 200
        return Quote(a, p - half, p + half, p, time.time() - self.age, "kraken", high_24h=p, low_24h=p)

    async def quotes(self, force: bool = False) -> Dict[str, Quote]:
        self.quote_calls += 1
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
