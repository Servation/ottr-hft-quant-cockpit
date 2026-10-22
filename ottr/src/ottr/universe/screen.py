"""
The screener's measurements and ranking. Pure: no network, no clock, no database.

What it does: `measure` turns one stock's daily history (split- and dividend-adjusted, so
neither looks like a crash) into a Row: the hard gates it fails, if any, the features the
score ranks, and two warning flags. `score` ranks the rows that pass every gate by a
weighted mean of percentile ranks among themselves, so units and one outlier can't
dominate.

The score (0-100), and why each part:
  momentum 30%         the 12-1 month return (the past year less the last month, which
                       tends to reverse) over its own volatility: the most durable
                       documented stock effect, and dividing by vol ranks a steady climb
                       above a violent one, the way the desk sizes.
  residual 20%         the same over returns net of SPY (beta fitted on the same window):
                       momentum that isn't just a high beta in a rising market.
  efficiency 15%       Kaufman efficiency ratio over 60 sessions: how straight the path is.
  near_high 10%        the close over its 52-week high: winners near their highs tend to
                       keep drifting up.
  sizable 10%          the risk model's own target weight at full conviction
                       (`risk/gate.target_weight`): what the desk could actually hold.
  diversification 10%  1 - |correlation with SPY| over 120 sessions.
  participation 5%     20-session median dollar volume over the 120-session one.
Two flags halve the score: "overextended" (a 5-session move over 3 sigma, or RSI(14)
over 80: chasing) and "pulling back" (more than twice the month's vol-implied move under
its 21-session high: catching a falling knife), each sigma taken from before the move. No fundamentals: there is no reliable
free source.

Hard gates (a satellite failing one is demoted at once): enough history, no missing
sessions lately (a halt, or a fresh listing), the price and liquidity floors, 60-session
volatility inside the band, and a close above the 200-session average. Entry checks (a
candidate must also pass them to be promoted, but a satellite isn't demoted for them): a
positive 12-1 momentum, no flag, and a known report date more than the earnings blackout
away. Why split them: a gate says the stock is no longer one the desk should hold; a
check only says "not now".
"""

from __future__ import annotations

import math
import statistics
from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass, replace
from datetime import date
from typing import Dict, List, Optional, Sequence, Tuple

from ottr.market import indicators as ind
from ottr.risk.gate import EARNINGS_BLACKOUT_SESSIONS, target_weight

STOCK = "stock"
MOM_LOOKBACK, MOM_SKIP = 252, 21     # 12-1 months in sessions
VOL_WINDOW = 60
ER_WINDOW = 60
CORR_WINDOW = 120
SMA_WINDOW = 200
HIGH_WINDOW = 252
RECENT_SESSIONS = 60                 # every one of these must have a bar
LIQUIDITY_WINDOW = 20
PARTICIPATION_LONG = 120
PULLBACK_WINDOW = 21
OVEREXTENDED_Z, OVEREXTENDED_RSI = 3.0, 80.0
WEIGHTS = {"momentum": 0.30, "residual": 0.20, "efficiency": 0.15, "near_high": 0.10, "sizable": 0.10,
           "diversification": 0.10, "participation": 0.05}

Daily = Tuple[date, float, float]    # (day, adjusted close, adjusted volume)


@dataclass(frozen=True)
class Row:
    symbol: str
    name: str = ""
    sessions: int = 0
    price: Optional[float] = None
    dollar_volume: Optional[float] = None     # 20-session median, dollars a day
    vol: Optional[float] = None               # 60-session, annualized
    momentum: Optional[float] = None          # 12-1 month log return / (daily vol * sqrt(sessions))
    momentum_return: Optional[float] = None   # the plain 12-1 month return, for people
    residual: Optional[float] = None
    efficiency: Optional[float] = None
    near_high: Optional[float] = None
    sizable: Optional[float] = None
    corr_spy: Optional[float] = None
    participation: Optional[float] = None
    rsi: Optional[float] = None
    regime: str = "UNKNOWN"
    earnings_sessions: Optional[int] = None
    quote_usable: Optional[float] = None      # share of sampled minutes the IEX quote was usable
    flags: Tuple[str, ...] = ()
    gate: str = ""                            # the first hard gate failed ("" = passed)
    score: Optional[float] = None
    rank: Optional[int] = None

    @property
    def passed(self) -> bool:
        return not self.gate

    def entry_problem(self) -> str:
        """Why it can't be promoted now ("" = it can), beyond the hard gates."""
        if self.momentum is None or self.momentum <= 0:
            return "no positive 12-1 month momentum"
        if self.flags:
            return " and ".join(self.flags)
        if self.earnings_sessions is None:
            return "no known report date"
        if self.earnings_sessions <= EARNINGS_BLACKOUT_SESSIONS:
            return f"reports in {self.earnings_sessions} sessions"
        return ""

    def to_dict(self) -> dict:
        return asdict(self)


