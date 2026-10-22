"""
Benchmarks: what the desk's starting equity would be worth in SPY alone, or in a 60/40
SPY/IEF portfolio rebalanced at each month's first session. Total return, from day 0.

What it does: `benchmark_values` replays both from adjusted closes (dividends reinvested)
starting at the baseline session's close, then carries the last close forward by each
ETF's move since (from its current mark), so an intraday equity point is compared with an
intraday benchmark.

Why recomputed every time rather than stored: adjusted closes are rewritten whenever a
dividend goes ex, so only ratios taken within one refresh are right. Why these two: SPY is
what a stock-heavy desk must beat to be worth running, and 60/40 is the plain diversified
portfolio a multi-asset desk like this one claims to improve on.
"""

from __future__ import annotations

from datetime import date
from typing import Dict, Mapping, Optional, Sequence, Tuple

SPY, BONDS = "SPY", "IEF"
WEIGHTS_60_40 = {SPY: 0.60, BONDS: 0.40}
NAMES = ("spy", "60_40")
Series = Sequence[Tuple[date, float]]


def replay(equity0: float, day0: date, closes: Mapping[str, Series],
           weights: Mapping[str, float]) -> Optional[Tuple[float, Dict[str, float]]]:
    """(value, current weights) of `equity0` put into `weights` at day0's close and
    rebalanced back to them at each month's first session, through the last session every
    leg has. None if a leg has no close on day0."""
    legs = {s: {d: c for d, c in closes.get(s, ()) if d >= day0 and c > 0} for s in weights}
    days = sorted(set.intersection(*(set(v) for v in legs.values())))
    if not days or days[0] != day0:
        return None
    units = {s: equity0 * w / legs[s][day0] for s, w in weights.items()}
    month = (day0.year, day0.month)
    for d in days[1:]:
        if (d.year, d.month) != month and len(weights) > 1:
            month = (d.year, d.month)
            value = sum(units[s] * legs[s][d] for s in units)
            units = {s: value * w / legs[s][d] for s, w in weights.items()}
    values = {s: units[s] * legs[s][days[-1]] for s in units}
    total = sum(values.values())
    return total, {s: v / total for s, v in values.items()}


def benchmark_values(base: Optional[Mapping], closes: Mapping[str, Series],
                     moves: Mapping[str, Optional[float]]) -> Dict[str, Optional[float]]:
    """{"spy": ..., "60_40": ...}; None when unknown (no baseline, or missing history).

    `base` = {"day": ISO date of the baseline session, "equity": equity then}.
    `moves` = each ETF's return since its last adjusted close (mark / that close - 1, so a
    dividend already folded into the adjusted close isn't counted as a loss); a missing
    move leaves that benchmark unknown."""
    if not base:
        return {n: None for n in NAMES}
    day0, eq0 = date.fromisoformat(base["day"]), float(base["equity"])
    out: Dict[str, Optional[float]] = {}
    for name, weights in (("spy", {SPY: 1.0}), ("60_40", WEIGHTS_60_40)):
        r = replay(eq0, day0, closes, weights)
        if r is None or any(moves.get(s) is None for s in weights):
            out[name] = None
            continue
        value, w = r
        # Today's move on the weights held now (drifted since the last rebalance).
        out[name] = value * (1 + sum(w[s] * moves[s] for s in weights))
    return out
