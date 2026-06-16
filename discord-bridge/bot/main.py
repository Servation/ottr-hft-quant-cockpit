"""
OTTR Trading Floor — Discord Bot Entry Point

Bot lifecycle management, webhook orchestration, and message routing.
"""

import os
import sys
import signal
import asyncio
import logging
import time
from typing import Dict, Optional

import discord
from dotenv import load_dotenv

# Load environment before any bot-internal imports
load_dotenv()

from bot import settings
from bot.agents import AGENTS
from bot.scheduler import meeting_scheduler
from bot.ceo_handler import ceo_handler
from bot.alerts import alert_monitor

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Discord.py internal noise reduction
logging.getLogger("discord").setLevel(logging.WARNING)
logging.getLogger("discord.http").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DISCORD_MSG_LIMIT = 2000
WEBHOOK_POST_DELAY = 2.0  # seconds between webhook posts to avoid rate-limits


class TradingFloorBot(discord.Client):
    """Manages the Discord presence for the OTTR trading floor."""

    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.guild_messages = True
        super().__init__(intents=intents)

        self.webhooks: Dict[str, discord.Webhook] = {}
        self._trading_floor_channel: Optional[discord.TextChannel] = None
        self._system_status_channel: Optional[discord.TextChannel] = None
        self._last_webhook_post: float = 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def setup_hook(self) -> None:
        """Called once when the bot is starting up, before on_ready."""
        logger.info("Running setup_hook — initializing scheduler...")

    async def on_ready(self) -> None:
        logger.info(f"Logged in as {self.user} (ID: {self.user.id})")

        # Resolve channels
        trading_floor_id = settings.get("discord_trading_floor_channel_id")
        system_status_id = settings.get("discord_system_status_channel_id")

        if not trading_floor_id or not system_status_id:
            logger.error("Channel IDs not configured. Check settings / .env")
            await self.close()
            return

        self._trading_floor_channel = self.get_channel(int(trading_floor_id))
        self._system_status_channel = self.get_channel(int(system_status_id))

        if not self._trading_floor_channel:
            logger.error(f"Trading floor channel {trading_floor_id} not found")
            await self.close()
            return

        if not self._system_status_channel:
            logger.warning(
                f"System status channel {system_status_id} not found — "
                "status messages will go to trading floor"
            )
            self._system_status_channel = self._trading_floor_channel

        # Set up agent webhooks
        await self._setup_webhooks(self._trading_floor_channel)

        # Startup announcements
        await self._trading_floor_channel.send(
            "```\n"
            "╔══════════════════════════════════════════╗\n"
            "║   🦦 OTTR Trading Floor — ONLINE        ║\n"
            "║   Agents syncing… meetings scheduled.    ║\n"
            "╚══════════════════════════════════════════╝\n"
            "```"
        )
        await self.post_system_status(
            f"✅ Bot online. {len(self.webhooks)}/{len(AGENTS)} agent webhooks ready."
        )

        # Start background services
        await meeting_scheduler.start(self)
        self.loop.create_task(alert_monitor.start(self))

        # Fire a startup meeting after a short delay (lets webhooks settle)
        self.loop.create_task(self._startup_meeting())

        logger.info("on_ready complete — bot is fully operational")

    async def _startup_meeting(self) -> None:
        """Run an initial Morning Briefing shortly after boot."""
        await asyncio.sleep(10)  # let everything settle
        
        import os, time
        last_meeting_file = "data/last_startup_meeting.txt"
        try:
            if os.path.exists(last_meeting_file):
                with open(last_meeting_file, "r") as f:
                    last_time = float(f.read().strip())
                if time.time() - last_time < 3600:
                    logger.info("Skipping startup meeting (last startup was < 60 mins ago).")
                    return
        except Exception:
            pass

        logger.info("Triggering startup meeting...")
        try:
            await self._trading_floor_channel.send(
                "☀️ **Startup Briefing** — The floor is open. "
                "Let's get a read on the market."
            )
            await meeting_scheduler._execute_meeting(emergency_data=None)
            
            try:
                os.makedirs("data", exist_ok=True)
                with open(last_meeting_file, "w") as f:
                    f.write(str(time.time()))
            except Exception:
                pass
        except Exception:
            logger.exception("Startup meeting failed")

    # ------------------------------------------------------------------
    # Webhook management
    # ------------------------------------------------------------------
    async def _setup_webhooks(self, channel: discord.TextChannel) -> None:
        """Create or reuse a webhook for every registered agent."""
        existing_webhooks = await channel.webhooks()

        for agent_id, persona in AGENTS.items():
            # Look for an existing webhook whose name matches the agent
            webhook = discord.utils.get(existing_webhooks, name=persona.name)

            if webhook is None:
                try:
                    webhook = await channel.create_webhook(name=persona.name)
                    logger.info(f"Created webhook for agent '{persona.name}'")
                except discord.HTTPException as exc:
                    logger.error(
                        f"Failed to create webhook for '{persona.name}': {exc}"
                    )
                    continue

            self.webhooks[agent_id] = webhook
            logger.info(f"Webhook ready: {persona.name} (id={webhook.id})")

    # ------------------------------------------------------------------
    # Posting helpers
    # ------------------------------------------------------------------
    async def post_as_agent(self, agent_id: str, content: str) -> None:
        """Post a message as a specific agent via its webhook.

        Enforces a minimum delay between posts and automatically splits
        messages that exceed Discord's 2000-char limit.
        """
        webhook = self.webhooks.get(agent_id)
        if webhook is None:
            logger.warning(f"No webhook for agent '{agent_id}' — falling back to channel send")
            if self._trading_floor_channel:
                persona = AGENTS.get(agent_id)
                agent_name = persona.name if persona else agent_id
                await self._trading_floor_channel.send(f"**[{agent_name}]** {content}")
            return

        agent_persona = AGENTS.get(agent_id)
        avatar_url = agent_persona.avatar_url if agent_persona else None
        display_name = agent_persona.name if agent_persona else agent_id

        # Rate-limit enforcement
        elapsed = time.monotonic() - self._last_webhook_post
        if elapsed < WEBHOOK_POST_DELAY:
            await asyncio.sleep(WEBHOOK_POST_DELAY - elapsed)

        # Split oversized messages
        chunks = _split_message(content, DISCORD_MSG_LIMIT)
        for chunk in chunks:
            try:
                await webhook.send(
                    content=chunk,
                    username=display_name,
                    avatar_url=avatar_url,
                )
                self._last_webhook_post = time.monotonic()
                # Small delay between multi-part chunks
                if len(chunks) > 1:
                    await asyncio.sleep(0.5)
            except discord.HTTPException as exc:
                logger.error(f"Webhook send failed for '{agent_id}': {exc}")

    async def post_system_status(self, message: str) -> None:
        """Post an operational message to the system-status channel."""
        channel = self._system_status_channel or self._trading_floor_channel
        if channel is None:
            logger.warning("No channel available for system status message")
            return
        try:
            await channel.send(f"🔧 **System** | {message}")
        except discord.HTTPException as exc:
            logger.error(f"System status post failed: {exc}")

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------
    async def on_message(self, message: discord.Message) -> None:
        # Ignore bots (including ourselves) and DMs
        if message.author.bot or message.guild is None:
            return

        # Only process messages in the trading floor channel
        if (
            self._trading_floor_channel
            and message.channel.id == self._trading_floor_channel.id
        ):
            await ceo_handler.on_message(message, bot=self)

    async def on_error(self, event: str, *args, **kwargs) -> None:
        logger.exception(f"Unhandled error in event '{event}'")
        try:
            await self.post_system_status(
                f"⚠️ Unhandled error in `{event}` — check logs."
            )
        except Exception:
            # If we can't even post the error, just log it
            logger.exception("Failed to post error notification")


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------
def _split_message(text: str, limit: int = DISCORD_MSG_LIMIT) -> list[str]:
    """Split a message into chunks that fit within Discord's character limit.

    Tries to split on newlines first, then falls back to hard splits.
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break

        # Prefer splitting at the last newline within the limit
        split_idx = text.rfind("\n", 0, limit)
        if split_idx == -1 or split_idx < limit // 2:
            # No good newline — try a space
            split_idx = text.rfind(" ", 0, limit)
        if split_idx == -1 or split_idx < limit // 2:
            # Hard split as last resort
            split_idx = limit

        chunks.append(text[:split_idx])
        text = text[split_idx:].lstrip("\n")

    return chunks


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------
async def _shutdown(bot: TradingFloorBot) -> None:
    """Shut down background services and disconnect the bot."""
    logger.info("Shutting down...")
    try:
        await alert_monitor.stop()
    except Exception:
        logger.exception("Error stopping alert monitor")
    try:
        await meeting_scheduler.stop()
    except Exception:
        logger.exception("Error stopping scheduler")
    try:
        await bot.close()
    except Exception:
        logger.exception("Error closing bot")
    logger.info("Shutdown complete.")


# ---------------------------------------------------------------------------
# Entry-point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    load_dotenv()

    token = settings.get("discord_bot_token")
    if not token:
        logger.critical("DISCORD_BOT_TOKEN is not set. Exiting.")
        sys.exit(1)

    bot = TradingFloorBot()

    # Register OS signal handlers for graceful shutdown
    loop = asyncio.new_event_loop()

    def _handle_signal() -> None:
        loop.create_task(_shutdown(bot))

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            signal.signal(sig, lambda s, f: loop.create_task(_shutdown(bot)))

    logger.info("Starting OTTR Trading Floor Bot...")
    bot.run(token, log_handler=None)  # log_handler=None — we own the logging config
