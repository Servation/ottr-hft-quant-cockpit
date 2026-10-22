"""
Pure indicator math on daily closes (oldest -> newest). No I/O, no pandas.

What it does: EMA, Wilder RSI, MACD, Kaufman efficiency ratio + regime label, annualized
volatility and return correlation. Why pure Python: v1 depended on pandas-ta, which
pinned the interpreter to 3.12; these are a few lines each and trivially testable.
Every function returns None (never 0.0) when the input is too short, so callers can't
mistake "unknown" for "zero".
"""

from __future__ import annotations

import math
from typing import Hashable, List, Optional, Sequence, Tuple


def ema_series(values: Sequence[float], length: int) -> List[float]:
    """EMA seeded with the SMA of the first `length` values; empty if too short."""
    if length <= 0 or len(values) < length:
        return []
    k = 2.0 / (length + 1)
    out = [sum(values[:length]) / length]
    for v in values[length:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def ema(values: Sequence[float], length: int) -> Optional[float]:
    s = ema_series(values, length)
    return s[-1] if s else None


def rsi(closes: Sequence[float], length: int = 14) -> Optional[float]:
    """Wilder's RSI."""
    if len(closes) < length + 1:
        return None
    deltas = [b - a for a, b in zip(closes, closes[1:])]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_g = sum(gains[:length]) / length
    avg_l = sum(losses[:length]) / length
    for g, l in zip(gains[length:], losses[length:]):
        avg_g = (avg_g * (length - 1) + g) / length
        avg_l = (avg_l * (length - 1) + l) / length
    if avg_l == 0:
        return 100.0 if avg_g > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + avg_g / avg_l)


def macd(closes: Sequence[float], fast: int = 12, slow: int = 26,
         signal: int = 9) -> Optional[tuple]:
    """(macd, signal, histogram) at the last bar."""
    f, s = ema_series(closes, fast), ema_series(closes, slow)
    if not s:
        return None
    f = f[-len(s):]  # align the fast EMA to the slow EMA's start
    line = [a - b for a, b in zip(f, s)]
    sig = ema_series(line, signal)
    if not sig:
        return None
    return line[-1], sig[-1], line[-1] - sig[-1]


def efficiency_ratio(closes: Sequence[float], window: int = 20) -> Optional[float]:
    """Kaufman ER: |net move| / path length over `window` bars. ~1 trend, ~0 chop."""
    if len(closes) < window + 1:
        return None
    seg = closes[-(window + 1):]
    path = sum(abs(b - a) for a, b in zip(seg, seg[1:]))
    if path == 0:
        return None
    return abs(seg[-1] - seg[0]) / path


def regime(er: Optional[float], threshold: float = 0.30) -> str:
    if er is None:
        return "UNKNOWN"
    return "TRENDING" if er >= threshold else "CHOPPY"


def log_returns(closes: Sequence[float]) -> List[float]:
    return [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]


# Periods per year for annualizing daily volatility: crypto trades every calendar day,
# US markets about 252 sessions a year. Using 365 on stock bars would overstate vol by
# sqrt(365/252), about 20%, and shrink every position by the same factor.
CRYPTO_DAYS = 365.0
TRADING_DAYS = 252.0


def annualized_vol(closes: Sequence[float], window: int = 30,
                   periods_per_year: float = CRYPTO_DAYS) -> Optional[float]:
    """Sample stdev of daily log returns over `window` bars, * sqrt(periods_per_year)."""
    rets = log_returns(list(closes)[-(window + 1):])
    if len(rets) < 2:
        return None
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    vol = math.sqrt(var) * math.sqrt(periods_per_year)
    return vol if vol > 0 else None


def return_correlation(a: Sequence[float], b: Sequence[float],
                       window: int = 30) -> Optional[float]:
    """Pearson correlation of daily log RETURNS (price levels give spurious ~1s)."""
    ra = log_returns(list(a)[-(window + 1):])
    rb = log_returns(list(b)[-(window + 1):])
    n = min(len(ra), len(rb))
    return _pearson(ra[-n:], rb[-n:]) if n else None


def _pearson(x: Sequence[float], y: Sequence[float]) -> Optional[float]:
    n = len(x)
    if n < 3:
        return None
    mx, my = sum(x) / n, sum(y) / n
    cov = sum((p - mx) * (q - my) for p, q in zip(x, y))
    vx = sum((p - mx) ** 2 for p in x)
    vy = sum((q - my) ** 2 for q in y)
    if vx == 0 or vy == 0:
        return None
    return cov / math.sqrt(vx * vy)


def aligned_return_correlation(a: Sequence[Tuple[Hashable, float]], b: Sequence[Tuple[Hashable, float]],
                               window: int = 30) -> Optional[float]:
    """Pearson correlation of log returns over the last `window` dates BOTH series have.

    `a` and `b` are (date, close) rows. Matching by position (as `return_correlation`
    does) is only right when both series cover the same days. Stocks halt, list late and
    trade different holiday calendars (EFA vs SPY), so one missing day would pair Monday's
    move with Tuesday's. Here each return spans the same two dates in both series.
    """
    da, db = dict(a), dict(b)
    common = sorted(set(da) & set(db))[-(window + 1):]
    ra, rb = [], []
    for d0, d1 in zip(common, common[1:]):
        if da[d0] > 0 and da[d1] > 0 and db[d0] > 0 and db[d1] > 0:
            ra.append(math.log(da[d1] / da[d0]))
            rb.append(math.log(db[d1] / db[d0]))
    return _pearson(ra, rb)
