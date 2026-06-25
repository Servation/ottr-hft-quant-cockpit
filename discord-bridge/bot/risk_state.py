"""
Persisted risk-breaker latch (Tier 3 / R0).

The ONLY mutable risk state on disk: whether the drawdown breaker is currently halted
(and since when), plus per-asset cooldown timestamps that stop the enforcer from
re-firing a forced action on every 60s tick. Bridge-owned single writer; the atomic
write mirrors bot.portfolio.save() (temp file + os.replace) so a crash can't leave a
half-written latch.

Deliberately minimal: the drawdown PEAK is NOT stored here — it's derived from the
equity curve (the single source of truth). Only the latch + cooldowns need to survive a
restart, so a bounce mid-drawdown doesn't silently resume buying.
"""

import json
import os
import tempfile
from typing import Any, Dict

from bot import PROJECT_ROOT

# Overridable in tests (mirrors the bot.equity / bot.portfolio fixture pattern).
_DATA_DIR = PROJECT_ROOT / "data"
_STATE_FILE = _DATA_DIR / "risk_state.json"


def _default_state() -> Dict[str, Any]:
    # `highs`: per-asset trailing high-water marks (for stop_loss_mode=trailing).
    return {"halted": False, "halted_since": None, "last_action_ts": {}, "highs": {}}


def load() -> Dict[str, Any]:
    """Load the latch, healing missing keys; returns a fresh default if absent/corrupt."""
    if _STATE_FILE.exists():
        try:
            with open(_STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
            if not isinstance(state, dict):
                return _default_state()
            for key, value in _default_state().items():
                state.setdefault(key, value)
            if not isinstance(state.get("last_action_ts"), dict):
                state["last_action_ts"] = {}
            if not isinstance(state.get("highs"), dict):
                state["highs"] = {}
            return state
        except (json.JSONDecodeError, OSError):
            return _default_state()
    return _default_state()


def save(state: Dict[str, Any]) -> None:
    """Atomically persist the latch (temp file + os.replace), like portfolio.save()."""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=str(_DATA_DIR), suffix=".tmp", prefix="risk_")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_path, str(_STATE_FILE))
        tmp_path = None  # renamed; nothing to clean up
    except OSError:
        # Best-effort: a failed latch write must never crash the enforcer loop.
        pass
    finally:
        if tmp_path is not None and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def cooldown_active(
    state: Dict[str, Any], asset: str, now: float, cooldown_seconds: float
) -> bool:
    """True if a forced action on `asset` fired within the last `cooldown_seconds`."""
    ts = state.get("last_action_ts", {}).get(asset)
    return ts is not None and (now - float(ts)) < cooldown_seconds


def mark_action(state: Dict[str, Any], asset: str, now: float) -> None:
    """Record that a forced action on `asset` just fired (mutates `state` in place)."""
    state.setdefault("last_action_ts", {})[asset] = now
