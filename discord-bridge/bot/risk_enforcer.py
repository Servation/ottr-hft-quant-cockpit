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

from bot import equity, risk, risk_state, settings
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
        emergencies: List[dict] = []

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
                emergencies.append(
                    {"asset": action.asset, "direction": "STOP_LOSS", "pct_change": 0.0}
                )

        # R2 — drawdown circuit breaker: latch a halt on a deep portfolio drawdown.
        dd_changed, dd_emergencies = await _eval_drawdown(state, prices, cfg, now, bot)
        dirty = dirty or dd_changed
        emergencies.extend(dd_emergencies)

        # R3 — concentration trim: pull any position that drifted over its cap back to it.
        if await _eval_concentration(holdings, prices, state, now, cooldown, bot, cfg):
            dirty = True

        if dirty:
            risk_state.save(state)
        if emergencies:
            await _convene_emergency(bot, emergencies)
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


async def _eval_drawdown(state, prices, cfg, now, bot):
    """Evaluate the portfolio drawdown breaker; mutate the halt latch in `state`.

    Returns (changed, emergencies): whether the latch toggled, and any emergency-meeting
    alerts to raise. Skips entirely until the equity curve has >= min_curve_points (a thin
    series must never trip the breaker). The peak is the curve high-water mark vs the live
    value, so it's responsive at 60s even though the curve only samples hourly.
    """
    min_points = int(cfg.get("min_curve_points", 24))
    values = [
        float(r["total_value"])
        for r in equity.load_curve()
        if isinstance(r, dict) and r.get("total_value") is not None
    ]
    if len(values) < min_points:
        return False, []
    try:
        live_value = portfolio.get_total_value(prices)
    except Exception:
        logger.exception("Drawdown breaker: failed to value the portfolio")
        return False, []

    halt_pct = float(cfg.get("max_drawdown_halt_pct", 15.0))
    resume_pct = float(cfg.get("drawdown_resume_pct", 10.0))
    peak = max(max(values), live_value)
    ds = risk.drawdown_state(peak, live_value, halt_pct, resume_pct,
                             was_halted=bool(state.get("halted", False)))

    if ds.tripped:
        state["halted"] = True
        state["halted_since"] = now
        audit_event("risk_halt", drawdown=round(ds.drawdown, 4), peak=peak,
                    value=live_value, halt_pct=halt_pct)
        await _post_floor(bot, (
            f"🚦 **Trading halted — drawdown breaker:** the book is "
            f"{ds.drawdown * 100:.1f}% off its peak (limit {halt_pct:.0f}%). New BUYs are "
            f"blocked until it recovers below {resume_pct:.0f}%; SELLs/de-risking stay open."
        ))
        return True, [{"asset": "PORTFOLIO", "direction": "DRAWDOWN_HALT",
                       "pct_change": round(-ds.drawdown * 100, 2)}]
    if ds.recovered:
        state["halted"] = False
        state["halted_since"] = None
        audit_event("risk_resume", drawdown=round(ds.drawdown, 4))
        await _post_floor(bot, (
            f"✅ **Trading resumed:** drawdown recovered to {ds.drawdown * 100:.1f}% "
            f"(below the {resume_pct:.0f}% resume line). New BUYs re-enabled."
        ))
        return True, []
    return False, []


async def _eval_concentration(holdings, prices, state, now, cooldown, bot, cfg) -> bool:
    """Trim any position that has drifted over its concentration cap + band back to the
    cap, via a partial SELL. Uses the SAME caps as the buy-time gate (`thresholds.*` with
    per-asset overrides), so block and trim never disagree. Returns True if a forced sell
    stamped the latch (so the caller persists it). Runs regardless of any halt — a trim
    only ever reduces risk.
    """
    thresholds = settings.get("thresholds", {})
    default_cap = float(thresholds.get("max_asset_exposure_pct", 0) or 0)
    per_asset_caps = {}
    for asset in holdings:
        override = thresholds.get(f"max_{asset.lower()}_exposure_pct")
        if override is not None:
            per_asset_caps[asset] = float(override)
    if default_cap <= 0 and not per_asset_caps:
        return False
    try:
        total_value = portfolio.get_total_value(prices)
    except Exception:
        logger.exception("Concentration trim: failed to value the portfolio")
        return False

    band = float(cfg.get("concentration_trim_band_pct", 5.0))
    breaches = risk.concentration_breaches(
        holdings, prices, total_value, default_cap, per_asset_caps, band
    )
    dirty = False
    for action in breaches:
        result = await _execute_risk_action(action, prices, state, now, cooldown, bot)
        if result in ("sold", "dry_run"):
            dirty = True
    return dirty


async def _convene_emergency(bot, alert_data: List[dict]) -> None:
    """Convene one emergency meeting after forced actions, so the desk learns *why* and
    doesn't immediately re-enter what the controls just unwound."""
    try:
        from bot.scheduler import meeting_scheduler
        await meeting_scheduler.schedule_emergency(alert_data)
    except Exception:
        logger.exception("Failed to convene emergency meeting after risk action")
