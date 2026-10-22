"""
The council's view of the US market: one immutable snapshot and its text packet.

What it does: `build_snapshot` turns prices (a live quote or mark per symbol), adjusted
daily closes, the session, macro readings and per-asset events into a UsSnapshot. Each
asset gets returns over 1/5/20/60 sessions, distance from its 52-week high, EMA20/50,
RSI14, MACD, the efficiency-ratio regime, 60-session vol and its correlation with SPY,
plus flags for anything unknown or unusual. `packet()` renders it in New York time, with
the session, a macro block (FRED) and breadth (sector ETFs above their 50-day average,
equal-weight vs cap-weight S&P).

Why its own module, and pure: the models judge only what this text says, so it must be
deterministic, testable without a network, and explicit about what each number is (live,
delayed or a close; sessions, not calendar days) and about what is unknown ("n/a").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from ottr.events.view import MacroReading
from ottr.market import indicators as ind
from ottr.market.calendar import ET, MarketTime, SessionState
from ottr.market.types import BASIS_CLOSE, BASIS_DELAYED, Quote

MIN_HISTORY = 60          # closes before EMA50/MACD/vol are trusted
YEAR = 252
RETURN_WINDOWS = (1, 5, 20, 60)
BENCH = "SPY"


@dataclass(frozen=True)
class UsAssetView:
    symbol: str
    asset_class: Optional[str]                 # None for a data-only reference symbol
    price: Optional[Quote]                     # live quote in session, else the mark
    returns: Dict[int, Optional[float]]        # sessions -> return
    off_high: Optional[float] = None           # price / 52-week high close - 1
    ema20: Optional[float] = None
    ema50: Optional[float] = None
    rsi14: Optional[float] = None
    macd: Optional[float] = None
    macd_signal: Optional[float] = None
    er: Optional[float] = None
    regime: str = "UNKNOWN"
    vol: Optional[float] = None
    corr_spy: Optional[float] = None
    earnings_sessions: Optional[int] = None    # single stocks only
    corp_action: Optional[str] = None          # what blocks a BUY, if anything
    flags: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Breadth:
    above_50d: Optional[int]
    sectors: int
    rsp_vs_spy_20d: Optional[float]


@dataclass(frozen=True)
class UsSnapshot:
    as_of: float
    market: MarketTime
    assets: Dict[str, UsAssetView]
    breadth: Breadth
    macro: Tuple[MacroReading, ...] = ()
    warnings: Tuple[str, ...] = field(default_factory=tuple)

    def quote(self, symbol: str) -> Optional[Quote]:
        v = self.assets.get(symbol)
        return v.price if v else None

    def packet(self) -> str:
        """The deterministic text block the models judge."""
        lines = [f"AS OF {_et(self.as_of, '%a %Y-%m-%d %H:%M')} ET. {session_line(self.market)}",
                 "Judge ONLY the numbers below. Anything you remember about market prices from "
                 "training is out of date and must not be used.",
                 "Price: the live IEX mid in session, else marked as 'delayed' (a consolidated "
                 "minute close 16+ minutes old) or 'close' (the official close of the last "
                 "session). Returns count trading sessions. vol = 60-session annualized; ER = "
                 "efficiency ratio over 20 sessions (>= 0.30 TRENDING, else CHOPPY); corrSPY = "
                 "60-session daily-return correlation with SPY. 'n/a' means unknown, not zero.",
                 ""]
        if self.macro:
            lines.append("MACRO (FRED; change over 1 week / 1 month):")
            for m in self.macro:
                if m.value is None:
                    lines.append(f"  {m.label}: n/a")
                    continue
                stale = " STALE" if m.stale else ""
                lines.append(f"  {m.label}: {m.value:g} on {m.day}{stale} "
                             f"(1w {_chg(m.change_1w)}, 1m {_chg(m.change_1m)})")
        b = self.breadth
        sectors = ("sector trend n/a" if b.above_50d is None
                   else f"{b.above_50d} of {b.sectors} sector ETFs above their 50-day average")
        lines.append(f"BREADTH: {sectors}; equal-weight S&P (RSP) vs SPY over 20 sessions: "
                     f"{_pct(b.rsp_vs_spy_20d)}")
        lines += ["", "ASSETS (class | price | returns 1d 5d 20d 60d | off 52w high | EMA20 EMA50 | RSI | "
                  "MACD vs signal | ER regime | vol | corrSPY | events):"]
        for v in self.assets.values():
            lines.append(asset_line(v))
        if self.warnings:
            lines.append("DATA WARNINGS: " + "; ".join(self.warnings))
        return "\n".join(lines)


def asset_line(v: UsAssetView) -> str:
    p = v.price
    if p is None:
        px = "n/a"
    else:
        px = f"${p.mid:,.2f}" + {BASIS_DELAYED: " delayed", BASIS_CLOSE: " close"}.get(p.basis, " live")
    rets = " ".join(_pct(v.returns.get(n)) for n in RETURN_WINDOWS)
    events = []
    if v.asset_class == "stock":
        events.append("earnings date unknown" if v.earnings_sessions is None
                      else f"earnings in {v.earnings_sessions} sessions")
    if v.corp_action:
        events.append(v.corp_action)
    vol = "n/a" if v.vol is None else f"{v.vol * 100:.0f}%"      # a level, not a change: no sign
    return (f"{v.symbol} [{v.asset_class or 'reference'}]: {px} | {rets} | {_pct(v.off_high)} | "
            f"{_num(v.ema20)} {_num(v.ema50)} | {_fix(v.rsi14, 1)} | {_num(v.macd)} vs {_num(v.macd_signal)} | "
            f"{_fix(v.er, 2)} {v.regime} | {vol} | {_fix(v.corr_spy, 2)} | "
            f"{'; '.join(events) or 'none'}" + (f" | FLAGS: {', '.join(v.flags)}" if v.flags else ""))


def session_line(mt: MarketTime) -> str:
    if mt.state is SessionState.OPEN:
        s = mt.session
        left = mt.seconds_to_close() or 0
        early = " (early close)" if s.early_close else ""
        return (f"The US market is OPEN: regular session {_et(s.open_ts, '%H:%M')}-{_et(s.close_ts, '%H:%M')} ET"
                f"{early}, closing in {int(left // 3600)}h{int(left % 3600 // 60):02d}m.")
    nxt = mt.next_open
    when = f"next open {_et(nxt.open_ts, '%a %Y-%m-%d %H:%M')} ET" if nxt else "next open beyond the calendar"
    state = {SessionState.PRE_OPEN: "not open yet today", SessionState.POST_CLOSE: "closed for the day"}.get(
        mt.state, "closed (no session today)")
    return f"The US market is {state}; {when}. Orders now would wait for the open."


def _et(ts: float, fmt: str) -> str:
    return datetime.fromtimestamp(ts, tz=ET).strftime(fmt)


def _pct(x: Optional[float], digits: int = 1) -> str:
    if x is None:
        return "n/a"
    s = f"{x * 100:+.{digits}f}%"
    return s[1:] if float(s[:-1]) == 0 else s      # "0.0%", never "-0.0%"


def _chg(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x:+.2f}"


def _num(x: Optional[float], digits: int = 4) -> str:
    """Prices and price-scaled indicators: 4 significant digits, whole dollars above 1000."""
    return "n/a" if x is None else f"{x:,.{digits}g}" if abs(x) < 1000 else f"{x:,.0f}"


def _fix(x: Optional[float], decimals: int) -> str:
    """Bounded indicators (RSI 0-100, ER 0-1, correlation -1..1): fixed decimals."""
    return "n/a" if x is None else f"{x:.{decimals}f}"


# -- building ------------------------------------------------------------------------------
def build_view(symbol: str, asset_class: Optional[str], price: Optional[Quote],
               closes: Sequence[Tuple[date, float]], spy: Sequence[Tuple[date, float]], data_cfg,
               earnings_sessions: Optional[int] = None, corp_action: Optional[str] = None) -> UsAssetView:
    """`closes` are adjusted daily closes through the last final session, oldest first."""
    cl = [c for _, c in closes]
    flags: List[str] = []
    if len(cl) < MIN_HISTORY:
        flags.append("insufficient_history")
    if price is None:
        flags.append("no_price")
    elif price.basis == BASIS_DELAYED:
        flags.append("delayed_price")
    warm = len(cl) >= MIN_HISTORY
    # The return base: N sessions before the price's own session. A close-basis price IS
    # the last close, so its 1-session return is that close against the one before.
    returns: Dict[int, Optional[float]] = {n: None for n in RETURN_WINDOWS}
    if price is not None and cl:
        series = cl if price.basis == BASIS_CLOSE else cl + [price.mid]
        for n in RETURN_WINDOWS:
            if len(series) > n and series[-1 - n] > 0:
                returns[n] = series[-1] / series[-1 - n] - 1
    year = cl[-YEAR:]
    m = ind.macd(cl) if warm else None
    er = ind.efficiency_ratio(cl, data_cfg.er_sessions)
    return UsAssetView(
        symbol=symbol, asset_class=asset_class, price=price, returns=returns,
        off_high=(price.mid / max(year) - 1) if price is not None and year else None,
        ema20=ind.ema(cl, 20) if warm else None, ema50=ind.ema(cl, 50) if warm else None,
        rsi14=ind.rsi(cl, 14) if warm else None,
        macd=m[0] if m else None, macd_signal=m[1] if m else None,
        er=er, regime=ind.regime(er, data_cfg.er_threshold),
        vol=ind.annualized_vol(cl, data_cfg.vol_sessions, ind.TRADING_DAYS) if len(cl) > data_cfg.vol_sessions else None,
        corr_spy=1.0 if symbol == BENCH and closes else ind.aligned_return_correlation(closes, spy, data_cfg.vol_sessions),
        earnings_sessions=earnings_sessions if asset_class == "stock" else None,
        corp_action=corp_action, flags=tuple(flags))


def breadth(sectors: Sequence[str], closes: Mapping[str, Sequence[Tuple[date, float]]]) -> Breadth:
    """Sectors above their 50-day EMA, out of those with enough history to say; and RSP's
    20-session return relative to SPY's (positive: the average stock is keeping up)."""
    above, known = 0, 0
    for s in sectors:
        cl = [c for _, c in closes.get(s, ())]
        e = ind.ema(cl, 50) if len(cl) >= MIN_HISTORY else None
        if e is not None:
            known += 1
            above += cl[-1] > e
    rel = None
    rsp, spy = list(closes.get("RSP", ())), list(closes.get(BENCH, ()))
    if len(rsp) > 20 and len(spy) > 20 and rsp[-1][0] == spy[-1][0] and rsp[-21][1] > 0 and spy[-21][1] > 0:
        rel = (rsp[-1][1] / rsp[-21][1]) / (spy[-1][1] / spy[-21][1]) - 1
    return Breadth(above if known else None, known, rel)


def build_snapshot(cfg, now: float, mt: MarketTime, prices: Mapping[str, Quote],
                   closes: Mapping[str, Sequence[Tuple[date, float]]], members: Mapping[str, str],
                   macro: Sequence[MacroReading] = (), events=None) -> UsSnapshot:
    """`members` = traded symbol -> asset class (core, plus satellites when there are any)."""
    spy = closes.get(BENCH, ())
    views: Dict[str, UsAssetView] = {}
    warnings: List[str] = []
    for sym, cls in members.items():
        views[sym] = build_view(sym, cls, prices.get(sym), closes.get(sym, ()), spy, cfg.data,
                                events.earnings_sessions(sym, now) if events and cls == "stock" else None,
                                events.corp_action(sym, now) if events else None)
        if prices.get(sym) is None:
            warnings.append(f"{sym} has no price")
    if mt.is_open:
        missing = [s for s, q in prices.items() if s in members and q.basis != "live"]
        if missing:
            warnings.append("no live quote (marked delayed) for " + ", ".join(sorted(missing)))
    return UsSnapshot(as_of=now, market=mt, assets=views, breadth=breadth(cfg.universe.sectors, closes),
                      macro=tuple(macro), warnings=tuple(warnings))
