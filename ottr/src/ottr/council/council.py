"""
Council: independent analysts, a round 2 where they split, a code-computed tally, and a
chair that decides.

What it does: sends one context (market packet + book) to every analyst in parallel,
tallies their stances in code, asks the chair to decide on anonymized reports, then
applies code-side rules to the decision (no BUY without analyst support, no SELL of an
asset not held). Returns a record; it never trades. Why: models judge, code counts and
enforces, and the record keeps every raw input/output for audit and the scorecard.

Round 2 (`debate`): where the analysts' round-1 stances differ on an asset, each reads the
others' cases on those assets (anonymized, the same labels the chair sees) and gives a
final stance with a reply to the strongest point against its own. Why: on their own, the
chair settles every split in one read, and in practice it bought what all three backed
(24 of 26, 2026-10-22 to 11-16) and almost never a lone BUY (2 of 24), whose author never
got to defend it. Round 1 stays independent so the views don't anchor on each other and
the scorecard grades each model's own call; round 2 is recorded apart, so the scorecard
can tell whether it helps. The tally, the chair's support rule and the chair's reading
use the final stances; an analyst that fails round 2 keeps its round-1 stances.

If the chair fails (its CLI signed out, a timeout, JSON it can't repair) a fallback model
chairs on the same prompt, so one provider being down doesn't cost the meeting; the record
says who chaired and why.

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
from typing import Any, Callable, Dict, List, Optional, Sequence, Set

from ottr.council.models import ModelAdapter, ModelError, ask_json
from ottr.council.schemas import (ANALYST_SHAPE, CHAIR_SHAPE, DEBATE_SHAPE, analyst_validator,
                                  chair_validator, debate_validator)

logger = logging.getLogger(__name__)

# observe(kind, data). Kinds: analysts_started {analysts}, analyst_done {name, report,
# latency}, analyst_dropped {name, error, latency}, debate_started {assets, analysts},
# debate_done {name, answers, changes, latency}, debate_dropped {name, error, latency},
# chair_started {reports},
# chair_replaced {name, replaced, error, latency} (the fallback stands in),
# chair_done {decision, trades, dropped, latency}, chair_failed {error, latency}.
Observer = Callable[[str, Dict[str, Any]], None]


def new_meeting_id() -> str:
    return f"m-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}-{uuid.uuid4().hex[:6]}"

LENSES = {
    "claude": "Your lens: risk and regime. Weigh drawdown risk, trending vs choppy regimes, "
              "volatility, concentration and correlation across the book, and what could go wrong.",
    "gpt": "Your lens: quant and technical. Weigh the returns, trend indicators, momentum and "
           "volatility, and whether the numbers actually support an entry or exit.",
    "gemini": "Your lens: cross-asset risk appetite. Weigh rates, the curve, credit spreads, "
              "inflation expectations, the VIX and breadth, and what they say about whether to "
              "own stocks, bonds, real assets or cash right now.",
}


def analyst_prompt(context: str, lens: str, universe: List[str]) -> str:
    return (
        "You are one analyst on a US-markets paper-trading desk (ETFs across stocks, bonds, "
        "commodities and real estate, plus a few single stocks). Other analysts work "
        f"independently; you will not see their views before you answer.\n{lens}\n\n"
        f"Give exactly one stance for each asset: {', '.join(universe)}. HOLD is a valid "
        "answer; do not force trades. The desk is long-only with no leverage: SELL means "
        "reduce or exit a position it HOLDS; for an asset you would merely avoid, say HOLD. "
        "Idle cash is swept into the T-bill ETF by code, so cash is never idle. Position "
        "sizes are set by the desk's risk model from your conviction, so state conviction "
        "honestly (0.3 weak, 0.6 solid, 0.9 rare and strong). The desk holds for days to "
        "weeks: judge the trend and the risk, not the next hour.\n\n"
        f"=== CONTEXT ===\n{context}\n=== END CONTEXT ===\n\n"
        f"Reply with ONLY a JSON object of this shape (no prose, no code fences):\n{ANALYST_SHAPE}"
    )


def debate_prompt(context: str, lens: str, me: str, contested_assets: List[str],
                  reports: Dict[str, Dict[str, Any]]) -> str:
    """Round 2 for Analyst `me`: every analyst's round-1 case on the contested assets, by
    label, and a final stance on each. `reports` are the anonymized round-1 reports."""
    lines = [f"{'You' if label == me else 'Analyst ' + label}, overall: {rep['overall_view']}"
             for label, rep in reports.items()]
    for asset in contested_assets:
        lines.append(f"\n{asset}:")
        for label, rep in reports.items():
            a = next(x for x in rep["assets"] if x["asset"] == asset)
            lines.append(f"  {'You' if label == me else 'Analyst ' + label}: {a['stance']} ({a['conviction']:.2f}) "
                         f"{a['thesis']} Risk: {a['key_risk']}")
    return (
        f"You are Analyst {me}, one of the analysts on a US-markets paper-trading desk.\n{lens}\n\n"
        "In round 1 every analyst rated every asset without seeing the others. You split on "
        f"{', '.join(contested_assets)}. This is round 2: read the other analysts' cases, check them "
        "against the CONTEXT, and give your final stance on each of these assets.\n"
        "- Change a stance only where an argument backed by the numbers in the CONTEXT persuades "
        "you. Holding firm is a good answer; agreeing for the sake of agreement is not.\n"
        "- In each reply, name the strongest point against your round-1 view and say why it does "
        "or doesn't move you.\n"
        "- The rules are the same as in round 1: SELL means reduce or exit a position the desk "
        "HOLDS; for an asset you would merely avoid, say HOLD. State conviction honestly "
        "(0.3 weak, 0.6 solid, 0.9 rare and strong).\n\n"
        f"=== CONTEXT ===\n{context}\n=== END CONTEXT ===\n\n"
        "=== ROUND 1 ===\n" + "\n".join(lines) + "\n=== END ROUND 1 ===\n\n"
        f"Reply with ONLY a JSON object of this shape (no prose, no code fences):\n{DEBATE_SHAPE}"
    )


def chair_prompt(context: str, reports: Dict[str, Dict[str, Any]], tally: Dict[str, Any],
                 universe: List[str], max_trades: int,
                 round2: Optional[Dict[str, Dict[str, Dict[str, Any]]]] = None,
                 contested_assets: Sequence[str] = ()) -> str:
    """`reports` are the anonymized round-1 reports; `round2` is label -> asset -> the
    analyst's round-2 answer (an analyst missing from it didn't answer round 2)."""
    round2 = round2 or {}
    lines = []
    for label, rep in reports.items():
        lines.append(f"--- Analyst {label} ---\nView: {rep['overall_view']}")
        for a in rep["assets"]:
            lines.append(f"{a['asset']}: {a['stance']} ({a['conviction']:.2f}) {a['thesis']} "
                         f"Risk: {a['key_risk']}")
            if a["asset"] in contested_assets:
                r2 = (round2.get(label) or {}).get(a["asset"])
                if r2 is None:
                    lines.append("  Round 2: no answer, so its round-1 stance stands.")
                else:
                    moved = "held" if r2["stance"] == a["stance"] else "changed to"
                    lines.append(f"  Round 2: {moved} {r2['stance']} ({r2['conviction']:.2f}). {r2['reply']}")
    debated = (
        "The analysts first rated every asset without seeing each other. Where they split ("
        f"{', '.join(contested_assets)}), each then read the others' cases and gave a final stance "
        "in round 2. The tally and the BUY rule count final stances.\n") if contested_assets else ""
    return (
        "You chair a US-markets paper-trading desk. Decide what, if anything, to trade. You are "
        "not bound by the majority; if you overrule the analysts, say why in dissent_notes. "
        "An empty trades list is a good answer when the case is weak.\n" + debated +
        "Rules enforced by code (violations are dropped): at most "
        f"{max_trades} trades, one per asset; BUY only an asset at least one analyst's final stance "
        "rates BUY and the CONTEXT doesn't mark as blocked (earnings, a corporate action, being "
        "sold off); SELL only an asset in HOLDINGS. Do NOT give dollar amounts: BUY takes a "
        "conviction (the risk model sizes it and only tops up to its target), SELL takes "
        "fraction_of_position (1.0 = exit).\n\n"
        f"=== CONTEXT ===\n{context}\n=== END CONTEXT ===\n\n"
        "=== ANALYST REPORTS ===\n" + "\n".join(lines) + "\n=== END REPORTS ===\n\n"
        f"=== TALLY (computed by code) ===\n{format_tally(tally)}\n=== END TALLY ===\n\n"
        f"Reply with ONLY a JSON object of this shape (no prose, no code fences):\n{CHAIR_SHAPE}"
    )


def contested(reports: Dict[str, Dict[str, Any]], universe: List[str]) -> List[str]:
    """Assets whose round-1 stances differ: at least one analyst wants to act (BUY or SELL)
    where another doesn't, or they want opposite things. Agreement, HOLD included, isn't
    argued again."""
    return [asset for asset in universe
            if len({a["stance"] for r in reports.values() for a in r["assets"] if a["asset"] == asset}) > 1]


def final_reports(reports: Dict[str, Dict[str, Any]],
                  answers: Dict[str, Dict[str, Dict[str, Any]]]) -> Dict[str, Dict[str, Any]]:
    """Each analyst's report with its round-2 stance and conviction where it gave one."""
    out = {}
    for name, rep in reports.items():
        mine = answers.get(name) or {}
        out[name] = dict(rep, assets=[dict(a, stance=mine[a["asset"]]["stance"],
                                           conviction=mine[a["asset"]]["conviction"])
                                      if a["asset"] in mine else a for a in rep["assets"]])
    return out


