"""
Regime- and volatility-aware position sizing (Tier 2 / S2).

Operationalizes the backtested regime edge as a sizing *guardrail*: the LLM still
proposes a trade, but a deterministic model resizes a BUY to something
regime-appropriate instead of taking the LLM's dollar figure at face value.

Two levers, both grounded in the Tier 2 findings:
  - Volatility targeting: deploy a fixed risk budget, so a more volatile asset gets
    a smaller position (size ~ risk_budget / volatility).
  - Regime scaling: trend-following only pays in a TRENDING regime (the validated
    edge), so size is shrunk hard in a CHOPPY regime.

Pure (math on numbers); the caller fetches volatility/regime from the price feed.
"""

from typing import Optional

from bot import settings

_cfg = settings.get("sizing", {})
# Risk budget per position as a % of total portfolio value (vs annualized vol).
_TARGET_RISK_PCT = float(_cfg.get("target_risk_pct", 2.0))
# Multiplier applied to the size in a CHOPPY regime (trend signals unreliable).
_CHOPPY_FACTOR = float(_cfg.get("choppy_regime_factor", 0.25))
# A single BUY never exceeds this % of total value (caps the low-vol blow-up).
_MAX_POSITION_PCT = float(_cfg.get("max_position_pct", 50.0))
# Floor on volatility so a near-zero vol doesn't produce an enormous size.
_MIN_VOL = 0.05


def max_buy_notional(
    total_value: float,
    volatility: Optional[float],
    regime: Optional[str],
    conviction: Optional[float] = None,
) -> float:
    """Max USD to deploy on a BUY, vol-targeted and regime-scaled.

    `volatility` is annualized (e.g. 0.8 = 80%); `regime` is "TRENDING"/"CHOPPY"/
    None; `conviction` (0..1) optionally scales the size. Missing inputs are handled
    conservatively. Returns 0.0 for a non-positive portfolio.
    """
    if total_value <= 0:
        return 0.0

    vol = volatility if (volatility and volatility > _MIN_VOL) else _MIN_VOL
    size = (_TARGET_RISK_PCT / 100.0) * total_value / vol

    if regime == "CHOPPY":
        size *= _CHOPPY_FACTOR

    if conviction is not None:
        size *= max(0.0, min(1.0, conviction))

    # Hard per-trade ceiling as a fraction of the book.
    size = min(size, (_MAX_POSITION_PCT / 100.0) * total_value)
    return max(0.0, size)
