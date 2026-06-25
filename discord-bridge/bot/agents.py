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


# Backoff before retrying an empty (no-side-effect) LLM response. Brief, so it
# doesn't slow a healthy meeting, but enough for a momentarily-busy backend to settle.
_EMPTY_RETRY_BACKOFF_SEC = 0.8

# Shared desk rules prepended to every agent's system prompt at inference time.
# Kept here (not in the persona files) so it sits ABOVE the strippable OUTPUT FORMAT
# section and applies uniformly. The regime rule converts the backtested edge —
# trend signals only pay in a trending regime — into the agents' live behavior; an
# A/B probe showed it flips a choppy-regime call from "SELL @0.75" to "ABSTAIN @0.35".
_DESK_RULES = (
    "TRADING DESK RULES (apply before any call):\n"
    "- REGIME: each asset shows Regime: TRENDING or CHOPPY (efficiency ratio). Trend/"
    "momentum indicators (EMA, MACD) and the deterministic signals are reliable ONLY in a "
    "TRENDING regime. In a CHOPPY regime they are noise — do NOT issue a high-conviction "
    "BUY/SELL off them; prefer HOLD/ABSTAIN or a low confidence score. Act decisively only "
    "when the regime is TRENDING and the signals agree."
)


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
        print("AgentLLM initializing with base_url:", settings["llm_base_url"])
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
        strip_output_format: bool = False,
        _allow_retry: bool = True,
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
        if strip_output_format:
            import re
            system_prompt = re.sub(r"OUTPUT FORMAT.*", "", system_prompt, flags=re.IGNORECASE | re.DOTALL).strip()

        # Prepend shared desk rules (regime awareness) above the persona — applied
        # uniformly, and ahead of the strippable OUTPUT FORMAT section so it survives.
        system_prompt = _DESK_RULES + "\n\n" + system_prompt

        messages = list(context_messages)  # shallow copy
        if not messages or messages[0].get("role") != "system":
            messages.insert(0, {"role": "system", "content": system_prompt})

        # Load token budget from settings
        token_limit = max_tokens or settings.get("token_budgets", {}).get("max_response", 300)

        total_latency = 0.0
        final_content = ""

        # Track tool calls already executed this turn so a model that repeats
        # the same call (e.g. a local model re-leaking a raw <|tool_call> tag on
        # the next loop iteration) cannot execute the same side-effecting tool twice.
        executed_tool_signatures: set[str] = set()

        def _tool_signature(name: str, args: dict) -> str:
            import json as _json
            try:
                return name + ":" + _json.dumps(args, sort_keys=True, default=str)
            except Exception:
                return name + ":" + str(args)

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

                            sig = _tool_signature(tool_call.function.name, args)
                            if sig in executed_tool_signatures:
                                logger.warning(
                                    "Skipping duplicate tool call %s (already executed this turn)",
                                    tool_call.function.name,
                                )
                                tool_result = f"[skipped] {tool_call.function.name} was already executed in this turn."
                            else:
                                executed_tool_signatures.add(sig)
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
                        
                        # Fallback for Gemma leaking raw tool calls
                        if tool_handler and "<|tool_call>" in final_content:
                            import re, json
                            raw_calls = re.findall(r"<\|?tool_call\|?>call:([a-zA-Z0-9_]+)(\{.*?\})<\|?/?tool_call\|?>", final_content)
                            if raw_calls:
                                messages.append({"role": "assistant", "content": final_content})
                                for func_name, raw_args in raw_calls:
                                    clean_args = raw_args.replace('<|"|>', '"')
                                    clean_args = re.sub(r'([{,]\s*)([a-zA-Z0-9_]+)\s*:', r'\1"\2":', clean_args)
                                    try:
                                        args = json.loads(clean_args)
                                    except Exception:
                                        args = {}

                                    sig = _tool_signature(func_name, args)
                                    if sig in executed_tool_signatures:
                                        logger.warning(
                                            "Skipping duplicate raw tool call %s (already executed this turn)",
                                            func_name,
                                        )
                                        tool_result = f"[skipped] {func_name} was already executed in this turn."
                                    else:
                                        executed_tool_signatures.add(sig)
                                        tool_result = await tool_handler(func_name, args)
                                    messages.append({
                                        "role": "tool",
                                        "name": func_name,
                                        "content": str(tool_result),
                                        "tool_call_id": "call_" + func_name
                                    })

                                # Strip it from final content so it doesn't leak to Discord
                                final_content = re.sub(r"<\|?tool_call\|?>.*?<\|?/?tool_call\|?>", "", final_content, flags=re.DOTALL)
                                tools = None
                                continue

                        break

            # Clean up known reasoning tags that some models might leak
            import re
            # Log the raw content so we can debug if it leaks again
            logger.debug(f"Raw LLM response before cleaning: {repr(final_content)}")

            # Remove DeepSeek <think>...</think>
            final_content = re.sub(r"<think>.*?</think>\n?", "", final_content, flags=re.DOTALL | re.IGNORECASE)

            # Remove <|channel>thought ... <channel|>-
            final_content = re.sub(r"<\|?channel\|?>\s*thought.*?<\|?channel\|?>-?\n?", "", final_content, flags=re.DOTALL | re.IGNORECASE)

            # Catch-all for standalone <|channel> tags that might still be lingering at the very start
            final_content = re.sub(r"^\s*<\|?channel\|?>\s*thought\s*", "", final_content, flags=re.IGNORECASE)
            final_content = re.sub(r"^\s*<\|?channel\|?>-?\s*", "", final_content, flags=re.IGNORECASE)

            final_content = final_content.strip()

            # Retry once on an EMPTY response that had no side effects. Local LLMs
            # occasionally return an empty completion (backend momentarily busy /
            # loading); without this an agent's whole turn — and its vote — is lost.
            # Guarded to no-tool turns so a retry can never re-execute a trade, and to
            # one attempt so a persistently-empty model can't loop.
            if _allow_retry and not final_content and not executed_tool_signatures:
                logger.warning(
                    "Empty LLM response for %s; retrying once after %.1fs backoff",
                    agent_id, _EMPTY_RETRY_BACKOFF_SEC,
                )
                await asyncio.sleep(_EMPTY_RETRY_BACKOFF_SEC)
                return await self.generate_response(
                    agent_id, context_messages, max_tokens=max_tokens, tools=tools,
                    tool_handler=tool_handler, strip_output_format=strip_output_format,
                    _allow_retry=False,
                )

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
