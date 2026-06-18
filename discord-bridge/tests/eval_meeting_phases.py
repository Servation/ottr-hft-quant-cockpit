import asyncio
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from bot.agents import AGENTS, AgentLLM
from bot.meetings import MeetingEngine
from bot.meetings import MEETING_TYPES

MOCK_PRICE_DATA = "Index Reading: 15 - Extreme Fear. EMA(20) < EMA(50). RSI: 35. MACD: Bearish. Volatility: High."
MOCK_PORTFOLIO_SUMMARY = "Holding 2 BTC, 15 ETH."
MOCK_CONVERSATION_LOG = [
    "[Athena (Meeting Chair)]: Welcome team. The current market state is highly volatile. Please provide your reports.",
    "[Sentiment Analyst]: - Initial Assessment: BUY BTC\nThe crowd is panicking.",
]

async def run_phase_eval():
    print("Running Meeting Phases Evaluation...")
    
    engine = MeetingEngine()
    mt = MEETING_TYPES["strategy_session"]
    agent_id = "technical_analyst"
    agent_persona = AGENTS[agent_id]
    
    # ---------------------------------------------------------
    # Test 1: Independent Report Phase
    # ---------------------------------------------------------
    print(f"\n--- Testing Phase 1 (Independent Report) for {agent_persona.name} ---")
    context_msgs_phase1 = engine._build_agent_context(
        agent_id=agent_id,
        meeting_type=mt,
        conversation_log=[],  # Empty log for independent report
        price_data=MOCK_PRICE_DATA,
        portfolio_summary=MOCK_PORTFOLIO_SUMMARY,
        ceo_directives="",
        memory_context="",
        is_debate_round=False
    )
    
    try:
        service = AgentLLM()
        response_p1, _ = await service.generate_response(
            agent_id=agent_id,
            context_messages=context_msgs_phase1,
            max_tokens=300
        )
        print("=== RESPONSE (Phase 1) ===")
        print(response_p1)
        print("==========================")
        
        if "- Final Vote:" in response_p1:
             print("\n❌ EVALUATION FAILED: Agent cast a 'Final Vote' during Phase 1.")
        elif "- Initial Assessment:" not in response_p1:
             print("\n❌ EVALUATION FAILED: Agent failed to provide an 'Initial Assessment' in Phase 1.")
        else:
             print("\n✅ EVALUATION PASSED: Phase 1 constraints met.")
             
    except Exception as e:
        print("Phase 1 Evaluation failed due to exception:", e)

    # ---------------------------------------------------------
    # Test 2: Debate Round Phase
    # ---------------------------------------------------------
    print(f"\n--- Testing Phase 2 (Debate Round) for {agent_persona.name} ---")
    context_msgs_phase2 = engine._build_agent_context(
        agent_id=agent_id,
        meeting_type=mt,
        conversation_log=MOCK_CONVERSATION_LOG,
        price_data=MOCK_PRICE_DATA,
        portfolio_summary=MOCK_PORTFOLIO_SUMMARY,
        ceo_directives="",
        memory_context="",
        is_debate_round=True
    )
    
    try:
        service = AgentLLM()
        response_p2, _ = await service.generate_response(
            agent_id=agent_id,
            context_messages=context_msgs_phase2,
            max_tokens=300
        )
        print("=== RESPONSE (Phase 2) ===")
        print(response_p2)
        print("==========================")
        
        if "Final Vote:" not in response_p2 and "**Final Vote" not in response_p2:
             print("\n❌ EVALUATION FAILED: Agent failed to cast a 'Final Vote' in Phase 2.")
        else:
             print("\n✅ EVALUATION PASSED: Phase 2 constraints met.")
             
    except Exception as e:
        print("Phase 2 Evaluation failed due to exception:", e)

if __name__ == "__main__":
    asyncio.run(run_phase_eval())
