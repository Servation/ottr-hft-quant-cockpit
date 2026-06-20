"""Structured audit trail for accountability (who / what / when).

Appends one JSON object per line to an audit log file. Every state-changing
action (trade, parameter change, order cancel, blocked attempt) and every
accepted CEO directive should be recorded here. Closes the repudiation gap from
threat_model.md.
"""
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from bot import PROJECT_ROOT

logger = logging.getLogger(__name__)


def _audit_file() -> Path:
    return Path(os.getenv("AUDIT_LOG_FILE") or (PROJECT_ROOT / "data" / "audit_log.jsonl"))


def audit_event(kind: str, **fields) -> dict:
    """Append a structured audit record and also log it. Never raises."""
    rec = {"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, **fields}
    try:
        path = _audit_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except OSError as e:
        logger.error("Failed to write audit event (%s): %s", kind, e)
    logger.info("AUDIT %s %s", kind, json.dumps(fields, default=str))
    return rec
