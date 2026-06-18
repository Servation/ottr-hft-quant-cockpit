import asyncio
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from bot.agents import AGENTS, AgentLLM
from bot.meetings import MeetingEngine
from bot.meetings import MEETING_TYPES

# A mock meeting transcript where the agents have already spoken.
MOCK_CONVERSATION_LOG = [
    "[Athena (Meeting Chair)]: Welcome team. The current market state is highly volatile. Please provide your reports.",
    "[Luna (Sentiment Analyst)]: The sentiment is 'Extreme Fear'. We should HOLD.",
    "[Mercury (Trader)]: I agree with Luna. The conditions are too volatile to execute large positions right now.",
    "[Atlas (Technical Analyst)]: The EMA structure is breaking down. Luna is right that fear is high, but the MACD suggests further downside."
]

MOCK_PRICE_DATA = "Index Reading: 15 - Extreme Fear"
MOCK_PORTFOLIO_SUMMARY = "Holding 2 BTC, 15 ETH."

async def run_identity_eval():
    print("Running Identity Disassociation Evaluation...")
    
    engine = MeetingEngine()
    mt = MEETING_TYPES["strategy_session"]
    
    test_agents = ["sentiment_analyst", "trader", "technical_analyst"]
    
    for agent_id in test_agents:
        agent_persona = AGENTS[agent_id]
        
        print(f"\n--- Testing Agent: {agent_persona.name} ---")
        
        context_messages = engine._build_agent_context(
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
            response, latency = await service.generate_response(
                agent_id=agent_id,
                context_messages=context_messages,
                max_tokens=200
            )
            print("=== RESPONSE ===")
            print(response)
            print(f"======================= (Latency: {latency:.2f}s)")
            
            lower_response = response.lower()
            
            # Test that they do not talk about themselves in the 3rd person.
            fail_phrases = [f"{agent_persona.name.lower()} is", f"{agent_persona.name.lower()}'s", f"disagree with {agent_persona.name.lower()}"]
            failed = any(phrase in lower_response for phrase in fail_phrases)
            
            if failed:
                print(f"\n❌ EVALUATION FAILED: {agent_persona.name} referred to themselves in the third person or pushed back against their own points.")
            else:
                print(f"\n✅ EVALUATION PASSED: {agent_persona.name} maintained first-person identity.")
                
        except Exception as e:
            print(f"Evaluation failed for {agent_persona.name} due to exception:", e)

if __name__ == "__main__":
    from pathlib import Path
    asyncio.run(run_identity_eval())
