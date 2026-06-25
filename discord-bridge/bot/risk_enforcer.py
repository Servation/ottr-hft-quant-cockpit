"""
Risk-limit enforcer (Tier 3 / R1+).

The thin execution layer between the pure policies (bot/risk.py) and the sole portfolio
writer. Called once per tick from the 60s AlertMonitor loop: it reads the live book, asks
bot/risk which protective actions are due, and executes them as SELLs through
portfolio.sell — gated by the kill-switch, throttled by a per-asset cooldown latch
(bot/risk_state), audited, and announced to the trading floor.

Master switch: settings.risk_limits.enabled. While false (the default), enforce() is a
no-op, so the whole layer ships DARK until deliberately activated.

R1 wires the stop-loss control. R2 (drawdown halt) and R3 (concentration trim) extend
enforce() and reuse _execute_risk_action().
"""

import logging
import os
import time
from typing import Dict, List

from bot import settings, risk, risk_state
from bot.audit import audit_event
from bot.portfolio import portfolio

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    """Master switch. Enforcement is fully dark until risk_limits.enabled is true."""
    return bool(settings.get("risk_limits", {}).get("enabled", False))


def _dry_run() -> bool:
    """The TRADING_DRY_RUN kill-switch also blocks forced risk SELLs (log + audit only)."""
    return os.getenv("TRADING_DRY_RUN", "0").strip().lower() in ("1", "true", "yes", "on")


async def enforce(bot, prices: Dict[str, dict]) -> None:
    """Run one enforcement pass. No-op unless enabled; never raises into the loop."""
    if not _enabled() or not prices:
        return
    try:
        cfg = settings.get("risk_limits", {})
        state = risk_state.load()
        now = time.time()
        cooldown = float(cfg.get("action_cooldown_seconds", 900))
        dirty = False
        stopped: List[str] = []

        # R1 — stop-loss: exit any position trading past stop_loss_pct below avg cost.
        holdings = portfolio._state.get("holdings", {})
        breaches = risk.stop_loss_breaches(
            holdings,
            prices,
            stop_pct=float(cfg.get("stop_loss_pct", 10.0)),
            mode=str(cfg.get("stop_loss_mode", "avg_cost")),
        )
        for action in breaches:
            result = await _execute_risk_action(action, prices, state, now, cooldown, bot)
            if result in ("sold", "dry_run"):
                dirty = True
            if result == "sold":
                stopped.append(action.asset)

        if dirty:
            risk_state.save(state)
        if stopped:
            await _convene_emergency(bot, stopped)
    except Exception:
        # Enforcement must never take down the monitor loop.
        logger.exception("Risk enforcement pass failed")


async def _execute_risk_action(action, prices, state, now, cooldown, bot) -> str:
    """Execute one RiskAction as a protective SELL through the sole writer.

    Returns a status: "sold" (executed), "dry_run" (kill-switch blocked it), "cooldown"
    (acted on this asset too recently), or "skip" (no usable price / the sell failed).
    Both "sold" and "dry_run" stamp the per-asset cooldown so a 60s tick can't re-fire.
    """
    asset = action.asset
    if risk_state.cooldown_active(state, asset, now, cooldown):
        return "cooldown"
    price = float(prices.get(asset, {}).get("price", 0.0) or 0.0)
    if price <= 0:
        return "skip"

    # Kill-switch: a forced risk SELL is still a trade, so TRADING_DRY_RUN blocks it.
    if _dry_run():
        logger.warning(
            "[DRY-RUN] risk %s on %s suppressed by kill-switch (qty %.8f)",
            action.kind, asset, action.sell_qty,
        )
        audit_event("risk_action_blocked", reason="dry_run", action=action.kind,
                    asset=asset, sell_qty=action.sell_qty, detail=action.detail)
        risk_state.mark_action(state, asset, now)  # throttle re-logging within the window
        return "dry_run"

    try:
        trade = portfolio.sell(asset, action.sell_qty, price)
    except Exception as e:
        logger.error("Risk %s SELL failed for %s: %s", action.kind, asset, e)
        return "skip"

    risk_state.mark_action(state, asset, now)
    audit_event("risk_action", action=action.kind, asset=asset,
                quantity=trade["quantity"], fill_price=trade["fill_price"],
                reason=action.reason, detail=action.detail)
    msg = (
        f"🛑 **Risk control [{action.kind}]:** SELL {trade['quantity']:.8f} {asset} "
        f"@ ${trade['fill_price']:,.2f}\n{action.reason}"
    )
    await _post_floor(bot, msg)
    return "sold"


async def _post_floor(bot, msg: str) -> None:
    """Announce a forced action to the trading floor + audit-log channel (best-effort)."""
    try:
        if bot and getattr(bot, "_trading_floor_channel", None):
            await bot._trading_floor_channel.send(msg)
    except Exception:
        logger.exception("Failed to post risk-control message to trading floor")
    try:
        if bot and hasattr(bot, "post_audit_log"):
            await bot.post_audit_log(msg)
    except Exception:
        logger.exception("Failed to post risk-control message to audit log")


async def _convene_emergency(bot, assets: List[str]) -> None:
    """Convene one emergency meeting after forced exits, so the desk learns *why* and
    doesn't immediately re-enter what the controls just unwound."""
    try:
        from bot.scheduler import meeting_scheduler
        alert_data = [
            {"asset": a, "direction": "STOP_LOSS", "pct_change": 0.0} for a in assets
        ]
        await meeting_scheduler.schedule_emergency(alert_data)
    except Exception:
        logger.exception("Failed to convene emergency meeting after risk exits")
