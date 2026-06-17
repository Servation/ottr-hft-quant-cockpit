import pytest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
import os
import asyncio

from bot.main import TradingFloorBot

@pytest.fixture
def bot(mocker):
    b = TradingFloorBot()
    mocker.patch("discord.Client.user", new_callable=PropertyMock, return_value=MagicMock(id=123))
    b.loop = MagicMock()
    b.loop.create_task = MagicMock()
    b._system_status_channel = MagicMock()
    b._system_status_channel.send = AsyncMock()
    return b

@pytest.mark.asyncio
async def test_on_ready_channels_not_configured(bot, mocker):
    mocker.patch.dict("bot.settings", {
        "discord_trading_floor_channel_id": "",
        "discord_system_status_channel_id": "",
    })
    
    bot.close = AsyncMock()
    await bot.on_ready()
    bot.close.assert_awaited_once()

@pytest.mark.asyncio
async def test_on_ready_trading_floor_not_found(bot, mocker):
    mocker.patch.dict("bot.settings", {
        "discord_trading_floor_channel_id": "123",
        "discord_system_status_channel_id": "456",
    })
    
    bot.get_channel = MagicMock(side_effect=lambda cid: None if cid == 123 else MagicMock())
    bot.close = AsyncMock()
    
    await bot.on_ready()
    bot.close.assert_awaited_once()

@pytest.mark.asyncio
async def test_on_ready_system_status_not_found(bot, mocker):
    mocker.patch.dict("bot.settings", {
        "discord_trading_floor_channel_id": "123",
        "discord_system_status_channel_id": "456",
    })
    
    floor_mock = MagicMock()
    bot.get_channel = MagicMock(side_effect=lambda cid: floor_mock if cid == 123 else None)
    
    mocker.patch.object(bot, "_setup_webhooks", new_callable=AsyncMock)
    floor_mock.send = AsyncMock()
    bot.post_system_status = AsyncMock()
    
    # prevent loop from running tasks forever
    mocker.patch("bot.main.meeting_scheduler.start", new_callable=AsyncMock)
    mocker.patch.object(bot.loop, "create_task")
    
    await bot.on_ready()
    
    assert bot._system_status_channel == floor_mock

@pytest.mark.asyncio
async def test_startup_meeting_logic(bot, tmp_path, mocker):
    mocker.patch("asyncio.sleep", new_callable=AsyncMock)
    mocker.patch("bot.scheduler.meeting_scheduler._execute_meeting", new_callable=AsyncMock)
    
    bot._trading_floor_channel = MagicMock()
    bot._trading_floor_channel.send = AsyncMock()
    
    original_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        os.makedirs("data", exist_ok=True)
        # Test meeting execution
        await bot._startup_meeting()
        assert os.path.exists("data/last_startup_meeting.txt")
        
        # Test skip
        await bot._startup_meeting()
        bot._trading_floor_channel.send.assert_awaited_once() # Only called once from the first time
    finally:
        os.chdir(original_cwd)

@pytest.mark.asyncio
async def test_post_as_agent_splitting(bot, mocker):
    mock_webhook = AsyncMock()
    bot.webhooks["test_agent"] = mock_webhook
    bot._last_webhook_post = 0
    
    mocker_agents = {"test_agent": MagicMock(name="Test", avatar_url="url")}
    with patch("bot.main.AGENTS", mocker_agents):
        # Create a message > 2000 chars
        long_msg = "A" * 2500
        await bot.post_as_agent("test_agent", long_msg)
        
    assert mock_webhook.send.call_count == 2
