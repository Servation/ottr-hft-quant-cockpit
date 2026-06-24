"""
Pure performance metrics over an equity-value series.

Why this exists: the system could not previously answer "is this any good / does
it beat holding BTC?" because nothing computed risk-adjusted return. These are
pure functions (no I/O, no network) over lists of values / (timestamp, value)
points, so they are fully deterministic and unit-testable, and are reused by the
backtester (Phase M2) and the bridge /performance endpoint (Phase M1).

Convention: functions return ``None`` when there is insufficient data rather than
a fake ``0.0`` — a real 0.0 (e.g. a flat curve) must be distinguishable from "not
enough data yet", so callers can render "—" instead of a misleading number.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import List, Optional, Sequence, Tuple

# (unix_seconds, portfolio_value)
Point = Tuple[float, float]

_SECONDS_PER_YEAR = 365.0 * 24.0 * 3600.0
# Crypto trades every day, so a daily-sampled series annualizes with 365.
_DAILY_PERIODS_PER_YEAR = 365.0
# Standard deviations at or below this are floating-point noise on a flat curve.
_EPS = 1e-12


def _stdev(xs: Sequence[float]) -> Optional[float]:
    """Sample standard deviation (n-1). None if fewer than 2 points."""
    n = len(xs)
    if n < 2:
        return None
    mean = sum(xs) / n
    var = sum((x - mean) ** 2 for x in xs) / (n - 1)
    return math.sqrt(var)


def period_returns(values: Sequence[float]) -> List[float]:
    """Period-over-period simple returns. Skips non-positive prior values."""
    out: List[float] = []
    for i in range(1, len(values)):
        prev = values[i - 1]
        if prev and prev > 0:
            out.append(values[i] / prev - 1.0)
    return out


def total_return(values: Sequence[float]) -> Optional[float]:
    """Cumulative return from first to last value (0.10 = +10%)."""
    if len(values) < 2 or not values[0] or values[0] <= 0:
        return None
    return values[-1] / values[0] - 1.0


def max_drawdown(values: Sequence[float]) -> Optional[float]:
    """Largest peak-to-trough decline as a positive fraction (0.25 = a -25% drop)."""
    if len(values) < 2:
        return None
    peak = values[0]
    mdd = 0.0
    for v in values:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (peak - v) / peak
            if dd > mdd:
                mdd = dd
    return mdd


def annualized_volatility(
    returns: Sequence[float], periods_per_year: float = _DAILY_PERIODS_PER_YEAR
) -> Optional[float]:
    """Stdev of per-period returns scaled to a yearly figure."""
    sd = _stdev(returns)
    if sd is None:
        return None
    return sd * math.sqrt(periods_per_year)


def sharpe_ratio(
    returns: Sequence[float],
    periods_per_year: float = _DAILY_PERIODS_PER_YEAR,
    risk_free_rate: float = 0.0,
) -> Optional[float]:
    """Annualized Sharpe: mean excess return / volatility of returns."""
    if len(returns) < 2:
        return None
    rf_per = risk_free_rate / periods_per_year
    excess = [r - rf_per for r in returns]
    sd = _stdev(excess)
    # Below _EPS the dispersion is floating-point noise on a flat curve; Sharpe is
    # undefined (0/0), so report None rather than an absurd 1e17.
    if sd is None or sd <= _EPS:
        return None
    mean = sum(excess) / len(excess)
    return (mean / sd) * math.sqrt(periods_per_year)


def sortino_ratio(
    returns: Sequence[float],
    periods_per_year: float = _DAILY_PERIODS_PER_YEAR,
    risk_free_rate: float = 0.0,
) -> Optional[float]:
    """Like Sharpe but penalizes only downside (below-target) volatility."""
    if len(returns) < 2:
        return None
    rf_per = risk_free_rate / periods_per_year
    excess = [r - rf_per for r in returns]
    downside = [min(0.0, e) for e in excess]
    # Downside deviation: RMS of the negative excess returns.
    dd = math.sqrt(sum(d * d for d in downside) / len(downside))
    if dd <= _EPS:
        return None
    mean = sum(excess) / len(excess)
    return (mean / dd) * math.sqrt(periods_per_year)


def cagr(points: Sequence[Point]) -> Optional[float]:
    """Compound annual growth rate from the first/last (timestamp, value) points."""
    if len(points) < 2:
        return None
    t0, v0 = points[0]
    t1, v1 = points[-1]
    if v0 <= 0 or v1 <= 0:
        return None
    years = (t1 - t0) / _SECONDS_PER_YEAR
    if years <= 0:
        return None
    return (v1 / v0) ** (1.0 / years) - 1.0


def calmar_ratio(cagr_value: Optional[float], mdd: Optional[float]) -> Optional[float]:
    """CAGR divided by max drawdown — return per unit of worst-case pain."""
    if cagr_value is None or mdd is None or mdd == 0:
        return None
    return cagr_value / mdd


def _resample_daily(points: Sequence[Point]) -> List[float]:
    """Collapse an irregular series to one (last) value per UTC day.

    The equity curve is sampled hourly plus on trade events, so intervals are
    uneven. Resampling to daily last-values gives a regular series we can
    annualize with a fixed periods-per-year, which is the standard way to compute
    Sharpe/vol for a daily strategy.
    """
    ordered = sorted(points, key=lambda p: p[0])
    by_day: dict = {}
    for ts, value in ordered:
        day = datetime.fromtimestamp(ts, tz=timezone.utc).date()
        by_day[day] = value  # later same-day points overwrite → last wins
    return [by_day[d] for d in sorted(by_day.keys())]


def summarize(
    points: Sequence[Point],
    btc_prices: Optional[Sequence[Optional[float]]] = None,
) -> dict:
    """Compute the full metric set for a (timestamp, value) equity series.

    ``btc_prices`` (aligned 1:1 with ``points``) enables the buy-and-hold-BTC
    benchmark: ``benchmark_return`` is BTC's return over the same window and
    ``alpha`` is the strategy's excess return over it. Any metric that lacks
    enough data is ``None`` (never a fake 0.0).
    """
    n = len(points)
    result = {
        "num_points": n,
        "period_days": None,
        "total_return": None,
        "cagr": None,
        "annualized_volatility": None,
        "sharpe": None,
        "sortino": None,
        "max_drawdown": None,
        "calmar": None,
        "benchmark_return": None,
        "alpha": None,
        "insufficient_data": n < 2,
    }
    if n < 2:
        return result

    values = [v for _, v in points]
    result["period_days"] = (points[-1][0] - points[0][0]) / 86400.0
    result["total_return"] = total_return(values)
    result["max_drawdown"] = max_drawdown(values)
    result["cagr"] = cagr(points)
    result["calmar"] = calmar_ratio(result["cagr"], result["max_drawdown"])

    daily = _resample_daily(points)
    daily_rets = period_returns(daily)
    result["annualized_volatility"] = annualized_volatility(daily_rets)
    result["sharpe"] = sharpe_ratio(daily_rets)
    result["sortino"] = sortino_ratio(daily_rets)

    if btc_prices is not None:
        # Compare strategy vs BTC over the same window. Return is scale-invariant,
        # so we just need BTC's first/last price over the rows where it's known.
        pairs = [
            (v, b)
            for (_, v), b in zip(points, btc_prices)
            if b is not None and b > 0
        ]
        if len(pairs) >= 2:
            strat_ret = total_return([p[0] for p in pairs])
            bench_ret = total_return([p[1] for p in pairs])
            result["benchmark_return"] = bench_ret
            if strat_ret is not None and bench_ret is not None:
                result["alpha"] = strat_ret - bench_ret

    return result
