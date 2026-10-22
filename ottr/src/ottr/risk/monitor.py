"""
Protective monitor: trailing stops, concentration and sleeve trims, drawdown breaker. Pure.

What it does: given the book, live quotes (it runs in session only, from `open_delay_min`
after the open: a stop needs a bid to sell at, and the opening auction's first prints are
noise), the persisted risk state and the equity history, returns (intents to submit, new
risk state, high-water updates, events).

- Stops: each position carries the trailing-stop distance it was given when it opened
  (`entry_stop_pct`: 2x the move its volatility implies over 10 sessions, clamped 3%..15%),
  measured from its high-water mark. The distance never changes and the high-water mark
  only rises, so a stop is never loosened. When the bid is at or under the stop, the
  position is sold at the bid. If it closed above the stop and is under it at the first
  check of the day, it gapped through overnight: the exit is still at the bid (the stop
  price was never available), and a `gap` event says so.
- Trims: a position over its cap (class or symbol) by more than `trim_band_pct` is cut to
  the cap; the single-stock sleeve over `universe.sleeve_pct` by more than the band is cut
  back pro rata. Holdings that left the universe count as stocks.
- Breaker: a drawdown past `drawdown_halt_pct` halts BUYs and (with `drawdown_derisk`)
  sells `drawdown_derisk_pct` of each holding. The holdings to de-risk are kept in the risk
  state and their SELLs are re-issued on every check under one stable key until they
  fill, so a sale that couldn't fill (a bad quote, the close, a restart) still happens the
  next session. The executor's idempotency makes the repeats harmless.
- The cash ETF is swept cash: never stopped, trimmed, de-risked or reported blind.

Drawdown deadlock fix (v1 bug): the breaker only blocks BUYs. It resumes when drawdown
recovers under resume_pct, or after halt_max_hours with the peak re-baselined to current
equity, because a book de-risked into cash may never climb back on its own.

Why pure: every rule is unit-testable without clocks, network or a database, and the
engine applies the outputs in one place.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Tuple

from ottr.ledger.book import Position, Valuation
from ottr.market.types import Quote
from ottr.risk.types import Intent

STOCK = "stock"
TRADING_DAYS = 252.0


def entry_stop_pct(risk, vol: Optional[float]) -> float:
    """The trailing-stop distance (a fraction) for a position opening at this annualized
    volatility: `stop_vol_mult` x the move vol implies over `stop_horizon_sessions`,
    clamped. Why that horizon: the desk holds for days to weeks, so a stop inside the
    ordinary ten-session wobble would sell on noise, and one far outside it would let a
    real break run. Unknown vol gets `stop_unknown_vol_pct`, never a guess."""
    if vol is None or vol <= 0 or not math.isfinite(vol):
        return risk.stop_unknown_vol_pct / 100
    move = vol * math.sqrt(risk.stop_horizon_sessions / TRADING_DAYS)
    return min(max(risk.stop_vol_mult * move, risk.stop_min_pct / 100), risk.stop_max_pct / 100)


@dataclass
class RiskState:
    halted: bool = False
    halted_since: Optional[float] = None
    peak_equity: Optional[float] = None
    cooldowns: Dict[str, float] = field(default_factory=dict)  # asset -> until epoch
    derisk: List[str] = field(default_factory=list)              # holdings the breaker is selling down

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "RiskState":
        d = d or {}
        return cls(halted=bool(d.get("halted", False)), halted_since=d.get("halted_since"),
                   peak_equity=d.get("peak_equity"), cooldowns=dict(d.get("cooldowns", {})),
                   derisk=list(d.get("derisk", [])))

    def to_dict(self) -> dict:
        return {"halted": self.halted, "halted_since": self.halted_since,
                "peak_equity": self.peak_equity, "cooldowns": self.cooldowns, "derisk": self.derisk}


@dataclass
class MonitorResult:
    intents: List[Intent]
    state: RiskState
    high_water: Dict[str, float]
    events: List[Tuple[str, dict]]

    def summary(self) -> str:
        """One line for the risk heartbeat: what this check did."""
        blind = sorted(d["asset"] for k, d in self.events if k == "risk_blind")
        parts = [f"{i.origin} {i.asset}" for i in self.intents]
        if blind:
            parts.append(f"blind on {', '.join(blind)}")
        if self.state.halted:
            parts.append("breaker halted")
        return "; ".join(parts) or "all clear"


def cap_for(cfg, symbol: str, classes: Mapping[str, str]) -> Optional[float]:
    """The position cap (% of equity) for a holding; a holding that left the universe is
    capped as a single stock, the tightest class."""
    return cfg.cap_pct(symbol, classes.get(symbol, STOCK))


def check(cfg, now: float, positions: Mapping[str, Position], valuation: Optional[Valuation],
          quotes: Mapping[str, Quote], state: RiskState, equity_points: int,
          classes: Mapping[str, str], prev_close: Optional[Mapping[str, float]] = None) -> MonitorResult:
    """`quotes`: live quotes that passed the sanity check. `classes`: member -> asset class.
    `prev_close`: each holding's last official close (to tell a gap from an intraday fall)."""
    r = cfg.risk
    prev_close = prev_close or {}
    st = RiskState.from_dict(state.to_dict())
    st.cooldowns = {a: u for a, u in st.cooldowns.items() if u > now}
    intents: List[Intent] = []
    events: List[Tuple[str, dict]] = []
    hwm: Dict[str, float] = {}
    if not r.enabled:
        return MonitorResult(intents, st, hwm, events)

    cash_symbol = cfg.universe.cash_symbol
    exiting = set()
    for a, p in positions.items():
        if a == cash_symbol:
            continue
        q = quotes.get(a)
        if q is None or q.age(now) > cfg.data.max_quote_age_sec:
            events.append(("risk_blind", {"asset": a, "why": "no live quote"}))
            continue
        new_hwm = max(p.high_water, q.bid)
        if new_hwm > p.high_water:
            hwm[a] = new_hwm
        pct = p.stop_pct if p.stop_pct else r.stop_unknown_vol_pct / 100
        stop_px = new_hwm * (1 - pct)
        if q.bid > stop_px:
            continue
        exiting.add(a)
        closed = prev_close.get(a)
        if closed is not None and closed > stop_px:
            why = (f"gapped through its stop: bid {q.bid:.2f} vs stop {stop_px:.2f} ({pct * 100:.1f}% off "
                   f"high {new_hwm:.2f}); last close {closed:.2f}. Exiting at the bid")
            events.append(("gap", {"asset": a, "bid": q.bid, "stop": stop_px, "close": closed}))
        else:
            why = f"bid {q.bid:.2f} <= stop {stop_px:.2f} ({pct * 100:.1f}% off high {new_hwm:.2f})"
        intents.append(Intent(origin="stop", asset=a, side="SELL", fraction=1.0, ref="trailing_stop",
                              idem_key=f"stop:{a}:{p.opened_ts:.0f}", created_ts=now, reason=why))

    if valuation is None:
        return MonitorResult(intents, st, hwm, events)

    band = r.trim_band_pct / 100
    bucket = int(now // r.cooldown_sec)
    trimmed = set()
    for a, w in valuation.weights.items():
        cap = cap_for(cfg, a, classes)
        if cap is None or a in exiting or st.cooldowns.get(a, 0) > now:
            continue
        cap /= 100
        if w > cap + band:
            trimmed.add(a)
            intents.append(Intent(
                origin="trim", asset=a, side="SELL", fraction=round((w - cap) / w, 6),
                ref="concentration_cap", idem_key=f"trim:{a}:{bucket}", created_ts=now,
                reason=f"weight {w * 100:.1f}% > cap {cap * 100:.0f}% + band"))

    stocks = {a: w for a, w in valuation.weights.items() if a != cash_symbol and classes.get(a, STOCK) == STOCK}
    sleeve, limit = sum(stocks.values()), cfg.universe.sleeve_pct / 100
    if sleeve > limit + band:
        cut = (sleeve - limit) / sleeve
        for a in sorted(stocks):
            if a in exiting or a in trimmed or st.cooldowns.get(a, 0) > now:
                continue
            intents.append(Intent(
                origin="trim", asset=a, side="SELL", fraction=round(cut, 6), ref="sleeve",
                idem_key=f"sleeve:{a}:{bucket}", created_ts=now,
                reason=f"single stocks {sleeve * 100:.1f}% of equity > sleeve {limit * 100:.0f}% + band"))

    eq = valuation.equity
    st.peak_equity = max(st.peak_equity or eq, eq)
    dd = 1 - eq / st.peak_equity if st.peak_equity > 0 else 0.0
    if not st.halted and equity_points >= r.min_equity_points and dd >= r.drawdown_halt_pct / 100:
        st.halted, st.halted_since = True, now
        st.derisk = sorted(a for a in positions if a != cash_symbol and a not in exiting) if r.drawdown_derisk else []
        events.append(("halt", {"drawdown": dd, "equity": eq, "peak": st.peak_equity, "derisk": st.derisk}))
    elif st.halted:
        if dd <= r.drawdown_resume_pct / 100:
            st.halted, st.halted_since, st.derisk = False, None, []
            events.append(("resume", {"why": "recovered", "drawdown": dd}))
        elif st.halted_since and now - st.halted_since >= r.halt_max_hours * 3600:
            st.halted, st.halted_since, st.peak_equity, st.derisk = False, None, eq, []
            events.append(("resume", {"why": "timeout_rebaseline", "new_peak": eq}))
    if st.halted:
        # Re-issued on every check under one key per halt: once it has filled the executor
        # answers "duplicate", and until then a deferred or lost sale is tried again.
        for a in st.derisk:
            if a in positions and a not in exiting:
                intents.append(Intent(
                    origin="derisk", asset=a, side="SELL", fraction=r.drawdown_derisk_pct / 100,
                    ref="drawdown", idem_key=f"derisk:{a}:{st.halted_since:.0f}", created_ts=now,
                    reason=f"drawdown {dd * 100:.1f}% tripped the breaker"))
    return MonitorResult(intents, st, hwm, events)