def changes(report: Dict[str, Any], answers: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """[{asset, from, to, conviction}] for every stance round 2 moved."""
    was = {a["asset"]: a["stance"] for a in report["assets"]}
    return [{"asset": x, "from": was[x], "to": r["stance"], "conviction": r["conviction"]}
            for x, r in answers.items() if r["stance"] != was.get(x)]


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
                  held: Set[str], blocked: Optional[Dict[str, str]] = None) -> tuple:
    """Code-side checks on the chair's decision -> (kept trades, dropped [(trade, why)]).

    `blocked` names assets the gate would refuse to BUY right now and why (a member being
    sold off, an earnings blackout, a pending corporate action): those BUYs are dropped here
    with the reason, so the record says why instead of showing a rejection at the gate."""
    support = {a["asset"] for r in reports.values() for a in r["assets"] if a["stance"] == "BUY"}
    blocked = blocked or {}
    kept, dropped = [], []
    for t in decision["trades"]:
        if t["action"] == "BUY" and t["asset"] not in support:
            dropped.append((t, "no analyst rated it BUY"))
        elif t["action"] == "BUY" and t["asset"] in blocked:
            dropped.append((t, blocked[t["asset"]]))
        elif t["action"] == "SELL" and t["asset"] not in held:
            dropped.append((t, "not held (desk is long-only)"))
        else:
            kept.append(t)
    return kept, dropped


