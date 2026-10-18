"""
Council: independent analysts, a code-computed tally, and a chair that decides.

What it does: sends one context (market packet + book) to every analyst in parallel,
tallies their stances in code, asks the chair to decide on anonymized reports, then
applies code-side rules to the decision (no BUY without analyst support, no SELL of an
asset not held). Returns a record; it never trades. Why: models judge, code counts and
enforces, and the record keeps every raw input/output for audit and the scorecard.

While it runs it can report progress to an `observe(kind, data)` callback (an analyst
done or dropped out, the chair deliberating or decided). The callback is synchronous and
its errors are swallowed, so whoever watches (the live Discord message, the API) can
never slow down or fail a meeting, and the council knows nothing about who is watching.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import Counter
from typing import Any, Callable, Dict, List, Optional, Set

from ottr.council.models import ModelAdapter, ModelError, ask_json
from ottr.council.schemas import ANALYST_SHAPE, CHAIR_SHAPE, analyst_validator, chair_validator

logger = logging.getLogger(__name__)

# observe(kind, data). Kinds: analysts_started {analysts}, analyst_done {name, report,
# latency}, analyst_dropped {name, error, latency}, chair_started {reports},
# chair_done {decision, trades, dropped, latency}, chair_failed {error, latency}.
Observer = Callable[[str, Dict[str, Any]], None]


def new_meeting_id() -> str:
    return f"m-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-{uuid.uuid4().hex[:6]}"

LENSES = {
    "claude": "Your lens: risk and regime. Weigh drawdown risk, trending vs choppy regimes, "
              "concentration and correlation, and what could go wrong.",
    "gpt": "Your lens: quant and technical. Weigh the indicators, momentum and volatility, "
           "and whether the numbers actually support an entry or exit.",
    "gemini": "Your lens: sentiment and contrarian. Weigh positioning, fear and greed, and "
              "where the crowd may be wrong.",
}


def analyst_prompt(context: str, lens: str, universe: List[str]) -> str:
    return (
        "You are one analyst on a crypto paper-trading desk. Other analysts work "
        f"independently; you will not see their views.\n{lens}\n\n"
        f"Give exactly one stance for each asset: {', '.join(universe)}. HOLD is a valid "
        "answer; do not force trades. The desk is long-only spot: SELL means reduce or exit "
        "a position it HOLDS; for an asset you would merely avoid, say HOLD. Position sizes "
        "are set by the desk's risk model from your conviction, so state conviction honestly "
        "(0.3 weak, 0.6 solid, 0.9 rare and strong).\n\n"
        f"=== CONTEXT ===\n{context}\n=== END CONTEXT ===\n\n"
        f"Reply with ONLY a JSON object of this shape (no prose, no code fences):\n{ANALYST_SHAPE}"
    )


def chair_prompt(context: str, reports: Dict[str, Dict[str, Any]], tally: Dict[str, Any],
                 universe: List[str], max_trades: int) -> str:
    lines = []
    for label, rep in reports.items():
        lines.append(f"--- Analyst {label} ---\nView: {rep['overall_view']}")
        for a in rep["assets"]:
            lines.append(f"{a['asset']}: {a['stance']} ({a['conviction']:.2f}) {a['thesis']} "
                         f"Risk: {a['key_risk']}")
    return (
        "You chair a crypto paper-trading desk. Decide what, if anything, to trade. You are "
        "not bound by the majority; if you overrule the analysts, say why in dissent_notes. "
        "An empty trades list is a good answer when the case is weak.\n"
        "Rules enforced by code (violations are dropped): at most "
        f"{max_trades} trades, one per asset; BUY only an asset at least one analyst rated "
        "BUY; SELL only an asset in HOLDINGS. Do NOT give dollar amounts: BUY takes a "
        "conviction (the risk model sizes it and only tops up to its target), SELL takes "
        "fraction_of_position (1.0 = exit).\n\n"
        f"=== CONTEXT ===\n{context}\n=== END CONTEXT ===\n\n"
        "=== ANALYST REPORTS ===\n" + "\n".join(lines) + "\n=== END REPORTS ===\n\n"
        f"=== TALLY (computed by code) ===\n{format_tally(tally)}\n=== END TALLY ===\n\n"
        f"Reply with ONLY a JSON object of this shape (no prose, no code fences):\n{CHAIR_SHAPE}"
    )


def compute_tally(reports: Dict[str, Dict[str, Any]], universe: List[str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for asset in universe:
        votes = [(a["stance"], a["conviction"]) for r in reports.values()
                 for a in r["assets"] if a["asset"] == asset]
        counts = Counter(s for s, _ in votes)
        out[asset] = {"counts": dict(counts),
                      "avg_conviction": round(sum(c for _, c in votes) / len(votes), 2) if votes else 0.0,
                      "split": len(counts) > 1,
                      "unanimous": len(counts) == 1 and len(votes) == len(reports)}
    return {"voters": len(reports), "assets": out}


def format_tally(t: Dict[str, Any]) -> str:
    rows = [f"Analysts responding: {t['voters']}"]
    for asset, a in t["assets"].items():
        c = a["counts"]
        rows.append(f"{asset}: BUY {c.get('BUY', 0)} / SELL {c.get('SELL', 0)} / HOLD {c.get('HOLD', 0)} "
                    f"(avg conviction {a['avg_conviction']:.2f}{', SPLIT' if a['split'] else ''}"
                    f"{', unanimous' if a['unanimous'] else ''})")
    return "\n".join(rows)


def enforce_rules(decision: Dict[str, Any], reports: Dict[str, Dict[str, Any]],
                  held: Set[str]) -> tuple:
    """Code-side checks on the chair's decision -> (kept trades, dropped [(trade, why)])."""
    support = {a["asset"] for r in reports.values() for a in r["assets"] if a["stance"] == "BUY"}
    kept, dropped = [], []
    for t in decision["trades"]:
        if t["action"] == "BUY" and t["asset"] not in support:
            dropped.append((t, "no analyst rated it BUY"))
        elif t["action"] == "SELL" and t["asset"] not in held:
            dropped.append((t, "not held (desk is long-only)"))
        else:
            kept.append(t)
    return kept, dropped


