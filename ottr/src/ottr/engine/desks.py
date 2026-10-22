"""
Desks: the market-specific half of the executor.

What it does: the Executor owns everything that must never differ between markets (the
priority queue, idempotency, dry run, the one-transaction commit, cooldowns, outcomes and
notifications). A Desk supplies the rest: `context()` gathers the facts the gate needs,
`evaluate()` is the gate, `book()` executes an approved order and records the fill, and
`funding()` names the order that frees cash for a BUY that's short of it.

- CryptoDesk is the crypto engine's behavior, unchanged, until the cutover retires it.
- UsDesk prices US equities: session-aware, quotes checked by the data layer, sized and
  capped by the US gate, filled by a Broker (the SimBroker, later Alpaca) and booked with
  the regulatory fees the broker says the fill accrued.

Why: one executor means every invariant (no double fill, crash safety, dry run, protective
exits first) holds for both desks by construction, and is tested once against each.
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
from ottr.risk import gate, us_gate
from ottr.risk.monitor import RiskState
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


# -- crypto (until the cutover) ------------------------------------------------------------
class CryptoDesk:
    transient = ("no_quote", "stale_quote", "book_unmarkable", "market_data_error")

    def __init__(self, cfg, store: Store, ledger: Ledger, market) -> None:
        self.cfg, self.store, self.ledger, self.market = cfg, store, ledger, market

    async def context(self, intent: Intent) -> gate.GateContext:
        import time
        from ottr.market.service import build_view
        quotes = await self.market.quotes()
        q = quotes.get(intent.asset)
        if q is None or q.age() > REFRESH_IF_OLDER_SEC:
            quotes = await self.market.quotes(force=True)
        view = None
        try:
            candles = await self.market.candles()
            btc = [c for _, c in candles.get("BTC", [])]
            view = build_view(intent.asset, quotes.get(intent.asset), candles.get(intent.asset, []), btc, self.cfg)
        except Exception:
            logger.warning("candles unavailable; sizing with unknown vol/regime", exc_info=True)
        st, hit = risk_flags(self.store, intent)
        return gate.GateContext(
            cfg=self.cfg, now=time.time(), quote=quotes.get(intent.asset),
            vol=view.vol if view else None, regime=view.regime if view else "UNKNOWN",
            valuation=self.ledger.value(quotes), position=self.ledger.positions().get(intent.asset),
            halted=st.halted, cooldown_until=st.cooldowns.get(intent.asset, 0.0), risk_event_since_intent=hit)

    def evaluate(self, intent: Intent, ctx) -> Verdict:
        return gate.evaluate(intent, ctx)

    def book(self, intent: Intent, verdict: Verdict, ctx) -> BookResult:
        return BookResult(self.ledger.apply_fill(
            idem_key=intent.idem_key, asset=intent.asset, side=verdict.side, quantity=verdict.quantity,
            price=verdict.price, fee_pct=self.cfg.trading.fee_pct, quote_source=verdict.quote_source,
            origin=intent.origin, ref=intent.ref, reason=intent.reason))

    def funding(self, intent: Intent, verdict: Verdict, ctx) -> Optional[Intent]:
        return None


# -- US equities ---------------------------------------------------------------------------
class UsMarket(Protocol):
    """What the US desk needs from market data (the Alpaca-backed service lands in PR 7)."""

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


class UsDesk:
    transient = ("market_closed", "no_quote", "book_unmarkable", "market_data_error",
                 "limit_not_marketable", "needs_cash")

    def __init__(self, cfg, store: Store, ledger: Ledger, market: UsMarket, broker: SimBroker,
                 members: Mapping[str, Member], events: Events, clock: Clock = SYSTEM) -> None:
        self.cfg, self.store, self.ledger, self.market = cfg, store, ledger, market
        self.broker, self.members, self.events, self.clock = broker, members, events, clock

    async def context(self, intent: Intent) -> us_gate.UsGateContext:
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
                         if self.members.get(a) is None or self.members[a].asset_class == us_gate.STOCK)
        vol, regime = await self.market.vol_regime(intent.asset)
        st, hit = risk_flags(self.store, intent)
        return us_gate.UsGateContext(
            cfg=self.cfg, now=now, market=cal.state_at(now), member=member,
            quote=quote, quote_problem=problem,
            limit_price=self.broker.price_for(intent.side, quote) if quote is not None else None,
            vol=vol, regime=regime, valuation=valuation, position=positions.get(intent.asset),
            buying_power=buying_power(self.cfg.account.type, self.ledger, cal, now).available,
            cash_etf_value=values.get(cash_symbol, 0.0), satellites_value=satellites,
            earnings_sessions=self.events.earnings_sessions(intent.asset, now),
            corp_action=self.events.corp_action(intent.asset, now),
            halted=st.halted, cooldown_until=st.cooldowns.get(intent.asset, 0.0), risk_event_since_intent=hit)

    def evaluate(self, intent: Intent, ctx: us_gate.UsGateContext) -> Verdict:
        return us_gate.evaluate(intent, ctx)

    def book(self, intent: Intent, verdict: Verdict, ctx: us_gate.UsGateContext) -> BookResult:
        member = self.members.get(intent.asset)
        order = Order(intent.idem_key, intent.asset, verdict.side, verdict.quantity,
                      ctx.limit_price, intent.origin)
        ex = self.broker.execute(order, ctx.quote, ctx.now, fractionable=member.fractionable if member else True)
        if not ex.filled:
            return BookResult(None, ex.reason)
        return BookResult(self.ledger.apply_fill(
            idem_key=intent.idem_key, asset=intent.asset, side=verdict.side, quantity=ex.qty, price=ex.price,
            accrued=ex.accrued, quote=ctx.quote, quote_source=ctx.quote.source, origin=intent.origin,
            ref=intent.ref, reason=intent.reason, now=ctx.now))

    def funding(self, intent: Intent, verdict: Verdict, ctx: us_gate.UsGateContext) -> Optional[Intent]:
        """Sell enough of the cash ETF to cover the shortfall (plus a hair for the fill's
        spread and rounding)."""
        held = ctx.cash_etf_value
        if verdict.funding_usd <= 0 or held <= 0:
            return None
        fraction = min(1.0, verdict.funding_usd * 1.005 / held)
        return Intent(origin="sweep", asset=self.cfg.universe.cash_symbol, side="SELL", fraction=fraction,
                      idem_key=f"fund:{intent.idem_key}", reason=f"fund {intent.side} {intent.asset}",
                      ref=intent.idem_key, created_ts=ctx.now)
