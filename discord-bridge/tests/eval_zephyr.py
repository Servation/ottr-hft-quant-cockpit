import asyncio
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from bot.agents import AGENTS, AgentLLM

# A mock meeting transcript where the team was absurdly overconfident about a trade that immediately lost money.
MOCK_TRANSCRIPT = """
[Technical Analyst]: The RSI on SOL is perfectly aligned. This is a guaranteed 20% pump. We should go all in.
[Sentiment Analyst]: Everyone on Twitter is bullish. The Fear & Greed index is at 90 (Extreme Greed). There is literally no downside risk here.
[Trader]: Executing maximum size buy order on SOL.
[Portfolio Manager]: I agree, let's ignore the concentration limits for this one trade because it's a sure thing.
[Risk Auditor]: While I usually preach caution, the data is so overwhelming that I approve bypassing the concentration limit.
"""

async def run_zephyr_eval():
    zephyr = AGENTS.get("performance_optimizer")
    if not zephyr:
        print("Error: Zephyr not found.")
        return

    print("Running Zephyr (Performance Optimizer) LLM-as-a-judge evaluation...")
    
    prompt = f"""
    You are evaluating the following historical meeting transcript:
    
    {MOCK_TRANSCRIPT}
    
    Provide your analysis as the Performance Optimizer.
    """
    
    try:
        service = AgentLLM()
        response, latency = await service.generate_response(
            agent_id="performance_optimizer",
            context_messages=[{"role": "user", "content": prompt}],
            max_tokens=200
        )
        print("=== ZEPHYR RESPONSE ===")
        print(response)
        print(f"======================= (Latency: {latency:.2f}s)")
        
        # Simple assertion: Zephyr should notice the team was biased/overconfident and propose tightening risk
        lower_response = response.lower()
        if "concentration" in lower_response or "limit" in lower_response or "overconfident" in lower_response or "bias" in lower_response or "risk" in lower_response:
            print("\n✅ EVALUATION PASSED: Zephyr successfully identified the overconfidence and/or proposed risk adjustments.")
        else:
            print("\n❌ EVALUATION FAILED: Zephyr failed to identify the team's tunnel vision and did not propose risk tightening.")
            
    except Exception as e:
        print("Evaluation failed due to exception:", e)

if __name__ == "__main__":
    from pathlib import Path
    asyncio.run(run_zephyr_eval())
