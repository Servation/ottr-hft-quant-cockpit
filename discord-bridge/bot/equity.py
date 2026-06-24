"""
Equity-curve logger — the (bridge-owned) single writer of a portfolio-value time
series.

Why this exists: portfolio_state.json only holds *current* state and trade
history is capped at 50, so there was no series to compute returns / Sharpe /
drawdown / a buy-and-hold benchmark from. This module appends one snapshot per
call to an append-only JSONL file (never truncated). The gateway reads it for the
/performance view (Phase M1); the metrics math lives in ``bot.metrics``.

Each row also records the BTC spot price so the benchmark (HODL-BTC) can be
derived later without a second data source.
"""

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from bot import PROJECT_ROOT

logger = logging.getLogger(__name__)

# Overridable in tests (mirrors the bot.portfolio fixture pattern).
_DATA_DIR = PROJECT_ROOT / "data"
_EQUITY_FILE = _DATA_DIR / "equity_curve.jsonl"


def _btc_price(prices: Dict[str, Any]) -> Optional[float]:
    """Pull BTC spot out of a price-feed dict, tolerant of how it's keyed."""
    for key, entry in prices.items():
        if not isinstance(entry, dict):
            continue
        symbol = str(entry.get("symbol") or key or "").upper()
        if symbol == "BTC":
            price = entry.get("price")
            if price:
                return float(price)
    return None


def log_snapshot(
    portfolio, prices: Dict[str, Any], *, source: str = "scheduled"
) -> Optional[Dict[str, Any]]:
    """Append one equity snapshot. Read-only with respect to the portfolio.

    Returns the written row, or None if it couldn't be taken (e.g. no prices, so
    holdings would be mis-valued — we skip rather than log a corrupt point).
    """
    if not prices:
        return None
    try:
        summary = portfolio.get_summary(prices)
    except Exception:
        logger.exception("equity snapshot: failed to read portfolio summary")
        return None

    total = summary.get("total_portfolio_value")
    if total is None:
        return None
    cash = float(summary.get("cash", 0.0))

    row = {
        "ts": time.time(),
        "total_value": float(total),
        "cash": cash,
        "holdings_value": float(total) - cash,
        "realized_pnl": float(summary.get("realized_pnl", 0.0)),
        "unrealized_pnl": float(summary.get("unrealized_pnl", 0.0)),
        "btc_price": _btc_price(prices),
        "source": source,
    }
    _append(row)
    return row


def _append(row: Dict[str, Any]) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(_EQUITY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except OSError as e:
        logger.error("Failed to append equity snapshot: %s", e)


def load_curve() -> List[Dict[str, Any]]:
    """Read all snapshots in write order. Tolerates blank/corrupt lines."""
    if not _EQUITY_FILE.exists():
        return []
    rows: List[Dict[str, Any]] = []
    try:
        with open(_EQUITY_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError as e:
        logger.error("Failed to read equity curve: %s", e)
    return rows
