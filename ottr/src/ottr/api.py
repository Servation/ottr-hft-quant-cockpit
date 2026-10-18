"""
Read-only HTTP API for the P4 dashboard, plus an authenticated kill switch.

What it does: serves JSON views of the book, positions, equity curve with benchmarks,
fills, meetings (with every analyst's stance), audit log and health, from inside the
engine process on 127.0.0.1. `POST /api/kill-switch` engages or releases the runtime dry
run and needs `Authorization: Bearer $OTTR_API_KEY`.

Why it can't corrupt anything: every GET reads through the API's own SQLite connection
opened `mode=ro` (SQLite itself refuses writes on it), inside one snapshot per request so
cash and positions always come from the same commit. The only write, the kill switch,
goes through the engine's KillSwitch on the engine's single writer connection.

Why the Host check: a web page can point its own domain at 127.0.0.1 (DNS rebinding) and
then read a loopback API from the browser. Requests whose Host isn't a loopback name are
refused. The key travels in a header, never a query string or form, so a cross-site form
post can't use it either.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import math
import re
import time
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from aiohttp import web

from ottr import health
from ottr.config import Config
from ottr.engine.engine import Engine, benchmarks
from ottr.engine.killswitch import KillSwitch
from ottr.ledger.book import Ledger
from ottr.ledger.store import Store
from ottr.market.types import Quote
from ottr.risk.monitor import RiskState

logger = logging.getLogger(__name__)

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
MAX_BODY = 16 * 1024
QUOTES_TIMEOUT = 5.0
KIND_RE = re.compile(r"^[a-z_]{1,40}$")
MEETING_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
BY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,40}$")

ENDPOINTS = [
    "GET  /api/health           alarms, task heartbeats, ledger check, kill switch",
    "GET  /api/heartbeats       per-task heartbeat age vs its stale limit",
    "GET  /api/book             equity, cash, P&L, risk state, benchmarks",
    "GET  /api/positions        holdings marked at the bid, with stop levels",
    "GET  /api/equity           equity curve + BTC and equal-weight benchmarks (?since=&until=&max_points=)",
    "GET  /api/fills            recent fills, newest first (?limit=&before=<id>)",
    "GET  /api/meetings         council meetings with analyst stances (?limit=&before=<ts>)",
    "GET  /api/meetings/{id}    one meeting in full, incl. the packet the models saw",
    "GET  /api/audit            audit log (?kind=&since=&limit=)",
    "GET  /api/council          whether a meeting is running now and how far it has got, the next and the last meeting",
    "GET  /api/kill-switch      kill switch state",
    "POST /api/kill-switch      {\"engaged\": true|false, \"reason\": \"...\", \"by\": \"...\"}; empty body engages. Bearer auth.",
]


@dataclass
class ApiContext:
    cfg: Config
    reader: Store                                    # read-only connection
    quotes: Callable[[], Awaitable[Dict[str, Quote]]]
    kill: KillSwitch
    set_kill: Callable[[bool, str, str], dict]       # (engaged, by, reason) -> state
    health: Callable[[Store], dict]
    api_key: Optional[str]
    meeting_started: Callable[[], Optional[float]] = lambda: None
    meeting_progress: Callable[[], Optional[dict]] = lambda: None

    @classmethod
    def from_engine(cls, engine: Engine, reader: Store) -> "ApiContext":
        def health_view(store: Store) -> dict:
            now = time.time()
            rep = health.evaluate(engine.cfg, store, now, inside_engine=True, epoch=engine.epoch,
                                  extra=list(engine.problems.values()))
            d = rep.to_dict()
            d["active_alarms"] = [dict(asdict(a), since=engine.watchdog.since.get(k))
                                  for k, a in sorted(engine.watchdog.active.items())]
            d["engine"] = store.get_state("engine")
            return d

        def set_kill(engaged: bool, by: str, reason: str) -> dict:
            state = engine.kill.set(engaged, by=by, reason=reason)
            logger.warning("kill switch %s by %s (%s); effective dry run %s",
                           "ENGAGED" if engaged else "released", by, reason or "no reason",
                           state["effective_dry_run"])
            engine.alerts.event("kill_switch", {"engaged": engaged, "by": by, "reason": reason,
                                                "effective": state["effective_dry_run"]})
            return state

        def progress_view() -> Optional[dict]:
            p = engine.progress
            return p.to_dict() if p is not None else None

        return cls(cfg=engine.cfg, reader=reader, quotes=engine.market.quotes, kill=engine.kill,
                   set_kill=set_kill, health=health_view, api_key=engine.cfg.api_key,
                   meeting_started=lambda: engine.meeting_started, meeting_progress=progress_view)


CTX: web.AppKey = web.AppKey("ctx", ApiContext)


# -- helpers ----------------------------------------------------------------------------------
def _clean(o: Any) -> Any:
    """JSON-safe: exact Decimals as strings, non-finite floats as null."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, dict):
        return {str(k): _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(v) for v in o]
    return o


