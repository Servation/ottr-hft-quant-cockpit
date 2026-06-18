"""
Agent persona definitions and LLM calling for the discord-bridge service.

Mirrors the AsyncOpenAI + asyncio.Lock serialization pattern from
agent-gateway/app/llm_connector.py, adapted for the Discord meeting context.
"""

from __future__ import annotations
import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Tuple, Optional, Callable

from openai import AsyncOpenAI

from bot import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Project root: discord-bridge/
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Agent Persona dataclass
# ---------------------------------------------------------------------------
@dataclass
class AgentPersona:
    """Defines a single agent's identity and LLM parameters."""

    id: str
    name: str
    emoji: str
    avatar_url: str
    persona_file: str
    temperature: float = field(default=0.7)


# ---------------------------------------------------------------------------
# Registry of all 7 agent personas
# ---------------------------------------------------------------------------
AGENTS: Dict[str, AgentPersona] = {
    "technical_analyst": AgentPersona(
        id="technical_analyst",
        name="Atlas (Technical Analyst)",
        emoji="🔬",
        avatar_url="https://api.dicebear.com/7.x/bottts-neutral/png?seed=technical_analyst",
        persona_file="technical_analyst.txt",
        temperature=0.4,
    ),
    "sentiment_analyst": AgentPersona(
        id="sentiment_analyst",
        name="Luna (Sentiment Analyst)",
        emoji="🧠",
        avatar_url="https://api.dicebear.com/7.x/bottts-neutral/png?seed=sentiment_analyst",
        persona_file="sentiment_analyst.txt",
        temperature=0.6,
    ),
    "trader": AgentPersona(
        id="trader",
        name="Mercury (Trader)",
        emoji="💰",
        avatar_url="https://api.dicebear.com/7.x/bottts-neutral/png?seed=trader",
        persona_file="trader.txt",
        temperature=0.5,
    ),
    "risk_auditor": AgentPersona(
        id="risk_auditor",
        name="Rogue (Risk Auditor)",
        emoji="🛡️",
        avatar_url="https://api.dicebear.com/7.x/bottts-neutral/png?seed=risk_auditor",
        persona_file="risk_auditor.txt",
        temperature=0.3,
    ),
    "performance_optimizer": AgentPersona(
        id="performance_optimizer",
        name="Zephyr (Performance Optimizer)",
        emoji="⚡",
        avatar_url="https://api.dicebear.com/7.x/bottts-neutral/png?seed=performance_optimizer",
        persona_file="performance_optimizer.txt",
        temperature=0.4,
    ),
    "portfolio_manager": AgentPersona(
        id="portfolio_manager",
        name="Midas (Portfolio Manager)",
        emoji="📊",
        avatar_url="https://api.dicebear.com/7.x/bottts-neutral/png?seed=portfolio_manager",
        persona_file="portfolio_manager.txt",
        temperature=0.5,
    ),
    "meeting_chair": AgentPersona(
        id="meeting_chair",
        name="Athena (Meeting Chair)",
        emoji="⚖️",
        avatar_url="https://api.dicebear.com/7.x/bottts-neutral/png?seed=meeting_chair",
        persona_file="meeting_chair.txt",
        temperature=0.2,
    ),
}


