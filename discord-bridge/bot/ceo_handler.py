"""
OTTR Trading Floor — CEO Directive Handler

Captures human messages from the trading-floor channel, queues them as
"CEO directives," and formats them for injection into the next meeting context.
"""

import logging
from datetime import datetime, timezone
from typing import Callable, Awaitable, Optional

import discord

logger = logging.getLogger(__name__)

# Maximum characters shown in the acknowledgement reply
_TRUNCATE_LEN = 120


class CEOHandler:
    """Collects and surfaces human directives for agent meetings."""

    def __init__(self) -> None:
        self.directive_queue: list[dict] = []

    # ------------------------------------------------------------------
    # Ingest
    # ------------------------------------------------------------------
    async def on_message(
        self,
        message: discord.Message,
        reply_fn: Callable[..., Awaitable[discord.Message]],
    ) -> None:
        """Process an incoming human message from the trading floor.

        Parameters
        ----------
        message:
            The Discord message object (already validated as non-bot,
            correct channel by the caller).
        reply_fn:
            An async callable used to send an acknowledgement back to
            the channel (typically ``channel.send``).
        """
        if message.author.bot:
            return

        directive = {
            "content": message.content,
            "author": str(message.author),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "channel_id": message.channel.id,
        }
        self.directive_queue.append(directive)
        logger.info(
            f"CEO directive queued from {directive['author']}: "
            f"{message.content[:80]}..."
        )

        # Acknowledge in-channel
        preview = message.content[:_TRUNCATE_LEN]
        if len(message.content) > _TRUNCATE_LEN:
            preview += "…"

        try:
            await reply_fn(
                f"📋 CEO directive received: *{preview}*. "
                "Will address in next meeting."
            )
        except discord.HTTPException as exc:
            logger.error(f"Failed to acknowledge directive: {exc}")

    # ------------------------------------------------------------------
    # Query interface (used by scheduler / meeting engine)
    # ------------------------------------------------------------------
    def get_pending_directives(self) -> list[dict]:
        """Return a copy of all pending directives and clear the queue."""
        pending = list(self.directive_queue)
        self.directive_queue.clear()
        return pending

    def has_pending(self) -> bool:
        """Return True if there are unprocessed directives."""
        return len(self.directive_queue) > 0

    def format_directives_for_context(self) -> str:
        """Format pending directives as a text block for agent context.

        Returns an empty string when there are no directives.
        """
        if not self.directive_queue:
            return ""

        lines = ["=== CEO DIRECTIVES (address these in this meeting) ==="]
        for i, d in enumerate(self.directive_queue, start=1):
            ts = d["timestamp"]
            author = d["author"]
            content = d["content"]
            lines.append(f"  [{i}] ({ts}) {author}: {content}")
        lines.append("=== END CEO DIRECTIVES ===")
        return "\n".join(lines)


# Module-level singleton
ceo_handler = CEOHandler()
