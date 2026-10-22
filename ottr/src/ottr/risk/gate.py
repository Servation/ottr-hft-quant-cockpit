"""
Pre-trade gate: turns an Intent into an exact order Verdict for a US equity. Pure.

What it does, for every order: a council decision must still be fresh, the market must be
in its regular session, and the quote must have passed the data layer's sanity check.
A SELL then needs only a position: limits exist to stop risk going up, never down, and a
symbol that left the universe can always be sold. A BUY must also:
- be for an active member (not a sunset one being sold off);
- not be in the session's last minutes, during the drawdown halt, during a cooldown,
  or after a risk action on the asset during the meeting;
- for a single stock, have a known earnings date more than the blackout away;
- not have a corporate action pending that blocks BUYs, or unknown corporate actions;
- fit its target (vol-target weight x conviction x regime, capped by class and symbol),
  the per-trade ceiling, gross exposure (the cash ETF doesn't count), the satellite
  sleeve, and buying power less the cash reserve.
A BUY short of buying power while the cash ETF is held asks to be funded first
("needs_cash"); the executor sells that much of the cash ETF and then asks again.
Quantities are fractional to 1e-9 only where the broker allows it, else whole shares (a
partial SELL rounds to the nearest share, at least one), and the limit price is the touch
rounded to a legal tick against us. A SELL that would leave a stub under the minimum trade
sells it all; a council SELL under the minimum is refused, a protective one never is.

Why pure: every rule is a plain function of the context the desk gathers, so each one is
unit-tested without a clock, a network or a database, and the gate can't change anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from ottr.broker.base import whole_or_fractional
from ottr.ledger.book import Position, Valuation
from ottr.market.calendar import MarketTime
from ottr.market.types import Quote
from ottr.risk.types import CODE_SIZED, PROTECTIVE, Intent, Verdict
from ottr.universe import Member

EARNINGS_BLACKOUT_SESSIONS = 5
STOCK = "stock"


@dataclass(frozen=True)
class GateContext:
    cfg: object                          # Config
    now: float
    market: MarketTime
    member: Optional[Member]             # None: not in the universe
    quote: Optional[Quote]               # only set when the quote passed the sanity check
    quote_problem: str                   # why there is no usable quote ("" when there is one)
    limit_price: Optional[Decimal]       # where the broker would fill now, on a tick
    vol: Optional[float]
    regime: str
    valuation: Optional[Valuation]
    position: Optional[Position]
    buying_power: Decimal
    cash_etf_value: float = 0.0          # held in the sweep ETF: can fund a BUY
    satellites_value: float = 0.0        # stocks held, sunset ones included
    earnings_sessions: Optional[int] = None   # sessions to the next report; None = unknown
    corp_action: Optional[str] = None         # what blocks a BUY (a pending action, or unknown)
    halted: bool = False
    cooldown_until: float = 0.0
    risk_event_since_intent: bool = False


def target_weight(cfg, symbol: str, asset_class: str, vol: Optional[float], regime: str,
                  conviction: float) -> float:
    """Fraction of equity this symbol should hold at this conviction (US sizing)."""
    s = cfg.sizing
    floor = cfg.vol_floor(asset_class)
    v = s.missing_vol if (vol is None or vol <= 0) else max(vol, floor)
    w = (s.target_risk_pct / 100.0) / v * max(0.0, min(1.0, conviction))
    if regime != "TRENDING":  # CHOPPY or UNKNOWN: the trend edge isn't there, size down
        w *= s.choppy_regime_factor
    cap = cfg.cap_pct(symbol, asset_class)
    return min(w, s.max_position_pct / 100.0, (cap if cap is not None else 100.0) / 100.0)


def evaluate(intent: Intent, ctx: GateContext) -> Verdict:
    t = ctx.cfg.trading
    if intent.usd and intent.origin not in CODE_SIZED:
        return Verdict.reject(intent, "sized_by_caller", "only code-originated orders carry a size")
    if intent.origin == "council" and ctx.now - intent.created_ts > t.decision_ttl_sec:
        return Verdict.reject(intent, "decision_expired",
                              f"{ctx.now - intent.created_ts:.0f}s > {t.decision_ttl_sec:.0f}s")
    if not ctx.market.is_open:
        return Verdict.reject(intent, "market_closed", ctx.market.state.value)
    if ctx.quote is None or ctx.limit_price is None:
        return Verdict.reject(intent, "no_quote", ctx.quote_problem or "no quote")
    if intent.side == "SELL":
        return _sell(intent, ctx)
    if intent.side == "BUY":
        return _buy(intent, ctx)
    return Verdict.reject(intent, "bad_side")


def _sell(intent: Intent, ctx: GateContext) -> Verdict:
    t = ctx.cfg.trading
    pos = ctx.position
    if pos is None or pos.qty <= 0:
        return Verdict.reject(intent, "no_position")
    if not 0 < intent.fraction <= 1:
        return Verdict.reject(intent, "bad_fraction")
    price = ctx.limit_price
    fractionable = ctx.member.fractionable if ctx.member else True
    want = pos.qty * Decimal(str(intent.fraction))
    if fractionable:
        q = whole_or_fractional(want, True)
    else:
        # Whole shares: the nearest whole number, and at least one. Flooring would turn a
        # 25% sale of 2 shares into 0 shares, which the stub rule below makes a full exit.
        q = min(pos.qty, max(Decimal(1), want.to_integral_value(rounding=ROUND_HALF_UP)))
    notes = []
    # Leaving a sub-minimum remainder just creates an untradeable stub: sell it all.
    remainder = (pos.qty - q) * price
    if 0 < remainder < Decimal(str(t.min_trade_usd)) or q == 0:
        q = pos.qty
        notes.append("rounded_to_full_exit")
    notional = q * price
    # The minimum keeps the council from churning tiny trades. A protective sale (stop,
    # trim, de-risk) is never refused for being small: refusing it leaves the risk on.
    if notional < Decimal(str(t.min_trade_usd)) and q != pos.qty and intent.origin not in PROTECTIVE:
        return Verdict.reject(intent, "below_min_trade", f"${notional:,.2f}")
    return Verdict(True, intent.asset, "SELL", "approved", quantity=q, price=float(price),
                   notional=float(notional), quote_source=ctx.quote.source, notes=tuple(notes))


def _buy(intent: Intent, ctx: GateContext) -> Verdict:
    cfg, t = ctx.cfg, ctx.cfg.trading
    m = ctx.member
    if m is None:
        return Verdict.reject(intent, "not_in_universe")
    if not m.buyable:
        return Verdict.reject(intent, "sunset_member", "being sold off: SELL or HOLD only")
    if ctx.halted:
        return Verdict.reject(intent, "halted", "drawdown breaker is latched")
    if ctx.now < ctx.cooldown_until:
        return Verdict.reject(intent, "cooldown", f"{ctx.cooldown_until - ctx.now:.0f}s left")
    if ctx.risk_event_since_intent:
        return Verdict.reject(intent, "risk_event_during_decision")
    left = ctx.market.seconds_to_close()
    if left is not None and left < t.no_buy_last_min * 60:
        return Verdict.reject(intent, "closing_soon", f"{left / 60:.0f} min to the close")
    if m.asset_class == STOCK:
        if ctx.earnings_sessions is None:
            return Verdict.reject(intent, "earnings_unknown", "no confirmed earnings date: no new BUY")
        if ctx.earnings_sessions <= EARNINGS_BLACKOUT_SESSIONS:
            return Verdict.reject(intent, "earnings_blackout", f"reports in {ctx.earnings_sessions} sessions")
    if ctx.corp_action:
        return Verdict.reject(intent, "corporate_action_pending", ctx.corp_action)
    val = ctx.valuation
    if val is None or val.equity <= 0:
        return Verdict.reject(intent, "book_unmarkable")

    cash_symbol = cfg.universe.cash_symbol
    notes = []
    if intent.origin in CODE_SIZED:
        if intent.asset != cash_symbol or intent.usd <= 0:
            return Verdict.reject(intent, "bad_sweep", "the sweep only buys the cash ETF")
        want, binding = intent.usd, "sweep"
    else:
        if not 0 < intent.conviction <= 1:
            return Verdict.reject(intent, "bad_conviction")
        w = target_weight(cfg, intent.asset, m.asset_class, ctx.vol, ctx.regime, intent.conviction)
        current = val.values.get(intent.asset, 0.0)
        want, binding = w * val.equity - current, "target"
        notes.append(f"target {w * 100:.2f}% (${w * val.equity:,.2f}), holding ${current:,.2f}")
        if want < t.min_trade_usd:
            return Verdict.reject(intent, "at_or_above_target", *notes)
        if t.max_trade_usd > 0 and want > t.max_trade_usd:
            want, binding = t.max_trade_usd, "max_trade_usd"
        invested = val.invested - ctx.cash_etf_value
        if intent.asset != cash_symbol and want > t.max_gross_pct / 100 * val.equity - invested:
            want, binding = t.max_gross_pct / 100 * val.equity - invested, "max_gross_pct"
        if m.asset_class == STOCK:
            room = cfg.universe.sleeve_pct / 100 * val.equity - ctx.satellites_value
            if want > room:
                want, binding = room, "sleeve"

    cash_room = float(ctx.buying_power) - t.cash_reserve_pct / 100 * val.equity
    if want > cash_room:
        shortfall = want - max(cash_room, 0.0)
        if intent.asset != cash_symbol and ctx.cash_etf_value > 0 and binding != "sweep":
            fund = min(shortfall, ctx.cash_etf_value)
            return Verdict(False, intent.asset, "BUY", "needs_cash", funding_usd=fund,
                           notes=(*notes, f"short ${shortfall:,.2f}: sell ${fund:,.2f} of {cash_symbol} first"))
        want, binding = cash_room, "buying_power"
    if want < t.min_trade_usd:
        return Verdict.reject(intent, f"sized_below_min_by_{binding}", *notes,
                              f"${max(want, 0):,.2f} < ${t.min_trade_usd:,.2f}")

    price = ctx.limit_price
    q = whole_or_fractional(Decimal(str(want)) / price, m.fractionable)
    if q <= 0:
        return Verdict.reject(intent, "below_one_share", f"${want:,.2f} < one share at {price}")
    notional = q * price
    if notional < Decimal(str(t.min_trade_usd)):
        return Verdict.reject(intent, "sized_below_min_by_whole_shares", *notes, f"${notional:,.2f}")
    notes.append(f"binding: {binding}")
    return Verdict(True, intent.asset, "BUY", "approved", quantity=q, price=float(price),
                   notional=float(notional), quote_source=ctx.quote.source, notes=tuple(notes))
