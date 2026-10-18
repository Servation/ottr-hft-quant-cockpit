"""
Optional Discord webhook notifications (meetings, fills, risk events).

What it does: formats an event as a short message and POSTs it to DISCORD_WEBHOOK_URL.
Why a webhook and not a bot: v2 only needs to report; it has no chat commands, so it
needs no gateway connection, token or intents. Failures are logged and never raised:
a Discord outage must not affect trading.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)
LIMIT = 1900  # Discord allows 2000 chars per message


def format_event(kind: str, d: dict) -> str:
    if kind == "fill":
        pnl = f", realized ${float(d['realized_pnl']):+,.2f}" if d["side"] == "SELL" else ""
        return (f"**{d['side']} {d['asset']}** ${float(d['notional']):,.2f} @ {float(d['price']):,.6g} "
                f"[{d['origin']}]{pnl}\n> {d.get('reason', '')}")
    if kind == "meeting":
        trades = ", ".join(f"{t['action']} {t['asset']}" for t in d["trades"]) or "no trades"
        dropped = "".join(f"\n- dropped {x['trade']['action']} {x['trade']['asset']}: {x['why']}"
                          for x in d.get("dropped", []))
        return f"**Council {d['id']}** ({d['status']}): {trades}\n{d.get('summary', '')}{dropped}"
    if kind == "risk_halt":
        return f"**Drawdown breaker tripped**: {d['drawdown'] * 100:.1f}% off peak. BUYs blocked."
    if kind == "risk_resume":
        return f"**Breaker resumed** ({d['why']})."
    return f"**{kind}**: {d}"


def make_discord_notifier(url: Optional[str]):
    if not url:
        return None

    async def notify(kind: str, data: dict) -> None:
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                r = await c.post(url, json={"content": format_event(kind, data)[:LIMIT],
                                            "allowed_mentions": {"parse": []}})
                r.raise_for_status()
        except httpx.HTTPError as e:
            logger.warning("discord notify failed: %s", e)

    return notify
