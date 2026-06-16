"""
OTTR Trading Floor — Alert Monitor

Periodically checks price movements for emergency thresholds and
triggers alerts / emergency meetings when breached.
"""

import asyncio
import logging
import time
from typing import Optional

from bot import settings
from bot.price_feed import price_feed

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default thresholds (overridden from settings if present)
# ---------------------------------------------------------------------------
DEFAULT_CHECK_INTERVAL_SECONDS = 60
DEFAULT_COOLDOWN_SECONDS = 30 * 60          # 30 minutes between alerts
DEFAULT_EMERGENCY_PRICE_DROP_PCT = 5.0      # 5% downward move
DEFAULT_EMERGENCY_PRICE_SPIKE_PCT = 8.0     # 8% upward move
DEFAULT_PRICE_HISTORY_WINDOW_MINUTES = 60   # look-back window


class AlertMonitor:
    """Watches price feeds for abnormal moves and triggers emergencies."""

    def __init__(self) -> None:
        self._running: bool = False
        self._task: Optional[asyncio.Task] = None
        self._last_alert_time: float = 0.0
        self._bot = None  # set in start()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self, bot) -> None:
        """Begin the background monitoring loop."""
        if self._running:
            logger.warning("AlertMonitor already running — skipping start")
            return

        self._bot = bot
        self._running = True
        self._task = asyncio.current_task() or asyncio.ensure_future(
            self._monitor_loop()
        )
        # If called directly (not already inside the loop task), launch it
        if asyncio.current_task() is not None:
            self._task = asyncio.ensure_future(self._monitor_loop())

        logger.info("AlertMonitor started")

    async def stop(self) -> None:
        """Cancel the monitoring loop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("AlertMonitor stopped")

    # ------------------------------------------------------------------
    # Core loop
    # ------------------------------------------------------------------
    async def _monitor_loop(self) -> None:
        alerts_cfg = settings.get("alerts", {})
        check_interval = alerts_cfg.get(
            "check_interval_seconds", DEFAULT_CHECK_INTERVAL_SECONDS
        )
        from bot.portfolio import portfolio

        while self._running:
            try:
                # 1. Check portfolio orders
                current_prices = {}
                try:
                    current_prices = await price_feed.get_prices()
                except Exception:
                    logger.exception("Failed to get prices for order check")
                
                if current_prices:
                    executed_orders = portfolio.check_orders(current_prices)
                    if executed_orders:
                        await self._notify_executed_orders(executed_orders, self._bot)
                
                # 2. Check thresholds
                alerts = await self.check_thresholds()

                if alerts and self._cooldown_elapsed():
                    await self._trigger_emergency(alerts, self._bot)
                    self._last_alert_time = time.monotonic()

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error in alert monitor loop")

            await asyncio.sleep(check_interval)

    # ------------------------------------------------------------------
    # Threshold checking
    # ------------------------------------------------------------------
    async def check_thresholds(self) -> list[dict]:
        """Check recent price history for emergency moves.

        Returns a list of dicts: ``{asset, direction, pct_change}``
        for every threshold breach detected.
        """
        thresholds_cfg = settings.get("thresholds", {})
        drop_pct = thresholds_cfg.get(
            "emergency_price_drop_pct", DEFAULT_EMERGENCY_PRICE_DROP_PCT
        )
        spike_pct = thresholds_cfg.get(
            "emergency_price_spike_pct", DEFAULT_EMERGENCY_PRICE_SPIKE_PCT
        )
        alerts_cfg = settings.get("alerts", {})
        window_minutes = alerts_cfg.get(
            "price_history_window_minutes", DEFAULT_PRICE_HISTORY_WINDOW_MINUTES
        )

        triggered: list[dict] = []

        try:
            history = price_feed.get_price_history()
        except Exception:
            logger.exception("Failed to fetch price history for threshold check")
            return triggered

        if len(history) < 2:
            return triggered

        # Filter history to the configured time window
        import time
        now = time.time()
        cutoff = now - (window_minutes * 60)
        window_entries = [h for h in history if h["timestamp"] >= cutoff]

        if len(window_entries) < 2:
            return triggered

        oldest_entry = window_entries[0]
        latest_entry = window_entries[-1]

        for asset in ("BTC", "ETH"):
            oldest_price = oldest_entry["prices"].get(asset, {}).get("price", 0.0)
            latest_price = latest_entry["prices"].get(asset, {}).get("price", 0.0)

            if oldest_price <= 0:
                continue

            pct_change = ((latest_price - oldest_price) / oldest_price) * 100.0

            if pct_change <= -drop_pct:
                triggered.append({
                    "asset": asset,
                    "direction": "DROP",
                    "pct_change": round(pct_change, 2),
                })
                logger.warning(
                    f"ALERT: {asset} dropped {pct_change:.2f}% in {window_minutes}min"
                )

            elif pct_change >= spike_pct:
                triggered.append({
                    "asset": asset,
                    "direction": "SPIKE",
                    "pct_change": round(pct_change, 2),
                })
                logger.warning(
                    f"ALERT: {asset} spiked {pct_change:.2f}% in {window_minutes}min"
                )

        return triggered

    # ------------------------------------------------------------------
    # Emergency trigger
    # ------------------------------------------------------------------
    async def _trigger_emergency(self, alerts: list[dict], bot) -> None:
        """Post alert to the trading floor and schedule an emergency meeting."""
        # Build the alert message
        lines = ["🚨 **EMERGENCY ALERT — Price Threshold Breached** 🚨", ""]
        for a in alerts:
            emoji = "📉" if a["direction"] == "DROP" else "📈"
            lines.append(
                f"{emoji} **{a['asset']}**: {a['direction']} of "
                f"**{abs(a['pct_change']):.1f}%** in the last "
                f"{settings.get('alerts', {}).get('price_history_window_minutes', DEFAULT_PRICE_HISTORY_WINDOW_MINUTES)} minutes"
            )
        lines.append("")
        lines.append("⏰ Emergency meeting being convened now…")

        message = "\n".join(lines)

        # Post to trading floor
        if bot and bot._trading_floor_channel:
            try:
                await bot._trading_floor_channel.send(message)
            except Exception:
                logger.exception("Failed to post emergency alert to trading floor")

        # Post to system status
        try:
            await bot.post_system_status(
                f"🚨 Emergency triggered: {len(alerts)} threshold breach(es)"
            )
        except Exception:
            logger.exception("Failed to post emergency to system status")

        # Trigger an emergency meeting via the scheduler (deferred import)
        try:
            from bot.scheduler import meeting_scheduler
            await meeting_scheduler.schedule_emergency(alerts)
        except Exception:
            logger.exception("Failed to schedule emergency meeting")

    async def _notify_executed_orders(self, executed_orders: list[dict], bot) -> None:
        """Post a notification for filled orders, and trigger emergency if STOP LOSS hit."""
        stop_loss_hit = False
        lines = ["🔔 **Orders Executed!**"]
        for t in executed_orders:
            order_type = t.get("triggered_order_type", "LIMIT")
            if order_type == "STOP":
                stop_loss_hit = True
            
            emoji = "🛑" if order_type == "STOP" else ("🎯" if order_type == "TAKE_PROFIT" else "📝")
            lines.append(
                f"{emoji} **{order_type} {t['action']}** {t['quantity']:.8f} {t['asset']} "
                f"@ **${t['fill_price']:,.2f}** (Value: ${t['usd_amount']:,.2f})"
            )
        
        if stop_loss_hit:
            lines.append("")
            lines.append("🚨 **STOP LOSS TRIGGERED** — Emergency meeting being convened now…")

        message = "\n".join(lines)
        
        if bot and bot._trading_floor_channel:
            try:
                await bot._trading_floor_channel.send(message)
            except Exception:
                logger.exception("Failed to post order execution to trading floor")
                
        if stop_loss_hit:
            try:
                from bot.scheduler import meeting_scheduler
                alerts = [{"asset": "PORTFOLIO", "direction": "STOP_LOSS", "pct_change": 0.0}]
                await meeting_scheduler.schedule_emergency(alerts)
            except Exception:
                logger.exception("Failed to schedule emergency meeting for stop loss")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _cooldown_elapsed(self) -> bool:
        """Check whether the alert cooldown period has passed."""
        cooldown = settings.get("alerts", {}).get("alert_cooldown_seconds", DEFAULT_COOLDOWN_SECONDS)
        return (time.monotonic() - self._last_alert_time) >= cooldown


# Module-level singleton
alert_monitor = AlertMonitor()