class Council:
    def __init__(self, analysts: Dict[str, ModelAdapter], chair: ModelAdapter, universe: List[str],
                 min_analysts: int = 2, max_trades: int = 3, timeout: float = 300.0) -> None:
        self.analysts, self.chair = analysts, chair
        self.universe, self.min_analysts = list(universe), min_analysts
        self.max_trades, self.timeout = max_trades, timeout

    async def _analyst(self, name: str, adapter: ModelAdapter, context: str, observe: Optional[Observer]):
        t0 = time.time()
        try:
            rep = await ask_json(adapter, analyst_prompt(context, LENSES.get(name, ""), self.universe),
                                 analyst_validator(self.universe), timeout=self.timeout)
        except ModelError as e:
            logger.warning("analyst %s dropped: %s", name, e)
            _emit(observe, "analyst_dropped", name=name, error=str(e), latency=time.time() - t0)
            return name, None, str(e), time.time() - t0
        # Reported as each analyst finishes, not when the slowest one does.
        _emit(observe, "analyst_done", name=name, report=rep, latency=time.time() - t0)
        return name, rep, None, time.time() - t0

    async def run(self, context: str, held: Set[str], meeting_id: Optional[str] = None,
                  observe: Optional[Observer] = None) -> Dict[str, Any]:
        mid = meeting_id or new_meeting_id()
        _emit(observe, "analysts_started", analysts=list(self.analysts))
        results = await asyncio.gather(*(self._analyst(n, a, context, observe) for n, a in self.analysts.items()))
        reports = {n: r for n, r, _, _ in results if r is not None}
        rec: Dict[str, Any] = {
            "id": mid, "started": time.time(), "analysts": reports,
            "errors": {n: e for n, _, e, _ in results if e},
            "latency": {n: round(dt, 1) for n, _, _, dt in results},
            "tally": None, "labels": {}, "decision": None, "trades": [], "dropped": [],
            "status": "ok",
        }
        if len(reports) < self.min_analysts:
            rec["status"] = f"abstain: {len(reports)} analyst(s) responded, need {self.min_analysts}"
            return rec
        tally = compute_tally(reports, self.universe)
        labels = {n: chr(ord("A") + i) for i, n in enumerate(sorted(reports))}
        anon = {labels[n]: reports[n] for n in sorted(reports)}
        rec.update(tally=tally, labels=labels)
        _emit(observe, "chair_started", reports=len(reports))
        t0 = time.time()
        try:
            decision = await ask_json(self.chair, chair_prompt(context, anon, tally, self.universe, self.max_trades),
                                      chair_validator(self.universe, self.max_trades), timeout=self.timeout)
        except ModelError as e:
            rec["status"] = f"abstain: chair failed ({e})"
            _emit(observe, "chair_failed", error=str(e), latency=time.time() - t0)
            return rec
        rec["latency"]["chair"] = round(time.time() - t0, 1)
        kept, dropped = enforce_rules(decision, reports, held)
        rec.update(decision=decision, trades=kept,
                   dropped=[{"trade": t, "why": why} for t, why in dropped])
        _emit(observe, "chair_done", decision=decision, trades=kept, dropped=rec["dropped"],
              latency=time.time() - t0)
        return rec


def _emit(observe: Optional[Observer], kind: str, **data: Any) -> None:
    if observe is None:
        return
    try:
        observe(kind, data)
    except Exception:  # a broken watcher must never cost us a meeting
        logger.warning("council observer failed on %s", kind, exc_info=True)
