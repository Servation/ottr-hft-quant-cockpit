"""
Read-only HTTP API for the P4 dashboard, plus an authenticated kill switch.

What it does: serves JSON views of the book, positions, equity curve with the SPY and
60/40 benchmarks, fills, meetings (with every analyst's stance), the universe, the audit
log and health, from inside the engine process on 127.0.0.1. `POST /api/kill-switch` engages or releases the runtime dry
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
from ottr.engine.engine import BENCH_BASE, NEXT_MEETING, Engine
from ottr.engine.killswitch import KillSwitch
from ottr.engine.engine import WATCH_STATE
from ottr.engine.watcher import WatchState
from ottr.ledger import tax
from ottr.ledger.book import Ledger, trade_day
from ottr.universe import db as udb
from ottr.ledger.store import Store
from ottr.market.types import Quote
from ottr.risk.monitor import RiskState, cap_for

logger = logging.getLogger(__name__)

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
MAX_BODY = 16 * 1024
PRICES_TIMEOUT = 5.0
KIND_RE = re.compile(r"^[a-z_]{1,40}$")
MEETING_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
BY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,40}$")

ENDPOINTS = [
    "GET  /api/health           alarms, task heartbeats, ledger check, kill switch",
    "GET  /api/heartbeats       per-task heartbeat age vs its stale limit",
    "GET  /api/book             equity, cash, P&L, risk state, the market session, SPY and 60/40 benchmarks",
    "GET  /api/positions        holdings marked (live, delayed or at the close), with stop levels",
    "GET  /api/equity           equity curve + SPY and 60/40 benchmarks (?since=&until=&max_points=)",
    "GET  /api/fills            recent fills, newest first (?limit=&before=<id>)",
    "GET  /api/meetings         council meetings with analyst stances (?limit=&before=<ts>)",
    "GET  /api/meetings/{id}    one meeting in full, incl. the packet the models saw",
    "GET  /api/audit            audit log (?kind=&since=&limit=)",
    "GET  /api/council          a running meeting's progress, the next and the last meeting, what the watcher watches",
    "GET  /api/universe         the members the desk trades: class, cap, buyable, fractional; the satellites",
    "GET  /api/screen           the latest stock screen (counts, changes, the top of the ranking), recent changes",
    "GET  /api/kill-switch      kill switch state",
    "POST /api/kill-switch      {\"engaged\": true|false, \"reason\": \"...\", \"by\": \"...\"}; empty body engages. Bearer auth.",
]


async def _no_benchmarks(base: Optional[dict]) -> Dict[str, Optional[float]]:
    return {}


@dataclass
class ApiContext:
    cfg: Config
    reader: Store                                    # read-only connection
    marks: Callable[[], Awaitable[Dict[str, Quote]]]
    kill: KillSwitch
    set_kill: Callable[[bool, str, str], dict]       # (engaged, by, reason) -> state
    health: Callable[[Store], dict]
    api_key: Optional[str]
    meeting_started: Callable[[], Optional[float]] = lambda: None
    meeting_progress: Callable[[], Optional[dict]] = lambda: None
    benchmarks: Callable[[Optional[dict]], Awaitable[Dict[str, Optional[float]]]] = _no_benchmarks
    market_state: Callable[[], Optional[dict]] = lambda: None
    members: Callable[[], Dict[str, dict]] = lambda: {}

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

        def market_view() -> Optional[dict]:
            try:
                mt = engine.market.calendar.state_at(engine.clock.time())
            except Exception:  # no calendar yet: the market task's alarm says why
                return None
            s, nxt = mt.session, mt.next_open
            return {"state": mt.state.value, "open": mt.is_open,
                    "session": {"day": s.day.isoformat(), "open_ts": s.open_ts, "close_ts": s.close_ts,
                                "early_close": s.early_close} if s else None,
                    "next_open_ts": nxt.open_ts if nxt else None}

        def members_view() -> Dict[str, dict]:
            return {s: {"class": m.asset_class, "state": m.state, "buyable": m.buyable,
                        "fractionable": m.fractionable, "cap_pct": cap_for(engine.cfg, s, engine.classes())}
                    for s, m in sorted(engine.members.items())}

        # Looked up per request (not bound once): the engine's market is the one source.
        return cls(cfg=engine.cfg, reader=reader, marks=lambda: engine.market.marks(), kill=engine.kill,
                   set_kill=set_kill, health=health_view, api_key=engine.cfg.api_key,
                   meeting_started=lambda: engine.meeting_started, meeting_progress=progress_view,
                   benchmarks=lambda base: engine.market.benchmarks(base), market_state=market_view,
                   members=members_view)


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


async def _marks(ctx: ApiContext) -> Tuple[Dict[str, Quote], Optional[str]]:
    # Usually a cache hit. When it isn't and Alpaca is slow (retries can take ~40s), answer
    # unmarked after PRICES_TIMEOUT instead of hanging the dashboard. shield(): the fetch
    # itself carries on for the engine's own tasks waiting on the same lock.
    try:
        return await asyncio.wait_for(asyncio.shield(ctx.marks()), PRICES_TIMEOUT), None
    except asyncio.TimeoutError:
        return {}, f"prices took longer than {PRICES_TIMEOUT:.0f}s"
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


def _position_rows(ctx: ApiContext, lg: Ledger, marks: Dict[str, Quote], now: float) -> Tuple[list, Any]:
    positions = lg.positions()
    v = lg.value(marks)
    blind = ctx.reader.get_state("risk_blind") or {}
    members = ctx.members()
    classes = {s: m["class"] for s, m in members.items()}
    rows = []
    for a, p in positions.items():
        q = marks.get(a)
        value = float(p.qty) * q.bid if q else None
        stop = p.stop_price  # None for the cash ETF, and until a new position's stop is set
        rows.append({
            "asset": a, "class": classes.get(a), "member": a in members,
            "qty": p.qty, "cost": p.cost, "avg_cost": p.avg_cost,
            "opened_ts": p.opened_ts, "high_water": p.high_water, "stop_pct": p.stop_pct, "trailing_stop": stop,
            "bid": q.bid if q else None, "ask": q.ask if q else None,
            "price_basis": q.basis if q else None,
            "price_age_sec": round(q.age(now), 1) if q else None,
            "value": value,
            "weight": v.weights.get(a) if v else None,
            "cap_pct": cap_for(ctx.cfg, a, classes),
            "unrealized_pnl": value - float(p.cost) if value is not None else None,
            "unrealized_pct": (value / float(p.cost) - 1) if value is not None and p.cost > 0 else None,
            "stop_distance_pct": (q.bid / stop - 1) if q and stop else None,
            "risk_blind_since": blind.get(a),
        })
    return rows, v


EQUITY_COLS = ("ts", "equity", "cash", "invested", "bench_spy", "bench_60_40", "after_tax")


async def get_book(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    marks, perr = await _marks(ctx)
    now = time.time()
    with ctx.reader.snapshot():
        lg = Ledger(ctx.reader)
        _require_book(lg)
        acct = lg.account()
        owed = lg.receivable()
        rows, v = _position_rows(ctx, lg, marks, now)
        after_tax = tax.after_tax_equity(ctx.cfg, ctx.reader, marks, v.equity, trade_day(now)) if v else None
        risk = RiskState.from_dict(ctx.reader.get_state("risk"))
        base = ctx.reader.get_state(BENCH_BASE)
        last = ctx.reader.conn.execute(
            f"SELECT {','.join(EQUITY_COLS)} FROM equity ORDER BY ts DESC LIMIT 1").fetchone()
        next_meeting = ctx.reader.get_state(NEXT_MEETING)
    try:  # the same math as the equity tick, at this moment's prices
        bench = await asyncio.wait_for(ctx.benchmarks(base), PRICES_TIMEOUT)
    except Exception:
        bench = {}
    cost = sum((p["cost"] for p in rows), Decimal(0))
    invested = v.invested if v else None
    peak = risk.peak_equity
    return ok({
        "as_of": now, "marked": v is not None, "prices_error": perr, "market": ctx.market_state(),
        "equity": v.equity if v else None, "cash": acct["cash"], "invested": invested,
        "dividends_owed": owed, "after_tax_equity_estimate": after_tax,
        "invested_pct": invested / v.equity if v and v.equity > 0 else None,
        "cost_basis": cost, "unrealized_pnl": invested - float(cost) if v else None,
        "realized_pnl": acct["realized_pnl"], "fees": acct["fees"], "positions": len(rows),
        "risk": {"halted": risk.halted, "halted_since": risk.halted_since, "peak_equity": peak,
                 "drawdown": 1 - v.equity / peak if v and peak else None,
                 "cooldowns": risk.cooldowns},
        "benchmarks": {"spy_hold": bench.get("spy"), "sixty_forty": bench.get("60_40"), "base": base},
        "last_equity_point": dict(zip(EQUITY_COLS, last)) if last else None,
        "next_meeting_at": next_meeting,
        "kill_switch": ctx.kill.state(),
    })


async def get_positions(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    marks, perr = await _marks(ctx)
    now = time.time()
    with ctx.reader.snapshot():
        lg = Ledger(ctx.reader)
        _require_book(lg)
        rows, v = _position_rows(ctx, lg, marks, now)
    return ok({"as_of": now, "marked": v is not None, "prices_error": perr, "positions": rows})


async def get_equity(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    since = _num(request, "since", 0.0, 0.0, 1e11, float)
    until = _num(request, "until", 1e11, 0.0, 1e11, float)
    max_points = _num(request, "max_points", 1000, 2, 20000)
    cols = EQUITY_COLS
    with ctx.reader.snapshot():
        rows = ctx.reader.conn.execute(
            f"SELECT {','.join(cols)} FROM equity WHERE ts>=? AND ts<=? ORDER BY ts", (since, until)).fetchall()
        base = ctx.reader.get_state(BENCH_BASE)
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
           "latency": rec.get("latency", {}), "tally": rec.get("tally"), "chair": rec.get("chair"),
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
        nxt = ctx.reader.get_state(NEXT_MEETING)
        last = ctx.reader.conn.execute("SELECT id, ts, status FROM meetings ORDER BY ts DESC LIMIT 1").fetchone()
        st = WatchState.from_dict(ctx.reader.get_state(WATCH_STATE))
    w = ctx.cfg.watch
    watch = {"enabled": w.enabled, "note": st.note, "set_at": st.set_ts,
             "conditions": [{"condition": t.describe(), "why": t.why, **asdict(t)} for t in st.triggers],
             "last_review_at": st.last_review_ts, "last_review": st.last_review, "last_error": st.last_error,
             "called_today": len(st.called_on(now)), "max_called_per_day": w.max_called_per_day,
             "check_every_min": w.check_every_min, "called": st.called[-5:], "pending": st.pending}
    return ok({"as_of": now, "running": started is not None, "started_at": started,
               "running_sec": round(now - started, 1) if started is not None else None,
               "progress": progress,
               "meeting_times_et": list(ctx.cfg.schedule.meeting_times), "timeout_sec": ctx.cfg.council.timeout_sec,
               "next_meeting_at": nxt, "watch": watch,
               "last_meeting": dict(zip(("id", "ts", "status"), last)) if last else None})


def _satellite(s) -> dict:
    return {"symbol": s.symbol, "state": s.state, "promoted_day": s.promoted_day.isoformat(),
            "demoted_day": s.demoted_day.isoformat() if s.demoted_day else None, "score": s.score, "why": s.why}


async def get_universe(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    u = ctx.cfg.universe
    with ctx.reader.snapshot():
        sats = udb.satellites(ctx.reader)
    return ok({"members": ctx.members(), "cash_symbol": u.cash_symbol, "reference": list(u.reference),
               "satellites_max": u.satellites_max, "sleeve_pct": u.sleeve_pct,
               "satellites": [_satellite(s) for s in sats.values()]})


async def get_screen(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    with ctx.reader.snapshot():
        last = udb.last_screen(ctx.reader)
        sats = udb.satellites(ctx.reader)
        changes = udb.events(ctx.reader, 20)
    sc = ctx.cfg.screen
    return ok({"screen": last, "satellites": [_satellite(s) for s in sats.values()], "changes": changes,
               "schedule": f"{sc.weekday} {sc.at} ET weekly" if sc.enabled else "off",
               "satellites_max": ctx.cfg.universe.satellites_max})


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
    app.router.add_get("/api/universe", get_universe)
    app.router.add_get("/api/screen", get_screen)
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
