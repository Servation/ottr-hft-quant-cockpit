"""
Council wire schemas + validators.

What it does: defines the JSON shapes analysts and the chair must return and validates
them with plain Python (no extra dependency). Why: model output is untrusted input, so
every field is checked and normalized before any code acts on it.
"""

from typing import Any, Dict, List

STANCES = ("BUY", "SELL", "HOLD")
ACTIONS = ("BUY", "SELL")


class SchemaError(ValueError):
    """Raised when a model response does not match the expected shape."""


def _conviction(v: Any, where: str) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise SchemaError(f"{where}: conviction must be a number 0..1")
    if not 0.0 <= f <= 1.0:
        raise SchemaError(f"{where}: conviction {f} outside 0..1")
    return f


def _text(v: Any, where: str, max_len: int = 600) -> str:
    if not isinstance(v, str) or not v.strip():
        raise SchemaError(f"{where}: must be a non-empty string")
    return v.strip()[:max_len]


def make_analyst_validator(universe: List[str]):
    """Validator for an analyst response. Every universe asset needs exactly one stance."""
    allowed = {a.upper() for a in universe}

    def validate(obj: Any) -> Dict[str, Any]:
        if not isinstance(obj, dict):
            raise SchemaError("top level must be an object")
        view = _text(obj.get("overall_view"), "overall_view", 800)
        rows = obj.get("assets")
        if not isinstance(rows, list):
            raise SchemaError("assets must be a list")
        seen: Dict[str, Dict[str, Any]] = {}
        for i, r in enumerate(rows):
            if not isinstance(r, dict):
                raise SchemaError(f"assets[{i}] must be an object")
            asset = str(r.get("asset", "")).upper().strip()
            if asset not in allowed:
                raise SchemaError(f"assets[{i}]: unknown asset {asset!r}")
            if asset in seen:
                raise SchemaError(f"assets[{i}]: duplicate asset {asset}")
            stance = str(r.get("stance", "")).upper().strip()
            if stance not in STANCES:
                raise SchemaError(f"assets[{i}]: stance must be one of {STANCES}")
            seen[asset] = {
                "asset": asset,
                "stance": stance,
                "conviction": _conviction(r.get("conviction"), f"assets[{i}]"),
                "thesis": _text(r.get("thesis"), f"assets[{i}].thesis"),
                "key_risk": _text(r.get("key_risk"), f"assets[{i}].key_risk"),
            }
        missing = allowed - set(seen)
        if missing:
            raise SchemaError(f"missing stance for: {sorted(missing)}")
        return {"overall_view": view, "assets": [seen[a] for a in sorted(seen)]}

    return validate


def make_chair_validator(universe: List[str], max_trades: int = 3):
    """Validator for the chair decision. BUY sizes in USD, SELL as a fraction of the position."""
    allowed = {a.upper() for a in universe}

    def validate(obj: Any) -> Dict[str, Any]:
        if not isinstance(obj, dict):
            raise SchemaError("top level must be an object")
        summary = _text(obj.get("summary"), "summary", 1000)
        dissent = obj.get("dissent_notes", "")
        dissent = dissent.strip()[:800] if isinstance(dissent, str) else ""
        trades_in = obj.get("trades", [])
        if not isinstance(trades_in, list):
            raise SchemaError("trades must be a list")
        if len(trades_in) > max_trades:
            raise SchemaError(f"at most {max_trades} trades per meeting")
        trades: List[Dict[str, Any]] = []
        used = set()
        for i, t in enumerate(trades_in):
            if not isinstance(t, dict):
                raise SchemaError(f"trades[{i}] must be an object")
            action = str(t.get("action", "")).upper().strip()
            asset = str(t.get("asset", "")).upper().strip()
            if action not in ACTIONS:
                raise SchemaError(f"trades[{i}]: action must be BUY or SELL")
            if asset not in allowed:
                raise SchemaError(f"trades[{i}]: unknown asset {asset!r}")
            if asset in used:
                raise SchemaError(f"trades[{i}]: only one trade per asset per meeting")
            used.add(asset)
            row = {
                "action": action,
                "asset": asset,
                "conviction": _conviction(t.get("conviction"), f"trades[{i}]"),
                "reasoning": _text(t.get("reasoning"), f"trades[{i}].reasoning", 500),
            }
            if action == "BUY":
                try:
                    usd = float(t.get("usd_amount"))
                except (TypeError, ValueError):
                    raise SchemaError(f"trades[{i}]: BUY needs numeric usd_amount")
                if usd <= 0:
                    raise SchemaError(f"trades[{i}]: usd_amount must be > 0")
                row["usd_amount"] = usd
            else:
                try:
                    frac = float(t.get("fraction_of_position"))
                except (TypeError, ValueError):
                    raise SchemaError(f"trades[{i}]: SELL needs numeric fraction_of_position")
                if not 0.0 < frac <= 1.0:
                    raise SchemaError(f"trades[{i}]: fraction_of_position must be in (0, 1]")
                row["fraction_of_position"] = frac
            trades.append(row)
        return {"summary": summary, "dissent_notes": dissent, "trades": trades}

    return validate


ANALYST_SHAPE = """{
  "overall_view": "2-3 sentences on the market right now",
  "assets": [
    {"asset": "BTC", "stance": "BUY|SELL|HOLD", "conviction": 0.0-1.0,
     "thesis": "one or two sentences using the data given",
     "key_risk": "the single biggest way this call is wrong"}
  ]
}"""

CHAIR_SHAPE = """{
  "summary": "2-4 sentences: what the council concluded and why",
  "dissent_notes": "which analyst disagreed with the decision and the strongest point they made",
  "trades": [
    {"action": "BUY", "asset": "ETH", "usd_amount": 500, "conviction": 0.0-1.0, "reasoning": "one sentence"},
    {"action": "SELL", "asset": "SOL", "fraction_of_position": 0.5, "conviction": 0.0-1.0, "reasoning": "one sentence"}
  ]
}"""
