import asyncio
import sys
import io
import os

# Set stdout to utf-8 to avoid charmap errors on Windows with emojis
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
# Default to a local LM Studio endpoint, but respect an LLM_BASE_URL already
# set by the caller (e.g. run_evals.py forces the endpoint it verified reachable).
os.environ.setdefault("LLM_BASE_URL", "http://127.0.0.1:1234/v1")

from bot.meetings import meeting_engine
from bot.agents import agent_llm
from bot.price_feed import price_feed

async def run():
    posted_messages = []
    async def mock_post(agent, msg):
        posted_messages.append((agent, msg))
        print(f"\n--- POST BY {agent} ---\n{msg}\n")
        
    original_get_prices = price_feed.get_prices
    async def mock_get_prices():
        return {
            'BTC': {'price': 60000.0, 'change_24h': 0.0},
            'ETH': {'price': 3000.0, 'change_24h': 0.0},
            'SOL': {'price': 150.0, 'change_24h': 0.0}
        }
    price_feed.get_prices = mock_get_prices
    
    print("Starting real LLM strategy session...")
    try:
        await meeting_engine.run_meeting(
            meeting_type_id="strategy_session",
            post_message_fn=mock_post,
            price_data="BTC is 60000. ETH is 3000. SOL is 150.",
            portfolio_summary="Cash: $20,000, Holdings: 1 BTC",
            ceo_directives="Test the new consensus breakdown features.",
            memory_context=""
        )
    except Exception as e:
        print(f"Error running meeting: {e}")
        import traceback
        traceback.print_exc()
    
    # Validation
    app_msgs = [m for a, m in posted_messages if a == "APP"]
    
    print("\n--- VALIDATION ---")
    if any("Algorithmic Consensus Breakdown" in m for m in app_msgs):
        print("✅ Algorithmic Consensus Breakdown posted successfully!")
    else:
        print("❌ Algorithmic Consensus Breakdown MISSING!")
        
    # Check that AGENTS actually voted on more than one asset. Parse the "Individual
    # Votes" section (real LLM votes), NOT "Net Asset Scores" — the scores table now
    # unions the full watchlist (every tradeable asset always appears), so counting it
    # would be vacuous. Don't hardcode tickers; the session trades whatever the data
    # supports. Vote line format: "- **Name**: <DIR> <ASSET> *(rep ...)*".
    import re
    for m in app_msgs:
        if "Algorithmic Consensus Breakdown" in m:
            print("Breakdown output:\n" + m)
            assets = set()
            in_votes = False
            for line in m.splitlines():
                if "Individual Votes" in line:
                    in_votes = True
                    continue
                if "Net Asset Scores" in line:
                    in_votes = False
                    continue
                if in_votes:
                    vm = re.search(r":\s*(?:BUY|SELL|HOLD|ABSTAIN)\s+([A-Z]{2,10})", line)
                    if vm and vm.group(1) != "MARKET":
                        assets.add(vm.group(1))
            if len(assets) >= 2:
                print(f"✅ Agents voted on multiple assets: {sorted(assets)}")
            else:
                # Informational only (NOT a failure): a unanimous meeting can legitimately
                # vote a single asset. Avoid the ❌ marker so the runner doesn't false-fail
                # on a quiet-market run where every agent HOLDs the same asset.
                print(f"NOTE: breakdown covered a single asset this run ({sorted(assets)}); "
                      "agents were unanimous, which is not a failure.")
                

    # ------------------------------------------------------------------
    # Validate the Risk Auditor's DEBATE-round behavior.
    # In Phase 1 (initial report) it uses its rigid persona OUTPUT FORMAT
    # ("Drawdown Risk:" etc.). In the debate round it must DROP that format,
    # speak conversationally, and end with a standardized "Final Vote:".
    # posted_messages holds raw (agent, msg) tuples in chronological order,
    # so the Risk Auditor's LAST message is its debate-round contribution.
    # ------------------------------------------------------------------
    risk_msgs = [m for a, m in posted_messages if a == "risk_auditor"]
    RIGID_MARKERS = ("Drawdown Risk:", "Slippage Assessment:", "Min Trade Check:")

    if len(risk_msgs) < 2:
        print(
            "⚠️  INCONCLUSIVE: Risk Auditor did not speak in the debate round "
            f"(spoke {len(risk_msgs)}x); debate-format adherence could not be checked."
        )
    else:
        debate_msg = risk_msgs[-1]
        leaked_rigid = [mk for mk in RIGID_MARKERS if mk in debate_msg]
        has_vote = "Final Vote:" in debate_msg

        if leaked_rigid:
            print(
                "❌ EVALUATION FAILED: Risk Auditor STILL used its rigid report "
                f"format during the debate round (found {leaked_rigid})."
            )
        elif not has_vote:
            print("❌ EVALUATION FAILED: Risk Auditor did not cast a 'Final Vote:' in the debate round.")
        else:
            print("✅ EVALUATION PASSED: Risk Auditor dropped its rigid format and cast a Final Vote in the debate round.")

    print("\nEval completed.")


if __name__ == "__main__":
    # Run inside isolated portfolio + meeting memory so the strategy session's
    # tool calls never mutate the live data/portfolio_state.json and the saved
    # meeting record never lands in the live data/meeting_log.json.
    from eval_utils import isolated_data
    with isolated_data():
        asyncio.run(run())