def ok(data: Any, status: int = 200) -> web.Response:
    return web.Response(text=json.dumps(_clean(data), allow_nan=False), status=status,
                        content_type="application/json")


def error(status: int, message: str) -> web.Response:
    return ok({"error": message, "status": status}, status)


def _num(request: web.Request, name: str, default, lo, hi, cast=int):
    raw = request.query.get(name)
    if raw is None or raw == "":
        return default
    try:
        v = cast(raw)
    except ValueError:
        raise web.HTTPBadRequest(reason=f"{name} must be a number")
    if isinstance(v, float) and not math.isfinite(v):
        raise web.HTTPBadRequest(reason=f"{name} must be finite")
    if not lo <= v <= hi:
        raise web.HTTPBadRequest(reason=f"{name} must be in [{lo}, {hi}]")
    return v


def _hostname(host: str) -> str:
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    return host.split(":", 1)[0].lower()


@web.middleware
async def guard(request: web.Request, handler) -> web.StreamResponse:
    if _hostname(request.host or "") not in LOOPBACK_HOSTS:
        resp: web.StreamResponse = error(403, "Host header must be a loopback name")
    else:
        try:
            resp = await handler(request)
        except web.HTTPException as e:
            resp = error(e.status, e.reason or e.text or "error")
        except Exception:
            logger.exception("API %s %s failed", request.method, request.path)
            resp = error(500, "internal error (see the engine log)")
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


async def _quotes(ctx: ApiContext) -> Tuple[Dict[str, Quote], Optional[str]]:
    # Usually a cache hit. When it isn't and Kraken is slow (retries can take ~40s), answer
    # unmarked after QUOTES_TIMEOUT instead of hanging the dashboard. shield(): the fetch
    # itself carries on for the engine's own tasks waiting on the same lock.
    try:
        return await asyncio.wait_for(asyncio.shield(ctx.quotes()), QUOTES_TIMEOUT), None
    except asyncio.TimeoutError:
        return {}, f"quotes took longer than {QUOTES_TIMEOUT:.0f}s"
    except Exception as e:  # unmarked is a valid answer; the book is still readable
        return {}, f"{type(e).__name__}: {e}"


def _require_book(lg: Ledger) -> None:
    if not lg.initialized():
        raise web.HTTPServiceUnavailable(reason="ledger not initialized (run `ottr init`)")


# -- handlers ---------------------------------------------------------------------------------
async def index(request: web.Request) -> web.Response:
    return ok({"name": "ottr", "read_only": True, "endpoints": ENDPOINTS})