def _stdev(xs: Sequence[float]) -> Optional[float]:
    return statistics.stdev(xs) if len(xs) > 2 else None


def _momentum(closes: Sequence[float]) -> Tuple[Optional[float], Optional[float]]:
    """(vol-adjusted 12-1 momentum, plain 12-1 return)."""
    if len(closes) < MOM_LOOKBACK + 1:
        return None, None
    seg = closes[-(MOM_LOOKBACK + 1):len(closes) - MOM_SKIP]
    rets = ind.log_returns(seg)
    sd = _stdev(rets)
    if not sd or seg[0] <= 0:
        return None, None
    total = math.log(seg[-1] / seg[0])
    return total / (sd * math.sqrt(len(rets))), math.exp(total) - 1


def _residual(days: Sequence[date], closes: Sequence[float], spy: Dict[date, float]) -> Optional[float]:
    """Residual momentum over the 12-1 window: the stock's daily log returns less beta x
    SPY's (beta fitted through the origin on the same window, so the residuals keep the
    stock's own drift), summed, over their stdev x sqrt(n)."""
    if len(closes) < MOM_LOOKBACK + 1:
        return None
    lo, hi = len(closes) - (MOM_LOOKBACK + 1), len(closes) - MOM_SKIP
    r, s = [], []
    for i in range(lo + 1, hi):
        d0, d1 = days[i - 1], days[i]
        if d0 in spy and d1 in spy and closes[i - 1] > 0 and closes[i] > 0 and spy[d0] > 0 and spy[d1] > 0:
            r.append(math.log(closes[i] / closes[i - 1]))
            s.append(math.log(spy[d1] / spy[d0]))
    ss = sum(x * x for x in s)
    if len(r) < MOM_LOOKBACK // 2 or ss == 0:
        return None
    beta = sum(a * b for a, b in zip(r, s)) / ss
    e = [a - beta * b for a, b in zip(r, s)]
    sd = _stdev(e)
    return sum(e) / (sd * math.sqrt(len(e))) if sd else None


def measure(cfg, symbol: str, daily: Sequence[Daily], spy: Sequence[Tuple[date, float]],
            recent_sessions: Sequence[date], earnings_sessions: Optional[int], name: str = "") -> Row:
    """One stock's Row. `daily`: its adjusted bars, oldest first. `spy`: SPY's adjusted
    closes. `recent_sessions`: the last completed sessions (each must have a bar)."""
    sc = cfg.screen
    days = [d for d, _, _ in daily]
    closes = [c for _, c, _ in daily]
    dollars = [c * v for _, c, v in daily]
    n = len(closes)
    price = closes[-1] if closes else None
    dv = statistics.median(dollars[-LIQUIDITY_WINDOW:]) if n >= LIQUIDITY_WINDOW else None
    vol = ind.annualized_vol(closes, VOL_WINDOW, ind.TRADING_DAYS) if n > VOL_WINDOW else None
    mom, mom_ret = _momentum(closes)
    er20 = ind.efficiency_ratio(closes, cfg.data.er_sessions)
    regime = ind.regime(er20, cfg.data.er_threshold)
    row = Row(
        symbol=symbol, name=name, sessions=n, price=price, dollar_volume=dv, vol=vol, momentum=mom,
        momentum_return=mom_ret, residual=_residual(days, closes, dict(spy)),
        efficiency=ind.efficiency_ratio(closes, ER_WINDOW),
        near_high=closes[-1] / max(closes[-HIGH_WINDOW:]) if n >= HIGH_WINDOW else None,
        sizable=target_weight(cfg, symbol, STOCK, vol, regime, 1.0) if vol else None,
        corr_spy=ind.aligned_return_correlation(list(zip(days, closes)), spy, CORR_WINDOW),
        participation=(dv / statistics.median(dollars[-PARTICIPATION_LONG:])
                       if dv and n >= PARTICIPATION_LONG and statistics.median(dollars[-PARTICIPATION_LONG:]) > 0
                       else None),
        rsi=ind.rsi(closes, 14), regime=regime, earnings_sessions=earnings_sessions)
    return replace(row, flags=_flags(closes, row.rsi), gate=_gate(sc, row, set(days), recent_sessions, closes))


