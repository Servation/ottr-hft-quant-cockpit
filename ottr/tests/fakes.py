"""
Test doubles: a US market rig, scripted model adapters, a Discord webhook stand-in, and
the engine's API as the chat bot sees it.

The market rig is the real MarketData over FakeAlpaca, on a fake clock set to a
mid-session Wednesday of the 2026 NYSE calendar, with a trending year of daily bars and
live quotes for every core and reference symbol. Engine and pipeline tests drive it
through the same code paths production uses, offline and deterministically.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

from ottr.clock import FakeClock
from ottr.council.council import Council
from ottr.council.models import ModelAdapter
from ottr.ledger.book import Ledger
from ottr.market.alpaca import Alpaca
from ottr.market.calendar import ET
from ottr.market.http import RateLimiter
from ottr.market.service import MarketData
from tests.fake_alpaca import CREDS, FakeAlpaca


def et(y, m, d, h, mi=0) -> float:
    return datetime(y, m, d, h, mi, tzinfo=ET).timestamp()


OPEN = et(2026, 10, 21, 11)          # a Wednesday, mid-session (after CAT's 2026-10-12 start)


def trend(days, start: float, step: float) -> Dict[date, float]:
    """Closes drifting by `step` a session with a +-0.4% zig-zag (about 12.7% annual vol)."""
    return {d: round(start * (1 + step) ** i * (1 + (0.004 if i % 2 else -0.004)), 4) for i, d in enumerate(days)}


@dataclass
class MarketRig:
    fake: FakeAlpaca
    clock: FakeClock
    market: MarketData

    def at(self, ts: float, requote: bool = True) -> None:
        """Move market time (the clock and Alpaca's idea of now) and refresh quotes."""
        self.clock.set(ts)
        self.fake.now = ts
        if requote:
            self.requote()

    def price(self, symbol: str) -> float:
        return self.fake.bars[symbol][-1]["c"]

    def requote(self, spread_pct: float = 0.02, age: float = 2.0) -> None:
        for s in self.fake.bars:
            p = self.price(s)
            h = p * spread_pct / 200
            self.fake.add_quote(s, round(p - h, 2), round(p + h, 2), age=age)

    async def ready(self) -> "MarketRig":
        await self.market.refresh_calendar()
        await self.market.refresh_bars(force=True)
        return self


HISTORY_SESSIONS = 100   # enough for every indicator (60-session vol, EMA50, MACD), and quick


def market_rig(cfg, store, now: float = OPEN, through: Optional[date] = None) -> MarketRig:
    """FakeAlpaca with the last HISTORY_SESSIONS sessions of history for every core and
    reference symbol (through the session before `now`, or before `through`), asset
    records, live quotes, and MarketData on it."""
    f = FakeAlpaca(now)
    today = datetime.fromtimestamp(now, tz=ET).date()
    days = [s.day for s in f.sessions if s.day < (through or today)][-HISTORY_SESSIONS:]
    for i, s in enumerate(list(cfg.universe.core) + list(cfg.universe.reference)):
        step = -0.0015 if s in ("XLU", "XLP", "XLV", "XLRE") else 0.0008 + 0.00005 * i
        f.add_daily_bars(s, trend(days, 50.0 + 10 * i, step))
        f.add_asset(s, fractionable=s != "PDBC")
    clock = FakeClock(now)
    alpaca = Alpaca(CREDS, client=f.client(), limiter=RateLimiter(1e6), backoff=0, clock=clock)
    rig = MarketRig(f, clock, MarketData(cfg, alpaca, store, clock))
    rig.requote()
    return rig


def open_book(store, cash: float = 10000.0, now: float = OPEN - 7 * 86400) -> Ledger:
    """A ledger opened a week before the rig's day, so every test fill comes after it."""
    lg = Ledger(store)
    lg.initialize(cash, now=now)
    return lg


def no_corporate_actions(store, symbols, now: float = OPEN) -> None:
    """Record a clean corporate-actions fetch for `symbols` (else every BUY is blocked as
    "corporate actions unknown", which is right for a real book and noise in most tests)."""
    from datetime import timedelta
    from ottr.events import db
    from ottr.events.view import FEED_CORPORATE
    day = datetime.fromtimestamp(now, tz=ET).date()
    db.save_corporate_actions(store, [], symbols, day - timedelta(days=60), day + timedelta(days=120), now)
    # The feed's own record, stamped with the wall clock: health judges feeds by real time.
    db.record_feed(store, FEED_CORPORATE, True, "test: none pending", time.time())


class ScriptedModel(ModelAdapter):
    """Answers from a script. `delay` makes it slow; with `gate` it answers only once the
    event is set (a model held mid-call, so a test can look at a meeting in progress)."""

    def __init__(self, name: str, replies, delay: float = 0.0, gate: Optional[asyncio.Event] = None) -> None:
        self.name, self.replies, self.prompts = name, list(replies), []
        self.delay, self.gate = delay, gate

    async def complete(self, prompt: str, timeout: float = 240.0) -> str:
        self.prompts.append(prompt)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.gate is not None:
            await self.gate.wait()
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


def council_for(cfg, analyst_replies, chair_reply, universe=None):
    analysts = {n: ScriptedModel(n, [r]) for n, r in zip(("claude", "gpt", "gemini"), analyst_replies)}
    return Council(analysts, ScriptedModel("chair", [chair_reply]), list(universe or cfg.core),
                   min_analysts=2, max_trades=3)


def buy_council(cfg, asset: str = "TLT", conviction: float = 0.7):
    """A council whose analysts back `asset` and whose chair buys it."""
    u = list(cfg.core)
    reps = [analyst_reply(u, {asset: ("BUY", 0.8)}), analyst_reply(u, {asset: ("BUY", 0.6)}), analyst_reply(u)]
    chair = {"summary": f"buy {asset.lower()}", "dissent_notes": "",
             "trades": [{"action": "BUY", "asset": asset, "conviction": conviction, "reasoning": "trend"}]}
    return council_for(cfg, reps, chair)


class LiveRecorder:
    """A Discord webhook stand-in: plain posts, plus the create/edit pair live messages use.

    Records every call with the loop time it started, so tests can count posts vs edits
    and measure edit spacing. `fail_edits` / `fail_create` make Discord refuse, and
    `hold` (an Event) makes edits hang until it is set (Discord very slow)."""

    def __init__(self) -> None:
        self.posts: List[str] = []
        self.calls: List[Tuple[str, float, str]] = []   # (create | edit, loop time, text)
        self.fail_edits = False
        self.fail_create = False
        self.raise_on_edit = False
        self.hold: Optional[asyncio.Event] = None
        self.shown: Optional[str] = None  # what Discord shows now: the last text that landed
        self._next = 1000

    async def __call__(self, text: str) -> bool:
        self.posts.append(text)
        return True

    async def create(self, text: str) -> Optional[str]:
        self.calls.append(("create", asyncio.get_running_loop().time(), text))
        if self.fail_create:
            return None
        self._next += 1
        self.shown = text
        return str(self._next)

    async def edit(self, message_id: str, text: str) -> bool:
        self.calls.append(("edit", asyncio.get_running_loop().time(), text))
        if self.hold is not None:
            await self.hold.wait()
        if self.raise_on_edit:
            raise RuntimeError("transport bug")
        if self.fail_edits:
            return False
        self.shown = text
        return True

    def kinds(self) -> List[str]:
        return [k for k, _, _ in self.calls]


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
        self.council = {"running": False, "started_at": None, "running_sec": None,
                        "meeting_times_et": ["10:15", "15:15"], "timeout_sec": 300.0,
                        "next_meeting_at": now + 7200,
                        "last_meeting": {"id": "m-1", "ts": now - 3600, "status": "ok"}}
        self.kill = {"effective_dry_run": False, "config_dry_run": False, "operator_engaged": False,
                     "since": None, "by": None, "reason": None}
        self.data = {
            "/api/health": {"ts": now, "ok": True, "exit_code": 0, "engine_alive": None, "alarms": [],
                            "tasks": [], "ledger_problems": [], "blind": {}},
            "/api/book": {"as_of": now, "marked": True, "prices_error": None, "equity": 10234.5,
                          "market": {"state": "open", "open": True,
                                     "session": {"day": "2026-10-21", "open_ts": now - 7200,
                                                 "close_ts": now + 3600, "early_close": False},
                                     "next_open_ts": now + 86400},
                          "cash": "5000.00", "invested": 5234.5, "invested_pct": 0.5115,
                          "cost_basis": "5222.20", "unrealized_pnl": 12.3, "realized_pnl": "-4.10",
                          "fees": "0.02", "positions": 1,
                          "risk": {"halted": False, "halted_since": None, "peak_equity": 10400.0,
                                   "drawdown": 0.0159, "cooldowns": {"TLT": now + 600}},
                          "benchmarks": {"spy_hold": 10100.0, "sixty_forty": 9950.0, "base": {}},
                          "last_equity_point": None, "next_meeting_at": now + 7200},
            "/api/positions": {"as_of": now, "marked": True, "prices_error": None, "positions": [
                {"asset": "TLT", "class": "bond_etf", "member": True, "qty": "58.0", "cost": "5222.20",
                 "avg_cost": "90.04", "opened_ts": now - 86400, "high_water": 92.0, "trailing_stop": 82.8,
                 "bid": 90.25, "ask": 90.27, "price_basis": "live", "price_age_sec": 3.0, "value": 5234.5,
                 "weight": 0.5115, "cap_pct": 25.0, "unrealized_pnl": 12.3, "unrealized_pct": 0.0024,
                 "stop_distance_pct": 0.0889, "risk_blind_since": None}]},
            "/api/meetings": {"meetings": [{
                "id": "m-1", "ts": now - 3600, "status": "ok", "summary": "Buy TLT as long yields roll over.",
                "dissent_notes": "Analyst C saw sticky inflation.", "errors": {}, "latency": {}, "tally": {},
                "trades": [{"action": "BUY", "asset": "TLT", "conviction": 0.7, "reasoning": "rates and trend"}],
                "dropped": [{"trade": {"action": "SELL", "asset": "GLD", "fraction_of_position": 1.0},
                             "why": "not held"}],
                "orders": [{"asset": "TLT", "side": "BUY", "status": "filled", "reason": "ok",
                            "notional": 5222.2, "notes": [], "updated_ts": now - 3500}],
                "analysts": {
                    "claude": {"label": "A", "overall_view": "Risk-on but careful.", "latency_sec": 41.0,
                               "stances": [{"asset": "SPY", "stance": "HOLD", "conviction": 0.5,
                                            "thesis": "range", "key_risk": "macro", "price": 773.0},
                                           {"asset": "TLT", "stance": "BUY", "conviction": 0.8,
                                            "thesis": "TLT reclaimed its 50-day EMA", "key_risk": "hot CPI",
                                            "price": 90.2}]},
                    "gpt": {"label": "B", "overall_view": "Duration looks cheap.", "latency_sec": 55.0,
                            "stances": [{"asset": "TLT", "stance": "BUY", "conviction": 0.6, "thesis": "MACD cross",
                                         "key_risk": "chop", "price": 90.2}]},
                }}], "next_before": None},
            "/api/fills": {"fills": [{"id": 7, "ts": now - 3500, "idem_key": "council:m-1:TLT", "asset": "TLT",
                                      "side": "BUY", "qty": "58.0", "price": "90.04", "notional": "5222.20",
                                      "fee": "0.00", "realized_pnl": "0", "quote_source": "alpaca",
                                      "origin": "council", "ref": "m-1", "reason": "rates and trend"}],
                           "next_before": None},
            "/api/screen": {"screen": None, "satellites": [], "changes": [], "schedule": "Sat 08:00 ET weekly",
                            "satellites_max": 6},
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

    def __init__(self, replies=("TLT is your only position.",), on_call=None) -> None:
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