class Council:
    def __init__(self, analysts: Dict[str, ModelAdapter], chair: ModelAdapter, universe: List[str],
                 min_analysts: int = 2, max_trades: int = 3, timeout: float = 300.0,
                 fallback: Optional[ModelAdapter] = None, debate: bool = False) -> None:
        """`debate`: hold round 2 on the assets the analysts split on (the engine sets it
        from `council.debate`; off by default so a scripted test model answers once)."""
        self.analysts, self.chair, self.fallback = analysts, chair, fallback
        self.universe, self.min_analysts = list(universe), min_analysts
        self.max_trades, self.timeout, self.debate = max_trades, timeout, debate

    async def _analyst(self, name: str, adapter: ModelAdapter, context: str, observe: Optional[Observer],
                       universe: List[str]):
        t0 = time.time()
        try:
            rep = await ask_json(adapter, analyst_prompt(context, LENSES.get(name, ""), universe),
                                 analyst_validator(universe), timeout=self.timeout)
        except ModelError as e:
            logger.warning("analyst %s dropped: %s", name, e)
            _emit(observe, "analyst_dropped", name=name, error=str(e), latency=time.time() - t0)
            return name, None, str(e), time.time() - t0, None
        # Reported as each analyst finishes, not when the slowest one does.
        _emit(observe, "analyst_done", name=name, report=rep, latency=time.time() - t0)
        return name, rep, None, time.time() - t0, adapter.identity()

    async def _rebut(self, name: str, adapter: ModelAdapter, prompt: str, contested_assets: List[str],
                     report: Dict[str, Any], observe: Optional[Observer]):
        """One analyst's round 2 -> (name, {asset: answer} or None, error or None, latency)."""
        t0 = time.time()
        try:
            out = await ask_json(adapter, prompt, debate_validator(contested_assets), timeout=self.timeout)
        except ModelError as e:
            logger.warning("analyst %s dropped out of round 2 (its round-1 stances stand): %s", name, e)
            _emit(observe, "debate_dropped", name=name, error=str(e), latency=time.time() - t0)
            return name, None, str(e), time.time() - t0
        answers = {a["asset"]: {"stance": a["stance"], "conviction": a["conviction"], "reply": a["reply"]}
                   for a in out["assets"]}
        _emit(observe, "debate_done", name=name, answers=answers, changes=changes(report, answers),
              latency=time.time() - t0)
        return name, answers, None, time.time() - t0

    async def _round2(self, context: str, reports: Dict[str, Dict[str, Any]], labels: Dict[str, str],
                      contested_assets: List[str], observe: Optional[Observer]) -> Dict[str, Any]:
        """Every analyst that reported answers round 2 at once; the record of it."""
        anon = {labels[n]: reports[n] for n in sorted(reports)}
        _emit(observe, "debate_started", assets=list(contested_assets), analysts=sorted(reports))
        results = await asyncio.gather(*(
            self._rebut(n, self.analysts[n], debate_prompt(context, LENSES.get(n, ""), labels[n],
                                                           contested_assets, anon),
                        contested_assets, reports[n], observe)
            for n in sorted(reports)))
        answers = {n: a for n, a, _, _ in results if a is not None}
        return {"assets": list(contested_assets), "answers": answers,
                "changes": {n: changes(reports[n], a) for n, a in answers.items()},
                "errors": {n: e for n, _, e, _ in results if e},
                "latency": {n: round(dt, 1) for n, _, _, dt in results}}

    async def run(self, context: str, held: Set[str], meeting_id: Optional[str] = None,
                  observe: Optional[Observer] = None, universe: Optional[List[str]] = None,
                  blocked: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """One meeting. `universe` (default: the constructor's) is what may be traded today,
        and `blocked` the BUYs code will drop and why (see `enforce_rules`)."""
        mid = meeting_id or new_meeting_id()
        universe = list(universe or self.universe)
        _emit(observe, "analysts_started", analysts=list(self.analysts))
        results = await asyncio.gather(*(self._analyst(n, a, context, observe, universe)
                                         for n, a in self.analysts.items()))
        reports = {n: r for n, r, _, _, _ in results if r is not None}
        rec: Dict[str, Any] = {
            "id": mid, "started": time.time(), "analysts": reports,
            # Which model (and effort) wrote each report: the scorecard grades by it.
            "models": {n: who for n, r, _, _, who in results if r is not None},
            "errors": {n: e for n, _, e, _, _ in results if e},
            "latency": {n: round(dt, 1) for n, _, _, dt, _ in results},
            "tally": None, "labels": {}, "debate": None, "decision": None, "trades": [], "dropped": [],
            "status": "ok",
        }
        if len(reports) < self.min_analysts:
            rec["status"] = f"abstain: {len(reports)} analyst(s) responded, need {self.min_analysts}"
            return rec
        labels = {n: chr(ord("A") + i) for i, n in enumerate(sorted(reports))}
        anon = {labels[n]: reports[n] for n in sorted(reports)}
        split = contested(reports, universe) if self.debate else []
        final = reports
        if split:
            rec["debate"] = await self._round2(context, reports, labels, split, observe)
            final = final_reports(reports, rec["debate"]["answers"])
        # `analysts` stays round 1 (each model's own view, which the scorecard grades); the
        # tally, the chair's reading and the BUY rule use the final stances.
        tally = compute_tally(final, universe)
        rec.update(tally=tally, labels=labels, chair={"name": self.chair.name})
        _emit(observe, "chair_started", reports=len(reports))
        round2 = {labels[n]: a for n, a in (rec["debate"] or {}).get("answers", {}).items()}
        prompt = chair_prompt(context, anon, tally, universe, self.max_trades, round2, split)
        validate = chair_validator(universe, self.max_trades)
        t0 = time.time()
        try:
            decision = await ask_json(self.chair, prompt, validate, timeout=self.timeout)
        except ModelError as e:
            if self.fallback is None:
                rec["status"] = f"abstain: chair failed ({e})"
                _emit(observe, "chair_failed", error=str(e), latency=time.time() - t0)
                return rec
            # Same reports, same rules: only who reads them changes. The fallback may have
            # been an analyst too, as the chair usually is; the reports stay anonymized.
            failed_after = round(time.time() - t0, 1)
            logger.warning("chair %s failed after %.0fs, %s stands in: %s",
                           self.chair.name, failed_after, self.fallback.name, e)
            rec["chair"] = {"name": self.fallback.name, "replaced": self.chair.name, "error": str(e),
                            "failed_after_sec": failed_after}
            _emit(observe, "chair_replaced", name=self.fallback.name, replaced=self.chair.name,
                  error=str(e), latency=failed_after)
            t0 = time.time()
            try:
                decision = await ask_json(self.fallback, prompt, validate, timeout=self.timeout)
            except ModelError as e2:
                rec["status"] = f"abstain: chair failed ({e}); fallback {self.fallback.name} failed too ({e2})"
                _emit(observe, "chair_failed", error=str(e2), latency=time.time() - t0)
                return rec
            rec["chair"].update(self.fallback.identity())
        else:
            rec["chair"].update(self.chair.identity())
        rec["latency"]["chair"] = round(time.time() - t0, 1)
        kept, dropped = enforce_rules(decision, final, held, blocked)
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
