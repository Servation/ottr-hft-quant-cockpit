"""
Engine: wires market data, the council, the risk monitor and the executor together.

What it does: periodic tasks (risk every 60s, equity every 15m, a council meeting every
N hours) feed one Executor. Every task writes a heartbeat so `ottr status` can tell
"running" from "running but a task is dead". Why one process: it is the only writer, so
there is exactly one place where state changes.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from typing import Any, Callable, Dict, Optional

from ottr.config import Config
from ottr.council.council import Council
from ottr.council.models import ClaudeCLI, CodexCLI, GeminiCLI, ModelAdapter
from ottr.engine.executor import Executor, Notifier
from ottr.ledger.book import Ledger
from ottr.ledger.store import Store, dumps
from ottr.market.service import MarketData, MarketSnapshot
from ottr.market.types import MarketDataError
from ottr.risk import gate, monitor
from ottr.risk.monitor import RiskState
from ottr.risk.types import Intent

logger = logging.getLogger(__name__)


def build_council(cfg: Config) -> Council:
    c = cfg.council
    make: Dict[str, Callable[[], ModelAdapter]] = {
        "claude": lambda: ClaudeCLI(model=c.claude_model, effort=c.reasoning_effort),
        "gpt": lambda: CodexCLI(effort=c.reasoning_effort),
        "gemini": lambda: GeminiCLI(model=c.gemini_model),
    }
    return Council(analysts={n: make[n]() for n in c.analysts}, chair=make[c.chair](),
                   universe=cfg.universe, min_analysts=c.min_analysts,
                   max_trades=cfg.trading.max_trades_per_meeting, timeout=c.timeout_sec)


class Engine:
    def __init__(self, cfg: Config, store: Store, market: MarketData,
                 council: Optional[Council] = None, notify: Optional[Notifier] = None) -> None:
        self.cfg, self.store, self.market = cfg, store, market
        self.ledger = Ledger(store)
        self.council = council or build_council(cfg)
        self.notify = notify
        self.executor = Executor(cfg, store, self.ledger, market, notify)

    # -- risk ------------------------------------------------------------------------------
    async def risk_tick(self) -> monitor.MonitorResult:
        quotes = await self.market.quotes()
        positions = self.ledger.positions()
        valuation = self.ledger.value(quotes)
        state = RiskState.from_dict(self.store.get_state("risk"))
        points = self.store.conn.execute("SELECT COUNT(*) FROM equity").fetchone()[0]
        res = monitor.check(self.cfg, time.time(), positions, valuation, quotes, state, points)
        with self.store.tx():
            for a, h in res.high_water.items():
                self.ledger.set_high_water(a, h)
            self.store.set_state("risk", res.state.to_dict())
            for kind, data in res.events:
                if kind != "risk_blind":
                    self.store.audit(f"risk_{kind}", **data)
        for kind, data in res.events:
            if kind == "risk_blind":
                logger.warning("risk monitor blind on %s: %s", data["asset"], data["why"])
            elif self.notify:
                await self.notify(f"risk_{kind}", data)
        for intent in res.intents:
            await self.executor.submit(intent)
        return res

    # -- equity ----------------------------------------------------------------------------
    def benchmarks(self, quotes) -> tuple:
        base = self.store.get_state("bench_base")
        if not base:
            return None, None
        eq0, p0 = base["equity"], base["prices"]
        btc = quotes.get("BTC")
        bench_btc = eq0 * btc.mid / p0["BTC"] if btc and p0.get("BTC") else None
        rel = [quotes[a].mid / p for a, p in p0.items() if a in quotes and p > 0]
        bench_ew = eq0 * sum(rel) / len(rel) if len(rel) == len(p0) else None
        return bench_btc, bench_ew

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
            import json
            d = json.loads(last[0]).get("decision") or {}
            lines.append(f"  LAST MEETING SUMMARY: {d.get('summary', '')}")
        return "\n".join(lines)

    async def meeting(self, execute: bool = True) -> Dict[str, Any]:
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
        if self.notify:
            await self.notify("meeting", {"id": rec["id"], "status": rec["status"],
                                          "summary": (rec["decision"] or {}).get("summary", ""),
                                          "trades": rec["trades"], "dropped": rec["dropped"]})
        return rec

    # -- loop ------------------------------------------------------------------------------
    async def _every(self, name: str, interval: float, fn, first_delay: float = 0.0) -> None:
        await asyncio.sleep(first_delay)
        while True:
            t0 = time.time()
            try:
                await fn()
                self.store.heartbeat(name, True)
            except Exception as e:
                logger.exception("task %s failed", name)
                self.store.heartbeat(name, False, f"{type(e).__name__}: {e}")
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
                self.store.heartbeat("council", rec["status"] == "ok", rec["status"])
            except Exception as e:
                logger.exception("meeting failed")
                self.store.heartbeat("council", False, f"{type(e).__name__}: {e}")

    async def run(self, stop: Optional[asyncio.Event] = None) -> None:
        """Run until `stop` is set or SIGINT/SIGTERM arrives (handled on THIS loop)."""
        stop = stop or asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):
                signal.signal(sig, lambda s, f: loop.call_soon_threadsafe(stop.set))
        e = self.cfg.engine
        tasks = [
            asyncio.create_task(self.executor.run(), name="executor"),
            asyncio.create_task(self._every("risk", e.risk_interval_sec, self.risk_tick, 5), name="risk"),
            asyncio.create_task(self._every("equity", e.equity_interval_sec, self.equity_tick, 10), name="equity"),
            asyncio.create_task(self._council_loop(), name="council"),
        ]
        self.store.audit("engine_start", dry_run=self.cfg.trading.dry_run)
        logger.info("engine running (dry_run=%s)", self.cfg.trading.dry_run)
        await stop.wait()
        logger.info("stopping: letting queued orders finish")
        try:
            await asyncio.wait_for(self.executor.queue.join(), timeout=30)
        except asyncio.TimeoutError:
            logger.warning("executor queue did not drain in 30s")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.store.audit("engine_stop")
        logger.info("engine stopped cleanly")
