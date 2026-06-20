import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from bot.scheduler import MeetingScheduler
from bot.main import TradingFloorBot

@pytest.fixture
def mock_bot():
    bot = MagicMock(spec=TradingFloorBot)
    bot.post_system_status = AsyncMock()
    bot.post_as_agent = AsyncMock()
    bot.post_audit_log = AsyncMock()
    return bot

@pytest.fixture
def scheduler(mock_bot):
    sched = MeetingScheduler()
    sched._bot = mock_bot
    return sched


@pytest.fixture(autouse=True)
def _llm_online(mocker):
    # _execute_meeting aborts if the LLM health check fails; tests don't run a
    # real LLM, so force it healthy. No test exercises the offline-abort path.
    mocker.patch("bot.agents.agent_llm.check_health", new_callable=AsyncMock, return_value=True)

@pytest.mark.asyncio
async def test_scheduler_locked(scheduler, mocker):
    mocker.patch.object(scheduler._meeting_lock, "locked", return_value=True)
    # Should log warning and skip
    await scheduler._run_scheduled_meeting()
    # Should queue after it
    await scheduler.schedule_emergency([])

def test_schedule_dynamic_meeting_no_scheduler(scheduler):
    # Should return early if no scheduler
    scheduler._scheduler = None
    scheduler.schedule_dynamic_meeting(5)

@pytest.mark.asyncio
async def test_execute_meeting_exceptions(scheduler, mocker):
    mock_price_feed = mocker.patch("bot.price_feed.price_feed")
    mock_portfolio = mocker.patch("bot.portfolio.portfolio")
    mock_ceo = mocker.patch("bot.ceo_handler.ceo_handler")
    mock_memory = mocker.patch("bot.memory.meeting_memory")
    mock_engine = mocker.patch("bot.meetings.meeting_engine")
    mock_rotation = mocker.patch("bot.meetings.meeting_rotation")
    
    # Exceptions
    mock_price_feed.get_prices = AsyncMock(side_effect=Exception("Price fail"))
    mock_portfolio.get_summary = MagicMock(side_effect=Exception("Port fail"))
    # Market data IS available here; the meeting must still run despite the other
    # (non-fatal) failures. Market-data failure is a deliberate abort, tested
    # separately in test_execute_meeting_aborts_without_market_data.
    mock_price_feed.get_market_state_summary = AsyncMock(return_value="Market summary OK")

    mock_memory.get_semantic_context = AsyncMock(side_effect=Exception("Memory fail 1"))
    mock_memory.get_recent_context = MagicMock(side_effect=Exception("Memory fail 2"))
    
    mock_rotation.get_next_meeting_type = MagicMock(return_value="morning_briefing")
    mock_engine.run_meeting = AsyncMock()
    
    await scheduler._execute_meeting()
    
    # Check that exceptions were caught and run_meeting was still called
    mock_engine.run_meeting.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_meeting_aborts_without_market_data(scheduler, mocker):
    """If market data is unavailable, the meeting must ABORT (agents must not
    trade blind) — run_meeting is never called and an abort status is posted."""
    mock_price_feed = mocker.patch("bot.price_feed.price_feed")
    mock_portfolio = mocker.patch("bot.portfolio.portfolio")
    mock_ceo = mocker.patch("bot.ceo_handler.ceo_handler")
    mock_memory = mocker.patch("bot.memory.meeting_memory")
    mock_engine = mocker.patch("bot.meetings.meeting_engine")
    mock_rotation = mocker.patch("bot.meetings.meeting_rotation")

    mock_price_feed.get_prices = AsyncMock(return_value={"BTC": {"price": 1.0}})
    mock_price_feed.get_market_state_summary = AsyncMock(side_effect=Exception("Summary fail"))
    mock_rotation.get_next_meeting_type = MagicMock(return_value="morning_briefing")
    mock_engine.run_meeting = AsyncMock()

    await scheduler._execute_meeting()

    mock_engine.run_meeting.assert_not_awaited()
    # An abort notice should have been posted to the system status channel.
    posted = " ".join(str(c) for c in scheduler._bot.post_system_status.call_args_list)
    assert "ABORT" in posted.upper()

@pytest.mark.asyncio
async def test_execute_meeting_portfolio_format_exception(scheduler, mocker):
    mock_price_feed = mocker.patch("bot.price_feed.price_feed")
    mock_portfolio = mocker.patch("bot.portfolio.portfolio")
    mock_ceo = mocker.patch("bot.ceo_handler.ceo_handler")
    mock_memory = mocker.patch("bot.memory.meeting_memory")
    mock_engine = mocker.patch("bot.meetings.meeting_engine")
    mock_rotation = mocker.patch("bot.meetings.meeting_rotation")
    
    mock_price_feed.get_prices = AsyncMock(return_value={})
    
    # Format exception due to invalid types
    mock_portfolio.get_summary = MagicMock(return_value={
        "cash": "invalid_type",
        "holdings": {"BTC": "invalid"}
    })
    mock_price_feed.get_market_state_summary = AsyncMock(return_value="Prices")
    mock_memory.get_semantic_context = AsyncMock(return_value="Memory")
    mock_memory.get_recent_context = MagicMock(return_value="Recent")
    
    mock_rotation.get_next_meeting_type = MagicMock(return_value="morning_briefing")
    mock_engine.run_meeting = AsyncMock()
    
    await scheduler._execute_meeting()
    
    mock_engine.run_meeting.assert_awaited_once()

@pytest.mark.asyncio
async def test_execute_meeting_engine_run_meeting_exception(scheduler, mocker):
    mock_price_feed = mocker.patch("bot.price_feed.price_feed")
    mock_portfolio = mocker.patch("bot.portfolio.portfolio")
    mock_ceo = mocker.patch("bot.ceo_handler.ceo_handler")
    mock_memory = mocker.patch("bot.memory.meeting_memory")
    mock_engine = mocker.patch("bot.meetings.meeting_engine")
    mock_rotation = mocker.patch("bot.meetings.meeting_rotation")
    
    mock_price_feed.get_prices = AsyncMock(return_value={})
    mock_portfolio.get_summary = MagicMock(return_value={})
    mock_price_feed.get_market_state_summary = AsyncMock(return_value="Prices")
    mock_memory.get_semantic_context = AsyncMock(return_value="Memory")
    mock_memory.get_recent_context = MagicMock(return_value="Recent")
    
    mock_rotation.get_next_meeting_type = MagicMock(return_value="morning_briefing")
    mock_engine.run_meeting = AsyncMock(side_effect=Exception("Meeting failed"))
    
    await scheduler._execute_meeting()
    mock_bot = scheduler._bot
    mock_bot.post_system_status.assert_awaited()
    
    mock_engine.run_meeting.assert_awaited_once()