# ---------------------------------------------------------------------------
# LLM client with inference serialization
# ---------------------------------------------------------------------------
class AgentLLM:
    """
    Thin wrapper around AsyncOpenAI that:
    - Loads and caches persona system prompts from disk
    - Serializes all inference calls behind an asyncio.Lock (single-GPU safety)
    - Measures per-call latency with time.perf_counter()
    """

    def __init__(self) -> None:
        self._client = AsyncOpenAI(
            base_url=settings["llm_base_url"],
            api_key="lm-studio",
        )
        self._lock: Optional[asyncio.Lock] = None
        self._persona_cache: Dict[str, str] = {}

    @property
    def lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    # -- persona loading ----------------------------------------------------

    def _load_persona(self, agent_id: str) -> str:
        """Read persona .txt from config/personas/ and cache it."""
        if agent_id in self._persona_cache:
            return self._persona_cache[agent_id]

        persona = AGENTS.get(agent_id)
        if persona is None:
            raise ValueError(f"Unknown agent_id: {agent_id}")

        path = PROJECT_ROOT / "config" / "personas" / persona.persona_file
        try:
            text = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            logger.error("Persona file not found: %s", path)
            raise

        self._persona_cache[agent_id] = text
        logger.debug("Loaded persona for %s (%d chars)", agent_id, len(text))
        return text

    async def check_health(self) -> bool:
        """Check if the LLM backend (LM Studio) is reachable."""
        try:
            # Pinging the models endpoint is a standard way to check OpenAI-compatible server health
            await self._client.models.list(timeout=2.0)
            return True
        except Exception as e:
            logger.error(f"LLM health check failed: {e}")
            return False

    # -- inference ----------------------------------------------------------

    async def generate_response(
        self,
        agent_id: str,
        context_messages: list[dict],
        max_tokens: Optional[int] = None,
        tools: Optional[list[dict]] = None,
        tool_handler: Optional[Callable] = None,
    ) -> Tuple[str, float]:
        """
        Generate a chat completion for the given agent.

        Parameters
        ----------
        agent_id : str
            Key into the AGENTS registry.
        context_messages : list[dict]
            Pre-built message list.  The system prompt from the persona file
            will be **prepended** automatically if the first message isn't
            already a system message matching the cached persona.
        max_tokens : int, optional
            Limit the response size. If not specified, loads 'max_response'
            from config settings, defaulting to 300.

        Returns
        -------
        tuple[str, float]
            (response_content, latency_seconds).
            On error returns (error_message, 0.0).
        """
        persona = AGENTS.get(agent_id)
        if persona is None:
            return f"[error] Unknown agent: {agent_id}", 0.0

        # Ensure the system prompt is the first message
        system_prompt = self._load_persona(agent_id)
        messages = list(context_messages)  # shallow copy
        if not messages or messages[0].get("role") != "system":
            messages.insert(0, {"role": "system", "content": system_prompt})

        # Load token budget from settings
        token_limit = max_tokens or settings.get("token_budgets", {}).get("max_response", 300)

        total_latency = 0.0
        final_content = ""

        try:
            async with self.lock:
                # We allow a maximum of 1 tool loop per turn
                for _ in range(2):
                    kwargs = {
                        "model": settings["llm_model_id"],
                        "messages": messages,
                        "temperature": persona.temperature,
                        "max_tokens": token_limit,
                        "timeout": 60.0,
                    }
                    if tools:
                        kwargs["tools"] = tools

                    start = time.perf_counter()
                    response = await self._client.chat.completions.create(**kwargs)
                    latency = time.perf_counter() - start
                    total_latency += latency

                    message = response.choices[0].message

                    if message.tool_calls and tool_handler:
                        # Convert to dict for appending to messages
                        # (Omit None values which sometimes cause issues with LLM endpoints)
                        msg_dict = message.model_dump(exclude_none=True)
                        messages.append(msg_dict)

                        for tool_call in message.tool_calls:
                            import json
                            try:
                                args = json.loads(tool_call.function.arguments)
                            except Exception:
                                args = {}
                            tool_result = await tool_handler(tool_call.function.name, args)

                            messages.append({
                                "role": "tool",
                                "tool_call_id": tool_call.id,
                                "name": tool_call.function.name,
                                "content": str(tool_result)
                            })
                        
                        # Remove tools to force a final text response on the next iteration
                        tools = None
                        continue
                    else:
                        final_content = message.content or ""
                        break

            logger.info(
                "LLM response for %s in %.2fs (%d chars)",
                agent_id,
                total_latency,
                len(final_content),
            )
            return final_content.strip(), total_latency

        except Exception as exc:
            logger.error("LLM call failed for %s: %s", agent_id, exc)
            return f"[error] LLM call failed: {exc}", 0.0




# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
agent_llm = AgentLLM()
