"""
Engine: runs the US desk. Market data, the council, the risk monitor, the daily jobs and
one Executor, in one process that is the only writer.

What it does: core tasks run on fixed cadences (so `health.task_health` stays meaningful)
and branch on the session, because nights, weekends and holidays are normal states:
- market (60s): the calendar and asset records (daily), the daily bars (after each close)
  and, in session, proof that live quotes are arriving.
- jobs (60s): the close-of-day fee charge, the cash sweep before the close, the events
  feeds that are due, and, in session, retries of orders deferred by a bad quote or a
  closed market.
- risk (60s): the protective monitor, in session only (a stop needs a live bid to sell at).
- equity (15m): a point in session, and one at each close, with the SPY and 60/40
  benchmarks measured from the first close after the book opened.
- council: meetings at the configured New York times on trading days.
- watchdog (60s): turns stale heartbeats, a blind risk monitor, stale events feeds and
  ledger problems into alarms (audit log and Discord); the optional API serves the book.

Why one process: it is the only writer, so there is exactly one place where state changes.
Why crash-only: if a core task ever exits, `run` returns 1 so the supervisor restarts a
fresh process, instead of limping on with, say, no risk monitor.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional, Set, Union

from ottr import health
from ottr.broker.account import buying_power
from ottr.broker.sim import SimBroker
from ottr.clock import SYSTEM, Clock
from ottr.config import Config
from ottr.control import (STOP_FILE, clear_stale_stop, install_stop_signals, restore_signals,
                          stop_requested)
from ottr.council.council import Council, new_meeting_id
from ottr.council.models import make_adapter
from ottr.engine.desks import Desk
from ottr.engine.executor import Executor
from ottr.engine.jobs import close_of_day, sweep_intent
from ottr.engine.killswitch import KillSwitch
from ottr.engine.progress import MeetingProgress
from ottr.events.refresh import Sources, refresh_due
from ottr.events.view import StoredEvents, macro_readings
from ottr.ledger.book import Ledger, trade_day
from ottr.ledger.marks import SIP_FINAL_AFTER_SEC
from ottr.ledger.store import Store, dumps
from ottr.market.alpaca import Asset
from ottr.market.calendar import ET, CalendarError, next_meeting_at
from ottr.market.packet import UsSnapshot
from ottr.market.service import MarketData
from ottr.market.types import BASIS_CLOSE, MarketDataError
from ottr.notify import Alerts
from ottr.risk import gate, monitor
from ottr.risk.monitor import RiskState
from ottr.risk.types import Intent, Verdict
from ottr.universe import SUNSET, Member, core_members

logger = logging.getLogger(__name__)

BENCH_BASE = "bench_base"
NEXT_MEETING = "next_meeting_at"
DEFERRED_RETRY_SEC = 60.0       # a deferred order is tried again at most this often
BARS_GRACE_SEC = 3600.0         # a final session's bars should be stored within this long
MEETING_GRACE_SEC = 1800.0      # a slot missed by more than this (engine down) is skipped
EARNINGS_BLACKOUT = gate.EARNINGS_BLACKOUT_SESSIONS


def build_council(cfg: Config) -> Council:
    c = cfg.council
    return Council(analysts={n: make_adapter(n, c) for n in c.analysts}, chair=make_adapter(c.chair, c),
                   universe=list(cfg.core), min_analysts=c.min_analysts,
                   max_trades=cfg.trading.max_trades_per_meeting, timeout=c.timeout_sec)


def members_from(cfg: Config, assets: Mapping[str, Asset]) -> Dict[str, Member]:
    """The core members, with Alpaca's fractional-share flag; a symbol Alpaca says isn't
    tradable can't be bought (SELL or HOLD only) until it is again."""
    out = core_members(cfg, {s: a.fractionable for s, a in assets.items()})
    for s, a in assets.items():
        if s in out and not (a.tradable and a.status == "active"):
            out[s] = Member(s, out[s].asset_class, SUNSET, out[s].fractionable)
    return out


def _et(ts: float, fmt: str = "%a %m-%d %H:%M") -> str:
    return datetime.fromtimestamp(ts, tz=ET).strftime(fmt)


