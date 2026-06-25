import asyncio
import logging
import json
import os
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# Configure logging to stdout
logging.basicConfig(level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger("eval_trades")

def setup_environment():
    """Ensure the path is set up to import bot modules."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def test_portfolio_logic():
    from bot.portfolio import Portfolio
    
    logger.info("=== Starting Portfolio Logic Eval ===")
    p = Portfolio()
    
    # 1. Start fresh
    p._state["cash"] = 10000.0
    p._state["holdings"] = {"BTC": {"quantity": 0.0, "avg_cost": 0.0}, "ETH": {"quantity": 0.0, "avg_cost": 0.0}}
    p._state["min_trade_usd"] = 100.0
    p.save()
    
    # 2. Test Minimum Trade Size (Buy)
    logger.info("Testing Minimum Trade Size validation...")
    try:
        p.buy("BTC", usd_amount=5.0, price=50000.0) # $5 value
        assert False, "Should have failed min trade size"
    except ValueError as e:
        assert "below the minimum trade limit" in str(e)
        logger.info("✅ Minimum trade validation passed.")
        
    # 3. Test Insufficient Funds
    logger.info("Testing Insufficient Funds validation...")
    try:
        p.buy("BTC", usd_amount=5000000.0, price=50000.0) # $5M value
        assert False, "Should have failed insufficient funds"
    except ValueError as e:
        assert "Insufficient cash" in str(e)
        logger.info("✅ Insufficient funds validation passed.")
        
    # 4. Test Successful Buy with Slippage
    logger.info("Testing Successful Buy with Slippage...")
    # Slippage and the per-side fee are configured in portfolio settings.
    from bot.portfolio import _SLIPPAGE_PCT, _FEE_PCT
    trade = p.buy("BTC", usd_amount=6000.0, price=60000.0) # $6000 value
    fill_price = 60000.0 * (1.0 + _SLIPPAGE_PCT / 100.0)
    expected_quantity = 6000.0 / fill_price
    buy_fee = 6000.0 * (_FEE_PCT / 100.0)  # fee is charged to cash on top of notional

    assert trade["action"] == "BUY"
    assert trade["asset"] == "BTC"
    assert abs(trade["fill_price"] - fill_price) < 1e-5
    assert abs(trade["fee_usd"] - buy_fee) < 1e-5
    assert abs(p._state["cash"] - (10000.0 - 6000.0 - buy_fee)) < 1e-5
    assert abs(p._state["holdings"]["BTC"]["quantity"] - expected_quantity) < 1e-5
    logger.info("✅ Successful buy with slippage passed.")
    
    # 5. Test Insufficient Asset to Sell
    logger.info("Testing Insufficient Asset to Sell...")
    try:
        p.sell("BTC", quantity=0.5, price=65000.0)
        assert False, "Should have failed insufficient asset"
    except ValueError as e:
        assert "Insufficient BTC" in str(e)
        logger.info("✅ Insufficient asset validation passed.")
        
    # 6. Test Successful Sell and Realized P&L
    logger.info("Testing Successful Sell and Realized P&L...")
    sell_price = 65000.0
    sell_fill_price = sell_price * (1.0 - _SLIPPAGE_PCT / 100.0)
    sell_qty = expected_quantity / 2.0
    expected_proceeds = sell_qty * sell_fill_price
    sell_fee = expected_proceeds * (_FEE_PCT / 100.0)
    expected_pnl = (sell_fill_price - fill_price) * sell_qty - sell_fee

    cash_before = p._state["cash"]
    pnl_before = p._state["total_pnl"]

    trade2 = p.sell("BTC", quantity=sell_qty, price=sell_price)

    assert trade2["action"] == "SELL"
    assert abs(trade2["fee_usd"] - sell_fee) < 1e-5
    assert abs(p._state["cash"] - (cash_before + expected_proceeds - sell_fee)) < 1e-5
    assert abs(p._state["total_pnl"] - (pnl_before + expected_pnl)) < 1e-5
    assert abs(p._state["holdings"]["BTC"]["quantity"] - (expected_quantity - sell_qty)) < 1e-5
    logger.info("✅ Successful sell and Realized P&L passed.")
    
    logger.info("=== Portfolio Logic Eval Completed Successfully ===")

async def test_end_to_end_trade_execution():
    """
    Mocks the LLM generating a raw tool-call tag at the end of a meeting to ensure
    AgentLLM fallback parsing and tools.py handle_tool_call work end-to-end.

    This is also a regression test for the double-execution bug: the mock
    deliberately re-emits the SAME tool call on every closing-phase turn (which is
    how a flaky local model behaves). The AgentLLM idempotency guard must ensure
    the trade executes EXACTLY ONCE despite the repeated tag.
    """
    logger.info("=== Starting End-to-End Trade Execution Eval ===")
    from bot.agents import agent_llm
    from bot.meetings import meeting_engine
    from bot.portfolio import portfolio, _SLIPPAGE_PCT

    # Reset cash and holdings
    portfolio._state["cash"] = 20000.0
    portfolio._state["holdings"] = {"SOL": {"quantity": 0.0, "avg_cost": 0.0}}
    portfolio.save()

    # Mock the LLM client instead of generate_response to test the fallback parser
    class MockChoice:
        def __init__(self, message_content):
            self.message = type('MockMessage', (), {'content': message_content, 'tool_calls': None, 'model_dump': lambda **k: {}})()

    class MockResponse:
        def __init__(self, content):
            self.choices = [MockChoice(content)]

    # Count how many times the mock emits the trade tag, so we can confirm the
    # guard (not the mock) is what prevents the double execution.
    tag_emissions = {"count": 0}

    original_create = agent_llm._client.chat.completions.create
    async def mock_create(**kwargs):
        messages = kwargs.get("messages", [])
        # If the system prompt is for the meeting chair, output a raw tool call
        if any("You are Athena" in m.get("content", "") for m in messages if m.get("role") == "system"):
            # Ensure it only fires at the end of the meeting, not during memory query expansion
            if any("## Meeting Closing" in m.get("content", "") for m in messages):
                tag_emissions["count"] += 1
                raw_tag = r'<|tool_call>call:execute_trade{"action":"BUY","asset":"SOL","amount":500}<|tool_call>'
                return MockResponse(f"I have reviewed the consensus.\n{raw_tag}")

        # Otherwise return an empty response so agents don't waste time/tokens
        return MockResponse("Final Vote: ABSTAIN SOL")

    agent_llm._client.chat.completions.create = mock_create

    # Mock price feed
    from bot.price_feed import price_feed
    original_get_prices = price_feed.get_prices
    original_get_vol = price_feed.get_volatility
    original_get_tech = price_feed.get_technical_indicators
    async def mock_get_prices():
        return {
            "BTC": {"price": 60000.0, "change_24h": 0.0},
            "ETH": {"price": 3000.0, "change_24h": 0.0},
            "SOL": {"price": 150.0, "change_24h": 0.0}
        }
    # Position sizing in execute_trade would otherwise fetch live volatility/regime
    # (network) and resize this fixed $500 test trade. Stub them for a deterministic e2e.
    async def _empty():
        return {}
    price_feed.get_prices = mock_get_prices
    price_feed.get_volatility = _empty
    price_feed.get_technical_indicators = _empty
    
    class MockDiscordBot:
        async def post_system_status(self, msg): pass
        async def post_audit_log(self, msg): 
            logger.info(f"Audit Log triggered: {msg}")
        async def post_as_agent(self, agent, msg): pass
    
    mock_bot = MockDiscordBot()
    
    try:
        # Run a simulated trade execution meeting
        await meeting_engine.run_meeting(
            meeting_type_id="trade_execution",
            post_message_fn=mock_bot.post_as_agent,
            price_data="BTC is 60000. SOL is 150.",
            portfolio_summary="Cash: $20,000",
            ceo_directives="",
            memory_context="",
            audit_log_fn=mock_bot.post_audit_log,
            # Pass a mock testing flag if we want, but let's just let it run normally with the mocked LLM
        )
        
        # After the meeting, the fallback parser should have executed exactly one
        # BUY of $500 of SOL. Compute the expected single-execution quantity.
        fill_price = 150.0 * (1.0 + _SLIPPAGE_PCT / 100.0)
        expected_qty = 500.0 / fill_price

        sol_holding = portfolio._state["holdings"].get("SOL", {}).get("quantity", 0)

        # The trade must have run at all...
        assert sol_holding > 0, (
            f"SOL holding is {sol_holding}, expected ~{expected_qty:.6f}. "
            "The trade tool did not execute!"
        )
        # ...and EXACTLY once. A double execution (~2x) is the regression we guard against.
        assert abs(sol_holding - expected_qty) < 1e-4, (
            f"SOL holding is {sol_holding}, expected exactly {expected_qty:.6f}. "
            f"The trade executed the wrong number of times "
            f"(tool tag was emitted {tag_emissions['count']}x by the mock) - "
            "this indicates the idempotency guard is not working."
        )
        logger.info(
            f"OK End-to-End tool parsing passed! Bought {sol_holding:.6f} SOL exactly once "
            f"despite the tag being emitted {tag_emissions['count']}x."
        )
        logger.info("=== End-to-End Trade Execution Eval Completed Successfully ===")

    finally:
        # Restore mocks
        agent_llm._client.chat.completions.create = original_create
        price_feed.get_prices = original_get_prices
        price_feed.get_volatility = original_get_vol
        price_feed.get_technical_indicators = original_get_tech

if __name__ == "__main__":
    setup_environment()
    from eval_utils import isolated_data
    try:
        # Run inside isolated portfolio + meeting memory so the eval never mutates
        # the live data/portfolio_state.json OR appends a test meeting to the live
        # data/meeting_log.json (the e2e runs a real meeting that calls save_meeting).
        with isolated_data():
            test_portfolio_logic()
            asyncio.run(test_end_to_end_trade_execution())
    except Exception as e:
        logger.exception("Evaluation failed!")
        sys.exit(1)