async def get_health(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    with ctx.reader.snapshot():
        d = ctx.health(ctx.reader)
    d["kill_switch"] = ctx.kill.state()
    return ok(d)


async def get_heartbeats(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    now = time.time()
    with ctx.reader.snapshot():
        hbs = ctx.reader.heartbeats()
        epoch = (ctx.reader.get_state("engine") or {}).get("epoch")
    tasks = [health.task_health(n, iv, hbs.get(n), now, epoch).to_dict(now)
             for n, iv in health.intervals(ctx.cfg).items()]
    return ok({"ts": now, "tasks": tasks})


def _position_rows(ctx: ApiContext, lg: Ledger, quotes: Dict[str, Quote], now: float) -> Tuple[list, Any]:
    positions = lg.positions()
    v = lg.value(quotes)
    blind = ctx.reader.get_state("risk_blind") or {}
    stop_pct = ctx.cfg.risk.trailing_stop_pct / 100
    rows = []
    for a, p in positions.items():
        q = quotes.get(a)
        value = float(p.qty) * q.bid if q else None
        stop = p.high_water * (1 - stop_pct)
        rows.append({
            "asset": a, "qty": p.qty, "cost": p.cost, "avg_cost": p.avg_cost,
            "opened_ts": p.opened_ts, "high_water": p.high_water, "trailing_stop": stop,
            "bid": q.bid if q else None, "ask": q.ask if q else None,
            "quote_source": q.source if q else None,
            "quote_age_sec": round(q.age(now), 1) if q else None,
            "value": value,
            "weight": v.weights.get(a) if v else None,
            "cap_pct": ctx.cfg.cap_pct(a),
            "unrealized_pnl": value - float(p.cost) if value is not None else None,
            "unrealized_pct": (value / float(p.cost) - 1) if value is not None and p.cost > 0 else None,
            "stop_distance_pct": (q.bid / stop - 1) if q and stop > 0 else None,
            "risk_blind_since": blind.get(a),
        })
    return rows, v


async def get_book(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    quotes, qerr = await _quotes(ctx)
    now = time.time()
    with ctx.reader.snapshot():
        lg = Ledger(ctx.reader)
        _require_book(lg)
        acct = lg.account()
        rows, v = _position_rows(ctx, lg, quotes, now)
        risk = RiskState.from_dict(ctx.reader.get_state("risk"))
        base = ctx.reader.get_state("bench_base")
        last = ctx.reader.conn.execute(
            "SELECT ts,equity,cash,invested,bench_btc,bench_ew FROM equity ORDER BY ts DESC LIMIT 1").fetchone()
        next_meeting = ctx.reader.get_state("next_meeting_at")
    b_btc, b_ew = benchmarks(base, quotes) if quotes else (None, None)  # same math as the equity tick
    cost = sum((p["cost"] for p in rows), Decimal(0))
    invested = v.invested if v else None
    peak = risk.peak_equity
    return ok({
        "as_of": now, "marked": v is not None, "quotes_error": qerr,
        "equity": v.equity if v else None, "cash": acct["cash"], "invested": invested,
        "invested_pct": invested / v.equity if v and v.equity > 0 else None,
        "cost_basis": cost, "unrealized_pnl": invested - float(cost) if v else None,
        "realized_pnl": acct["realized_pnl"], "fees": acct["fees"], "positions": len(rows),
        "risk": {"halted": risk.halted, "halted_since": risk.halted_since, "peak_equity": peak,
                 "drawdown": 1 - v.equity / peak if v and peak else None,
                 "cooldowns": risk.cooldowns},
        "benchmarks": {"btc_hold": b_btc, "equal_weight_hold": b_ew, "base": base},
        "last_equity_point": dict(zip(("ts", "equity", "cash", "invested", "bench_btc", "bench_ew"),
                                      last)) if last else None,
        "next_meeting_at": next_meeting,
        "kill_switch": ctx.kill.state(),
    })


async def get_positions(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    quotes, qerr = await _quotes(ctx)
    now = time.time()
    with ctx.reader.snapshot():
        lg = Ledger(ctx.reader)
        _require_book(lg)
        rows, v = _position_rows(ctx, lg, quotes, now)
    return ok({"as_of": now, "marked": v is not None, "quotes_error": qerr, "positions": rows})


async def get_equity(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    since = _num(request, "since", 0.0, 0.0, 1e11, float)
    until = _num(request, "until", 1e11, 0.0, 1e11, float)
    max_points = _num(request, "max_points", 1000, 2, 20000)
    cols = ("ts", "equity", "cash", "invested", "bench_btc", "bench_ew")
    with ctx.reader.snapshot():
        rows = ctx.reader.conn.execute(
            f"SELECT {','.join(cols)} FROM equity WHERE ts>=? AND ts<=? ORDER BY ts", (since, until)).fetchall()
        base = ctx.reader.get_state("bench_base")
    total = len(rows)
    if total > max_points:
        # Even stride, always keeping the newest point so the chart ends at "now".
        step = math.ceil(total / max_points)
        picked = rows[::step]
        if picked[-1] is not rows[-1]:
            picked = picked[:max_points - 1] + [rows[-1]]
        rows = picked
    return ok({"count": total, "returned": len(rows), "base": base,
               "points": [dict(zip(cols, r)) for r in rows]})


async def get_fills(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    limit = _num(request, "limit", 50, 1, 500)
    before = _num(request, "before", 2**62, 1, 2**62)
    cols = ("id", "ts", "idem_key", "asset", "side", "qty", "price", "notional", "fee",
            "realized_pnl", "quote_source", "origin", "ref", "reason")
    with ctx.reader.snapshot():
        rows = ctx.reader.conn.execute(
            f"SELECT {','.join(cols)} FROM fills WHERE id<? ORDER BY id DESC LIMIT ?", (before, limit)).fetchall()
    fills = [dict(zip(cols, r)) for r in rows]
    return ok({"fills": fills, "next_before": fills[-1]["id"] if len(fills) == limit else None})


def _meeting_view(ctx: ApiContext, mid: str, ts: float, status: str, record: str, full: bool) -> dict:
    rec = json.loads(record)
    d = rec.get("decision") or {}
    prices = {(r[0], r[1]): r[2] for r in ctx.reader.conn.execute(
        "SELECT analyst, asset, price FROM stances WHERE meeting_id=?", (mid,))}
    labels = rec.get("labels") or {}
    analysts = {}
    for name, rep in (rec.get("analysts") or {}).items():
        analysts[name] = {
            "label": labels.get(name), "overall_view": rep.get("overall_view"),
            "latency_sec": (rec.get("latency") or {}).get(name),
            "stances": [{"asset": a["asset"], "stance": a["stance"], "conviction": a["conviction"],
                         "thesis": a.get("thesis"), "key_risk": a.get("key_risk"),
                         "price": prices.get((name, a["asset"]))} for a in rep.get("assets", [])],
        }
    prefix = f"council:{mid}:"
    orders = []
    for key, ostatus, verdict, updated in ctx.reader.conn.execute(
            "SELECT idem_key, status, verdict, updated_ts FROM orders WHERE substr(idem_key, 1, ?) = ?",
            (len(prefix), prefix)):
        vd = json.loads(verdict or "{}")
        orders.append({"asset": key[len(prefix):], "side": vd.get("side"), "status": ostatus,
                       "reason": vd.get("reason"), "notional": vd.get("notional"),
                       "notes": vd.get("notes", []), "updated_ts": updated})
    out = {"id": mid, "ts": ts, "status": status, "summary": d.get("summary"),
           "dissent_notes": d.get("dissent_notes"), "trades": rec.get("trades", []),
           "dropped": rec.get("dropped", []), "errors": rec.get("errors", {}),
           "latency": rec.get("latency", {}), "tally": rec.get("tally"),
           "analysts": analysts, "orders": orders}
    if full:
        out["decision"] = rec.get("decision")
        out["packet"] = rec.get("context")
    return out


async def get_meetings(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    limit = _num(request, "limit", 10, 1, 50)
    before = _num(request, "before", 1e11, 0.0, 1e11, float)
    with ctx.reader.snapshot():
        rows = ctx.reader.conn.execute(
            "SELECT id, ts, status, record FROM meetings WHERE ts<? ORDER BY ts DESC LIMIT ?",
            (before, limit)).fetchall()
        meetings = [_meeting_view(ctx, *r, full=False) for r in rows]
    return ok({"meetings": meetings,
               "next_before": meetings[-1]["ts"] if len(meetings) == limit else None})


async def get_meeting(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    mid = request.match_info["id"]
    if not MEETING_ID_RE.match(mid):
        raise web.HTTPBadRequest(reason="bad meeting id")
    with ctx.reader.snapshot():
        row = ctx.reader.conn.execute("SELECT id, ts, status, record FROM meetings WHERE id=?",
                                      (mid,)).fetchone()
        if row is None:
            raise web.HTTPNotFound(reason=f"no meeting {mid}")
        return ok(_meeting_view(ctx, *row, full=True))


async def get_audit(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    kind = request.query.get("kind") or None
    if kind is not None and not KIND_RE.match(kind):
        raise web.HTTPBadRequest(reason="kind must be lowercase letters and underscores")
    since = _num(request, "since", 0.0, 0.0, 1e11, float)
    limit = _num(request, "limit", 100, 1, 500)
    with ctx.reader.snapshot():
        rows = ctx.reader.audits(kind, since=since, limit=limit)
    return ok({"audit": rows})


async def get_council(request: web.Request) -> web.Response:
    # `running` and `progress` come from the engine's memory, not the ledger: a meeting
    # writes its record only when it ends, and the chat bot needs to know one is under way
    # (it stays quiet then) and how far it has got.
    ctx = request.app[CTX]
    now = time.time()
    started = ctx.meeting_started()
    progress = ctx.meeting_progress() if started is not None else None
    with ctx.reader.snapshot():
        nxt = ctx.reader.get_state("next_meeting_at")
        last = ctx.reader.conn.execute("SELECT id, ts, status FROM meetings ORDER BY ts DESC LIMIT 1").fetchone()
    return ok({"as_of": now, "running": started is not None, "started_at": started,
               "running_sec": round(now - started, 1) if started is not None else None,
               "progress": progress,
               "interval_hours": ctx.cfg.council.interval_hours, "timeout_sec": ctx.cfg.council.timeout_sec,
               "next_meeting_at": nxt,
               "last_meeting": dict(zip(("id", "ts", "status"), last)) if last else None})


async def get_kill(request: web.Request) -> web.Response:
    return ok(request.app[CTX].kill.state())


def _authorized(request: web.Request, key: str) -> bool:
    auth = request.headers.get("Authorization", "")
    given = auth[7:].strip() if auth[:7].lower() == "bearer " else request.headers.get("X-API-Key", "")
    return bool(given) and hmac.compare_digest(given.encode("utf-8"), key.encode("utf-8"))


async def post_kill(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    if not ctx.api_key:
        return error(503, "kill switch API disabled: set OTTR_API_KEY (16+ chars) and restart")
    if not _authorized(request, ctx.api_key):
        logger.warning("kill switch: rejected unauthorized request from %s", request.remote)
        return error(401, "missing or wrong API key (Authorization: Bearer <OTTR_API_KEY>)")
    raw = await request.read()
    body: Dict[str, Any] = {}
    if raw.strip():
        try:
            body = json.loads(raw)
        except ValueError:
            return error(400, "body must be JSON")
        if not isinstance(body, dict):
            return error(400, "body must be a JSON object")
    engaged = body.get("engaged", True)  # an empty POST is the panic button: engage
    if not isinstance(engaged, bool):
        return error(400, "engaged must be true or false")
    reason = body.get("reason", "")
    if not isinstance(reason, str) or len(reason) > 500:
        return error(400, "reason must be a string of at most 500 characters")
    by = body.get("by", "api")  # a label for the audit log (e.g. "discord"); the key is the authority
    if not isinstance(by, str) or not BY_RE.match(by):
        return error(400, "by must be 1-40 letters, digits or _.:-")
    return ok(ctx.set_kill(engaged, by, reason))


def create_app(ctx: ApiContext) -> web.Application:
    app = web.Application(middlewares=[guard], client_max_size=MAX_BODY)
    app[CTX] = ctx
    app.router.add_get("/api", index)
    app.router.add_get("/api/health", get_health)
    app.router.add_get("/api/heartbeats", get_heartbeats)
    app.router.add_get("/api/book", get_book)
    app.router.add_get("/api/positions", get_positions)
    app.router.add_get("/api/equity", get_equity)
    app.router.add_get("/api/fills", get_fills)
    app.router.add_get("/api/meetings", get_meetings)
    app.router.add_get("/api/meetings/{id}", get_meeting)
    app.router.add_get("/api/audit", get_audit)
    app.router.add_get("/api/council", get_council)
    app.router.add_get("/api/kill-switch", get_kill)
    app.router.add_post("/api/kill-switch", post_kill)
    return app


class ApiServer:
    """Runs the API on the engine's own event loop (no threads, no second writer)."""

    def __init__(self, engine: Engine, host: str, port: int) -> None:
        self.engine, self.host, self.requested_port = engine, host, port
        self.port: Optional[int] = None
        self._runner: Optional[web.AppRunner] = None
        self._reader: Optional[Store] = None

    async def start(self) -> None:
        self._reader = Store(self.engine.store.path, writer=False)
        runner = web.AppRunner(create_app(ApiContext.from_engine(self.engine, self._reader)),
                               access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, self.host, self.requested_port).start()
        except BaseException:
            await runner.cleanup()
            self._reader.close()
            self._reader = None
            raise
        self._runner = runner
        addrs: List[Any] = list(runner.addresses)
        self.port = addrs[0][1] if addrs else self.requested_port

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        if self._reader is not None:
            self._reader.close()
            self._reader = None
