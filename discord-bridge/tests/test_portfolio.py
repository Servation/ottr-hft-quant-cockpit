import os
import json
import pytest
from bot.portfolio import Portfolio
import bot.portfolio

@pytest.fixture
def temp_portfolio(tmp_path):
    """Fixture to provide a clean portfolio instance with a temporary state file."""
    # Use monkeypatch or just override the _STATE_FILE for the test
    original_state_file = bot.portfolio._PORTFOLIO_FILE
    original_data_dir = bot.portfolio._DATA_DIR
    temp_file = tmp_path / "portfolio_state.json"
    bot.portfolio._PORTFOLIO_FILE = temp_file
    bot.portfolio._DATA_DIR = tmp_path
    
    # Initialize with 10k cash
    p = Portfolio()
    p._state["cash"] = 10000.0
    p.save()
    
    yield p
    
    # Restore original path just in case
    bot.portfolio._PORTFOLIO_FILE = original_state_file
    bot.portfolio._DATA_DIR = original_data_dir

def test_pnl_calculations(temp_portfolio):
    """Test Realized vs Unrealized P&L calculations."""
    p = temp_portfolio
    
    # Buy 1 BTC at $50,000 using $5,000 (0.1 BTC)
    # The portfolio buy applies slippage. Let's trace it.
    trade = p.buy("BTC", 5000.0, 50000.0)
    
    assert "quantity" in trade
    assert trade["asset"] == "BTC"
    
    # Slippage is hardcoded to 0.1% usually, so fill price is slightly worse
    fill_price = trade["fill_price"]
    assert fill_price > 50000.0
    
    # Check holdings
    assert "BTC" in p._state["holdings"]
    btc_holding = p._state["holdings"]["BTC"]
    assert btc_holding["quantity"] == trade["quantity"]
    assert btc_holding["avg_cost"] == fill_price
    
    # Mock current prices
    prices_dict = {"BTC": {"price": 60000.0}}
    
    summary = p.get_summary(prices_dict)
    
    # Unrealized PNL should be positive since price went up
    assert summary["unrealized_pnl"] > 0
    assert summary["realized_pnl"] == 0
    
    # Sell half the BTC at 60,000
    sell_qty = trade["quantity"] / 2
    sell_trade = p.sell("BTC", sell_qty, 60000.0)
    
    # Now we should have realized PNL
    summary2 = p.get_summary(prices_dict)
    assert summary2["realized_pnl"] > 0
    
    # And total portfolio value should be roughly 10000 + some profit minus slippage
    assert summary2["total_portfolio_value"] > 10000.0

def test_order_execution(temp_portfolio):
    """Test limit/stop order triggering."""
    p = temp_portfolio
    
    # Place a buy limit order for SOL at $100
    p.place_order("LIMIT", "BUY", "SOL", 1000.0, 100.0)
    
    # Verify order is pending
    assert len(p._state["orders"]) == 1
    
    # Price is 105, shouldn't trigger
    executed = p.check_orders({"SOL": {"price": 105.0}})
    assert len(executed) == 0
    assert len(p._state["orders"]) == 1
    
    # Price drops to 95, should trigger
    executed2 = p.check_orders({"SOL": {"price": 95.0}})
    assert len(executed2) == 1
    assert len(p._state["orders"]) == 0
    assert "SOL" in p._state["holdings"]
    
    # Place a sell stop order for SOL at $90
    p.place_order("STOP", "SELL", "SOL", p._state["holdings"]["SOL"]["quantity"], 90.0)
    
    # Price is 95, shouldn't trigger
    executed3 = p.check_orders({"SOL": {"price": 95.0}})
    assert len(executed3) == 0
    
    # Price drops to 85, should trigger
    executed4 = p.check_orders({"SOL": {"price": 85.0}})
    assert len(executed4) == 1
    assert len(p._state["orders"]) == 0
    assert p._state["holdings"]["SOL"]["quantity"] == 0.0

def test_state_persistence(temp_portfolio, tmp_path):
    """Test state saving and loading."""
    p = temp_portfolio
    
    p.buy("ETH", 1000.0, 2000.0)
    p.place_order("LIMIT", "BUY", "ADA", 500.0, 0.50)
    
    # Force a save
    p.save()
    
    # Ensure file exists
    assert os.path.exists(bot.portfolio._PORTFOLIO_FILE)
    
    # Create new portfolio instance, it should load from the same file
    p2 = Portfolio()
    assert "ETH" in p2._state["holdings"]
    assert len(p2._state["orders"]) == 1
    assert p2._state["orders"][0]["asset"] == "ADA"
    assert p2._state["cash"] == p._state["cash"]
