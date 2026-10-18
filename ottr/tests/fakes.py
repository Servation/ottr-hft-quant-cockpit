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


# -- chat bot fakes --------------------------------------------------------------------------
CHAT_KEY = "chat-test-key-0123456789"


class FakeEngineApi:
    """The engine's HTTP API as the chat bot sees it, scripted in memory.

    Plugs into the bot's real ApiClient through httpx.MockTransport, records every
    request (so tests can prove which writes the bot makes), and can be taken down,
    put mid-meeting, or made to 404 like an older engine.
    """

    def __init__(self, key: Optional[str] = CHAT_KEY) -> None:
        now = time.time()
        self.key = key
        self.down = False
        self.missing: set = set()
        self.requests: List[Tuple[str, str, dict, Optional[dict]]] = []
        self.council = {"running": False, "started_at": None, "running_sec": None, "interval_hours": 4.0,
                        "timeout_sec": 300.0, "next_meeting_at": now + 7200,
                        "last_meeting": {"id": "m-1", "ts": now - 3600, "status": "ok"}}
        self.kill = {"effective_dry_run": False, "config_dry_run": False, "operator_engaged": False,
                     "since": None, "by": None, "reason": None}
        self.screen: Optional[dict] = None  # the latest screener run (None: 404 "no screen yet")
        self.data = {
            "/api/health": {"ts": now, "ok": True, "exit_code": 0, "engine_alive": None, "alarms": [],
                            "tasks": [], "ledger_problems": [], "blind": {}},
            "/api/book": {"as_of": now, "marked": True, "quotes_error": None, "equity": 10234.5,
                          "cash": "5000.00", "invested": 5234.5, "invested_pct": 0.5115,
                          "cost_basis": "5222.20", "unrealized_pnl": 12.3, "realized_pnl": "-4.10",
                          "fees": "8.00", "positions": 1,
                          "risk": {"halted": False, "halted_since": None, "peak_equity": 10400.0,
                                   "drawdown": 0.0159, "cooldowns": {"ETH": now + 600}},
                          "benchmarks": {"btc_hold": 10100.0, "equal_weight_hold": 9950.0, "base": {}},
                          "last_equity_point": None, "next_meeting_at": now + 7200},
            "/api/positions": {"as_of": now, "marked": True, "quotes_error": None, "positions": [
                {"asset": "ETH", "qty": "2.18", "cost": "5222.20", "avg_cost": "2395.50", "opened_ts": now - 86400,
                 "high_water": 2450.0, "trailing_stop": 2205.0, "bid": 2401.1, "ask": 2403.5,
                 "quote_source": "kraken", "quote_age_sec": 3.0, "value": 5234.5, "weight": 0.5115,
                 "cap_pct": 35.0, "unrealized_pnl": 12.3, "unrealized_pct": 0.0024,
                 "stop_distance_pct": 0.0889, "risk_blind_since": None}]},
            "/api/meetings": {"meetings": [{
                "id": "m-1", "ts": now - 3600, "status": "ok", "summary": "Buy ETH on a clean trend.",
                "dissent_notes": "Analyst C saw froth.", "errors": {}, "latency": {}, "tally": {},
                "trades": [{"action": "BUY", "asset": "ETH", "conviction": 0.7, "reasoning": "trend and momentum"}],
                "dropped": [{"trade": {"action": "SELL", "asset": "XRP", "fraction_of_position": 1.0},
                             "why": "not held"}],
                "orders": [{"asset": "ETH", "side": "BUY", "status": "filled", "reason": "ok",
                            "notional": 5222.2, "notes": [], "updated_ts": now - 3500}],
                "analysts": {
                    "claude": {"label": "A", "overall_view": "Risk-on but careful.", "latency_sec": 41.0,
                               "stances": [{"asset": "BTC", "stance": "HOLD", "conviction": 0.5,
                                            "thesis": "range", "key_risk": "macro", "price": 80000.0},
                                           {"asset": "ETH", "stance": "BUY", "conviction": 0.8,
                                            "thesis": "ETH reclaimed its 50-day EMA", "key_risk": "ETF outflows",
                                            "price": 2400.0}]},
                    "gpt": {"label": "B", "overall_view": "Momentum favors ETH.", "latency_sec": 55.0,
                            "stances": [{"asset": "ETH", "stance": "BUY", "conviction": 0.6, "thesis": "MACD cross",
                                         "key_risk": "chop", "price": 2400.0}]},
                }}], "next_before": None},
            "/api/fills": {"fills": [{"id": 7, "ts": now - 3500, "idem_key": "council:m-1:ETH", "asset": "ETH",
                                      "side": "BUY", "qty": "2.18", "price": "2395.5", "notional": "5222.20",
                                      "fee": "20.89", "realized_pnl": "0", "quote_source": "kraken",
                                      "origin": "council", "ref": "m-1", "reason": "trend and momentum"}],
                           "next_before": None},
        }

    def handler(self, request):
        import httpx
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, dict(request.headers), body))
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        path = request.url.path
        if path in self.missing:
            return httpx.Response(404, json={"error": f"no route {path}", "status": 404})
        if path == "/api/council":
            return httpx.Response(200, json=self.council)
        if path == "/api/screen":
            if self.screen is None:
                return httpx.Response(404, json={"error": "no screen yet: the engine runs one a day", "status": 404})
            return httpx.Response(200, json=self.screen)
        if path == "/api/kill-switch":
            if request.method == "POST":
                if not self.key or request.headers.get("authorization") != f"Bearer {self.key}":
                    return httpx.Response(401, json={"error": "missing or wrong API key", "status": 401})
                self.kill.update(operator_engaged=body["engaged"], since=time.time(), by=body.get("by"),
                                 reason=body.get("reason"),
                                 effective_dry_run=body["engaged"] or self.kill["config_dry_run"])
            return httpx.Response(200, json=self.kill)
        if request.method != "GET" or path not in self.data:
            return httpx.Response(404, json={"error": "not found", "status": 404})
        d = dict(self.data[path])
        if path in ("/api/health", "/api/book"):  # like the real API: the live kill switch state
            d["kill_switch"] = dict(self.kill)
        return httpx.Response(200, json=d)

    def client(self, key: Optional[str] = CHAT_KEY):
        import httpx
        from ottr.chat.client import ApiClient
        return ApiClient("http://127.0.0.1:8765/api", key, transport=httpx.MockTransport(self.handler))

    def writes(self) -> List[Tuple[str, str]]:
        return [(m, p) for m, p, _, _ in self.requests if m != "GET"]


