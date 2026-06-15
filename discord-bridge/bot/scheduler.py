"""
OTTR Trading Floor — Meeting Scheduler

APScheduler-powered cron scheduler that runs trading-team meetings on a
fixed rotation (every 4 hours) and supports ad-hoc emergency meetings.
"""

import asyncio
import logging
import traceback
from datetime import datetime
from typing import Optional, Tuple

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from bot import settings

logger = logging.getLogger(__name__)

# Meeting hours (US/Pacific): 00:00, 04:00, 08:00, 12:00, 16:00, 20:00
_MEETING_HOURS = (0, 4, 8, 12, 16, 20)
_TIMEZONE = "US/Pacific"


class MeetingScheduler:
    """Schedules and orchestrates periodic and emergency agent meetings."""

    def __init__(self) -> None:
        self._scheduler: Optional[AsyncIOScheduler] = None
        self._meeting_lock: asyncio.Lock = asyncio.Lock()
        self._bot = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self, bot) -> None:
        """Initialise the APScheduler and add cron jobs for each meeting slot."""
        self._bot = bot
        self._scheduler = AsyncIOScheduler(timezone=_TIMEZONE)

        # Add a cron job for every meeting hour
        for hour in _MEETING_HOURS:
            self._scheduler.add_job(
                self._run_scheduled_meeting,
                trigger=CronTrigger(hour=hour, minute=0, timezone=_TIMEZONE),
                id=f"meeting_{hour:02d}",
                name=f"Scheduled meeting @ {hour:02d}:00 PT",
                replace_existing=True,
            )

        self._scheduler.start()
        next_type, next_time = self.get_next_meeting_info()
        logger.info(
            f"Scheduler started — {len(_MEETING_HOURS)} meeting slots registered. "
            f"Next: {next_type} at {next_time}"
        )

    async def stop(self) -> None:
        """Shut down the APScheduler gracefully."""
        if self._scheduler and self._scheduler.running:
            self._scheduler.shutdown(wait=False)
            logger.info("Scheduler stopped")

    # ------------------------------------------------------------------
    # Scheduled meeting execution
    # ------------------------------------------------------------------
    async def _run_scheduled_meeting(self) -> None:
        """Execute one full meeting cycle.

        Acquires a lock so only one meeting runs at a time; if the lock
        is already held (meeting in progress), this invocation is skipped.
        """
        if self._meeting_lock.locked():
            logger.warning("Meeting already in progress — skipping scheduled run")
            return

        async with self._meeting_lock:
            await self._execute_meeting(emergency_data=None)

    # ------------------------------------------------------------------
    # Emergency meeting
    # ------------------------------------------------------------------
    async def schedule_emergency(self, alert_data: list[dict]) -> None:
        """Immediately trigger an emergency meeting (lock-guarded)."""
        if self._meeting_lock.locked():
            logger.warning(
                "Meeting already in progress — emergency will queue after it"
            )

        async with self._meeting_lock:
            await self._execute_meeting(emergency_data=alert_data)

    # ------------------------------------------------------------------
    # Meeting orchestration (shared by scheduled and emergency paths)
    # ------------------------------------------------------------------
    async def _execute_meeting(
        self, emergency_data: Optional[list[dict]] = None
    ) -> None:
        """Gather context and run a meeting through the meeting engine.

        All heavy imports are deferred to method-level to avoid circular
        import chains at module load time.
        """
        from bot.price_feed import price_feed
        from bot.portfolio import portfolio
        from bot.ceo_handler import ceo_handler
        from bot.memory import meeting_memory
        from bot.meetings import meeting_engine, meeting_rotation, MEETING_TYPES

        meeting_type = "emergency_alert" if emergency_data else meeting_rotation.get_next_meeting_type()
        logger.info(f"Starting {'EMERGENCY' if emergency_data else 'scheduled'} meeting: {meeting_type}")

        try:
            await self._bot.post_system_status(
                f"📅 Meeting starting: **{meeting_type}**"
            )

            # Gather context ------------------------------------------------
            prices = {}
            try:
                prices = await price_feed.get_prices()
            except Exception:
                logger.exception("Failed to fetch prices for meeting context")

            portfolio_summary = ""
            try:
                portfolio_summary = portfolio.get_summary()
            except Exception:
                logger.exception("Failed to get portfolio summary")

            ceo_directives = ceo_handler.format_directives_for_context()
            # Consume the directives so they don't repeat
            ceo_handler.get_pending_directives()

            memory_context = ""
            try:
                memory_context = meeting_memory.get_recent_context()
            except Exception:
                logger.exception("Failed to load memory context")

            # Convert portfolio summary dict to string for context
            portfolio_str = ""
            if portfolio_summary:
                try:
                    import json
                    portfolio_str = json.dumps(portfolio_summary, indent=2)
                except Exception:
                    portfolio_str = str(portfolio_summary)

            # Format price data for context
            price_str = ""
            if prices:
                lines = []
                for sym, data in prices.items():
                    p = data.get("price", 0)
                    c = data.get("change_24h", 0)
                    d = "▲" if c >= 0 else "▼"
                    lines.append(f"{sym}: ${p:,.2f} ({d} {abs(c):.2f}%)")
                price_str = " | ".join(lines)

            # Run the meeting ------------------------------------------------
            await meeting_engine.run_meeting(
                meeting_type_id=meeting_type,
                post_message_fn=self._bot.post_as_agent,
                price_data=price_str,
                portfolio_summary=portfolio_str,
                ceo_directives=ceo_directives,
                memory_context=memory_context,
            )

            await self._bot.post_system_status(
                f"✅ Meeting completed: **{meeting_type}**"
            )
            logger.info(f"Meeting '{meeting_type}' completed successfully")

        except Exception:
            tb = traceback.format_exc()
            logger.exception(f"Meeting '{meeting_type}' failed")
            try:
                await self._bot.post_system_status(
                    f"❌ Meeting **{meeting_type}** failed:\n```\n{tb[-500:]}\n```"
                )
            except Exception:
                logger.exception("Failed to post meeting failure status")

    # ------------------------------------------------------------------
    # Info helpers
    # ------------------------------------------------------------------
    def get_next_meeting_info(self) -> Tuple[str, str]:
        """Return (meeting_type_name, next_fire_time_str) for the next job."""
        if not self._scheduler or not self._scheduler.running:
            return ("unknown", "scheduler not running")

        jobs = self._scheduler.get_jobs()
        if not jobs:
            return ("unknown", "no jobs scheduled")

        # Find the soonest next-fire-time
        soonest = min(jobs, key=lambda j: j.next_run_time)
        next_time = soonest.next_run_time.strftime("%Y-%m-%d %H:%M %Z")

        # Determine meeting type from rotation (deferred import)
        try:
            from bot.meetings import meeting_rotation
            meeting_type = meeting_rotation.peek_next_meeting_type()
        except Exception:
            meeting_type = "scheduled"

        return (meeting_type, next_time)


# Module-level singleton
meeting_scheduler = MeetingScheduler()
