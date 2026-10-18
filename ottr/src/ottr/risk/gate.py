"""
Pre-trade gate: turns an Intent into an exact, policy-compliant Verdict. Pure function.

What it does: BUYs are sized by code (vol-target weight x conviction x regime, capped),
trade only the gap to that target, then pass every limit (halt, cooldown, staleness,
decision expiry, single-trade ceiling, gross exposure, cash reserve, min trade). SELLs
are only refused when there is nothing to sell or no fresh price: limits exist to stop
risk going UP, so they must never block risk going DOWN.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from ottr.config import Config
from ottr.ledger.book import Position, Valuation, qty
from ottr.market.types import Quote
from ottr.risk.types import Intent, Verdict


@dataclass(frozen=True)
class GateContext:
    cfg: Config
    now: float
    quote: Optional[Quote]
    vol: Optional[float]
    regime: str
    valuation: Optional[Valuation]
    position: Optional[Position]
    halted: bool = False
    cooldown_until: float = 0.0
    risk_event_since_intent: bool = False


def target_weight(cfg: Config, asset: str, vol: Optional[float], regime: str,
                  conviction: float) -> float:
    """Fraction of equity this asset should hold at this conviction."""
    s = cfg.sizing
    v = s.missing_vol if (vol is None or vol <= 0) else max(vol, s.min_vol)
    w = (s.target_risk_pct / 100.0) / v * max(0.0, min(1.0, conviction))
    if regime != "TRENDING":  # CHOPPY or UNKNOWN: trend edge isn't there, size down
        w *= s.choppy_regime_factor
    return min(w, s.max_position_pct / 100.0, cfg.cap_pct(asset) / 100.0)


def evaluate(intent: Intent, ctx: GateContext) -> Verdict:
    cfg, t = ctx.cfg, ctx.cfg.trading
    if intent.asset not in cfg.universe:
        return Verdict.reject(intent, "not_in_universe")
    q = ctx.quote
    if q is None or not q.valid():
        return Verdict.reject(intent, "no_quote")
    if q.age(ctx.now) > t.max_data_age_sec:
        return Verdict.reject(intent, "stale_quote", f"age {q.age(ctx.now):.0f}s")
    if intent.origin == "council" and ctx.now - intent.created_ts > t.decision_ttl_sec:
        return Verdict.reject(intent, "decision_expired",
                              f"{ctx.now - intent.created_ts:.0f}s > {t.decision_ttl_sec:.0f}s")
    if intent.side == "SELL":
        return _sell(intent, ctx, q)
    if intent.side == "BUY":
        return _buy(intent, ctx, q)
    return Verdict.reject(intent, "bad_side")


def _sell(intent: Intent, ctx: GateContext, q: Quote) -> Verdict:
    t = ctx.cfg.trading
    pos = ctx.position
    if pos is None or pos.qty <= 0:
        return Verdict.reject(intent, "no_position")
    if not 0 < intent.fraction <= 1:
        return Verdict.reject(intent, "bad_fraction")
    price = q.bid * (1 - t.slippage_pct / 100)
    sell_qty = qty(pos.qty * Decimal(str(intent.fraction)))
    notes = []
    # Leaving a sub-minimum remainder just creates an untradeable stub: sell it all.
    remainder = float(pos.qty - sell_qty) * price
    if 0 < remainder < t.min_trade_usd:
        sell_qty = pos.qty
        notes.append("rounded_to_full_exit")
    notional = float(sell_qty) * price
    full_exit = sell_qty == pos.qty
    if notional < t.min_trade_usd and not full_exit:
        return Verdict.reject(intent, "below_min_trade", f"${notional:,.2f}")
    return Verdict(True, intent.asset, "SELL", "approved", quantity=sell_qty, price=price,
                   notional=notional, quote_source=q.source, notes=tuple(notes))


def _buy(intent: Intent, ctx: GateContext, q: Quote) -> Verdict:
    cfg, t = ctx.cfg, ctx.cfg.trading
    if ctx.halted:
        return Verdict.reject(intent, "halted", "drawdown breaker is latched")
    if ctx.now < ctx.cooldown_until:
        return Verdict.reject(intent, "cooldown", f"{ctx.cooldown_until - ctx.now:.0f}s left")
    if ctx.risk_event_since_intent:
        return Verdict.reject(intent, "risk_event_during_decision")
    if not 0 < intent.conviction <= 1:
        return Verdict.reject(intent, "bad_conviction")
    val = ctx.valuation
    if val is None or val.equity <= 0:
        return Verdict.reject(intent, "book_unmarkable")

    w = target_weight(cfg, intent.asset, ctx.vol, ctx.regime, intent.conviction)
    current = val.values.get(intent.asset, 0.0)
    want = w * val.equity - current
    notes = [f"target {w * 100:.2f}% (${w * val.equity:,.2f}), holding ${current:,.2f}"]
    if want < t.min_trade_usd:
        return Verdict.reject(intent, "at_or_above_target", *notes)

    binding = "target"
    if t.max_trade_usd > 0 and want > t.max_trade_usd:
        want, binding = t.max_trade_usd, "max_trade_usd"
    gross_room = t.max_gross_pct / 100 * val.equity - val.invested
    if want > gross_room:
        want, binding = gross_room, "max_gross_pct"
    fee = t.fee_pct / 100
    cash_room = (val.cash - t.cash_reserve_pct / 100 * val.equity) / (1 + fee)
    if want > cash_room:
        want, binding = cash_room, "cash"
    if want < t.min_trade_usd:
        return Verdict.reject(intent, f"sized_below_min_by_{binding}", *notes,
                              f"${max(want, 0):,.2f} < ${t.min_trade_usd:,.2f}")

    price = q.ask * (1 + t.slippage_pct / 100)
    buy_qty = qty(Decimal(str(want)) / Decimal(str(price)))
    notes.append(f"binding: {binding}")
    return Verdict(True, intent.asset, "BUY", "approved", quantity=buy_qty, price=price,
                   notional=float(buy_qty) * price, quote_source=q.source, notes=tuple(notes))
