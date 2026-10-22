"""
Council wire schemas + validators (plain Python, no dependency).

What it does: defines and checks the JSON that analysts and the chair must return.
Why: model output is untrusted input. Every field is type/range-checked (NaN and
infinity rejected), and the chair's shape has no dollar field at all, so sizing can
only ever come from code.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List

STANCES = ("BUY", "SELL", "HOLD")


class SchemaError(ValueError):
    pass


def _num01(v: Any, where: str) -> float:
    if isinstance(v, bool):
        raise SchemaError(f"{where}: must be a number")
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise SchemaError(f"{where}: must be a number")
    if not math.isfinite(f) or not 0.0 <= f <= 1.0:
        raise SchemaError(f"{where}: {v!r} outside 0..1")
    return f


def _text(v: Any, where: str, max_len: int = 600) -> str:
    if not isinstance(v, str) or not v.strip():
        raise SchemaError(f"{where}: must be a non-empty string")
    return v.strip()[:max_len]


def analyst_validator(universe: List[str]):
    allowed = set(universe)

    def validate(obj: Any) -> Dict[str, Any]:
        if not isinstance(obj, dict):
            raise SchemaError("top level must be an object")
        rows = obj.get("assets")
        if not isinstance(rows, list):
            raise SchemaError("assets must be a list")
        seen: Dict[str, Dict[str, Any]] = {}
        for i, r in enumerate(rows):
            if not isinstance(r, dict):
                raise SchemaError(f"assets[{i}] must be an object")
            a = str(r.get("asset", "")).upper().strip()
            if a not in allowed:
                raise SchemaError(f"assets[{i}]: unknown asset {a!r}")
            if a in seen:
                raise SchemaError(f"assets[{i}]: duplicate {a}")
            s = str(r.get("stance", "")).upper().strip()
            if s not in STANCES:
                raise SchemaError(f"assets[{i}]: stance must be one of {STANCES}")
            seen[a] = {"asset": a, "stance": s,
                       "conviction": _num01(r.get("conviction"), f"assets[{i}].conviction"),
                       "thesis": _text(r.get("thesis"), f"assets[{i}].thesis"),
                       "key_risk": _text(r.get("key_risk"), f"assets[{i}].key_risk")}
        missing = allowed - set(seen)
        if missing:
            raise SchemaError(f"missing stance for {sorted(missing)}")
        return {"overall_view": _text(obj.get("overall_view"), "overall_view", 800),
                "assets": [seen[a] for a in universe]}

    return validate


def chair_validator(universe: List[str], max_trades: int):
    allowed = set(universe)

    def validate(obj: Any) -> Dict[str, Any]:
        if not isinstance(obj, dict):
            raise SchemaError("top level must be an object")
        trades = obj.get("trades", [])
        if not isinstance(trades, list):
            raise SchemaError("trades must be a list")
        if len(trades) > max_trades:
            raise SchemaError(f"at most {max_trades} trades")
        out, used = [], set()
        for i, t in enumerate(trades):
            if not isinstance(t, dict):
                raise SchemaError(f"trades[{i}] must be an object")
            if "usd_amount" in t or "quantity" in t:
                raise SchemaError(f"trades[{i}]: do not size trades; code sizes them")
            action = str(t.get("action", "")).upper().strip()
            a = str(t.get("asset", "")).upper().strip()
            if action not in ("BUY", "SELL"):
                raise SchemaError(f"trades[{i}]: action must be BUY or SELL")
            if a not in allowed:
                raise SchemaError(f"trades[{i}]: unknown asset {a!r}")
            if a in used:
                raise SchemaError(f"trades[{i}]: one trade per asset")
            used.add(a)
            row = {"action": action, "asset": a,
                   "reasoning": _text(t.get("reasoning"), f"trades[{i}].reasoning", 500)}
            if action == "BUY":
                row["conviction"] = _num01(t.get("conviction"), f"trades[{i}].conviction")
                if row["conviction"] == 0:
                    raise SchemaError(f"trades[{i}]: BUY conviction must be > 0")
            else:
                frac = _num01(t.get("fraction_of_position"), f"trades[{i}].fraction_of_position")
                if frac == 0:
                    raise SchemaError(f"trades[{i}]: fraction_of_position must be > 0")
                row["fraction_of_position"] = frac
            out.append(row)
        dissent = obj.get("dissent_notes", "")
        return {"summary": _text(obj.get("summary"), "summary", 1200),
                "dissent_notes": dissent.strip()[:800] if isinstance(dissent, str) else "",
                "trades": out}

    return validate


ANALYST_SHAPE = """{
  "overall_view": "2-3 sentences on the market right now",
  "assets": [
    {"asset": "SPY", "stance": "BUY|SELL|HOLD", "conviction": 0.0-1.0,
     "thesis": "one or two sentences citing the numbers given",
     "key_risk": "the single biggest way this call is wrong"}
  ]
}"""

CHAIR_SHAPE = """{
  "summary": "2-4 sentences: what the council concluded and why",
  "dissent_notes": "who disagreed with the decision and their strongest point",
  "trades": [
    {"action": "BUY", "asset": "IEF", "conviction": 0.0-1.0, "reasoning": "one sentence"},
    {"action": "SELL", "asset": "GLD", "fraction_of_position": 0.0-1.0, "reasoning": "one sentence"}
  ]
}"""
