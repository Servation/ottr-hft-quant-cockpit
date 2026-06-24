"""
Deterministic trading signals (Tier 2 / S1).

Turns the technical indicators + market context into an explicit, reproducible
signal per asset, so a decision is grounded in code rather than only an LLM reading
a paragraph — and so the strategy is *backtestable* (bot/backtest.py) and comparable
to the buy-and-hold / SMA baselines. The LLM still makes the final call, but now over
these structured inputs.

The core scorer (`signal_from_indicators`) is pure: math on an indicator dict, no I/O,
no pandas — fully unit-testable. `indicator_series_from_closes` is the backtest's
bridge from a raw price series to per-bar indicator dicts (it imports pandas-ta lazily
so the pure core stays light).

Conventions: RSI is read mean-reversion (oversold = bullish), funding and Fear & Greed
are read contrarian (crowded long / extreme greed = bearish).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

# Tunables (kept module-level so they're easy to sweep later).
_RSI_OVERSOLD = 30.0
_RSI_OVERBOUGHT = 70.0
_FUNDING_EXTREME = 0.0005          # |perp funding| above this = crowded positioning
_FNG_FEAR = 25                     # Fear & Greed <= this = extreme fear
_FNG_GREED = 75                    # Fear & Greed >= this = extreme greed
_DIRECTION_THRESHOLD = 0.34        # |net score| above this => directional, else NEUTRAL

_REQUIRED_KEYS = ("EMA_20", "EMA_50", "RSI_14", "MACD", "MACD_signal")


@dataclass
class Signal:
    direction: str                 # BULLISH | BEARISH | NEUTRAL
    score: float                   # signed net, -1..1 (negative = bearish)
    strength: float                # |score|, 0..1
    reasons: List[str] = field(default_factory=list)

    def to_summary(self) -> str:
        body = ", ".join(self.reasons) if self.reasons else "no signal"
        return f"{self.direction} (strength {self.strength:.2f}: {body})"


def signal_from_indicators(
    ind: Dict[str, float],
    funding: Optional[float] = None,
    fng: Optional[int] = None,
) -> Signal:
    """Score one asset from its indicators + optional market context.

    `ind` should carry EMA_20, EMA_50, RSI_14, MACD, MACD_signal. Each rule votes
    -1/0/+1; the net is their mean (so missing inputs simply don't dilute). `funding`
    (perp funding rate) and `fng` (Fear & Greed 0..100) add contrarian votes.
    """
    subs: List[float] = []
    reasons: List[str] = []

    ema20, ema50 = ind.get("EMA_20"), ind.get("EMA_50")
    if ema20 is not None and ema50 is not None:
        if ema20 > ema50:
            subs.append(1.0); reasons.append("EMA20>EMA50 (uptrend)")
        elif ema20 < ema50:
            subs.append(-1.0); reasons.append("EMA20<EMA50 (downtrend)")
        else:
            subs.append(0.0)

    rsi = ind.get("RSI_14")
    if rsi is not None:
        if rsi < _RSI_OVERSOLD:
            subs.append(1.0); reasons.append(f"RSI {rsi:.0f} oversold")
        elif rsi > _RSI_OVERBOUGHT:
            subs.append(-1.0); reasons.append(f"RSI {rsi:.0f} overbought")
        else:
            subs.append(0.0)

    macd, macd_sig = ind.get("MACD"), ind.get("MACD_signal")
    if macd is not None and macd_sig is not None:
        if macd > macd_sig:
            subs.append(1.0); reasons.append("MACD>signal (bullish momentum)")
        elif macd < macd_sig:
            subs.append(-1.0); reasons.append("MACD<signal (bearish momentum)")
        else:
            subs.append(0.0)

    if funding is not None:
        if funding > _FUNDING_EXTREME:
            subs.append(-1.0); reasons.append("funding crowded long (contrarian-)")
        elif funding < -_FUNDING_EXTREME:
            subs.append(1.0); reasons.append("funding crowded short (contrarian+)")

    if fng is not None:
        if fng <= _FNG_FEAR:
            subs.append(1.0); reasons.append(f"F&G {fng} extreme fear (contrarian+)")
        elif fng >= _FNG_GREED:
            subs.append(-1.0); reasons.append(f"F&G {fng} extreme greed (contrarian-)")

    if not subs:
        return Signal("NEUTRAL", 0.0, 0.0, [])

    score = sum(subs) / len(subs)
    if score >= _DIRECTION_THRESHOLD:
        direction = "BULLISH"
    elif score <= -_DIRECTION_THRESHOLD:
        direction = "BEARISH"
    else:
        direction = "NEUTRAL"
    return Signal(direction, score, abs(score), reasons)


def signals_for_assets(
    indicators: Optional[Dict[str, Dict[str, float]]],
    funding_rates: Optional[Dict[str, float]] = None,
    fng: Optional[int] = None,
) -> Dict[str, Signal]:
    """Map asset -> Signal for every asset that has indicators."""
    funding_rates = funding_rates or {}
    out: Dict[str, Signal] = {}
    for asset, ind in (indicators or {}).items():
        out[asset] = signal_from_indicators(ind, funding_rates.get(asset), fng)
    return out


def consensus_from_signals(signals: Dict[str, Signal]) -> Dict[str, str]:
    """Per-asset deterministic BUY/SELL/HOLD from the signal direction.

    A code-only baseline the LLM consensus can be compared against over time."""
    mapping = {"BULLISH": "BUY", "BEARISH": "SELL", "NEUTRAL": "HOLD"}
    return {asset: mapping[s.direction] for asset, s in signals.items()}


def format_signals(signals: Dict[str, Signal]) -> str:
    """One line per asset, for LLM context injection."""
    if not signals:
        return "No deterministic signals available."
    return "\n".join(f"- **{a}**: {s.to_summary()}" for a, s in signals.items())


def indicator_series_from_closes(closes) -> List[Optional[Dict[str, float]]]:
    """Per-bar indicator dicts over a full close series (for the backtest).

    The indicators are causal (value at bar i depends only on closes[:i+1]), so
    computing the full series once and indexing by bar introduces no look-ahead.
    Bars before enough history (<50) are None. Imports pandas-ta lazily.
    """
    import pandas as pd
    import pandas_ta as ta

    s = pd.Series([float(c) for c in closes])
    n = len(s)
    out: List[Optional[Dict[str, float]]] = [None] * n
    if n < 50:
        return out

    ema20 = ta.ema(s, length=20)
    ema50 = ta.ema(s, length=50)
    rsi = ta.rsi(s, length=14)
    macd = ta.macd(s, fast=12, slow=26, signal=9)
    macd_line = macd["MACD_12_26_9"]
    macd_sig = macd["MACDs_12_26_9"]

    for i in range(n):
        vals = (ema20.iloc[i], ema50.iloc[i], rsi.iloc[i], macd_line.iloc[i], macd_sig.iloc[i])
        if any(pd.isna(v) for v in vals):
            continue
        out[i] = {
            "EMA_20": float(vals[0]), "EMA_50": float(vals[1]), "RSI_14": float(vals[2]),
            "MACD": float(vals[3]), "MACD_signal": float(vals[4]),
        }
    return out
