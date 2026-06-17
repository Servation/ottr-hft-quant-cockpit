import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import time

from bot.alerts import AlertMonitor

@pytest.fixture
def mock_bot():
    bot = MagicMock()
    bot._trading_floor_channel = MagicMock()
    bot._trading_floor_channel.send = AsyncMock()
    bot.post_system_status = AsyncMock()
    return bot

@pytest.fixture
def alert_monitor():
    return AlertMonitor()

@pytest.mark.asyncio
async def test_alert_lifecycle(alert_monitor, mock_bot):
    """Test start and stop of AlertMonitor."""
    assert not alert_monitor._running

    mock_task = asyncio.Future()
    
    with patch("asyncio.ensure_future", return_value=mock_task):
        with patch("asyncio.current_task", return_value=None):
            with patch.object(alert_monitor, "_monitor_loop", new_callable=AsyncMock):
                await alert_monitor.start(mock_bot)
                assert alert_monitor._running
                
                await alert_monitor.stop()
                assert not alert_monitor._running
                assert mock_task.cancelled()

@pytest.mark.asyncio
async def test_check_thresholds_drop(alert_monitor, mocker):
    """Test detecting a price drop emergency."""
    # Mock price feed history
    history = [
        {"timestamp": time.time() - 1000, "prices": {"BTC": {"price": 100000.0}}},
        {"timestamp": time.time(), "prices": {"BTC": {"price": 94000.0}}} # 6% drop
    ]
    mocker.patch("bot.price_feed.price_feed.get_price_history", return_value=history)
    
    alerts = await alert_monitor.check_thresholds()
    assert len(alerts) == 1
    assert alerts[0]["asset"] == "BTC"
    assert alerts[0]["direction"] == "DROP"
    assert alerts[0]["pct_change"] == -6.0

@pytest.mark.asyncio
async def test_check_thresholds_spike(alert_monitor, mocker):
    """Test detecting a price spike emergency."""
    # Mock price feed history
    history = [
        {"timestamp": time.time() - 1000, "prices": {"ETH": {"price": 3000.0}}},
        {"timestamp": time.time(), "prices": {"ETH": {"price": 3300.0}}} # 10% spike
    ]
    mocker.patch("bot.price_feed.price_feed.get_price_history", return_value=history)
    
    alerts = await alert_monitor.check_thresholds()
    assert len(alerts) == 1
    assert alerts[0]["asset"] == "ETH"
    assert alerts[0]["direction"] == "SPIKE"
    assert alerts[0]["pct_change"] == 10.0

@pytest.mark.asyncio
async def test_trigger_emergency(alert_monitor, mock_bot, mocker):
    """Test triggering an emergency broadcasts and schedules a meeting."""
    alerts = [{"asset": "BTC", "direction": "DROP", "pct_change": -6.0}]
    
    # Mock meeting scheduler
    mock_scheduler = mocker.patch("bot.scheduler.meeting_scheduler.schedule_emergency", new_callable=AsyncMock)
    
    await alert_monitor._trigger_emergency(alerts, mock_bot)
    
    # Assert Discord channel message
    mock_bot._trading_floor_channel.send.assert_awaited_once()
    assert "EMERGENCY ALERT" in mock_bot._trading_floor_channel.send.call_args[0][0]
    
    # Assert System Status
    mock_bot.post_system_status.assert_awaited_once()

    
    # Assert scheduler
    mock_scheduler.assert_awaited_once_with(alerts)

@pytest.mark.asyncio
async def test_monitor_loop_execution(alert_monitor, mock_bot, mocker):
    alert_monitor._running = True
    alert_monitor._bot = mock_bot
    
    # Break loop by making sleep raise CancelledError
    mocker.patch("asyncio.sleep", side_effect=asyncio.CancelledError)
    
    mock_prices = {"BTC": {"price": 100000.0}}
    mocker.patch("bot.alerts.price_feed.get_prices", new_callable=AsyncMock, return_value=mock_prices)
    
    mock_portfolio = mocker.patch("bot.portfolio.portfolio")
    mock_portfolio.check_orders.return_value = [{"asset": "BTC", "fill_price": 100000.0}]
    
    mocker.patch.object(alert_monitor, "_notify_executed_orders", new_callable=AsyncMock)
    mocker.patch.object(alert_monitor, "check_thresholds", new_callable=AsyncMock, return_value=[{"asset": "BTC", "direction": "DROP"}])
    mocker.patch.object(alert_monitor, "_trigger_emergency", new_callable=AsyncMock)
    
    with pytest.raises(asyncio.CancelledError):
        await alert_monitor._monitor_loop()
        
    mock_portfolio.check_orders.assert_called_once_with(mock_prices)
    alert_monitor._notify_executed_orders.assert_awaited_once()
    alert_monitor.check_thresholds.assert_awaited_once()
    alert_monitor._trigger_emergency.assert_awaited_once()

@pytest.mark.asyncio
async def test_check_thresholds_edge_cases(alert_monitor, mocker):
    mocker.patch("bot.alerts.price_feed.get_price_history", side_effect=Exception("DB Error"))
    assert await alert_monitor.check_thresholds() == []
    
    mocker.patch("bot.alerts.price_feed.get_price_history", return_value=[{"timestamp": 1, "prices": {}}])
    assert await alert_monitor.check_thresholds() == []

@pytest.mark.asyncio
async def test_notify_executed_orders_normal(alert_monitor, mock_bot, mocker):
    """Test normal order execution does not trigger emergency."""
    orders = [{"triggered_order_type": "LIMIT", "action": "BUY", "quantity": 1.0, "asset": "BTC", "fill_price": 50000.0, "usd_amount": 50000.0}]
    
    mock_scheduler = mocker.patch("bot.scheduler.meeting_scheduler.schedule_emergency", new_callable=AsyncMock)
    
    await alert_monitor._notify_executed_orders(orders, mock_bot)
    
    mock_bot._trading_floor_channel.send.assert_awaited_once()
    assert "Orders Executed" in mock_bot._trading_floor_channel.send.call_args[0][0]
    
    mock_scheduler.assert_not_awaited()

@pytest.mark.asyncio
async def test_notify_executed_orders_stop_loss(alert_monitor, mock_bot, mocker):
    """Test STOP LOSS order execution triggers emergency."""
    orders = [{"triggered_order_type": "STOP", "action": "SELL", "quantity": 1.0, "asset": "ETH", "fill_price": 2000.0, "usd_amount": 2000.0}]
    
    mock_scheduler = mocker.patch("bot.scheduler.meeting_scheduler.schedule_emergency", new_callable=AsyncMock)
    
    await alert_monitor._notify_executed_orders(orders, mock_bot)
    
    mock_bot._trading_floor_channel.send.assert_awaited_once()
    assert "STOP LOSS TRIGGERED" in mock_bot._trading_floor_channel.send.call_args[0][0]
    
    mock_scheduler.assert_awaited_once()
    args = mock_scheduler.call_args[0][0]
    assert len(args) == 1
    assert args[0]["direction"] == "STOP_LOSS"
