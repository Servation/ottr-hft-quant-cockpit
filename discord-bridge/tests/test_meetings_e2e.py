import pytest
import asyncio
from typing import List, Dict, Any

from bot.meetings import MeetingEngine
from bot.portfolio import Portfolio
from bot.price_feed import price_feed
from bot.memory import meeting_memory

@pytest.fixture
def mock_portfolio(tmp_path, mocker):
    import bot.portfolio
    original_state_file = bot.portfolio._PORTFOLIO_FILE
    original_data_dir = bot.portfolio._DATA_DIR
    bot.portfolio._PORTFOLIO_FILE = tmp_path / "portfolio_state.json"
    bot.portfolio._DATA_DIR = tmp_path
    
    p = bot.portfolio.Portfolio()
    p._state["cash"] = 10000.0
    p.save()
    
    mocker.patch("bot.tools.portfolio", p)
    mocker.patch("bot.meetings.portfolio", p)
    
    yield p
    
    bot.portfolio._PORTFOLIO_FILE = original_state_file
    bot.portfolio._DATA_DIR = original_data_dir

@pytest.mark.asyncio
async def test_live_price_feed():
    """Test that price_feed can fetch live data from CoinGecko."""
    prices = await price_feed.get_prices()
    assert "BTC" in prices
    assert "price" in prices["BTC"]
    assert prices["BTC"]["price"] > 0
    
    summary = await price_feed.get_market_state_summary()
    assert "BTC" in summary
    assert "ETH" in summary

@pytest.mark.asyncio
async def test_live_meeting_cycle(mock_portfolio):
    """
    Test a complete live meeting cycle without mocking the LLM.
    This ensures the prompts are valid and the models return actual data.
    """
    engine = MeetingEngine()
    
    # We will capture messages instead of sending to Discord
    captured_messages: List[dict] = []
    
    async def dummy_post(agent_id: str, content: str) -> None:
        captured_messages.append({"agent_id": agent_id, "content": content})
    
    # 1. Fetch live prices
    prices = await price_feed.get_prices()
    price_str = await price_feed.get_market_state_summary()
    
    # 2. Get portfolio summary
    port_str = str(mock_portfolio.get_summary(prices))
    
    # 3. Create a small meeting type just for testing to save time/tokens.
    # Risk Review is smaller than Morning Briefing.
    record = await engine.run_meeting(
        meeting_type_id="risk_review",
        post_message_fn=dummy_post,
        price_data=price_str,
        portfolio_summary=port_str,
        ceo_directives="Please check our drawdown and confirm it is safe.",
    )
    
    # Assertions
    assert len(captured_messages) >= 2  # Facilitator + at least 1 agent
    
    # Facilitator should speak first
    assert captured_messages[0]["agent_id"] == "meeting_chair"
    
    # The meeting record should be populated
    assert "summary" in record
    assert "decisions" in record
    assert len(record["decisions"]) >= 0

