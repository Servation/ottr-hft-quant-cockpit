"""
Executor: the single path from an Intent to a paper fill.

What it does: one consumer drains a priority queue (protective exits first). For each
intent it claims the idempotency key, asks the desk for a fresh context (quote, book,
risk state), runs the desk's gate, and either rejects, records a dry-run verdict, or books
the fill through the desk. The fill, order status, cooldown and audit commit in ONE
transaction. A BUY that is short of cash while the desk holds its cash ETF is funded first
(the desk names the SELL), then checked again, once. Why: v1 had several code paths that
could trade and needed a separate idempotency guard; here there is one, and a crash at any
point either fully booked the trade or left no trace of it. The market-specific parts live
in engine/desks.py, so every invariant here holds whatever the broker.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from typing import Awaitable, Callable, Optional

from ottr.engine.killswitch import KillSwitch
from ottr.ledger.book import Ledger
from ottr.ledger.store import Store, dumps
from ottr.market.types import MarketDataError
from ottr.risk.monitor import RiskState
from ottr.risk.types import PROTECTIVE, Intent, Verdict

logger = logging.getLogger(__name__)

Notifier = Callable[[str, dict], Awaitable[None]]
# on_outcome(intent, status, verdict) after every decision, once it is committed. Status:
# filled | dry_run | rejected | deferred | duplicate | error.
OutcomeHook = Callable[[Intent, str, Verdict], None]


class Executor:
    def __init__(self, cfg, store: Store, ledger: Ledger, desk,
                 notify: Optional[Notifier] = None, kill: Optional[KillSwitch] = None) -> None:
        self.cfg, self.store, self.ledger, self.desk = cfg, store, ledger, desk
        self.notify = notify
        self.on_outcome: Optional[OutcomeHook] = None
        # Read at every decision, so engaging the switch stops the very next fill.
        self.kill = kill or KillSwitch(cfg, store)
        self.queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._seq = itertools.count()

    async def submit(self, intent: Intent) -> None:
        await self.queue.put((intent.priority, next(self._seq), intent))

    async def run(self) -> None:
        while True:
            _, _, intent = await self.queue.get()
            try:
                await self.process(intent)
            except Exception as e:
                logger.exception("executor failed on %s", intent.idem_key)
                self.store.audit("executor_error", idem_key=intent.idem_key)
                self._outcome(intent, "error", Verdict.reject(intent, "executor_error", f"{type(e).__name__}: {e}"))
            finally:
                self.queue.task_done()

    async def drain(self) -> None:
        """Process everything queued right now (CLI one-shots and tests)."""
        while not self.queue.empty():
            _, _, intent = self.queue.get_nowait()
            await self.process(intent)
            self.queue.task_done()

    # ------------------------------------------------------------------------------------
    def _claim(self, intent: Intent) -> Optional[Verdict]:
        """Insert the order as pending, or return the stored verdict if already final."""
        row = self.store.conn.execute("SELECT status, verdict FROM orders WHERE idem_key=?",
                                      (intent.idem_key,)).fetchone()
        # A dry-run verdict is only final while we are still in dry run; otherwise a stop
        # first seen in dry run could never execute for real once trading is switched on.
        final = ("filled", "rejected", "dry_run") if self.kill.engaged else ("filled", "rejected")
        if row and row[0] in final:
            v = json.loads(row[1]) if row[1] else {}
            return Verdict(False, intent.asset, intent.side, f"duplicate:{row[0]}",
                           notes=(v.get("reason", ""),))
        now = time.time()
        with self.store.tx() as c:
            c.execute("INSERT INTO orders(idem_key,ts,origin,asset,side,intent,status,updated_ts) "
                      "VALUES(?,?,?,?,?,?,'pending',?) ON CONFLICT(idem_key) DO UPDATE SET "
                      "status='pending', updated_ts=excluded.updated_ts",
                      (intent.idem_key, now, intent.origin, intent.asset, intent.side,
                       dumps(intent.to_dict()), now))
        return None

    def _finish(self, intent: Intent, status: str, verdict: Verdict) -> None:
        self.store.conn.execute("UPDATE orders SET status=?, verdict=?, updated_ts=? WHERE idem_key=?",
                                (status, dumps(verdict.to_dict()), time.time(), intent.idem_key))

    def _outcome(self, intent: Intent, status: str, verdict: Verdict) -> None:
        # Called after the commit, and guarded: a watcher (the live meeting view) can see
        # what happened but can never undo or break it.
        if self.on_outcome is None:
            return
        try:
            self.on_outcome(intent, status, verdict)
        except Exception:
            logger.warning("order outcome hook failed for %s", intent.idem_key, exc_info=True)

    def _reject(self, intent: Intent, verdict: Verdict) -> Verdict:
        status = "deferred" if verdict.reason in self.desk.transient else "rejected"
        with self.store.tx():
            self._finish(intent, status, verdict)
            self.store.audit("order_rejected", idem_key=intent.idem_key, origin=intent.origin,
                             asset=intent.asset, side=intent.side, reason=verdict.reason,
                             notes=list(verdict.notes), status=status)
        logger.info("%s %s %s %s: %s %s", intent.origin, intent.side, intent.asset, status,
                    verdict.reason, "; ".join(verdict.notes))
        self._outcome(intent, status, verdict)
        return verdict

    async def _decide(self, intent: Intent):
        try:
            ctx = await self.desk.context(intent)
            return ctx, self.desk.evaluate(intent, ctx)
        except MarketDataError as e:
            return None, Verdict.reject(intent, "market_data_error", str(e))

    async def process(self, intent: Intent, _funding: bool = True) -> Verdict:
        dup = self._claim(intent)
        if dup:
            self._outcome(intent, "duplicate", dup)
            return dup
        ctx, verdict = await self._decide(intent)

        if verdict.reason == "needs_cash" and _funding and not self.kill.engaged:
            fund = self.desk.funding(intent, verdict, ctx)
            if fund is not None:
                logger.info("funding %s %s: %s", intent.side, intent.asset, fund.reason)
                funded = await self.process(fund, _funding=False)
                if funded.approved:
                    ctx, verdict = await self._decide(intent)
        if not verdict.approved:
            return self._reject(intent, verdict)

        if self.kill.engaged:
            with self.store.tx():
                self._finish(intent, "dry_run", verdict)
                self.store.audit("would_fill", idem_key=intent.idem_key, origin=intent.origin,
                                 **{k: v for k, v in verdict.to_dict().items() if k != "approved"})
            logger.info("DRY RUN would %s %s %s @ %.6g ($%.2f)", verdict.side, verdict.quantity,
                        verdict.asset, verdict.price, verdict.notional)
            self._outcome(intent, "dry_run", verdict)
            return verdict

        with self.store.tx():
            booked = self.desk.book(intent, verdict, ctx)
            if booked.fill is None:
                # The broker didn't fill (e.g. the quote moved past the limit): the
                # transaction holds no fill, so record the outcome and let it retry.
                refused = Verdict.reject(intent, booked.reason or "broker_rejected")
                self._finish(intent, "deferred" if refused.reason in self.desk.transient else "rejected", refused)
            else:
                self._finish(intent, "filled", verdict)
                if intent.origin in PROTECTIVE:
                    st = RiskState.from_dict(self.store.get_state("risk"))
                    st.cooldowns[intent.asset] = time.time() + self.cfg.risk.cooldown_sec
                    self.store.set_state("risk", st.to_dict())
        if booked.fill is None:
            status = "deferred" if refused.reason in self.desk.transient else "rejected"
            self.store.audit("order_rejected", idem_key=intent.idem_key, origin=intent.origin, asset=intent.asset,
                             side=intent.side, reason=refused.reason, notes=[], status=status)
            logger.info("%s %s %s %s by the broker: %s", intent.origin, intent.side, intent.asset, status, refused.reason)
            self._outcome(intent, status, refused)
            return refused
        fill = booked.fill
        logger.info("FILLED %s %s %s @ %s ($%s, fee $%s, pnl $%s) [%s]", fill.side, fill.qty,
                    fill.asset, fill.price, fill.notional, fill.fee, fill.realized_pnl, intent.origin)
        self._outcome(intent, "filled", verdict)
        if self.notify:
            try:
                await self.notify("fill", {"origin": intent.origin, "side": fill.side,
                                           "asset": fill.asset, "qty": str(fill.qty),
                                           "price": str(fill.price), "notional": str(fill.notional),
                                           "realized_pnl": str(fill.realized_pnl),
                                           "reason": intent.reason})
            except Exception:
                logger.warning("notify failed", exc_info=True)
        return verdict
