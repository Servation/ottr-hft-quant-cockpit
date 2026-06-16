"""
OTTR Trading Floor — CEO Directive Handler

Captures human messages from the trading-floor channel, queues them as
"CEO directives," and formats them for injection into the next meeting context.
Also features a Live LLM Router to handle immediate questions.
"""

import logging
import asyncio
from datetime import datetime, timezone
from typing import Callable, Awaitable, Optional, Any

import discord

from bot.agents import agent_llm, AGENTS
from bot.price_feed import price_feed
from bot.portfolio import portfolio
from bot.memory import meeting_memory
from bot.scheduler import meeting_scheduler

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
        bot: Any,
    ) -> None:
        """Process an incoming human message from the trading floor."""
        if message.author.bot:
            return

        user_msg = message.content.strip()
        if not user_msg:
            return

        logger.info(f"CEO message received: {user_msg[:80]}...")
        
        # 1. Ask the Router LLM
        agent_names = ", ".join([a.name for a in AGENTS.values()])
        router_prompt = f"""
You are the CEO Handler Router. The CEO just said: "{user_msg}"

Categorize this intent. Return ONLY ONE of the following tags:
- [QUEUE]: It's a general directive, strategy tweak, or order for the next meeting.
- [EMERGENCY]: They want an immediate full team meeting (e.g., "emergency", "meet now").
- [DIRECT:agent_id]: They are asking a specific question best answered by an individual agent or mentioning their name.

Valid agent_ids: {', '.join(AGENTS.keys())}
Valid names: {agent_names}

Tag:"""

        try:
            # We use meeting_chair's persona LLM config for the router, it's fast enough.
            # Using system role to ensure strict parsing.
            tag_response, _ = await agent_llm.generate_response(
                "meeting_chair", 
                [{"role": "user", "content": router_prompt}],
                max_tokens=20
            )
            tag = tag_response.strip().upper()
        except Exception as e:
            logger.error(f"Router LLM failed: {e}")
            tag = "[QUEUE]" # fallback

        # 2. Handle intent
        if "[EMERGENCY]" in tag:
            await message.channel.send("🚨 **Emergency Override Recognized!** Waking up the full team immediately...")
            self._queue_directive(message) # ensure it gets discussed
            meeting_scheduler.schedule_emergency()

        elif "[DIRECT:" in tag:
            # Extract agent_id
            try:
                start = tag.find("[DIRECT:") + 8
                end = tag.find("]", start)
                agent_id = tag[start:end].lower()
                
                if agent_id not in AGENTS:
                    raise ValueError("Invalid agent_id")
                    
                await self._handle_direct_message(message, agent_id, bot)
            except Exception as e:
                logger.error(f"Direct message routing failed: {e}")
                self._queue_directive(message)
                await message.channel.send(f"📋 CEO directive queued. Will address in next meeting.")
                
        else: # [QUEUE] or default
            self._queue_directive(message)
            preview = user_msg[:_TRUNCATE_LEN]
            if len(user_msg) > _TRUNCATE_LEN:
                preview += "…"
            await message.channel.send(f"📋 CEO directive received: *{preview}*. Will address in next meeting.")

    async def _handle_direct_message(self, message: discord.Message, agent_id: str, bot: Any):
        """Fetch context and have the specific agent reply live."""
        agent_name = AGENTS[agent_id].name
        
        # Send a typing indicator or initial acknowledgement
        ack_msg = await message.channel.send(f"*(Pinging {agent_name}...)*")
        
        try:
            # Fetch context
            price_str = await price_feed.get_market_state_summary()
            port_str = portfolio.get_summary()
            memory_str = await meeting_memory.get_semantic_context(message.content, limit=2)
            
            # Fetch recent chat history for follow-up context
            chat_history = []
            async for msg in message.channel.history(limit=6, before=message):
                if msg.content.strip():
                    author_name = "Bot" if msg.author.bot else str(msg.author)
                    chat_history.append(f"{author_name}: {msg.clean_content}")
            chat_history.reverse()
            chat_str = "\n".join(chat_history) if chat_history else "No recent messages."
            
            prompt = f"""
### Recent Chat History (Short-Term Memory)
{chat_str}

### Live Question from CEO
{message.author} just asked you directly: "{message.content}"

### Market State
{price_str}

### Portfolio
{port_str}

### Long-Term Semantic Memory
{memory_str}

Respond directly to the CEO. Keep it concise, helpful, and in character. Reference the short-term chat history if they are asking a follow-up question. Do not use [TRADE] or [ORDER] tags here, just converse.
"""
            response, _ = await agent_llm.generate_response(agent_id, [{"role": "user", "content": prompt}])
            
            # Delete ack message and post the real one
            await ack_msg.delete()
            await bot.post_as_agent(agent_id, response)
            
        except Exception as e:
            logger.error(f"Failed to fetch direct response: {e}")
            await ack_msg.edit(content=f"❌ **Error:** {agent_name} is currently unreachable.")

    def _queue_directive(self, message: discord.Message):
        directive = {
            "content": message.content,
            "author": str(message.author),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "channel_id": message.channel.id,
        }
        self.directive_queue.append(directive)

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