def _flags(closes: Sequence[float], rsi: Optional[float]) -> Tuple[str, ...]:
    """Each move is judged against the volatility from BEFORE it: measured over a window
    that includes it, a sudden spike or drop raises the very yardstick it is judged by."""
    flags = []
    before5 = ind.annualized_vol(closes[:-5], VOL_WINDOW, ind.TRADING_DAYS) if len(closes) > VOL_WINDOW + 6 else None
    if (rsi is not None and rsi > OVEREXTENDED_RSI) or (
            before5 and math.log(closes[-1] / closes[-6]) / (before5 / math.sqrt(ind.TRADING_DAYS) * math.sqrt(5))
            > OVEREXTENDED_Z):
        flags.append("overextended")
    n = PULLBACK_WINDOW
    before = ind.annualized_vol(closes[:-n], VOL_WINDOW, ind.TRADING_DAYS) if len(closes) > VOL_WINDOW + n + 1 else None
    if before and 1 - closes[-1] / max(closes[-n:]) > 2 * before * math.sqrt(n / ind.TRADING_DAYS):
        flags.append("pulling back")
    return tuple(flags)


def _gate(sc, row: Row, have: set, recent_sessions: Sequence[date], closes: Sequence[float]) -> str:
    if row.sessions < sc.min_sessions:
        return f"{row.sessions} sessions of history (needs {sc.min_sessions})"
    missing = [d for d in list(recent_sessions)[-RECENT_SESSIONS:] if d not in have]
    if missing:
        return f"no bar for {len(missing)} of the last {RECENT_SESSIONS} sessions (last missing {missing[-1]})"
    if row.price is None or row.price < sc.min_price:
        return f"price ${row.price or 0:,.2f} under ${sc.min_price:,.2f}"
    if row.dollar_volume is None or row.dollar_volume < sc.min_dollar_volume:
        return f"${(row.dollar_volume or 0) / 1e6:,.0f}M a day traded, under ${sc.min_dollar_volume / 1e6:,.0f}M"
    if row.vol is None or not sc.vol_min_pct <= row.vol * 100 <= sc.vol_max_pct:
        shown = f"{row.vol * 100:.0f}%" if row.vol else "unknown"
        return f"volatility {shown} outside {sc.vol_min_pct:g}-{sc.vol_max_pct:g}%"
    sma = sum(closes[-SMA_WINDOW:]) / SMA_WINDOW
    if closes[-1] <= sma:
        return f"at or under its 200-session average ({closes[-1]:,.2f} vs {sma:,.2f})"
    return ""


def percentiles(values: Sequence[Optional[float]]) -> List[float]:
    """Each value's percentile rank in [0, 1]: ties share the average rank, and unknowns
    (None) share the bottom ranks."""
    n = len(values)
    if n == 1:
        return [0.5]
    known = sorted(v for v in values if v is not None)
    unknown = n - len(known)
    out = []
    for v in values:
        if v is None:
            out.append(((unknown - 1) / 2) / (n - 1))
            continue
        lo, hi = bisect_left(known, v), bisect_right(known, v)
        out.append((unknown + lo + (hi - lo - 1) / 2) / (n - 1))
    return out


def score(rows: Sequence[Row]) -> List[Row]:
    """The rows that pass every gate, scored and ranked best first."""
    pool = [r for r in rows if r.passed]
    if not pool:
        return []
    feats = {
        "momentum": [r.momentum for r in pool],
        "residual": [r.residual for r in pool],
        "efficiency": [r.efficiency for r in pool],
        "near_high": [r.near_high for r in pool],
        "sizable": [r.sizable for r in pool],
        "diversification": [None if r.corr_spy is None else 1 - abs(r.corr_spy) for r in pool],
        "participation": [r.participation for r in pool],
    }
    pct = {k: percentiles(v) for k, v in feats.items()}
    scored = []
    for i, r in enumerate(pool):
        s = 100 * sum(w * pct[k][i] for k, w in WEIGHTS.items())
        s *= 0.5 ** len(r.flags)
        scored.append(replace(r, score=round(s, 1)))
    scored.sort(key=lambda r: (-(r.score or 0), r.symbol))
    return [replace(r, rank=i + 1) for i, r in enumerate(scored)]