class RecordingModel(ModelAdapter):
    """A chat model that records prompts, answers from a script and can be held mid-call."""

    name = "claude"

    def __init__(self, replies=("ETH is your only position.",), on_call=None) -> None:
        self.replies, self.prompts, self.on_call = list(replies), [], on_call
        self.hold: Optional[asyncio.Event] = None
        self.entered = asyncio.Event()

    async def complete(self, prompt: str, timeout: float = 240.0) -> str:
        self.prompts.append(prompt)
        if self.on_call:
            self.on_call()
        self.entered.set()
        if self.hold is not None:
            await self.hold.wait()
        r = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(r, Exception):
            raise r
        return r


# -- screener fakes --------------------------------------------------------------------------
def trend_rows(n: int = 200, drift: float = 0.004, noise: float = 0.01, start: float = 100.0,
               usd: float = 5e6, phase: int = 0, end_ts: Optional[int] = None):
    """Closed daily candles [(ts, close, usd_volume)] with a deterministic zig-zag around a
    drift: `drift` per day, +/- `noise` alternating (`phase` flips the zig-zag, which sets
    the correlation with another series)."""
    import math
    now = int(time.time())
    last = end_ts if end_ts is not None else now - now % DAY - DAY
    t0 = last - (n - 1) * DAY
    out, px = [], start
    for i in range(n):
        px *= math.exp(drift + (noise if (i + phase) % 2 else -noise))
        out.append((t0 + i * DAY, px, usd))
    return out


def sample_screen(cfg, qualifying: bool = True) -> dict:
    """A real screener result (dict form, as stored and served) built from synthetic candles."""
    return sample_screen_result(cfg, qualifying).to_dict()


def sample_screen_result(cfg, qualifying: bool = True):
    """The same, as the ScreenResult the engine gets from screen.run."""
    from ottr.market import screen
    btc = trend_rows(drift=0.001, noise=0.02)
    rows = [screen.measure("BTC", "XBTUSD", btc, btc, cfg, in_universe=True),
            screen.measure("ETH", "ETHUSD", trend_rows(drift=-0.002, noise=0.025), btc, cfg, in_universe=True),
            screen.measure("NEAR", "NEARUSD", trend_rows(drift=0.006 if qualifying else -0.003, noise=0.01,
                                                          phase=1), btc, cfg, in_universe=False),
            screen.measure("XMR", "XMRUSD", trend_rows(drift=0.003 if qualifying else -0.001, noise=0.012),
                           btc, cfg, in_universe=False)]
    res = screen.ScreenResult(id="s-test", ts=time.time(), rows=tuple(screen.score(rows)), rejected=(),
                              excluded={"stablecoin or fiat": 2}, listed=40,
                              settings={"top_n": 5})
    return res
