"""
Bridge -> gateway event push (the live SSE pipeline / Tier 4).

The bot POSTs state changes to the FastAPI gateway's /api/internal/discord-sync, which
re-broadcasts them to the React dashboard over SSE. Every send is best-effort and
fail-soft: a down or slow gateway is logged and swallowed, never raised, so the live feed
can never affect trading. Trade/portfolio events are fired fire-and-forget so they add no
latency to the trade path.
"""

import asyncio
import logging
import os
from typing import Any, Dict

import aiohttp

logger = logging.getLogger(__name__)

GATEWAY_URL = f"{os.environ.get('AGENT_GATEWAY_URL', 'http://localhost:8000')}/api/internal/discord-sync"
# Bound the push so a hung gateway can't stall a caller that awaits the send.
_TIMEOUT = aiohttp.ClientTimeout(total=2.0)


async def send_gateway_event(event_name: str, data: Any) -> None:
    """POST one event to the gateway. Never raises (best-effort live feed)."""
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
            payload = {"event": event_name, "data": data}
            async with session.post(GATEWAY_URL, json=payload) as response:
                if response.status != 200:
                    logger.warning("Gateway event %s rejected (status %s)", event_name, response.status)
    except Exception as e:
        logger.debug("Gateway event %s not delivered (%s)", event_name, type(e).__name__)


def publish(event_name: str, data: Any) -> None:
    """Fire-and-forget a gateway event: schedule the POST without blocking the caller (e.g.
    the trade path). send_gateway_event swallows its own errors, so a down/slow gateway never
    affects trading. No-op if there's no running loop."""
    try:
        asyncio.get_running_loop().create_task(send_gateway_event(event_name, data))
    except RuntimeError:
        pass


# ── Awaited wrappers (a small delay is fine here, e.g. meeting agent-state) ──

async def sync_agent_state(agent_states: list) -> None:
    await send_gateway_event("agent_state", agent_states)


async def sync_portfolio(portfolio_state: dict) -> None:
    await send_gateway_event("portfolio", portfolio_state)


async def sync_execution(execution_log: dict) -> None:
    await send_gateway_event("execution", execution_log)


# ── Trade events: fired fire-and-forget after a trade, shaped for the frontend ──

def _execution_payload(trade: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a portfolio trade dict to what the frontend execution listener expects
    (its mapLogEntry reads `symbol`/`price`, not the bridge's `asset`/`fill_price`)."""
    return {
        "timestamp": trade.get("timestamp"),
        "symbol": trade.get("asset"),
        "action": trade.get("action"),
        "quantity": trade.get("quantity"),
        "price": trade.get("fill_price"),
        "slippage_pct": trade.get("slippage_pct"),
        "fee_usd": trade.get("fee_usd"),
        "reasoning": trade.get("reasoning", ""),  # populated in O1
    }


def _portfolio_payload(portfolio, prices: Dict[str, Any]) -> Dict[str, Any]:
    """Snapshot-shaped portfolio dict for the frontend portfolio listener."""
    summary = portfolio.get_summary(prices)
    holdings = summary.get("holdings", {})
    return {
        "total_value": summary.get("total_portfolio_value"),
        "usd_cash": summary.get("cash"),
        "holdings": {s: {"quantity": h.get("quantity"), "avg_cost": h.get("avg_cost")}
                     for s, h in holdings.items()},
        "current_prices": {s: h.get("current_price") for s, h in holdings.items()},
        "purchase_prices": {s: h.get("avg_cost") for s, h in holdings.items()},
    }


def publish_trade(trade: Dict[str, Any], portfolio, prices: Dict[str, Any]) -> None:
    """Fire the live execution + portfolio events for a just-executed trade. Fire-and-forget,
    so it never adds latency to (or can fail) the trade path."""
    try:
        publish("execution", _execution_payload(trade))
        publish("portfolio", _portfolio_payload(portfolio, prices))
    except Exception:
        logger.debug("publish_trade failed to schedule events", exc_info=True)
