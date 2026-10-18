"""
Protective monitor: trailing stops, concentration trims, drawdown breaker. Pure function.

What it does: given the book, fresh quotes, the persisted risk state and the equity
history, returns (intents to submit, new risk state, high-water updates, events).
Why pure: every rule is unit-testable without clocks, network or a database, and the
engine applies the outputs in one place.

Drawdown deadlock fix (v1 bug): the breaker only blocks BUYs. It resumes when drawdown
recovers under resume_pct, or after halt_max_hours with the peak re-baselined to current
equity, because a book de-risked into cash may never climb back on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Tuple

from ottr.config import Config
from ottr.ledger.book import Position, Valuation
from ottr.market.types import Quote
from ottr.risk.types import Intent

DAY = 86400.0


@dataclass
class RiskState:
    halted: bool = False
    halted_since: Optional[float] = None
    peak_equity: Optional[float] = None
    cooldowns: Dict[str, float] = field(default_factory=dict)  # asset -> until epoch

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "RiskState":
        d = d or {}
        return cls(halted=bool(d.get("halted", False)), halted_since=d.get("halted_since"),
                   peak_equity=d.get("peak_equity"), cooldowns=dict(d.get("cooldowns", {})))

    def to_dict(self) -> dict:
        return {"halted": self.halted, "halted_since": self.halted_since,
                "peak_equity": self.peak_equity, "cooldowns": self.cooldowns}


@dataclass
class MonitorResult:
    intents: List[Intent]
    state: RiskState
    high_water: Dict[str, float]
    events: List[Tuple[str, dict]]


def check(cfg: Config, now: float, positions: Mapping[str, Position],
          valuation: Optional[Valuation], quotes: Mapping[str, Quote],
          state: RiskState, equity_points: int) -> MonitorResult:
    r = cfg.risk
    st = RiskState.from_dict(state.to_dict())
    st.cooldowns = {a: u for a, u in st.cooldowns.items() if u > now}
    intents: List[Intent] = []
    events: List[Tuple[str, dict]] = []
    hwm: Dict[str, float] = {}
    if not r.enabled:
        return MonitorResult(intents, st, hwm, events)

    exiting = set()
    for a, p in positions.items():
        q = quotes.get(a)
        if q is None or q.age(now) > cfg.trading.max_data_age_sec:
            events.append(("risk_blind", {"asset": a, "why": "no fresh quote"}))
            continue
        # The 24h high only counts once we've held for 24h, else it predates our entry.
        cand = q.bid
        if q.high_24h and now - p.opened_ts >= DAY:
            cand = max(cand, q.high_24h)
        new_hwm = max(p.high_water, cand)
        if new_hwm > p.high_water:
            hwm[a] = new_hwm
        stop_px = new_hwm * (1 - r.trailing_stop_pct / 100)
        if q.bid <= stop_px:
            exiting.add(a)
            intents.append(Intent(
                origin="stop", asset=a, side="SELL", fraction=1.0, ref="trailing_stop",
                idem_key=f"stop:{a}:{p.opened_ts:.0f}", created_ts=now,
                reason=f"bid {q.bid:.6g} <= stop {stop_px:.6g} ({r.trailing_stop_pct:.0f}% off high {new_hwm:.6g})"))

    if valuation is not None:
        for a, w in valuation.weights.items():
            cap = cfg.cap_pct(a) / 100
            if a in exiting or st.cooldowns.get(a, 0) > now:
                continue
            if w > cap + r.trim_band_pct / 100:
                intents.append(Intent(
                    origin="trim", asset=a, side="SELL", fraction=round((w - cap) / w, 6),
                    ref="concentration_cap", idem_key=f"trim:{a}:{int(now // r.cooldown_sec)}",
                    created_ts=now, reason=f"weight {w * 100:.1f}% > cap {cap * 100:.0f}% + band"))

        eq = valuation.equity
        st.peak_equity = max(st.peak_equity or eq, eq)
        dd = 1 - eq / st.peak_equity if st.peak_equity > 0 else 0.0
        if not st.halted and equity_points >= r.min_equity_points and dd >= r.drawdown_halt_pct / 100:
            st.halted, st.halted_since = True, now
            events.append(("halt", {"drawdown": dd, "equity": eq, "peak": st.peak_equity}))
            if r.drawdown_derisk:
                for a in positions:
                    if a not in exiting:
                        intents.append(Intent(
                            origin="derisk", asset=a, side="SELL", fraction=r.drawdown_derisk_pct / 100,
                            ref="drawdown", idem_key=f"derisk:{a}:{now:.0f}", created_ts=now,
                            reason=f"drawdown {dd * 100:.1f}% tripped the breaker"))
        elif st.halted:
            if dd <= r.drawdown_resume_pct / 100:
                st.halted, st.halted_since = False, None
                events.append(("resume", {"why": "recovered", "drawdown": dd}))
            elif st.halted_since and now - st.halted_since >= r.halt_max_hours * 3600:
                st.halted, st.halted_since, st.peak_equity = False, None, eq
                events.append(("resume", {"why": "timeout_rebaseline", "new_peak": eq}))
    return MonitorResult(intents, st, hwm, events)
