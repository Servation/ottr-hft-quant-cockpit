import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import discord
from bot.main import TradingFloorBot, _split_message, _shutdown

@pytest.fixture
def bot(mocker):
    # Mock discord.Client init to avoid actual connection
    mocker.patch("discord.Client.__init__")
    
    bot_instance = TradingFloorBot()
    mock_user = MagicMock()
    mock_user.id = 123
    mocker.patch("discord.Client.user", new_callable=mocker.PropertyMock, return_value=mock_user)
    bot_instance.loop = MagicMock()
    bot_instance.loop.create_task = MagicMock()
    return bot_instance

def test_split_message():
    # Short message
    chunks = _split_message("Hello", limit=10)
    assert chunks == ["Hello"]
    
    # Message splitting on newline
    long_msg = "A" * 15 + "\n" + "B" * 10
    chunks = _split_message(long_msg, limit=20)
    assert chunks[0] == "A" * 15
    assert chunks[1] == "B" * 10
    
    # Message splitting on space
    long_msg2 = "A" * 15 + " " + "B" * 10
    chunks = _split_message(long_msg2, limit=20)
    assert chunks[0] == "A" * 15
    assert chunks[1] == " " + "B" * 10
    
    # Hard split
    long_msg3 = "A" * 25
    chunks = _split_message(long_msg3, limit=10)
    assert chunks == ["A" * 10, "A" * 10, "A" * 5]

@pytest.mark.asyncio
async def test_on_ready_success(bot, mocker):
    mocker.patch.dict("bot.settings", {
        "discord_trading_floor_channel_id": "111",
        "discord_system_status_channel_id": "111"
    })
    
    mock_tf = MagicMock()
    mock_ss = MagicMock()
    
    def mock_get_channel(channel_id):
        if channel_id == 111:
            return mock_tf
        return None
    bot.get_channel = mock_get_channel
    bot._system_status_channel = mock_ss # To avoid fallback
    
    mocker.patch.object(bot, "_setup_webhooks", new_callable=AsyncMock)
    mocker.patch.object(bot, "post_system_status", new_callable=AsyncMock)
    mock_tf.send = AsyncMock()
    
    mocker.patch("bot.main.meeting_scheduler.start", new_callable=AsyncMock)
    mocker.patch("bot.main.alert_monitor.start", new_callable=AsyncMock)
    
    await bot.on_ready()
    
    assert bot._trading_floor_channel == mock_tf
    mock_tf.send.assert_awaited_once()
    bot.post_system_status.assert_awaited_once()
    bot._setup_webhooks.assert_awaited_once_with(mock_tf)

@pytest.mark.asyncio
async def test_on_ready_missing_config(bot, mocker):
    mocker.patch.dict("bot.settings", clear=True)
    bot.close = AsyncMock()
    
    await bot.on_ready()
    bot.close.assert_awaited_once()

@pytest.mark.asyncio
async def test_setup_webhooks(bot, mocker):
    mock_channel = MagicMock()
    
    # One existing webhook
    mock_existing_wh = MagicMock()
    mock_existing_wh.name = "Risk Manager" # Matches risk_manager persona
    mock_channel.webhooks = AsyncMock(return_value=[mock_existing_wh])
    
    mock_new_wh = MagicMock()
    mock_channel.create_webhook = AsyncMock(return_value=mock_new_wh)
    
    mocker_agents = {
        "risk_manager": MagicMock(name="Risk Manager"),
        "portfolio_manager": MagicMock(name="Portfolio Manager")
    }
    mocker_agents["risk_manager"].name = "Risk Manager"
    mocker_agents["portfolio_manager"].name = "Portfolio Manager"
    
    with patch("bot.main.AGENTS", mocker_agents):
        await bot._setup_webhooks(mock_channel)
    
    assert "risk_manager" in bot.webhooks
    assert bot.webhooks["risk_manager"] == mock_existing_wh
    assert "portfolio_manager" in bot.webhooks
    assert bot.webhooks["portfolio_manager"] == mock_new_wh

