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
    
    # The marker is bot.main.LAST_STARTUP_MEETING_FILE (a module constant). Redirect it to a
    # temp file so the test is CWD-independent and never pollutes the real data dir; this also
    # overrides the conftest data-isolation fixture's redirect for this test's own assertion.
    marker = tmp_path / "last_startup_meeting.txt"
    mocker.patch("bot.main.LAST_STARTUP_MEETING_FILE", marker)

    bot._trading_floor_channel = MagicMock()
    bot._trading_floor_channel.send = AsyncMock()

    # First call runs the meeting and writes the marker.
    await bot._startup_meeting()
    assert marker.exists()

    # Second call sees the recent marker and skips (cooldown).
    await bot._startup_meeting()
    bot._trading_floor_channel.send.assert_awaited_once()  # only the first call sends

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
