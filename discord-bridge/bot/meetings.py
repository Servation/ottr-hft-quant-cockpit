"""
Meeting types, turn sequencing, and rotation logic for agent meetings.

Each meeting type defines a facilitator, participant order, opening prompt,
and focus area.  The MeetingEngine orchestrates a full round-table by
calling agent LLM completions in sequence and posting messages via a
caller-supplied async callback.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Awaitable, Dict, List, Optional

from bot.agents import AGENTS, agent_llm
from bot.memory import meeting_memory, MeetingMemory

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ROTATION_STATE_PATH = PROJECT_ROOT / "data" / "rotation_state.json"


# ---------------------------------------------------------------------------
# Meeting type definitions
# ---------------------------------------------------------------------------
ALL_AGENT_IDS: List[str] = list(AGENTS.keys())


@dataclass
class MeetingType:
    """Describes a kind of agent meeting."""

    id: str
    name: str
    emoji: str
    facilitator_id: str
    participant_ids: List[str] = field(default_factory=list)
    opening_prompt: str = ""
    focus: str = ""


MEETING_TYPES: Dict[str, MeetingType] = {
    "morning_briefing": MeetingType(
        id="morning_briefing",
        name="Morning Briefing",
        emoji="🌅",
        facilitator_id="meeting_chair",
        participant_ids=ALL_AGENT_IDS,
        opening_prompt=(
            "Good morning team. Let's review overnight price action, "
            "any triggered alerts, and set today's priorities."
        ),
        focus="Overnight review and daily priority setting",
    ),
    "strategy_session": MeetingType(
        id="strategy_session",
        name="Strategy Session",
        emoji="♟️",
        facilitator_id="meeting_chair",
        participant_ids=ALL_AGENT_IDS,
        opening_prompt=(
            "Strategy session open. We need to debate current allocation "
            "weights, discuss any rebalancing opportunities, and align on "
            "the macro thesis."
        ),
        focus="Allocation debates and macro thesis alignment",
    ),
    "risk_review": MeetingType(
        id="risk_review",
        name="Risk Review",
        emoji="⚠️",
        facilitator_id="meeting_chair",
        participant_ids=[
            "risk_auditor",
            "portfolio_manager",
            "trader",
            "technical_analyst",
        ],
        opening_prompt=(
            "Risk review convened. Current drawdown, exposure limits, "
            "and any compliance concerns are on the table."
        ),
        focus="Exposure audit, drawdown check, compliance review",
    ),
    "trade_execution": MeetingType(
        id="trade_execution",
        name="Trade Execution",
        emoji="⚡",
        facilitator_id="meeting_chair",
        participant_ids=[
            "trader",
            "technical_analyst",
            "sentiment_analyst",
            "risk_auditor",
            "portfolio_manager",
        ],
        opening_prompt=(
            "Trade execution meeting. We have a potential setup — let's "
            "evaluate the entry, size the position, and get risk clearance."
        ),
        focus="Position sizing, entry/exit, execution path selection",
    ),
    "performance_retrospective": MeetingType(
        id="performance_retrospective",
        name="Performance Retrospective",
        emoji="📈",
        facilitator_id="meeting_chair",
        participant_ids=ALL_AGENT_IDS,
        opening_prompt=(
            "Performance retro is open. Let's review recent trade outcomes, "
            "win/loss metrics, and discuss parameter adjustments."
        ),
        focus="Trade history review, parameter tuning proposals",
    ),
    "altcoin_scouting": MeetingType(
        id="altcoin_scouting",
        name="Altcoin Scouting",
        emoji="🔭",
        facilitator_id="meeting_chair",
        participant_ids=[
            "altcoin_screener",
            "technical_analyst",
            "risk_auditor",
            "portfolio_manager",
        ],
        opening_prompt=(
            "Altcoin scouting session. Let's scan for alpha — any sectors "
            "heating up, breakout setups, or new narratives worth tracking?"
        ),
        focus="Altcoin opportunity scanning and watchlist updates",
    ),
    "emergency_alert": MeetingType(
        id="emergency_alert",
        name="Emergency Alert",
        emoji="🚨",
        facilitator_id="meeting_chair",
        participant_ids=[
            "risk_auditor",
            "trader",
            "technical_analyst",
            "portfolio_manager",
        ],
        opening_prompt=(
            "EMERGENCY ALERT. A significant price move or risk threshold "
            "breach has been detected. Assessing impact and immediate actions."
        ),
        focus="Rapid risk assessment and emergency response",
    ),
}


# ---------------------------------------------------------------------------
# Meeting engine
# ---------------------------------------------------------------------------

# Type alias for the message-posting callback
PostMessageFn = Callable[[str, str], Awaitable[None]]


class MeetingEngine:
    """
    Orchestrates a full agent meeting with real debate:

    1. Facilitator opens with context
    2. Each participant gives their initial take (Round 1)
    3. Debate round: agents who disagree push back on each other (Round 2)
    4. Facilitator closes with summary, decisions, action items
    5. Meeting record is persisted to memory
    """

    # Agents most likely to clash — used to pick debate participants
    _NATURAL_TENSIONS = {
        ("trader", "risk_auditor"),        # aggression vs caution
        ("altcoin_screener", "risk_auditor"),  # opportunity vs risk
        ("technical_analyst", "sentiment_analyst"),  # data vs narrative
        ("trader", "portfolio_manager"),    # action vs balance
        ("performance_optimizer", "trader"),  # hindsight vs execution
    }

    async def run_meeting(
        self,
        meeting_type_id: str,
        post_message_fn: PostMessageFn,
        price_data: str = "",
        portfolio_summary: str = "",
        ceo_directives: str = "",
        memory_context: str = "",
    ) -> dict:
        """
        Run a complete meeting with debate and return the meeting record.
        """
        mt = MEETING_TYPES.get(meeting_type_id)
        if mt is None:
            raise ValueError(f"Unknown meeting type: {meeting_type_id}")

        logger.info("Starting meeting: %s (%s)", mt.name, mt.id)

        if not memory_context:
            query_text = price_data or "general market state"
            try:
                from bot import settings
                budget = settings.get("token_budgets", {}).get("meeting_history", 500)
                similar = meeting_memory.query_similar_meetings(query_text, n=3)
                if similar:
                    lines = []
                    current_words = 0
                    for m in similar:
                        ts = m.get("timestamp", "?")
                        mtype = m.get("type", "?")
                        summary = m.get("summary", "—")
                        formatted = f"• [{ts}] {mtype} — {summary}"
                        
                        # Approximate token counts via word count
                        words = formatted.split()
                        if current_words + len(words) > budget:
                            if not lines:
                                # Truncate the first meeting to fit the budget
                                allowed_words = max(1, budget - current_words)
                                truncated_formatted = " ".join(words[:allowed_words])
                                lines.append(truncated_formatted)
                            break
                        lines.append(formatted)
                        current_words += len(words)
                    memory_context = "\n".join(lines) or "No prior meetings on record."
                else:
                    memory_context = "No prior meetings on record."
            except Exception:
                logger.exception("Failed to query similar meetings")
                memory_context = "No prior meetings on record."

        # ---- 1. Facilitator opening message --------------------------------
        opening_msg = self._build_opening(mt, price_data, portfolio_summary, ceo_directives)
        await post_message_fn(mt.facilitator_id, opening_msg)
        conversation_log: List[str] = [
            f"[{AGENTS[mt.facilitator_id].emoji} {AGENTS[mt.facilitator_id].name}]: {opening_msg}"
        ]

        # ---- 2. Round 1: Initial takes -------------------------------------
        agent_contributions: Dict[str, str] = {}
        non_facilitator_ids = [
            pid for pid in mt.participant_ids if pid != mt.facilitator_id
        ]

        for agent_id in non_facilitator_ids:
            context = self._build_agent_context(
                agent_id, mt, conversation_log,
                price_data, portfolio_summary, ceo_directives, memory_context,
                is_debate_round=False,
            )
            response, latency = await agent_llm.generate_response(agent_id, context)
            agent_contributions[agent_id] = response

            await post_message_fn(agent_id, response)
            conversation_log.append(
                f"[{AGENTS[agent_id].emoji} {AGENTS[agent_id].name}]: {response}"
            )
            logger.debug("%s responded in %.2fs", agent_id, latency)

        # ---- 3. Debate round: pushback & challenges ------------------------
        debate_agents = self._pick_debate_agents(non_facilitator_ids, agent_contributions)
        if debate_agents:
            import random
            random.shuffle(debate_agents)
            # Post a separator to signal the debate
            await post_message_fn(
                mt.facilitator_id,
                "📢 **Open floor** — I'm hearing some tension. Let's hash it out."
            )
            conversation_log.append(
                f"[{AGENTS[mt.facilitator_id].emoji} {AGENTS[mt.facilitator_id].name}]: "
                "Open floor — let's hash out the disagreements."
            )

            for agent_id in debate_agents:
                context = self._build_agent_context(
                    agent_id, mt, conversation_log,
                    price_data, portfolio_summary, ceo_directives, memory_context,
                    is_debate_round=True,
                )
                response, latency = await agent_llm.generate_response(agent_id, context)
                agent_contributions[agent_id] += f"\n\n[DEBATE]: {response}"

                await post_message_fn(agent_id, response)
                conversation_log.append(
                    f"[{AGENTS[agent_id].emoji} {AGENTS[agent_id].name}]: {response}"
                )
                logger.debug("%s (debate) responded in %.2fs", agent_id, latency)

        # ---- 4. Facilitator closing summary --------------------------------
        closing_msg, _ = await self._build_closing(
            mt, conversation_log, price_data, portfolio_summary,
        )
        agent_contributions[mt.facilitator_id] = closing_msg
        await post_message_fn(mt.facilitator_id, closing_msg)

        # ---- 4.5 Parse and execute directives from closing message -------
        try:
            await self._parse_and_execute_directives(closing_msg, post_message_fn)
        except Exception:
            logger.exception("Failed to parse and execute directives")

        # ---- 5. Persist meeting record -------------------------------------
        meeting_record = MeetingMemory.make_meeting_record(
            meeting_type=mt.id,
            summary=closing_msg[:300],
            agent_contributions=agent_contributions,
            decisions=self._extract_decisions(closing_msg),
            actions=self._extract_actions(closing_msg),
        )
        await meeting_memory.save_meeting(meeting_record)

        logger.info("Meeting %s completed (%s).", mt.name, meeting_record["id"])
        return meeting_record

    async def _parse_and_execute_directives(
        self,
        closing_msg: str,
        post_message_fn: PostMessageFn,
    ) -> None:
        """Parse structured tags [TRADE: ...] and [PARAM: ...] and apply them."""
        import re
        from bot.portfolio import portfolio
        from bot.price_feed import price_feed

        # 1. Parse parameters: [PARAM: min_trade_usd=150.0]
        param_matches = re.findall(r"\[PARAM:\s*([a-zA-Z0-9_]+)\s*=\s*([0-9.]+)\]", closing_msg)
        for param_name, param_val in param_matches:
            if param_name == "min_trade_usd":
                try:
                    val = float(param_val)
                    if 10.0 <= val <= 1000.0:
                        portfolio.min_trade_usd = val
                        await post_message_fn(
                            "portfolio_manager",
                            f"⚙️ **Parameter Updated:** `min_trade_usd` set to **${val:.2f}**"
                        )
                        logger.info("Updated min_trade_usd parameter to %f", val)
                    else:
                        await post_message_fn(
                            "portfolio_manager",
                            f"⚠️ **Parameter Update Rejected:** `min_trade_usd` value **${val:.2f}** is out of bounds ($10 to $1,000)."
                        )
                except ValueError:
                    logger.exception("Failed to parse param value: %s", param_val)

        # 2. Parse trades: [TRADE: BUY BTC 500] or [TRADE: SELL BTC 0.15]
        trade_matches = re.findall(
            r"\[TRADE:\s*(BUY|SELL)\s+([a-zA-Z0-9]+)\s+([0-9.]+)\]",
            closing_msg,
            re.IGNORECASE
        )
        for action, asset, amount_str in trade_matches:
            action = action.upper()
            asset = asset.upper()
            try:
                amount = float(amount_str)
                # Fetch latest price
                prices = await price_feed.get_prices()
                asset_data = prices.get(asset)
                if not asset_data:
                    raise ValueError(f"Asset price for {asset} not found in price feed")
                price = float(asset_data["price"])

                if action == "BUY":
                    trade = portfolio.buy(asset, amount, price)
                    await post_message_fn(
                        "trader",
                        f"💰 **Trade Executed:** **BUY** {trade['quantity']:.8f} {asset} @ **${trade['fill_price']:,.2f}** "
                        f"(Value: **${trade['usd_amount']:,.2f}**)"
                    )
                elif action == "SELL":
                    trade = portfolio.sell(asset, amount, price)
                    await post_message_fn(
                        "trader",
                        f"💰 **Trade Executed:** **SELL** {trade['quantity']:.8f} {asset} @ **${trade['fill_price']:,.2f}** "
                        f"(Proceeds: **${trade['usd_amount']:,.2f}**)"
                    )
            except Exception as e:
                logger.exception("Trade execution failed")
                await post_message_fn(
                    "trader",
                    f"❌ **Trade Execution Failed:** {str(e)}"
                )

        # 3. Parse orders: [ORDER: LIMIT BUY BTC 500 @ 60000]
        order_matches = re.findall(
            r"\[ORDER:\s*(LIMIT|STOP|TAKE_PROFIT)\s+(BUY|SELL)\s+([a-zA-Z0-9]+)\s+([0-9.]+)\s*@\s*([0-9.]+)\]",
            closing_msg,
            re.IGNORECASE
        )
        for order_type, action, asset, amount_str, price_str in order_matches:
            try:
                amount = float(amount_str)
                price = float(price_str)
                order_id = portfolio.place_order(order_type, action, asset, amount, price)
                await post_message_fn(
                    "portfolio_manager",
                    f"📝 **Order Placed:** **{order_type.upper()} {action.upper()}** {amount} {asset.upper()} @ **${price:,.2f}** (ID: {order_id})"
                )
            except Exception as e:
                logger.exception("Order placement failed")
                await post_message_fn("portfolio_manager", f"❌ **Order Placement Failed:** {str(e)}")

        # 4. Parse [CANCEL: ALL <ASSET>]
        cancel_matches = re.findall(r"\[CANCEL:\s*ALL\s+([a-zA-Z0-9]+)\]", closing_msg, re.IGNORECASE)
        for asset in cancel_matches:
            count = portfolio.cancel_all_orders(asset)
            if count > 0:
                await post_message_fn(
                    "portfolio_manager",
                    f"🗑️ **Orders Canceled:** Canceled {count} pending orders for **{asset.upper()}**."
                )

        # 5. Parse [SCHEDULE_MEETING: <MINUTES>]
        schedule_matches = re.findall(r"\[SCHEDULE_MEETING:\s*([0-9]+)\]", closing_msg, re.IGNORECASE)
        for mins_str in schedule_matches:
            try:
                mins = int(mins_str)
                from bot.scheduler import meeting_scheduler
                meeting_scheduler.schedule_dynamic_meeting(mins)
                await post_message_fn(
                    "meeting_chair",
                    f"⏱️ **Dynamic Meeting Scheduled:** We will reconvene in **{mins}** minutes."
                )
            except Exception as e:
                logger.exception("Failed to schedule dynamic meeting")

        # 3. Always print updated running totals
        try:
            prices = await price_feed.get_prices()
            cash = portfolio._state["cash"]
            total_val = portfolio.get_total_value(prices)
            pnl = portfolio._state["total_pnl"]
            min_trade = portfolio.min_trade_usd

            lines = [
                "📊 **Portfolio Running Totals:**",
                f"• **Cash:** ${cash:,.2f}",
                "• **Holdings:**"
            ]
            for sym in portfolio._state["holdings"]:
                qty = portfolio._state["holdings"][sym]["quantity"]
                sym_price = prices.get(sym, {}).get("price", 0.0)
                val = qty * sym_price
                lines.append(f"  - **{sym}:** {qty:.6f} (${val:,.2f})")
            lines.append(f"• **Total Value:** ${total_val:,.2f} (P&L: ${pnl:,.2f})")
            lines.append(f"• **Current Min Trade Limit:** ${min_trade:,.2f}")

            await post_message_fn("portfolio_manager", "\n".join(lines))
        except Exception:
            logger.exception("Failed to post portfolio totals")

    def _pick_debate_agents(
        self,
        participant_ids: List[str],
        contributions: Dict[str, str],
    ) -> List[str]:
        """Return all non-facilitator participants so everyone can participate in the debate."""
        return list(participant_ids)

    # -- context building ---------------------------------------------------

    def _build_opening(
        self,
        mt: MeetingType,
        price_data: str,
        portfolio_summary: str,
        ceo_directives: str,
    ) -> str:
        """Compose the facilitator's opening message."""
        parts = [
            f"{AGENTS[mt.facilitator_id].emoji} **{mt.name}**",
            "",
            mt.opening_prompt,
            "",
            f"**Focus:** {mt.focus}",
        ]
        if price_data:
            parts += ["", f"**Market State:**\n{price_data}"]
        if portfolio_summary:
            parts += ["", f"**Portfolio Snapshot:**\n{portfolio_summary}"]
        if ceo_directives:
            parts += ["", f"**CEO Directives:**\n{ceo_directives}"]
        return "\n".join(parts)

    def _build_agent_context(
        self,
        agent_id: str,
        meeting_type: MeetingType,
        conversation_log: List[str],
        price_data: str,
        portfolio_summary: str,
        ceo_directives: str,
        memory_context: str,
        is_debate_round: bool = False,
    ) -> list[dict]:
        """
        Build the chat-completion messages list for a single agent turn.

        Keeps total context concise to stay within a ~4 K token budget.
        The prompt explicitly instructs agents to engage with and challenge
        what others have said rather than delivering isolated monologues.
        """
        # Truncate conversation log to last 8 entries to stay within budget
        recent_convo = conversation_log[-8:]
        convo_text = "\n\n".join(recent_convo)

        user_content_parts = [
            f"## {meeting_type.name} — {meeting_type.focus}",
        ]
        if price_data:
            user_content_parts.append(
                "### Market Data Definitions\n"
                "- **Vol (Volatility):** 14-day annualized historical volatility. High volatility means wide price swings.\n"
                "- **Fund (Funding Rate):** Perpetual futures funding rate. Positive means longs pay shorts (bullish sentiment, potential long squeeze). Negative means shorts pay longs (bearish sentiment, short squeeze).\n"
                "- **Correlation:** Pearson correlation (-1.0 to 1.0) between BTC and ETH. 1.0 means they move perfectly together.\n"
                "- **Yield:** Average APY of top stablecoin DeFi pools (risk-free baseline rate)."
            )
            user_content_parts.append(f"### Market State\n{price_data}")
        if portfolio_summary:
            user_content_parts.append(f"### Portfolio\n{portfolio_summary}")
        if ceo_directives:
            user_content_parts.append(f"### CEO Directives\n{ceo_directives}")
        if memory_context:
            user_content_parts.append(f"### Recent Meeting History\n{memory_context}")

        user_content_parts.append(
            "### Available Tools\n"
            "You can recommend the following actions to the Meeting Chair:\n"
            "- **Market Orders**: Buy or sell at the current price.\n"
            "- **Limit/Stop/Take-Profit Orders**: Set defensive bounds or target prices.\n"
            "- **Cancel Orders**: Clear stale pending orders.\n"
            "- **Schedule Follow-up Meeting**: If you anticipate short-term volatility, you can request an out-of-band meeting (e.g., 'Let's reconvene in 60 minutes')."
        )

        user_content_parts.append(f"### Conversation So Far\n{convo_text}")

        if is_debate_round:
            user_content_parts.append(
                "### YOUR TASK — DEBATE ROUND\n"
                "Now that all initial reports are on the table, this is the full assessment round.\n"
                "You must critically evaluate the proposals. You MUST:\n"
                "1. If you disagree with a colleague, push back directly. Name them and explain why.\n"
                "2. If you agree with a trade, use your turn to refine the sizing or timing.\n"
                "3. Do NOT simply repeat or pile onto someone else's critique. Bring a unique perspective based on your specific role.\n\n"
                "Do NOT just summarize your own point again. Engage with what others have said.\n"
                "Keep it under 150 words."
            )
        else:
            user_content_parts.append(
                "### YOUR TASK — INDEPENDENT REPORT\n"
                "Give your independent report based on the Market Data. You MUST:\n"
                "1. State your own position clearly based on the data.\n"
                "2. Do NOT critique or react to your colleagues yet. Just put your foundational analysis on the table.\n\n"
                "Keep it concise (under 150 words). Bullet points preferred."
            )

        return [
            # system prompt is prepended by AgentLLM.generate_response()
            {"role": "user", "content": "\n\n".join(user_content_parts)},
        ]

    async def _build_closing(
        self,
        mt: MeetingType,
        conversation_log: List[str],
        price_data: str,
        portfolio_summary: str,
    ) -> tuple[str, float]:
        """Ask the facilitator LLM to produce a closing summary."""
        recent_convo = "\n\n".join(conversation_log[-8:])

        closing_prompt = (
            f"## Meeting Closing — {mt.name}\n\n"
            f"You are the facilitator. Summarize the discussion, state any "
            f"decisions made, and list action items with assigned agents.\n\n"
            f"### Discussion\n{recent_convo}\n\n"
            f"Produce a structured closing with:\n"
            f"1. Key perspectives summary (2-3 bullets)\n"
            f"2. Decision(s)\n"
            f"3. Action items\n"
            f"4. Next review checkpoint\n\n"
            f"CRITICAL: If a trade, order, or parameter change is approved by the majority, you MUST append the exact execution tag at the very end of your response:\n"
            f"- `[TRADE: BUY <ASSET> <USD_AMOUNT>]` (e.g. `[TRADE: BUY BTC 500]`)\n"
            f"- `[TRADE: SELL <ASSET> <QUANTITY>]` (e.g. `[TRADE: SELL BTC 0.15]`)\n"
            f"- `[ORDER: LIMIT BUY <ASSET> <USD_AMOUNT> @ <PRICE>]` (e.g. `[ORDER: LIMIT BUY BTC 500 @ 62000]`)\n"
            f"- `[ORDER: STOP SELL <ASSET> <QUANTITY> @ <PRICE>]` (e.g. `[ORDER: STOP SELL ETH 0.5 @ 3000]`)\n"
            f"- `[ORDER: TAKE_PROFIT SELL <ASSET> <QUANTITY> @ <PRICE>]` (e.g. `[ORDER: TAKE_PROFIT SELL BTC 0.25 @ 75000]`)\n"
            f"- `[CANCEL: ALL <ASSET>]` (e.g. `[CANCEL: ALL BTC]`)\n"
            f"- `[SCHEDULE_MEETING: <MINUTES>]` (e.g. `[SCHEDULE_MEETING: 60]`)\n"
            f"- `[PARAM: min_trade_usd=<VALUE>]` (e.g. `[PARAM: min_trade_usd=150]`)\n\n"
            f"Be concise — under 250 words."
        )

        messages = [{"role": "user", "content": closing_prompt}]
        return await agent_llm.generate_response(mt.facilitator_id, messages, max_tokens=600)

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _extract_decisions(closing_text: str) -> List[str]:
        """Best-effort extraction of decision lines from closing text."""
        decisions: List[str] = []
        for line in closing_text.splitlines():
            stripped = line.strip().lower()
            if stripped.startswith("decision:") or stripped.startswith("- decision:"):
                decisions.append(line.strip())
        return decisions or ["See facilitator summary for details."]

    @staticmethod
    def _extract_actions(closing_text: str) -> List[str]:
        """Best-effort extraction of action-item lines from closing text."""
        actions: List[str] = []
        for line in closing_text.splitlines():
            stripped = line.strip().lower()
            if stripped.startswith("action:") or stripped.startswith("- action:"):
                actions.append(line.strip())
        return actions or ["See facilitator summary for details."]


