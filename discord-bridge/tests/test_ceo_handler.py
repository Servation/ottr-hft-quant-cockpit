import pytest
from unittest.mock import AsyncMock, MagicMock
import discord

from bot.ceo_handler import CEOHandler
from bot.agents import AGENTS

@pytest.fixture
def mock_message():
    msg = MagicMock(spec=discord.Message)
    msg.content = "Test message"
    msg.author = MagicMock()
    msg.author.bot = False
    msg.channel = MagicMock()
    msg.channel.send = AsyncMock()
    return msg

@pytest.fixture
def mock_bot():
    return MagicMock()

@pytest.fixture
def mock_scheduler():
    scheduler = MagicMock()
    scheduler.schedule_emergency = AsyncMock()
    return scheduler

@pytest.fixture
def ceo_handler(mock_scheduler):
    handler = CEOHandler()
    # Mock meeting_scheduler global within the module
    import bot.ceo_handler as ch
    ch.meeting_scheduler = mock_scheduler
    return handler

@pytest.mark.asyncio
async def test_routing_emergency(ceo_handler, mock_message, mock_bot, mocker):
    """Test [EMERGENCY] routing."""
    # Mock LLM
    mock_llm = mocker.patch("bot.ceo_handler.agent_llm.generate_response", new_callable=AsyncMock)
    mock_llm.return_value = ("[EMERGENCY]", None)
    
    # Spy on _queue_directive
    mocker.patch.object(ceo_handler, "_queue_directive")
    
    await ceo_handler.on_message(mock_message, mock_bot)
    
    # Assertions
    mock_message.channel.send.assert_awaited_with("🚨 **Emergency Override Recognized!** Waking up the full team immediately...")
    ceo_handler._queue_directive.assert_called_once_with(mock_message)
    # The scheduler should be called
    import bot.ceo_handler as ch
    ch.meeting_scheduler.schedule_emergency.assert_called_once()

@pytest.mark.asyncio
async def test_routing_direct_valid(ceo_handler, mock_message, mock_bot, mocker):
    """Test [DIRECT:agent_id] routing with valid ID."""
    mock_llm = mocker.patch("bot.ceo_handler.agent_llm.generate_response", new_callable=AsyncMock)
    mock_llm.return_value = ("[DIRECT:portfolio_manager]", None)
    
    # Spy
    mocker.patch.object(ceo_handler, "_handle_direct_message", new_callable=AsyncMock)
    
    await ceo_handler.on_message(mock_message, mock_bot)
    
    # Assertions
    ceo_handler._handle_direct_message.assert_awaited_once_with(mock_message, "portfolio_manager", mock_bot)

@pytest.mark.asyncio
async def test_routing_direct_name_fallback(ceo_handler, mock_message, mock_bot, mocker):
    """Test [DIRECT:agent_name] resolving to agent_id."""
    mock_llm = mocker.patch("bot.ceo_handler.agent_llm.generate_response", new_callable=AsyncMock)
    mock_llm.return_value = ("[DIRECT:midas]", None)
    
    # Spy
    mocker.patch.object(ceo_handler, "_handle_direct_message", new_callable=AsyncMock)
    
    await ceo_handler.on_message(mock_message, mock_bot)
    
    # Assertions
    ceo_handler._handle_direct_message.assert_awaited_once_with(mock_message, "portfolio_manager", mock_bot)

@pytest.mark.asyncio
async def test_routing_direct_invalid_fallback(ceo_handler, mock_message, mock_bot, mocker):
    """Test [DIRECT:invalid] falling back to [QUEUE]."""
    mock_llm = mocker.patch("bot.ceo_handler.agent_llm.generate_response", new_callable=AsyncMock)
    mock_llm.return_value = ("[DIRECT:satoshi]", None)
    
    mocker.patch.object(ceo_handler, "_queue_directive")
    
    await ceo_handler.on_message(mock_message, mock_bot)
    
    # Assertions
    ceo_handler._queue_directive.assert_called_once_with(mock_message)
    mock_message.channel.send.assert_awaited_with("📋 CEO directive queued. Will address in next meeting.")

@pytest.mark.asyncio
async def test_routing_queue(ceo_handler, mock_message, mock_bot, mocker):
    """Test [QUEUE] routing."""
    mock_llm = mocker.patch("bot.ceo_handler.agent_llm.generate_response", new_callable=AsyncMock)
    mock_llm.return_value = ("[QUEUE]", None)
    
    mocker.patch.object(ceo_handler, "_queue_directive")
    
    await ceo_handler.on_message(mock_message, mock_bot)
    
    # Assertions
    ceo_handler._queue_directive.assert_called_once_with(mock_message)
    mock_message.channel.send.assert_awaited_with(f"📋 CEO directive received: *{mock_message.content[:50]}*. Will address in next meeting.")

@pytest.mark.asyncio
async def test_routing_llm_failure(ceo_handler, mock_message, mock_bot, mocker):
    """Test LLM exception falling back to [QUEUE]."""
    mock_llm = mocker.patch("bot.ceo_handler.agent_llm.generate_response", new_callable=AsyncMock)
    mock_llm.side_effect = Exception("API offline")
    
    mocker.patch.object(ceo_handler, "_queue_directive")
    
    await ceo_handler.on_message(mock_message, mock_bot)
    
    ceo_handler._queue_directive.assert_called_once_with(mock_message)

