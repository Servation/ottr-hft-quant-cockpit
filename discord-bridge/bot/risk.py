"""
Risk-limit policies (Tier 3 / R0) — the pure decision core.

Turns the live book (holdings + prices + portfolio value) into explicit, structured
RiskActions: which positions should be stopped out, whether portfolio drawdown should
halt new risk, and which positions exceed their concentration cap and by how much. It
DECIDES; it never executes and never touches disk. The enforcer (R1+) calls these from
the existing 60s loop and routes the actions through the sole portfolio writer.

Pure by design (template: bot/sizing.py, bot/metrics.py): deterministic, no I/O, so
every policy is unit-testable and backtestable. Missing or non-positive inputs yield
NO action (never a fabricated one) — fail safe, consistent with "never act on bad data".

The persisted breaker latch lives in bot/risk_state.py (the only risk state on disk);
the drawdown PEAK is derived from the equity curve, never stored here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class RiskAction:
    """A protective SELL the enforcer should execute (stop-loss or concentration trim).

    `sell_qty` is a coin quantity (both kinds reduce a position): a stop-loss liquidates
    the whole holding; a trim sells only the excess over the cap. `detail` carries the
    numbers behind the decision for the audit log.
    """

    kind: str                       # "STOP_LOSS" | "CONCENTRATION_TRIM"
    asset: str
    sell_qty: float
    reason: str
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DrawdownState:
    """Result of evaluating the portfolio drawdown breaker.

    `halt` is the resulting latched state (block new BUYs while True). `tripped` /
    `recovered` flag the *transitions* this evaluation caused, so the enforcer can post a
    one-time alert and (un)latch exactly once.
    """

    drawdown: float                 # current peak-to-current decline, 0.15 == -15%
    halt: bool
    tripped: bool = False
    recovered: bool = False


def _price_of(prices: Dict[str, Any], asset: str) -> float:
    """Spot price for `asset` from a price-feed dict, or 0.0 if absent/malformed."""
    entry = prices.get(asset)
    if not isinstance(entry, dict):
        return 0.0
    try:
        return float(entry.get("price", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def stop_loss_breaches(
    holdings: Dict[str, Dict[str, Any]],
    prices: Dict[str, Any],
    stop_pct: float,
    mode: str = "avg_cost",
    highs: Optional[Dict[str, float]] = None,
) -> List[RiskAction]:
    """Positions trading at or below `stop_pct`% under their stop reference.

    Emits a full-liquidation SELL per breached position. `stop_pct` is a positive percent
    (10.0 == a 10% loss). Mode "avg_cost" measures the loss from the cost basis; mode
    "trailing" measures it from the position's running high-water mark (`highs[asset]`,
    maintained by the caller), which ratchets up to lock in gains — falling back to
    avg_cost until a high is recorded, and never trailing below the cost basis. A position
    with no quantity, no cost basis, or no live price is skipped — a missing price must
    never be read as a 100% loss.
    """
    if stop_pct <= 0:
        return []
    highs = highs or {}
    threshold = -abs(stop_pct) / 100.0
    actions: List[RiskAction] = []
    for asset, h in holdings.items():
        qty = float(h.get("quantity", 0.0) or 0.0)
        avg_cost = float(h.get("avg_cost", 0.0) or 0.0)
        price = _price_of(prices, asset)
        if qty <= 0 or avg_cost <= 0 or price <= 0:
            continue
        if mode == "trailing":
            ref = max(float(highs.get(asset, 0.0) or 0.0), avg_cost)  # never below entry
            ref_label = "trailing high"
        else:
            ref = avg_cost
            ref_label = "avg cost"
        change = (price - ref) / ref
        if change <= threshold:
            actions.append(
                RiskAction(
                    kind="STOP_LOSS",
                    asset=asset,
                    sell_qty=qty,
                    reason=(
                        f"{asset} at ${price:,.2f} is {change * 100:.1f}% vs {ref_label} "
                        f"${ref:,.2f}, at/under the -{abs(stop_pct):.0f}% stop"
                    ),
                    detail={
                        "price": price,
                        "ref": ref,
                        "mode": mode,
                        "avg_cost": avg_cost,
                        "loss_pct": round(change * 100, 2),
                        "stop_pct": stop_pct,
                    },
                )
            )
    return actions


def drawdown_state(
    peak: Optional[float],
    current_value: Optional[float],
    halt_pct: float,
    resume_pct: float,
    was_halted: bool,
) -> DrawdownState:
    """Evaluate the portfolio drawdown breaker with hysteresis.

    `peak` is the equity-curve high-water mark; `current_value` the live portfolio value.
    Trips the halt when drawdown >= `halt_pct`%, and (once halted) only clears it when
    drawdown recedes to <= `resume_pct`% — the gap between the two thresholds prevents
    flapping around a single line. On missing/non-positive inputs it preserves the prior
    latch and reports no transition (bad data must not auto-resume a halt).
    """
    if peak is None or peak <= 0 or current_value is None or current_value < 0:
        return DrawdownState(drawdown=0.0, halt=was_halted)
    dd = (peak - current_value) / peak
    if dd < 0:
        dd = 0.0  # current value above the recorded peak => no drawdown
    halt_frac = abs(halt_pct) / 100.0
    resume_frac = abs(resume_pct) / 100.0
    if was_halted:
        halt = dd > resume_frac
    else:
        halt = dd >= halt_frac
    return DrawdownState(
        drawdown=dd,
        halt=halt,
        tripped=halt and not was_halted,
        recovered=was_halted and not halt,
    )


def concentration_breaches(
    holdings: Dict[str, Dict[str, Any]],
    prices: Dict[str, Any],
    total_value: float,
    default_cap_pct: float,
    per_asset_caps: Optional[Dict[str, float]] = None,
    band_pct: float = 0.0,
) -> List[RiskAction]:
    """Positions whose weight exceeds their concentration cap (plus a tolerance band).

    `total_value` is the full portfolio value (cash + holdings) the caller computes via
    portfolio.get_total_value(). Each asset's cap is its `per_asset_caps` override or
    `default_cap_pct` — the SAME resolution the buy-time gate uses, so block and trim agree
    by construction. A position is trimmed only once it exceeds `cap + band_pct`, and is
    sized back to exactly the cap (not the band) via a partial SELL. Selling to cash leaves
    total value ~unchanged, so the trim notional is simply `value - cap% * total`. Missing
    price / non-positive cap / zero total => skipped.
    """
    per_asset_caps = per_asset_caps or {}
    actions: List[RiskAction] = []
    if total_value <= 0:
        return actions
    for asset, h in holdings.items():
        qty = float(h.get("quantity", 0.0) or 0.0)
        price = _price_of(prices, asset)
        if qty <= 0 or price <= 0:
            continue
        cap = float(per_asset_caps.get(asset, default_cap_pct) or 0.0)
        if cap <= 0:
            continue
        value = qty * price
        pct = value / total_value * 100.0
        if pct > cap + band_pct:
            target_value = (cap / 100.0) * total_value
            trim_notional = value - target_value
            sell_qty = trim_notional / price
            if sell_qty <= 0:
                continue
            actions.append(
                RiskAction(
                    kind="CONCENTRATION_TRIM",
                    asset=asset,
                    sell_qty=sell_qty,
                    reason=(
                        f"{asset} is {pct:.1f}% of the book, over its {cap:.0f}% cap "
                        f"(+{band_pct:.0f}% band); trim ${trim_notional:,.2f} back to cap"
                    ),
                    detail={
                        "weight_pct": round(pct, 2),
                        "cap_pct": cap,
                        "band_pct": band_pct,
                        "trim_notional": round(trim_notional, 2),
                        "price": price,
                    },
                )
            )
    return actions