# ---------------------------------------------------------------------------
# Meeting rotation
# ---------------------------------------------------------------------------
ROTATION_ORDER: List[str] = [
    "morning_briefing",
    "strategy_session",
    "risk_review",
    "trade_execution",
    "performance_retrospective",
    "altcoin_scouting",
]


class MeetingRotation:
    """
    Cycles through the standard meeting rotation order.
    Persists the current index to data/rotation_state.json.
    """

    def __init__(self) -> None:
        self._index: int = 0
        self._load()

    def _load(self) -> None:
        if ROTATION_STATE_PATH.exists():
            try:
                data = json.loads(ROTATION_STATE_PATH.read_text(encoding="utf-8"))
                self._index = int(data.get("current_index", 0)) % len(ROTATION_ORDER)
            except (json.JSONDecodeError, ValueError):
                self._index = 0

    def _save(self) -> None:
        ROTATION_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        ROTATION_STATE_PATH.write_text(
            json.dumps({"current_index": self._index}, indent=2),
            encoding="utf-8",
        )

    def get_next_meeting_type(self) -> str:
        """Return the next meeting type id and advance the index."""
        meeting_id = ROTATION_ORDER[self._index]
        self._index = (self._index + 1) % len(ROTATION_ORDER)
        self._save()
        return meeting_id

    def peek_next_meeting_type(self) -> str:
        """Return the next meeting type id *without* advancing."""
        return ROTATION_ORDER[self._index]


# ---------------------------------------------------------------------------
# Module-level singletons
# ---------------------------------------------------------------------------
meeting_engine = MeetingEngine()
meeting_rotation = MeetingRotation()