@pytest.mark.asyncio
async def test_handle_direct_message_success(ceo_handler, mock_message, mock_bot, mocker):
    mock_price_feed = mocker.patch("bot.ceo_handler.price_feed")
    mock_price_feed.get_market_state_summary = AsyncMock(return_value="Prices")
    mock_price_feed.get_prices = AsyncMock(return_value={})
    
    mock_portfolio = mocker.patch("bot.ceo_handler.portfolio")
    mock_portfolio.get_summary.return_value = "Portfolio"
    
    mock_memory = mocker.patch("bot.ceo_handler.meeting_memory")
    mock_memory.get_semantic_context = AsyncMock(return_value="Memory")
    
    mock_history_msg = MagicMock()
    mock_history_msg.content = "Prev"
    mock_history_msg.author.bot = False
    mock_history_msg.clean_content = "Prev"
    mock_message.channel.history.return_value.__aiter__.return_value = [mock_history_msg]
    
    mock_llm = mocker.patch("bot.ceo_handler.agent_llm.generate_response", new_callable=AsyncMock)
    mock_llm.return_value = ("Direct Response", None)
    
    mock_bot.post_as_agent = AsyncMock()
    
    mock_scheduler = mocker.patch("bot.ceo_handler.meeting_scheduler")
    mock_scheduler.get_next_meeting_info.return_value = ("meeting", "time")
    
    # Mock ack message
    mock_ack = AsyncMock()
    mock_message.channel.send.return_value = mock_ack
    
    await ceo_handler._handle_direct_message(mock_message, "portfolio_manager", mock_bot)
    
    mock_ack.delete.assert_awaited_once()
    mock_bot.post_as_agent.assert_awaited_once_with("portfolio_manager", "Direct Response")

@pytest.mark.asyncio
async def test_handle_direct_message_error(ceo_handler, mock_message, mock_bot, mocker):
    mock_price_feed = mocker.patch("bot.ceo_handler.price_feed")
    mock_price_feed.get_market_state_summary = AsyncMock(side_effect=Exception("Failed"))
    
    mock_ack = AsyncMock()
    mock_message.channel.send.return_value = mock_ack
    
    await ceo_handler._handle_direct_message(mock_message, "portfolio_manager", mock_bot)
    
    mock_ack.edit.assert_called_once()

@pytest.mark.asyncio
async def test_routing_discussion_valid(ceo_handler, mock_message, mock_bot, mocker):
    """Test [DISCUSSION:agent1,agent2] routing with valid IDs."""
    mock_llm = mocker.patch("bot.ceo_handler.agent_llm.generate_response", new_callable=AsyncMock)
    mock_llm.return_value = ("[DISCUSSION:portfolio_manager,risk_auditor]", None)
    
    # Spy
    mocker.patch.object(ceo_handler, "_handle_discussion_mode", new_callable=AsyncMock)
    
    await ceo_handler.on_message(mock_message, mock_bot)
    
    # Assertions
    ceo_handler._handle_discussion_mode.assert_awaited_once_with(mock_message, ["portfolio_manager", "risk_auditor"], mock_bot)

@pytest.mark.asyncio
async def test_handle_discussion_mode_success(ceo_handler, mock_message, mock_bot, mocker):
    """Test the discussion mode loop runs exactly 3 times and calls the moderator."""
    mock_price_feed = mocker.patch("bot.ceo_handler.price_feed")
    mock_price_feed.get_market_state_summary = AsyncMock(return_value="Prices")
    mock_price_feed.get_prices = AsyncMock(return_value={})
    
    mock_portfolio = mocker.patch("bot.ceo_handler.portfolio")
    mock_portfolio.get_summary.return_value = "Portfolio"
    
    mock_memory = mocker.patch("bot.ceo_handler.meeting_memory")
    mock_memory.get_semantic_context = AsyncMock(return_value="Memory")
    
    mock_llm = mocker.patch("bot.ceo_handler.agent_llm.generate_response", new_callable=AsyncMock)
    mock_llm.return_value = ("Discussion Response", None)
    
    mock_bot.post_as_agent = AsyncMock()
    
    mock_scheduler = mocker.patch("bot.ceo_handler.meeting_scheduler")
    mock_scheduler.get_next_meeting_info.return_value = ("meeting", "time")
    
    await ceo_handler._handle_discussion_mode(mock_message, ["portfolio_manager", "risk_auditor"], mock_bot)
    
    # Assertions
    # Called 3 times for the discussion, plus 1 time for Athena (meeting_chair)
    assert mock_llm.call_count == 4
    assert mock_bot.post_as_agent.call_count == 4

def test_queue_and_format_directives(ceo_handler, mock_message):
    ceo_handler._queue_directive(mock_message)
    
    assert ceo_handler.has_pending() is True
    
    formatted = ceo_handler.format_directives_for_context()
    assert mock_message.content in formatted
    
    pending = ceo_handler.get_pending_directives()
    assert len(pending) == 1
    assert not ceo_handler.has_pending()

@pytest.mark.asyncio
async def test_truncate_long_directive(ceo_handler, mock_bot, mocker):
    mock_msg = MagicMock()
    mock_msg.content = "A" * 150
    mock_msg.author.bot = False
    mock_msg.channel.send = AsyncMock()
    
    mock_llm = mocker.patch("bot.ceo_handler.agent_llm.generate_response", new_callable=AsyncMock)
    mock_llm.return_value = ("[QUEUE]", None)
    
    await ceo_handler.on_message(mock_msg, mock_bot)
    
    mock_msg.channel.send.assert_awaited_once()
    assert "…" in mock_msg.channel.send.call_args[0][0]