@pytest.mark.asyncio
async def test_post_as_agent_with_webhook(bot):
    mock_webhook = AsyncMock()
    bot.webhooks["test_agent"] = mock_webhook
    bot._last_webhook_post = 0 # Ensure no rate limit delay
    
    mocker_agents = {"test_agent": MagicMock(name="Test", avatar_url="url")}
    with patch("bot.main.AGENTS", mocker_agents):
        await bot.post_as_agent("test_agent", "Hello world")
        
    mock_webhook.send.assert_awaited_once()
    kwargs = mock_webhook.send.call_args[1]
    assert kwargs["content"] == "Hello world"
    assert kwargs["username"] == mocker_agents["test_agent"].name

@pytest.mark.asyncio
async def test_post_as_agent_fallback(bot, mocker):
    bot._trading_floor_channel = MagicMock()
    bot._trading_floor_channel.send = AsyncMock()
    
    await bot.post_as_agent("unknown_agent", "Hello world")

    bot._trading_floor_channel.send.assert_awaited_once()
    assert "Hello world" in bot._trading_floor_channel.send.call_args[0][0]


@pytest.mark.asyncio
async def test_post_as_agent_fallback_splits_long_message(bot):
    """The webhook-less fallback path must also respect Discord's 2000-char limit.
    A long message (e.g. the full-universe consensus breakdown posted as "APP") used to
    hit channel.send unsplit -> 400 "Invalid Form Body"."""
    bot._trading_floor_channel = MagicMock()
    bot._trading_floor_channel.send = AsyncMock()

    long_content = "X" * 5000  # ~2.5 messages' worth
    await bot.post_as_agent("APP", long_content)

    calls = bot._trading_floor_channel.send.await_args_list
    assert len(calls) >= 3  # split into multiple sends
    for c in calls:
        assert len(c[0][0]) <= 2000  # every chunk fits Discord's limit


@pytest.mark.asyncio
async def test_post_as_agent_fallback_swallows_send_error(bot):
    """A Discord send failure on the fallback path must not propagate (the meeting
    that posts the breakdown must keep running)."""
    import discord
    bot._trading_floor_channel = MagicMock()
    bot._trading_floor_channel.send = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(), "boom")
    )
    # Should not raise.
    await bot.post_as_agent("APP", "Hello world")

@pytest.mark.asyncio
async def test_post_system_status(bot):
    bot._system_status_channel = MagicMock()
    bot._system_status_channel.send = AsyncMock()
    
    await bot.post_system_status("Test status")
    
    bot._system_status_channel.send.assert_awaited_once()
    assert "Test status" in bot._system_status_channel.send.call_args[0][0]

@pytest.mark.asyncio
async def test_on_message_ceo_handler(bot, mocker):
    mock_message = MagicMock()
    mock_message.author.bot = False
    mock_message.guild = True
    
    bot._trading_floor_channel = MagicMock()
    bot._trading_floor_channel.id = 456
    mock_message.channel.id = 456
    
    mock_ceo_handler = mocker.patch("bot.main.ceo_handler.on_message", new_callable=AsyncMock)
    
    await bot.on_message(mock_message)
    mock_ceo_handler.assert_awaited_once_with(mock_message, bot=bot)

@pytest.mark.asyncio
async def test_shutdown(bot, mocker):
    mock_alert_stop = mocker.patch("bot.main.alert_monitor.stop", new_callable=AsyncMock)
    mock_sched_stop = mocker.patch("bot.main.meeting_scheduler.stop", new_callable=AsyncMock)
    bot.close = AsyncMock()
    
    await _shutdown(bot)
    
    mock_alert_stop.assert_awaited_once()
    mock_sched_stop.assert_awaited_once()
    bot.close.assert_awaited_once()

@pytest.mark.asyncio
async def test_startup_meeting(bot, mocker):
    bot._trading_floor_channel = MagicMock()
    bot._trading_floor_channel.send = AsyncMock()
    
    mock_exec = mocker.patch("bot.main.meeting_scheduler._execute_meeting", new_callable=AsyncMock)
    mocker.patch("asyncio.sleep", new_callable=AsyncMock)
    
    # Avoid early exit due to recently run meeting
    mocker.patch("os.path.exists", return_value=False)
    
    await bot._startup_meeting()
    
    bot._trading_floor_channel.send.assert_awaited_once()
    mock_exec.assert_awaited_once_with(emergency_data=None)

@pytest.mark.asyncio
async def test_on_error(bot, mocker):
    mocker.patch.object(bot, "post_system_status", new_callable=AsyncMock)
    
    await bot.on_error("test_event")
    bot.post_system_status.assert_awaited_once()
