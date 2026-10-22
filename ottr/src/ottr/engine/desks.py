"""
The Desk: the market-specific half of the executor.

What it does: the Executor owns everything that must never differ between brokers (the
priority queue, idempotency, dry run, the one-transaction commit, cooldowns, outcomes and
notifications). The Desk supplies the rest for US equities: `context()` gathers the facts
the gate needs (session, a sanity-checked quote, the book, buying power, events),
`evaluate()` is the gate, `book()` has the Broker (the SimBroker, later Alpaca) execute an
approved order and records the fill with the regulatory fees it accrued, and `funding()`
names the cash-ETF sale that frees cash for a BUY that's short of it.

Why the split: every invariant (no double fill, crash safety, dry run, protective exits
first) lives in the executor by construction, so swapping the SimBroker for a live
broker later changes nothing above this file.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Mapping, Optional, Protocol, Tuple

from ottr.broker.account import buying_power
from ottr.broker.base import Order
from ottr.broker.sim import SimBroker
from ottr.clock import SYSTEM, Clock
from ottr.ledger.book import Fill, Ledger
from ottr.ledger.store import Store
from ottr.market.calendar import TradingCalendar
from ottr.market.types import Quote
from ottr.risk import gate
from ottr.risk.monitor import RiskState, entry_stop_pct
from ottr.risk.types import PROTECTIVE, Intent, Verdict
from ottr.universe import Member

logger = logging.getLogger(__name__)
REFRESH_IF_OLDER_SEC = 10.0


@dataclass(frozen=True)
class BookResult:
    fill: Optional[Fill]
    reason: str = ""          # why the broker didn't fill, when it didn't


def risk_flags(store: Store, intent: Intent) -> Tuple[RiskState, bool]:
    """The persisted risk state, and whether a protective fill hit this asset after the
    intent was created (a BUY decided before a stop-out must not reopen the position)."""
    st = RiskState.from_dict(store.get_state("risk"))
    since = store.conn.execute(
        "SELECT 1 FROM fills WHERE asset=? AND origin IN (%s) AND ts>=? LIMIT 1" % ",".join("?" * len(PROTECTIVE)),
        (intent.asset, *PROTECTIVE, intent.created_ts)).fetchone()
    return st, since is not None


class Market(Protocol):
    """What the desk needs from market data (`ottr.market.service.MarketData`)."""

    calendar: TradingCalendar

    async def quote(self, symbol: str) -> Tuple[Optional[Quote], str]:
        """A fresh quote that passed the sanity check, or (None, why not)."""

    async def marks(self) -> Mapping[str, Quote]:
        """A price for every held symbol, to value the book (None-safe: missing = unknown)."""

    async def vol_regime(self, symbol: str) -> Tuple[Optional[float], str]:
        """Annualized vol (252 trading days) and TRENDING / CHOPPY / UNKNOWN."""


class Events(Protocol):
    """Earnings and corporate actions for the gate (`ottr.events.view.StoredEvents`). There
    is deliberately no default: a desk built without real events would trade blind."""

    def earnings_sessions(self, symbol: str, now: float) -> Optional[int]:
        """Sessions to the next report; None when unknown (which blocks a stock's BUY)."""

    def corp_action(self, symbol: str, now: float) -> Optional[str]:
        """What blocks a BUY (a pending action, or actions unknown), or None."""


class Desk:
    transient = ("market_closed", "no_quote", "book_unmarkable", "market_data_error",
                 "limit_not_marketable", "needs_cash")

    def __init__(self, cfg, store: Store, ledger: Ledger, market: Market, broker: SimBroker,
                 members: Mapping[str, Member], events: Events, clock: Clock = SYSTEM) -> None:
        self.cfg, self.store, self.ledger, self.market = cfg, store, ledger, market
        self.broker, self.members, self.events, self.clock = broker, members, events, clock

    async def context(self, intent: Intent) -> gate.GateContext:
        now = self.clock.time()
        cal = self.market.calendar
        quote, problem = await self.market.quote(intent.asset)
        marks = dict(await self.market.marks())
        if quote is not None:
            marks[intent.asset] = quote
        valuation = self.ledger.value(marks)
        positions = self.ledger.positions()
        member = self.members.get(intent.asset)
        cash_symbol = self.cfg.universe.cash_symbol
        values = valuation.values if valuation else {}
        satellites = sum(v for a, v in values.items()
                         if self.members.get(a) is None or self.members[a].asset_class == gate.STOCK)
        vol, regime = await self.market.vol_regime(intent.asset)
        st, hit = risk_flags(self.store, intent)
        return gate.GateContext(
            cfg=self.cfg, now=now, market=cal.state_at(now), member=member,
            quote=quote, quote_problem=problem,
            limit_price=self.broker.price_for(intent.side, quote) if quote is not None else None,
            vol=vol, regime=regime, valuation=valuation, position=positions.get(intent.asset),
            buying_power=buying_power(self.cfg.account.type, self.ledger, cal, now).available,
            cash_etf_value=values.get(cash_symbol, 0.0), satellites_value=satellites,
            earnings_sessions=self.events.earnings_sessions(intent.asset, now),
            corp_action=self.events.corp_action(intent.asset, now),
            halted=st.halted, cooldown_until=st.cooldowns.get(intent.asset, 0.0), risk_event_since_intent=hit)

    def evaluate(self, intent: Intent, ctx: gate.GateContext) -> Verdict:
        return gate.evaluate(intent, ctx)

    def book(self, intent: Intent, verdict: Verdict, ctx: gate.GateContext) -> BookResult:
        member = self.members.get(intent.asset)
        order = Order(intent.idem_key, intent.asset, verdict.side, verdict.quantity,
                      ctx.limit_price, intent.origin)
        ex = self.broker.execute(order, ctx.quote, ctx.now, fractionable=member.fractionable if member else True)
        if not ex.filled:
            return BookResult(None, ex.reason)
        fill = self.ledger.apply_fill(
            idem_key=intent.idem_key, asset=intent.asset, side=verdict.side, quantity=ex.qty, price=ex.price,
            accrued=ex.accrued, quote=ctx.quote, quote_source=ctx.quote.source, origin=intent.origin,
            ref=intent.ref, reason=intent.reason, now=ctx.now)
        if verdict.side == "BUY" and intent.asset != self.cfg.universe.cash_symbol:
            # The stop is set by the BUY that opens the position (same transaction), from
            # the vol it was sized on; an add to an open position keeps the stop it has.
            self.ledger.set_stop(intent.asset, entry_stop_pct(self.cfg.risk, ctx.vol))
        return BookResult(fill)

    def funding(self, intent: Intent, verdict: Verdict, ctx: gate.GateContext) -> Optional[Intent]:
        """Sell enough of the cash ETF to cover the shortfall (plus a hair for the fill's
        spread and rounding)."""
        held = ctx.cash_etf_value
        if verdict.funding_usd <= 0 or held <= 0:
            return None
        fraction = min(1.0, verdict.funding_usd * 1.005 / held)
        return Intent(origin="sweep", asset=self.cfg.universe.cash_symbol, side="SELL", fraction=fraction,
                      idem_key=f"fund:{intent.idem_key}", reason=f"fund {intent.side} {intent.asset}",
                      ref=intent.idem_key, created_ts=ctx.now)
