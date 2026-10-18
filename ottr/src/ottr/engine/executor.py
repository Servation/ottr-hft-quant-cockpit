"""
Executor: the single path from an Intent to a paper fill.

What it does: one consumer drains a priority queue (protective exits first). For each
intent it claims the idempotency key, refreshes the quote, rebuilds the gate context
from the live book, and either rejects, records a dry-run verdict, or books the fill.
The fill, order status, cooldown and audit commit in ONE transaction. Why: v1 had several
code paths that could trade and needed a separate idempotency guard; here there is one,
and a crash at any point either fully booked the trade or left no trace of it.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import time
from typing import Awaitable, Callable, Optional

from ottr.config import Config
from ottr.ledger.book import Ledger
from ottr.ledger.store import Store, dumps
from ottr.market.service import MarketData, build_view
from ottr.market.types import MarketDataError
from ottr.risk import gate
from ottr.risk.monitor import RiskState
from ottr.risk.types import PROTECTIVE, Intent, Verdict

logger = logging.getLogger(__name__)

# Rejections that can clear on their own: leave the key retryable instead of terminal.
TRANSIENT = ("no_quote", "stale_quote", "book_unmarkable", "market_data_error")
REFRESH_IF_OLDER_SEC = 10.0

Notifier = Callable[[str, dict], Awaitable[None]]


class Executor:
    def __init__(self, cfg: Config, store: Store, ledger: Ledger, market: MarketData,
                 notify: Optional[Notifier] = None) -> None:
        self.cfg, self.store, self.ledger, self.market = cfg, store, ledger, market
        self.notify = notify
        self.queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._seq = itertools.count()

    async def submit(self, intent: Intent) -> None:
        await self.queue.put((intent.priority, next(self._seq), intent))

    async def run(self) -> None:
        while True:
            _, _, intent = await self.queue.get()
            try:
                await self.process(intent)
            except Exception:
                logger.exception("executor failed on %s", intent.idem_key)
                self.store.audit("executor_error", idem_key=intent.idem_key)
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
        final = ("filled", "rejected", "dry_run") if self.cfg.trading.dry_run else ("filled", "rejected")
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

    async def _context(self, intent: Intent) -> gate.GateContext:
        quotes = await self.market.quotes()
        q = quotes.get(intent.asset)
        if q is None or q.age() > REFRESH_IF_OLDER_SEC:
            quotes = await self.market.quotes(force=True)
        snap_view = None
        try:
            candles = await self.market.candles()
            btc = [c for _, c in candles.get("BTC", [])]
            snap_view = build_view(intent.asset, quotes.get(intent.asset),
                                   candles.get(intent.asset, []), btc, self.cfg)
        except Exception:
            logger.warning("candles unavailable; sizing with unknown vol/regime", exc_info=True)
        st = RiskState.from_dict(self.store.get_state("risk"))
        since = self.store.conn.execute(
            "SELECT 1 FROM fills WHERE asset=? AND origin IN (%s) AND ts>=? LIMIT 1"
            % ",".join("?" * len(PROTECTIVE)),
            (intent.asset, *PROTECTIVE, intent.created_ts)).fetchone()
        return gate.GateContext(
            cfg=self.cfg, now=time.time(), quote=quotes.get(intent.asset),
            vol=snap_view.vol if snap_view else None,
            regime=snap_view.regime if snap_view else "UNKNOWN",
            valuation=self.ledger.value(quotes),
            position=self.ledger.positions().get(intent.asset),
            halted=st.halted, cooldown_until=st.cooldowns.get(intent.asset, 0.0),
            risk_event_since_intent=since is not None,
        )

    async def process(self, intent: Intent) -> Verdict:
        dup = self._claim(intent)
        if dup:
            return dup
        try:
            ctx = await self._context(intent)
            verdict = gate.evaluate(intent, ctx)
        except MarketDataError as e:
            verdict = Verdict.reject(intent, "market_data_error", str(e))

        if not verdict.approved:
            status = "deferred" if verdict.reason in TRANSIENT else "rejected"
            with self.store.tx():
                self._finish(intent, status, verdict)
                self.store.audit("order_rejected", idem_key=intent.idem_key, origin=intent.origin,
                                 asset=intent.asset, side=intent.side, reason=verdict.reason,
                                 notes=list(verdict.notes), status=status)
            logger.info("%s %s %s rejected: %s %s", intent.origin, intent.side, intent.asset,
                        verdict.reason, "; ".join(verdict.notes))
            return verdict

        if self.cfg.trading.dry_run:
            with self.store.tx():
                self._finish(intent, "dry_run", verdict)
                self.store.audit("would_fill", idem_key=intent.idem_key, origin=intent.origin,
                                 **{k: v for k, v in verdict.to_dict().items() if k != "approved"})
            logger.info("DRY RUN would %s %s %s @ %.6g ($%.2f)", verdict.side, verdict.quantity,
                        verdict.asset, verdict.price, verdict.notional)
            return verdict

        with self.store.tx():
            fill = self.ledger.apply_fill(
                idem_key=intent.idem_key, asset=intent.asset, side=verdict.side,
                quantity=verdict.quantity, price=verdict.price, fee_pct=self.cfg.trading.fee_pct,
                quote_source=verdict.quote_source, origin=intent.origin, ref=intent.ref,
                reason=intent.reason)
            self._finish(intent, "filled", verdict)
            if intent.origin in PROTECTIVE:
                st = RiskState.from_dict(self.store.get_state("risk"))
                st.cooldowns[intent.asset] = time.time() + self.cfg.risk.cooldown_sec
                self.store.set_state("risk", st.to_dict())
        logger.info("FILLED %s %s %s @ %s ($%s, fee $%s, pnl $%s) [%s]", fill.side, fill.qty,
                    fill.asset, fill.price, fill.notional, fill.fee, fill.realized_pnl, intent.origin)
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
