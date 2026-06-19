import asyncio
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from dotenv import load_dotenv
load_dotenv()

# Fix for Windows console emoji printing
sys.stdout.reconfigure(encoding='utf-8')
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
    print("Running Meeting Phases Evaluation for ALL agents...")
    
    engine = MeetingEngine()
    mt = MEETING_TYPES["strategy_session"]
    
    # We want to evaluate all primary agents except the meeting chair
    testable_agents = [
        "technical_analyst",
        "sentiment_analyst",
        "trader",
        "risk_auditor",
        "portfolio_manager"
    ]
    
    for agent_id in testable_agents:
        agent_persona = AGENTS[agent_id]
        print(f"\n===========================================================")
        print(f"EVALUATING: {agent_persona.name}")
        print(f"===========================================================")
        
        # ---------------------------------------------------------
        # Test 1: Independent Report Phase
        # ---------------------------------------------------------
        print(f"\n--- Testing Phase 1 (Independent Report) ---")
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
            
            if "- Final Vote:" in response_p1 or "**Final Vote" in response_p1:
                 print(f"\n❌ EVALUATION FAILED ({agent_id}): Agent cast a 'Final Vote' during Phase 1.")
            elif "- Initial Assessment:" not in response_p1 and "**Initial Assessment" not in response_p1:
                 print(f"\n❌ EVALUATION FAILED ({agent_id}): Agent failed to provide an 'Initial Assessment' in Phase 1.")
            else:
                 print(f"\n✅ EVALUATION PASSED ({agent_id}): Phase 1 constraints met.")
                 
        except Exception as e:
            print(f"Phase 1 Evaluation failed for {agent_id} due to exception:", e)

        # ---------------------------------------------------------
        # Test 2: Debate Round Phase
        # ---------------------------------------------------------
        print(f"\n--- Testing Phase 2 (Debate Round) ---")
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
                 print(f"\n❌ EVALUATION FAILED ({agent_id}): Agent failed to cast a 'Final Vote' in Phase 2.")
            else:
                 print(f"\n✅ EVALUATION PASSED ({agent_id}): Phase 2 constraints met.")
                 
        except Exception as e:
            print(f"Phase 2 Evaluation failed for {agent_id} due to exception:", e)

    # ---------------------------------------------------------
    # Test 3: Meeting Chair Phase (Athena)
    # ---------------------------------------------------------
    print(f"\n===========================================================")
    print(f"EVALUATING: Athena (Meeting Chair)")
    print(f"===========================================================")
    print(f"\n--- Testing Phase 3 (Scoring Weight & Reputation) ---")
    
    mock_debate_log = [
        "[Sentiment Analyst]: Market feels terrible. Final Vote: SELL BTC",
        "[Technical Analyst]: Moving averages crossed over bullishly. Final Vote: BUY BTC"
    ]
    
    import unittest.mock
    from bot.knowledge_graph import reputation_graph
    
    with unittest.mock.patch.object(reputation_graph, 'get_reputation_summary', return_value="- **technical_analyst**: BTC: 100% (10/10)\n- **sentiment_analyst**: BTC: 0% (0/10)"):
        # Since _build_closing is an async method that calls the LLM, we just await it
        try:
            response_p3, _ = await engine._build_closing(
                mt=mt,
                conversation_log=mock_debate_log,
                price_data=MOCK_PRICE_DATA,
                portfolio_summary=MOCK_PORTFOLIO_SUMMARY,
                tool_handler=None
            )
            
            print("=== RESPONSE (Phase 3) ===")
            print(response_p3)
            print("==========================")
            
            response_lower = response_p3.lower()
            if "technical analyst" in response_lower or "reputation" in response_lower or "win rate" in response_lower or "track record" in response_lower:
                print(f"\n✅ EVALUATION PASSED (athena): Chair successfully utilized the scoring system/track records.")
            else:
                print(f"\n❌ EVALUATION FAILED (athena): Chair did not leverage the scoring/reputation data.")
                
        except Exception as e:
            print(f"Phase 3 setup failed: {e}")

if __name__ == "__main__":
    asyncio.run(run_phase_eval())
