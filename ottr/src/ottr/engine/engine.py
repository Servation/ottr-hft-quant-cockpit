"""
Engine: wires market data, the council, the risk monitor and the executor together.

What it does: periodic tasks (market every 60s, risk every 60s, equity every 15m, a
council meeting every N hours) feed one Executor. Every task writes a heartbeat; a
watchdog task turns stale heartbeats, a blind risk monitor and ledger problems into
alarms (audit log + Discord), and the optional HTTP API serves the book read-only.

Why one process: it is the only writer, so there is exactly one place where state
changes. Why crash-only: if a core task ever exits, `run` returns 1 so the supervisor
restarts a fresh process, instead of limping on with, say, no risk monitor.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, Optional, Set, Tuple

from ottr import health
from ottr.config import Config
from ottr.control import (STOP_FILE, clear_stale_stop, install_stop_signals, restore_signals,
                          stop_requested)
from ottr.council.council import Council
from ottr.council.models import make_adapter
from ottr.engine.executor import Executor
from ottr.engine.killswitch import KillSwitch
from ottr.ledger.book import Ledger
from ottr.ledger.store import Store, dumps
from ottr.market.service import MarketData, MarketSnapshot
from ottr.market.types import MarketDataError
from ottr.notify import Alerts
from ottr.risk import gate, monitor
from ottr.risk.monitor import RiskState
from ottr.risk.types import Intent

logger = logging.getLogger(__name__)

# Kraken stamps a daily candle with its 00:00 UTC open and only closed candles are kept,
# so the newest is normally 24-48h old. Older than 3 days means closes stopped updating.
CANDLE_STALE_SEC = 3 * 86400


def build_council(cfg: Config) -> Council:
    c = cfg.council
    return Council(analysts={n: make_adapter(n, c) for n in c.analysts}, chair=make_adapter(c.chair, c),
                   universe=cfg.universe, min_analysts=c.min_analysts,
                   max_trades=cfg.trading.max_trades_per_meeting, timeout=c.timeout_sec)


def benchmarks(base: Optional[dict], quotes) -> Tuple[Optional[float], Optional[float]]:
    """(BTC buy-and-hold, equal-weight buy-and-hold) equity from the day-0 baseline."""
    if not base:
        return None, None
    eq0, p0 = base["equity"], base["prices"]
    btc = quotes.get("BTC")
    bench_btc = eq0 * btc.mid / p0["BTC"] if btc and p0.get("BTC") else None
    rel = [quotes[a].mid / p for a, p in p0.items() if a in quotes and p > 0]
    bench_ew = eq0 * sum(rel) / len(rel) if rel and len(rel) == len(p0) else None
    return bench_btc, bench_ew


class Engine:
    def __init__(self, cfg: Config, store: Store, market: MarketData,
                 council: Optional[Council] = None, alerts: Optional[Alerts] = None) -> None:
        self.cfg, self.store, self.market = cfg, store, market
        self.ledger = Ledger(store)
        self.council = council or build_council(cfg)
        self.alerts = alerts or Alerts(None, cfg.alerts)
        self.kill = KillSwitch(cfg, store)
        self.executor = Executor(cfg, store, self.ledger, market, self.alerts, kill=self.kill)
        self.watchdog = health.Watchdog(cfg.engine.alarm_clear_sec)
        self.problems: Dict[str, health.Alarm] = {}  # engine-internal alarms (e.g. API bind)
        self.started: Optional[float] = None
        self.epoch: Optional[float] = None  # start, or resume from a system sleep
        self._last_watch: Optional[float] = None
        self.api = None
        # When the meeting in progress began (None: no meeting). In memory on purpose: a
        # persisted flag would outlive a crash and claim a meeting that is no longer running.
        self.meeting_started: Optional[float] = None

    # -- risk ------------------------------------------------------------------------------
    def _blind_state(self, assets: Set[str], now: float) -> Dict[str, float]:
        """asset -> blind since, for the watchdog. Logs only the transitions."""
        prev = self.store.get_state("risk_blind") or {}
        cur = {a: prev.get(a, now) for a in sorted(assets)}
        for a in sorted(set(cur) - set(prev)):
            logger.warning("risk monitor blind on %s: no fresh quote", a)
        for a in sorted(set(prev) - set(cur)):
            logger.info("risk monitor can see %s again", a)
        return cur

    async def risk_tick(self) -> monitor.MonitorResult:
        positions = self.ledger.positions()
        try:
            quotes = await self.market.quotes()
        except MarketDataError:
            # No quotes at all: every holding is unprotected, which the watchdog must see.
            self.store.set_state("risk_blind", self._blind_state(set(positions), time.time()))
            raise
        valuation = self.ledger.value(quotes)
        state = RiskState.from_dict(self.store.get_state("risk"))
        points = self.store.conn.execute("SELECT COUNT(*) FROM equity").fetchone()[0]
        now = time.time()
        res = monitor.check(self.cfg, now, positions, valuation, quotes, state, points)
        blind = self._blind_state({d["asset"] for k, d in res.events if k == "risk_blind"}, now)
        with self.store.tx():
            for a, h in res.high_water.items():
                self.ledger.set_high_water(a, h)
            self.store.set_state("risk", res.state.to_dict())
            self.store.set_state("risk_blind", blind)
            for kind, data in res.events:
                if kind != "risk_blind":
                    self.store.audit(f"risk_{kind}", **data)
        for kind, data in res.events:
            if kind != "risk_blind":
                self.alerts.event(f"risk_{kind}", data)
        for intent in res.intents:
            await self.executor.submit(intent)
        return res

    # -- market ----------------------------------------------------------------------------
    async def market_tick(self) -> None:
        """Keep quotes and candles warm and prove they are fresh (the market heartbeat).

        Raises (a failed heartbeat) when any universe asset lacks a fresh quote or its
        daily closes stopped updating, since sizing and the council would see stale data.
        """
        quotes = await self.market.quotes()
        now = time.time()  # judge quotes before the (hourly, multi-second) candle refresh
        problems = []
        for a in self.cfg.universe:
            q = quotes.get(a)
            if q is None:
                problems.append(f"{a} no quote")
            elif q.age(now) > self.cfg.trading.max_data_age_sec:
                problems.append(f"{a} quote {health.fmt_age(q.age(now))} old")
        candles = await self.market.candles()  # TTL-cached: really refreshes hourly
        now = time.time()
        for a in self.cfg.universe:
            rows = candles.get(a) or []
            if not rows:
                problems.append(f"{a} no daily candles")
            elif now - rows[-1][0] > CANDLE_STALE_SEC:
                problems.append(f"{a} last daily close {health.fmt_age(now - rows[-1][0])} old")
        if problems:
            raise MarketDataError("; ".join(problems))

    # -- equity ----------------------------------------------------------------------------
    def benchmarks(self, quotes) -> tuple:
        return benchmarks(self.store.get_state("bench_base"), quotes)

    async def equity_tick(self) -> Optional[float]:
        quotes = await self.market.quotes()
        v = self.ledger.value(quotes)
        if v is None:
            raise MarketDataError("a held asset has no quote; equity unknown (not recorded)")
        if not self.store.get_state("bench_base"):
            self.store.set_state("bench_base", {"ts": time.time(), "equity": v.equity,
                                                "prices": {a: q.mid for a, q in quotes.items()}})
        b_btc, b_ew = self.benchmarks(quotes)
        self.store.add_equity(time.time(), v.equity, v.cash, v.invested, b_btc, b_ew,
                              {"weights": v.weights})
        return v.equity

    # -- council ---------------------------------------------------------------------------
    def book_context(self, snap: MarketSnapshot) -> str:
        quotes = {a: v.quote for a, v in snap.assets.items() if v.quote}
        v = self.ledger.value(quotes)
        positions = self.ledger.positions()
        st = RiskState.from_dict(self.store.get_state("risk"))
        lines = ["DESK BOOK (paper):"]
        if v is None:
            lines.append("  valuation unavailable (missing quote)")
        else:
            lines.append(f"  equity ${v.equity:,.2f} | cash ${v.cash:,.2f} ({v.cash / v.equity * 100:.1f}%) | "
                         f"invested {v.invested / v.equity * 100:.1f}%")
        lines.append("  HOLDINGS: " + (", ".join(
            f"{a} {v.weights.get(a, 0) * 100:.1f}% of book (entry avg ${float(p.avg_cost):,.6g}, "
            f"now ${quotes[a].bid:,.6g}, {(quotes[a].bid / float(p.avg_cost) - 1) * 100:+.1f}%)"
            for a, p in positions.items() if a in quotes and v) or "none"))
        lines.append("  Risk-model target weight at conviction 1.0 (BUYs only top up to this): " + ", ".join(
            f"{a} {gate.target_weight(self.cfg, a, av.vol, av.regime, 1.0) * 100:.1f}%"
            for a, av in snap.assets.items()))
        lines.append("  Per-asset caps: " + ", ".join(f"{a} {self.cfg.cap_pct(a):.0f}%" for a in self.cfg.universe))
        lines.append(f"  Drawdown breaker: {'HALTED (BUYs blocked)' if st.halted else 'normal'}")
        recent = self.ledger.fills(5)
        if recent:
            lines.append("  RECENT FILLS: " + "; ".join(
                f"{time.strftime('%m-%d %H:%M', time.gmtime(f['ts']))} {f['side']} {f['asset']} "
                f"${float(f['notional']):,.0f} ({f['origin']})" for f in recent))
        last = self.store.conn.execute(
            "SELECT record FROM meetings WHERE status='ok' ORDER BY ts DESC LIMIT 1").fetchone()
        if last:
            d = json.loads(last[0]).get("decision") or {}
            lines.append(f"  LAST MEETING SUMMARY: {d.get('summary', '')}")
        return "\n".join(lines)

    async def meeting(self, execute: bool = True) -> Dict[str, Any]:
        """One council meeting; `meeting_started` is set while it runs (GET /api/council)."""
        self.meeting_started = time.time()
        try:
            return await self._meeting(execute)
        finally:
            self.meeting_started = None

    async def _meeting(self, execute: bool) -> Dict[str, Any]:
        snap = await self.market.snapshot()
        if all(v.quote is None for v in snap.assets.values()):
            raise MarketDataError("no quotes: meeting aborted")
        context = snap.packet() + "\n\n" + self.book_context(snap)
        held = set(self.ledger.positions())
        rec = await self.council.run(context, held)
        rec["context"] = context
        with self.store.tx() as c:
            c.execute("INSERT INTO meetings(id,ts,status,packet,record) VALUES(?,?,?,?,?)",
                      (rec["id"], snap.as_of, rec["status"], context, dumps(rec)))
            for name, rep in rec["analysts"].items():
                for a in rep["assets"]:
                    q = snap.quote(a["asset"])
                    c.execute("INSERT OR REPLACE INTO stances VALUES(?,?,?,?,?,?,?)",
                              (rec["id"], name, a["asset"], a["stance"], a["conviction"],
                               q.mid if q else None, snap.as_of))
            self.store.audit("meeting", id=rec["id"], status=rec["status"], trades=rec["trades"],
                             dropped=rec["dropped"], errors=rec["errors"], latency=rec["latency"])
        if execute:
            for t in rec["trades"]:
                await self.executor.submit(Intent(
                    origin="council", asset=t["asset"], side=t["action"],
                    conviction=t.get("conviction", 0.0), fraction=t.get("fraction_of_position", 0.0),
                    reason=t["reasoning"], ref=rec["id"], idem_key=f"council:{rec['id']}:{t['asset']}",
                    created_ts=snap.as_of))
        self.alerts.event("meeting", {"id": rec["id"], "status": rec["status"],
                                      "summary": (rec["decision"] or {}).get("summary", ""),
                                      "trades": rec["trades"], "dropped": rec["dropped"]})
        return rec

    # -- health ----------------------------------------------------------------------------
    def _beat(self, task: str, ok: bool, detail: str = "") -> None:
        # A heartbeat write must never take its task down with it (e.g. a full disk).
        try:
            self.store.heartbeat(task, ok, detail)
        except Exception:
            logger.exception("could not record %s heartbeat", task)

    def _save_engine_state(self, **fields: Any) -> None:
        st = self.store.get_state("engine") or {}
        st.update(fields)
        self.store.set_state("engine", st)

    def health_report(self, now: Optional[float] = None) -> health.HealthReport:
        return health.evaluate(self.cfg, self.store, now, inside_engine=True, epoch=self.epoch,
                               extra=list(self.problems.values()))

    def watchdog_tick(self, now: Optional[float] = None) -> health.HealthReport:
        """One health pass: evaluate, apply hysteresis, audit transitions, notify."""
        now = time.time() if now is None else now
        iv = self.cfg.engine.watchdog_interval_sec
        if self._last_watch is not None and now - self._last_watch > health.STALE_FACTOR * iv:
            # The process was frozen (system sleep, or a blocking call hogged the loop).
            # Every heartbeat is old by the wall clock, so restart their grace period
            # rather than raising an alarm per task for the time we weren't running.
            gap = now - self._last_watch
            self.epoch = now
            self._save_engine_state(epoch=now)
            self.store.audit("engine_resumed", gap_sec=round(gap))
            logger.warning("engine was suspended for %s; heartbeat grace restarted", health.fmt_age(gap))
            self.alerts.event("engine_resumed", {"gap": health.fmt_age(gap)})
        self._last_watch = now
        report = self.health_report(now)
        raised, cleared = self.watchdog.update(report.alarms, now)
        for a in raised:
            logger.warning("ALARM %s (%s): %s", a.key, a.severity, a.message)
            self.store.audit("alarm_raised", key=a.key, severity=a.severity, message=a.message)
        for a in cleared:
            logger.info("alarm cleared %s: %s", a.key, a.message)
            self.store.audit("alarm_cleared", key=a.key, message=a.message)
        if raised or cleared:  # persisted so a restart can say which alarms it lost track of
            self.store.set_state("alarms", {k: {"severity": a.severity, "message": a.message,
                                                "since": self.watchdog.since.get(k)}
                                            for k, a in self.watchdog.active.items()})
        self.alerts.sync_alarms(self.watchdog.active)
        self._beat("watchdog", True, f"{len(self.watchdog.active)} active alarm(s)")
        return report

    async def _watchdog_loop(self) -> None:
        iv = self.cfg.engine.watchdog_interval_sec
        await asyncio.sleep(min(15.0, iv))  # let the first ticks land before judging them
        while True:
            if "api" in self.problems and self.api is None:
                await self._start_api()  # retry a port that was taken at start-up
            try:
                self.watchdog_tick()
            except Exception as e:
                logger.exception("watchdog tick failed")
                self._beat("watchdog", False, f"{type(e).__name__}: {e}")
            await asyncio.sleep(iv)

    async def _watch_stop_file(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            if stop_requested(self.cfg.data_dir, self.started or 0.0):
                logger.info("stop requested via %s", self.cfg.data_dir / STOP_FILE)
                self.store.audit("stop_requested", source="stop_file")
                stop.set()
                return
            await asyncio.sleep(1.0)

    # -- api -------------------------------------------------------------------------------
    async def _start_api(self) -> None:
        from ottr.api import ApiServer  # aiohttp is only imported when the API is on
        a = self.cfg.api
        server = ApiServer(self, a.host, a.port)
        try:
            await server.start()
        except Exception as e:
            # The dashboard is optional; trading must not stop because a port is taken.
            # The watchdog retries every tick, so a freed port heals on its own.
            if "api" not in self.problems:
                logger.error("API could not listen on %s:%s: %s", a.host, a.port, e)
            self.problems["api"] = health.Alarm("api", health.WARNING,
                                                f"API could not listen on {a.host}:{a.port} ({e})")
            return
        self.problems.pop("api", None)
        self.api = server
        logger.info("API listening on http://%s:%s/api", a.host, server.port)

    async def _stop_api(self) -> None:
        if self.api is not None:
            await self.api.stop()
            self.api = None

    # -- loop ------------------------------------------------------------------------------
    async def _every(self, name: str, interval: float, fn, first_delay: float = 0.0) -> None:
        await asyncio.sleep(first_delay)
        while True:
            t0 = time.time()
            try:
                await fn()
                ok, detail = True, ""
            except MarketDataError as e:  # an outage, not a bug: no traceback every minute
                logger.warning("task %s: %s", name, e)
                ok, detail = False, f"MarketDataError: {e}"
            except Exception as e:
                logger.exception("task %s failed", name)
                ok, detail = False, f"{type(e).__name__}: {e}"
            self._beat(name, ok, detail)
            await asyncio.sleep(max(1.0, interval - (time.time() - t0)))

    async def _council_loop(self) -> None:
        interval = self.cfg.council.interval_hours * 3600
        while True:
            due = self.store.get_state("next_meeting_at") or 0
            wait = due - time.time()
            if wait > 0:
                await asyncio.sleep(min(wait, 60))
                continue
            self.store.set_state("next_meeting_at", time.time() + interval)
            try:
                rec = await self.meeting(execute=True)
                self._beat("council", rec["status"] == "ok", rec["status"])
            except Exception as e:
                logger.exception("meeting failed")
                self._beat("council", False, f"{type(e).__name__}: {e}")

    async def run(self, stop: Optional[asyncio.Event] = None, *, serve_api: Optional[bool] = None) -> int:
        """Run until `stop` is set, a signal arrives or a stop file is dropped.

        Returns 0 on a requested stop, 1 if a core task died (the supervisor restarts us).
        """
        stop = stop or asyncio.Event()
        undo = install_stop_signals(stop.set)
        now = time.time()
        self.started = self.epoch = self._last_watch = now
        clear_stale_stop(self.cfg.data_dir, now)
        unclean = bool((self.store.get_state("engine") or {}).get("running"))
        # Alarm state is per run: a new run re-checks everything from scratch. Say which
        # alarms were open, since their "resolved" message would otherwise never come.
        previous = sorted((self.store.get_state("alarms") or {}).keys())
        self.store.set_state("alarms", {})
        self._save_engine_state(running=True, started_at=now, epoch=now, pid=os.getpid(),
                                stopped_at=None, exit_code=None)
        self.store.audit("engine_start", dry_run=self.kill.engaged, unclean_previous_exit=unclean,
                         previous_alarms=previous, pid=os.getpid())
        code, why = 0, ""
        try:
            async with self.alerts.running():
                self.alerts.event("engine_start", {"dry_run": self.kill.engaged, "unclean_previous_exit": unclean,
                                                   "previous_alarms": previous})
                if self.cfg.api.enabled if serve_api is None else serve_api:
                    await self._start_api()
                e, m = self.cfg.engine, self.cfg.market
                core = [
                    asyncio.create_task(self.executor.run(), name="executor"),
                    asyncio.create_task(self._every("market", m.spot_ttl_sec, self.market_tick), name="market"),
                    asyncio.create_task(self._every("risk", e.risk_interval_sec, self.risk_tick, 5), name="risk"),
                    asyncio.create_task(self._every("equity", e.equity_interval_sec, self.equity_tick, 10),
                                        name="equity"),
                    asyncio.create_task(self._council_loop(), name="council"),
                    asyncio.create_task(self._watchdog_loop(), name="watchdog"),
                ]
                aux = [asyncio.create_task(self._watch_stop_file(stop), name="stopfile")]
                stopper = asyncio.create_task(stop.wait(), name="stop")
                logger.info("engine running (dry_run=%s)", self.kill.engaged)
                done, _ = await asyncio.wait({stopper, *core}, return_when=asyncio.FIRST_COMPLETED)
                for t in core:
                    if t in done:  # these loops never return: one that did is a bug
                        exc = None if t.cancelled() else t.exception()
                        why = f"task {t.get_name()} ended unexpectedly: {exc!r}"
                        logger.critical(why, exc_info=exc)
                        code = 1
                logger.info("stopping: letting queued orders finish")
                if not core[0].done():
                    try:
                        await asyncio.wait_for(self.executor.queue.join(), timeout=30)
                    except asyncio.TimeoutError:
                        logger.warning("executor queue did not drain in 30s")
                for t in (*core, *aux, stopper):
                    t.cancel()
                await asyncio.gather(*core, *aux, stopper, return_exceptions=True)
                await self._stop_api()
                self._save_engine_state(running=False, stopped_at=time.time(), exit_code=code)
                self.store.audit("engine_stop", code=code, why=why)
                self.alerts.event("engine_stop", {"code": code, "why": why})
        finally:
            restore_signals(undo)
        logger.info("engine stopped %s", "cleanly" if code == 0 else f"with exit code {code}")
        return code
