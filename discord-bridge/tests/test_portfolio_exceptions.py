import pytest
import os
import json
from unittest.mock import patch, mock_open

import bot.portfolio
from bot.portfolio import Portfolio

@pytest.fixture
def temp_portfolio(tmp_path):
    original_state_file = bot.portfolio._PORTFOLIO_FILE
    original_data_dir = bot.portfolio._DATA_DIR
    bot.portfolio._PORTFOLIO_FILE = tmp_path / "portfolio_state.json"
    bot.portfolio._DATA_DIR = tmp_path
    
    p = Portfolio()
    p._state["cash"] = 10000.0
    p.save()
    
    yield p
    
    bot.portfolio._PORTFOLIO_FILE = original_state_file
    bot.portfolio._DATA_DIR = original_data_dir

def test_min_trade_usd(temp_portfolio):
    temp_portfolio.min_trade_usd = 500.0
    assert temp_portfolio.min_trade_usd == 500.0

def test_portfolio_load_exceptions(tmp_path):
    original_state_file = bot.portfolio._PORTFOLIO_FILE
    bot.portfolio._PORTFOLIO_FILE = tmp_path / "portfolio_state.json"
    
    try:
        # Test json decode error
        bot.portfolio._PORTFOLIO_FILE.write_text("INVALID JSON")
        p = Portfolio()
        assert p._state["cash"] == 10000.0 # reset to default
        
        # Test missing keys healing
        bot.portfolio._PORTFOLIO_FILE.write_text(json.dumps({"cash": 500.0, "holdings": {}}))
        p2 = Portfolio()
        assert "min_trade_usd" in p2._state
        assert "orders" in p2._state
    finally:
        bot.portfolio._PORTFOLIO_FILE = original_state_file

def test_portfolio_save_exception(temp_portfolio, mocker):
    mocker.patch("os.fdopen", side_effect=OSError("Disk Full"))
    temp_portfolio.save() # should catch exception and not crash

def test_buy_exceptions(temp_portfolio):
    # Negative amount
    with pytest.raises(ValueError, match="positive"):
        temp_portfolio.buy("BTC", -100.0, 50000.0)
        
    # Below min trade limit
    temp_portfolio.min_trade_usd = 1000.0
    with pytest.raises(ValueError, match="below the minimum"):
        temp_portfolio.buy("BTC", 500.0, 50000.0)
        
    # Insufficient cash
    with pytest.raises(ValueError, match="Insufficient cash"):
        temp_portfolio.buy("BTC", 20000.0, 50000.0)

def test_sell_exceptions(temp_portfolio):
    temp_portfolio.buy("BTC", 5000.0, 50000.0)
    
    # Negative quantity
    with pytest.raises(ValueError, match="positive"):
        temp_portfolio.sell("BTC", -1.0, 50000.0)
        
    # Insufficient asset
    with pytest.raises(ValueError, match="Insufficient"):
        temp_portfolio.sell("BTC", 10.0, 50000.0)
        
    # Asset not held
    with pytest.raises(ValueError, match="Insufficient"):
        temp_portfolio.sell("ETH", 1.0, 2000.0)
        
    # Below min trade
    temp_portfolio.min_trade_usd = 2000.0
    with pytest.raises(ValueError, match="below the minimum"):
        temp_portfolio.sell("BTC", 0.01, 50000.0) # proceeds < 2000

def test_cancel_orders(temp_portfolio):
    temp_portfolio.place_order("LIMIT", "BUY", "BTC", 5000.0, 40000.0)
    temp_portfolio.place_order("LIMIT", "BUY", "ETH", 1000.0, 2000.0)
    
    # Cancel all BTC
    temp_portfolio.cancel_all_orders(asset="BTC")
    assert len(temp_portfolio._state["orders"]) == 1

def test_place_order_invalid(temp_portfolio):
    temp_portfolio.place_order("UNKNOWN", "BUY", "BTC", 1000.0, 40000.0)
    assert len(temp_portfolio._state["orders"]) == 1

def test_check_orders_limit_sell(temp_portfolio):
    temp_portfolio.buy("BTC", 5000.0, 50000.0)
    # limit sell at 60000
    temp_portfolio.place_order("LIMIT", "SELL", "BTC", 0.05, 60000.0)
    
    # price 55000 - no trigger
    assert len(temp_portfolio.check_orders({"BTC": {"price": 55000.0}})) == 0
    
    # price 65000 - triggers
    assert len(temp_portfolio.check_orders({"BTC": {"price": 65000.0}})) == 1

def test_check_orders_take_profit_sell(temp_portfolio):
    temp_portfolio.buy("BTC", 5000.0, 50000.0)
    temp_portfolio.place_order("TAKE_PROFIT", "SELL", "BTC", 0.05, 60000.0)
    
    # price 55000 - no trigger
    assert len(temp_portfolio.check_orders({"BTC": {"price": 55000.0}})) == 0
    
    # price 65000 - triggers
    assert len(temp_portfolio.check_orders({"BTC": {"price": 65000.0}})) == 1

def test_get_summary_unknown(temp_portfolio):
    # Just cover summary without prices
    assert "total_portfolio_value" in temp_portfolio.get_summary({})