class Engine:
    def __init__(self, cfg: Config, store: Store, market: MarketData,
                 council: Optional[Council] = None, alerts: Optional[Alerts] = None,
                 sources: Optional[Sources] = None, clock: Clock = SYSTEM) -> None:
        self.cfg, self.store, self.market, self.clock = cfg, store, market, clock
        self.sources = sources or Sources()
        self.ledger = Ledger(store)
        self.council = council or build_council(cfg)
        self.alerts = alerts or Alerts(None, cfg.alerts)
        self.kill = KillSwitch(cfg, store)
        stored = (store.get_state("assets") or {}).get("assets") or {}
        # Updated in place, so the desk (which holds this dict) always sees the current members.
        self.members: Dict[str, Member] = members_from(cfg, {s: Asset(**a) for s, a in stored.items()})
        self.events = StoredEvents.from_config(store, lambda: market.calendar, cfg)
        self.desk = Desk(cfg, store, self.ledger, market, SimBroker(cfg.fees, cfg.trading.slippage_pct),
                         self.members, self.events, clock)
        self.executor = Executor(cfg, store, self.ledger, self.desk, self.alerts, kill=self.kill)
        self.executor.on_outcome = self._order_outcome
        self.watchdog = health.Watchdog(cfg.engine.alarm_clear_sec)
        self.problems: Dict[str, health.Alarm] = {}  # engine-internal alarms (e.g. API bind)
        self.started: Optional[float] = None
        self.epoch: Optional[float] = None  # start, or resume from a system sleep
        self._last_watch: Optional[float] = None
        self.api = None
        # When the meeting in progress began (None: no meeting), and how far it has got. In
        # memory on purpose: a persisted flag would outlive a crash and claim a meeting that
        # is no longer running.
        self.meeting_started: Optional[float] = None
        self.progress: Optional[MeetingProgress] = None
        # Adjourned meetings whose trades the risk gate hasn't decided yet, by meeting id:
        # their live message keeps updating until every order has an outcome.
        self._awaiting_orders: Dict[str, MeetingProgress] = {}

    # -- members -----------------------------------------------------------------------------
    def classes(self) -> Dict[str, str]:
        return {s: m.asset_class for s, m in self.members.items()}

    async def refresh_members(self) -> None:
        """Rebuild the members from Alpaca's asset records (fetched once a New York day)."""
        fresh = members_from(self.cfg, await self.market.refresh_assets(self.cfg.core))
        for s in sorted(set(self.members) - set(fresh)):
            del self.members[s]
        self.members.update(fresh)

    def buy_blocks(self, now: float) -> Dict[str, str]:
        """Members the gate would refuse to BUY right now, and why, for the council."""
        out = {}
        for s, m in self.members.items():
            if not m.buyable:
                out[s] = "being sold off or not tradable: SELL or HOLD only"
                continue
            if m.asset_class == gate.STOCK:
                n = self.events.earnings_sessions(s, now)
                if n is None:
                    out[s] = "no confirmed earnings date"
                    continue
                if n <= EARNINGS_BLACKOUT:
                    out[s] = f"earnings in {n} sessions"
                    continue
            ca = self.events.corp_action(s, now)
            if ca:
                out[s] = ca
        return out

    # -- market ------------------------------------------------------------------------------
    async def market_tick(self) -> str:
        """Keep the calendar, asset records and daily bars current, and in session prove the
        live feed answers. Raises (a failed heartbeat) when the data can't be trusted."""
        cal = await self.market.refresh_calendar()
        await self.refresh_members()
        await self.market.refresh_bars()
        now = self.clock.time()
        try:
            mt = cal.state_at(now)
        except CalendarError as e:
            raise MarketDataError(str(e)) from e
        problems = []
        final = self.market.final_session(now)
        bars = self.market.raw_bars("SPY")
        if final and (not bars or bars[-1].day < final.day) and now > final.close_ts + SIP_FINAL_AFTER_SEC + BARS_GRACE_SEC:
            problems.append(f"daily bars stop at {bars[-1].day if bars else 'nothing'}, but {final.day} closed")
        if mt.is_open:
            live = await self.market.live_quotes()
            missing = sorted(s for s, m in self.members.items() if m.buyable and s not in live)
            if not live:
                problems.append("no usable live quote for any member")
            detail = (f"open, {len(live)} live quotes" + (f"; none for {', '.join(missing)}" if missing else ""))
        else:
            nxt = mt.next_open
            detail = f"{mt.state.value}; next open {_et(nxt.open_ts) if nxt else 'beyond the calendar'} ET"
        if problems:
            raise MarketDataError("; ".join(problems))
        return detail

    # -- jobs --------------------------------------------------------------------------------
    async def jobs_tick(self) -> str:
        """The day's scheduled work. Events feeds that fail are recorded in `feeds` (and
        alarmed by health); they don't fail this heartbeat."""
        now = self.clock.time()
        cal = self.market.calendar
        notes: List[str] = []
        for day, amount in close_of_day(self.ledger, cal, now):
            logger.info("regulatory fees for %s charged: $%s", day, amount)
            self.store.audit("fees_charged", day=str(day), amount=str(amount))
            notes.append(f"fees for {day} charged")
        if cal.state_at(now).is_open:
            intent = sweep_intent(self.cfg, self.ledger, cal, await self.market.marks(), now)
            if intent is not None:
                await self.executor.submit(intent)
                notes.append("sweep submitted")
            retried = await self._retry_deferred()
            if retried:
                notes.append(f"{retried} deferred order(s) retried")
        held = set(self.ledger.positions())
        for r in await refresh_due(self.cfg, self.store, self.sources, sorted(set(self.members) | held), now):
            notes.append(f"{r.feed} refreshed" if r.ok else f"{r.feed} feed: {r.detail}")
        return "; ".join(notes) or "nothing due"

    async def _retry_deferred(self) -> int:
        """Resubmit orders deferred for a transient reason (a bad quote, the market closed).
        The gate decides again from scratch: an expired council decision is rejected then."""
        wall = time.time()
        rows = self.store.conn.execute(
            "SELECT intent, updated_ts FROM orders WHERE status='deferred' ORDER BY ts").fetchall()
        n = 0
        for raw, updated in rows:
            if wall - updated < DEFERRED_RETRY_SEC:
                continue
            await self.executor.submit(Intent.from_dict(json.loads(raw)))
            n += 1
        return n

    # -- risk --------------------------------------------------------------------------------
    def _blind_state(self, assets: Set[str], now: float) -> Dict[str, float]:
        """asset -> blind since, for the watchdog. Logs only the transitions."""
        prev = self.store.get_state("risk_blind") or {}
        cur = {a: prev.get(a, now) for a in sorted(assets)}
        for a in sorted(set(cur) - set(prev)):
            logger.warning("risk monitor blind on %s: no live quote", a)
        for a in sorted(set(prev) - set(cur)):
            logger.info("risk monitor can see %s again", a)
        return cur

    async def _ensure_stops(self) -> None:
        """Give every holding without a stop its entry stop (the BUY that opens a position
        sets it; holdings brought in at `ottr init` get theirs here, from today's vol)."""
        for s, p in self.ledger.positions().items():
            if s == self.cfg.universe.cash_symbol or p.stop_pct is not None:
                continue
            vol, _ = await self.market.vol_regime(s)
            pct = monitor.entry_stop_pct(self.cfg.risk, vol)
            if self.ledger.set_stop(s, pct):
                logger.info("stop for %s set at %.1f%% under its high (vol %s)", s, pct * 100,
                            f"{vol:.0%}" if vol else "unknown")

    async def risk_tick(self) -> Union[monitor.MonitorResult, str]:
        """The protective monitor in session (its result), or why it rested (a string)."""
        now = self.clock.time()
        mt = self.market.calendar.state_at(now)
        if not mt.is_open:
            self.store.set_state("risk_blind", {})  # no market to see: nothing is blind
            return "market closed: stops wait for the open"
        start = mt.session.open_ts + self.cfg.risk.open_delay_min * 60
        if now < start:
            # The opening auction's first prints are noise; a gap is still caught (and sold
            # at the bid) at the first check after this.
            self.store.set_state("risk_blind", {})
            return f"the open: stops wait until {_et(start, '%H:%M')} ET"
        await self._ensure_stops()
        positions = self.ledger.positions()
        cash_symbol = self.cfg.universe.cash_symbol
        try:
            quotes = await self.market.live_quotes()
            marks = await self.market.marks()
        except MarketDataError:
            # No quotes at all: every holding is unprotected, which the watchdog must see.
            self.store.set_state("risk_blind", self._blind_state(set(positions) - {cash_symbol}, now))
            raise
        valuation = self.ledger.value(marks)
        state = RiskState.from_dict(self.store.get_state("risk"))
        points = self.store.conn.execute("SELECT COUNT(*) FROM equity").fetchone()[0]
        prev_close = {}
        for s in positions:
            done = [b for b in self.market.raw_bars(s, since=mt.session.day - timedelta(days=10))
                    if b.day < mt.session.day]
            if done:
                prev_close[s] = done[-1].close
        res = monitor.check(self.cfg, now, positions, valuation, quotes, state, points, self.classes(), prev_close)
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

    # -- equity ------------------------------------------------------------------------------
    async def equity_tick(self) -> str:
        """An equity point in session; outside it, one point per close, once the close is
        final. The benchmark baseline is the first close after the book opened."""
        now = self.clock.time()
        mt = self.market.calendar.state_at(now)
        final = self.market.final_session(now)
        if not mt.is_open:
            last = self.store.conn.execute("SELECT MAX(ts) FROM equity").fetchone()[0]
            if final is None or (last is not None and last >= final.close_ts + SIP_FINAL_AFTER_SEC):
                return "market closed; the last close is recorded"
        marks = await self.market.marks()
        v = self.ledger.value(marks)
        if v is None:
            raise MarketDataError("a holding has no price; equity unknown (not recorded)")
        if not mt.is_open and any(m.basis != BASIS_CLOSE for m in marks.values()):
            return "waiting for the official close to mark the book"
        base = self.store.get_state(BENCH_BASE)
        if base is None and not mt.is_open and final is not None:
            base = {"day": final.day.isoformat(), "equity": v.equity, "ts": now}
            self.store.set_state(BENCH_BASE, base)
            logger.info("benchmarks start from the %s close at $%.2f", final.day, v.equity)
        bench = await self.market.benchmarks(base)
        self.store.add_equity(now, v.equity, v.cash, v.invested, bench["spy"], bench["60_40"],
                              {"weights": v.weights, "basis": {s: m.basis for s, m in marks.items()}})
        return f"equity ${v.equity:,.2f}"

    # -- council -----------------------------------------------------------------------------
    def book_context(self, snap: UsSnapshot) -> str:
        now = snap.as_of
        marks = {s: v.price for s, v in snap.assets.items() if v.price is not None}
        v = self.ledger.value(marks)
        positions = self.ledger.positions()
        st = RiskState.from_dict(self.store.get_state("risk"))
        cfg, u = self.cfg, self.cfg.universe
        lines = [f"DESK BOOK (paper; {cfg.account.type}: long-only, no leverage, no shorting):"]
        if v is None:
            lines.append("  valuation unavailable (a holding has no price)")
        else:
            bp = buying_power(cfg.account.type, self.ledger, self.market.calendar, now).available
            cash_etf = v.values.get(u.cash_symbol, 0.0)
            risky = v.invested - cash_etf
            stocks = sum(x for s, x in v.values.items() if self.members.get(s) is None
                         or self.members[s].asset_class == gate.STOCK)
            lines.append(f"  equity ${v.equity:,.2f} | cash ${v.cash:,.2f}, buying power ${float(bp):,.2f} | "
                         f"in {u.cash_symbol} (swept cash) ${cash_etf:,.2f} | invested {risky / v.equity * 100:.1f}% "
                         f"of equity (max {cfg.trading.max_gross_pct:.0f}%) | single stocks {stocks / v.equity * 100:.1f}% "
                         f"(sleeve max {u.sleeve_pct:.0f}%)")
        held = []
        for s, p in positions.items():
            q = marks.get(s)
            if q is None or v is None:
                held.append(f"{s} (no price)")
                continue
            cls = self.members[s].asset_class if s in self.members else "no longer a member: SELL or HOLD"
            held.append(f"{s} {v.weights.get(s, 0) * 100:.1f}% of book ({cls}; avg cost ${float(p.avg_cost):,.2f}, "
                        f"now ${q.bid:,.2f} {q.basis}, {(q.bid / float(p.avg_cost) - 1) * 100:+.1f}%)")
        lines.append("  HOLDINGS: " + ("; ".join(held) or "none"))
        lines.append("  Risk-model target weight at conviction 1.0 (BUYs only top up to this): " + ", ".join(
            f"{s} {gate.target_weight(cfg, s, av.asset_class, av.vol, av.regime, 1.0) * 100:.1f}%"
            for s, av in snap.assets.items() if av.asset_class and s != u.cash_symbol))
        caps = ", ".join(f"{c} {p:.0f}%" for c, p in sorted(cfg.caps.by_class.items()))
        over = ", ".join(f"{s} {p:.0f}%" for s, p in sorted(cfg.caps.by_symbol.items()))
        lines.append(f"  Caps (% of equity): {caps}" + (f"; overrides {over}" if over else "") +
                     f"; {u.cash_symbol} uncapped")
        blocked = self.buy_blocks(now)
        if blocked:
            lines.append("  BUYS BLOCKED (code drops these): " + "; ".join(f"{s} ({why})" for s, why in sorted(blocked.items())))
        lines.append(f"  Drawdown breaker: {'HALTED (BUYs blocked)' if st.halted else 'normal'}")
        recent = self.ledger.fills(5)
        if recent:
            lines.append("  RECENT FILLS: " + "; ".join(
                f"{_et(f['ts'])} {f['side']} {f['asset']} ${float(f['notional']):,.0f} ({f['origin']})" for f in recent))
        last = self.store.conn.execute(
            "SELECT record FROM meetings WHERE status='ok' ORDER BY ts DESC LIMIT 1").fetchone()
        if last:
            d = json.loads(last[0]).get("decision") or {}
            lines.append(f"  LAST MEETING SUMMARY: {d.get('summary', '')}")
        return "\n".join(lines)

    async def meeting(self, execute: bool = True) -> Dict[str, Any]:
        """One council meeting. While it runs, `meeting_started` and `progress` say so (GET
        /api/council) and the live Discord message follows it."""
        for stale in list(self._awaiting_orders.values()):
            stale.close("the next meeting started first")
            self._publish(stale)
        self._awaiting_orders.clear()
        prog = MeetingProgress(new_meeting_id(), list(self.council.analysts), self.council.chair.name)
        self.meeting_started, self.progress = prog.started, prog
        self._publish(prog)
        try:
            return await self._meeting(execute, prog)
        except asyncio.CancelledError:
            prog.abort("cancelled, the engine is stopping")
            self._publish(prog)
            raise
        except Exception as e:
            prog.abort(str(e) if isinstance(e, MarketDataError) else f"{type(e).__name__}: {e}")
            self._publish(prog)
            raise
        finally:
            self.meeting_started = self.progress = None

    def _publish(self, prog: MeetingProgress) -> None:
        """Hand the meeting's current state to the live Discord message (never raises)."""
        if not self.cfg.alerts.live_meeting:
            return
        try:
            self.alerts.live(f"meeting:{prog.id}", "meeting_live", prog.to_dict(), final=prog.final)
        except Exception:  # a display bug must not cost a meeting
            logger.warning("live meeting view failed", exc_info=True)

    def _on_council(self, prog: MeetingProgress, kind: str, d: Dict[str, Any]) -> None:
        prog.apply(kind, d)
        if kind == "analyst_done":
            leans = ", ".join(f"{s['stance']} {s['asset']} {s['conviction']:.2f}"
                              for s in prog.analysts[d["name"]]["stances"])
            logger.info("council %s: %s done in %.0fs (%s)", prog.id, d["name"], d["latency"], leans or "all HOLD")
        elif kind == "chair_started":
            logger.info("council %s: chair deliberating on %d reports", prog.id, d["reports"])
        elif kind == "chair_done":
            logger.info("council %s: chair decided in %.0fs (%d trade(s))", prog.id, d["latency"], len(d["trades"]))
        elif kind == "chair_failed":
            logger.warning("council %s: chair failed after %.0fs: %s", prog.id, d["latency"], d["error"])
        self._publish(prog)

    def _order_outcome(self, intent: Intent, status: str, verdict: Verdict) -> None:
        """Executor hook: the risk gate's answer to a council trade goes into its live message."""
        prog = self._awaiting_orders.get(intent.ref) if intent.origin == "council" else None
        if prog is None or not prog.order(intent.asset, status, verdict.reason, verdict.notional,
                                          verdict.price, verdict.notes):
            return
        if prog.final:
            del self._awaiting_orders[prog.id]
        self._publish(prog)

    def _close_awaiting(self, why: str) -> None:
        for prog in self._awaiting_orders.values():
            prog.close(why)
            self._publish(prog)
        self._awaiting_orders.clear()

    async def _meeting(self, execute: bool, prog: MeetingProgress) -> Dict[str, Any]:
        now = self.clock.time()
        traded = self.classes()
        macro = macro_readings(self.store, self.cfg.events.macro, trade_day(now), self.cfg.events.macro_stale_days)
        snap = await self.market.snapshot(traded, self.events, macro)
        if all(v.price is None for v in snap.assets.values()):
            raise MarketDataError("no price for any asset")
        context = snap.packet() + "\n\n" + self.book_context(snap)
        held = set(self.ledger.positions())
        rec = await self.council.run(context, held, meeting_id=prog.id,
                                     observe=lambda kind, d: self._on_council(prog, kind, d),
                                     universe=sorted(traded), blocked=self.buy_blocks(snap.as_of))
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
        prog.adjourn(rec, executing=execute)
        if prog.phase == "orders":  # registered before submitting, so no outcome can slip past
            self._awaiting_orders[prog.id] = prog
        self._publish(prog)
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

    def next_meeting(self, after: float) -> float:
        s = self.cfg.schedule
        return next_meeting_at(self.market.calendar, after, s.meeting_times, s.min_before_close_min)

    async def _council_loop(self) -> None:
        while True:
            try:
                now = self.clock.time()
                due = self.store.get_state(NEXT_MEETING)
                if due is None:
                    due = self.next_meeting(now)
                    self.store.set_state(NEXT_MEETING, due)
            except (MarketDataError, CalendarError) as e:
                logger.warning("council schedule unknown (%s); retrying in a minute", e)
                await asyncio.sleep(60)
                continue
            if now < due:
                await asyncio.sleep(min(due - now, 60))
                continue
            try:
                self.store.set_state(NEXT_MEETING, self.next_meeting(max(now, due)))
            except CalendarError as e:
                logger.warning("no council slot ahead in the calendar (%s); retrying in a minute", e)
                await asyncio.sleep(60)
                continue
            if now - due > MEETING_GRACE_SEC or not self.market.calendar.state_at(now).is_open:
                # The engine was down (or asleep) through the slot: a late meeting would trade
                # into the close or a closed market, so wait for the next one instead.
                logger.warning("skipped the council slot of %s ET: it passed while the engine was down",
                               _et(due, "%Y-%m-%d %H:%M"))
                self.store.audit("meeting_skipped", slot=due)
                continue
            try:
                rec = await self.meeting(execute=True)
                self._beat("council", rec["status"] == "ok", rec["status"])
            except Exception as e:
                logger.exception("meeting failed")
                self._beat("council", False, f"{type(e).__name__}: {e}")

    # -- health ------------------------------------------------------------------------------
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

    # -- api ---------------------------------------------------------------------------------
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

    # -- loop --------------------------------------------------------------------------------
    async def _every(self, name: str, interval: float, fn: Callable[[], Awaitable[Any]],
                     first_delay: float = 0.0) -> None:
        await asyncio.sleep(first_delay)
        while True:
            t0 = time.time()
            try:
                out = await fn()
                summary = getattr(out, "summary", None)
                ok, detail = True, out if isinstance(out, str) else summary() if callable(summary) else ""
            except MarketDataError as e:  # an outage, not a bug: no traceback every minute
                logger.warning("task %s: %s", name, e)
                ok, detail = False, f"MarketDataError: {e}"
            except Exception as e:
                logger.exception("task %s failed", name)
                ok, detail = False, f"{type(e).__name__}: {e}"
            self._beat(name, ok, detail)
            await asyncio.sleep(max(1.0, interval - (time.time() - t0)))

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
                e = self.cfg.engine
                try:
                    # The first market tick fetches the calendar every other task needs.
                    await self.market.refresh_calendar()
                except MarketDataError as err:
                    logger.warning("no calendar at start-up (%s); the market task keeps trying", err)
                core = [
                    asyncio.create_task(self.executor.run(), name="executor"),
                    asyncio.create_task(self._every("market", e.market_interval_sec, self.market_tick), name="market"),
                    asyncio.create_task(self._every("jobs", e.jobs_interval_sec, self.jobs_tick, 20), name="jobs"),
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
                self._close_awaiting("the engine stopped before the risk gate decided")
                self._save_engine_state(running=False, stopped_at=time.time(), exit_code=code)
                self.store.audit("engine_stop", code=code, why=why)
                self.alerts.event("engine_stop", {"code": code, "why": why})
        finally:
            restore_signals(undo)
        logger.info("engine stopped %s", "cleanly" if code == 0 else f"with exit code {code}")
        return code
