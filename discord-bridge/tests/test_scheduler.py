import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from bot.scheduler import MeetingScheduler

@pytest.fixture
def mock_bot():
    bot = MagicMock()
    bot.post_system_status = AsyncMock()
    bot.post_as_agent = AsyncMock()
    return bot

@pytest.fixture
def scheduler():
    return MeetingScheduler()


@pytest.fixture(autouse=True)
def _llm_online(mocker):
    # _execute_meeting aborts if the LLM health check fails; tests don't run a
    # real LLM, so force it healthy. No test exercises the offline-abort path.
    mocker.patch("bot.agents.agent_llm.check_health", new_callable=AsyncMock, return_value=True)

@pytest.mark.asyncio
async def test_scheduler_lifecycle(scheduler, mock_bot):
    """Test start and stop of MeetingScheduler."""
    assert scheduler._scheduler is None
    
    await scheduler.start(mock_bot)
    assert scheduler._scheduler is not None
    assert scheduler._scheduler.running
    
    # 6 meeting-hour cron jobs + 2 background interval jobs
    # ("evaluate_predictions" and "log_equity_snapshot").
    jobs = scheduler._scheduler.get_jobs()
    meeting_jobs = [j for j in jobs if j.id.startswith("meeting_")]
    assert len(meeting_jobs) == 6
    assert any(j.id == "evaluate_predictions" for j in jobs)
    assert any(j.id == "log_equity_snapshot" for j in jobs)
    assert len(jobs) == 8
    
    await scheduler.stop()
    # apscheduler wait=False might not immediately reflect running status if pending jobs
    await asyncio.sleep(0.1)
    assert not scheduler._scheduler.running

@pytest.mark.asyncio
async def test_schedule_emergency(scheduler, mock_bot, mocker):
    """Test schedule_emergency calls _execute_meeting."""
    alerts = [{"asset": "BTC", "direction": "DROP", "pct_change": -5.0}]
    
    mock_execute = mocker.patch.object(scheduler, "_execute_meeting", new_callable=AsyncMock)
    
    await scheduler.schedule_emergency(alerts)
    mock_execute.assert_awaited_once_with(emergency_data=alerts)

@pytest.mark.asyncio
async def test_run_scheduled_meeting(scheduler, mocker):
    """Test _run_scheduled_meeting calls _execute_meeting."""
    mock_execute = mocker.patch.object(scheduler, "_execute_meeting", new_callable=AsyncMock)
    
    await scheduler._run_scheduled_meeting()
    mock_execute.assert_awaited_once_with(emergency_data=None)

@pytest.mark.asyncio
async def test_schedule_dynamic_meeting(scheduler, mock_bot):
    """Test schedule_dynamic_meeting adds a new job."""
    await scheduler.start(mock_bot)
    
    initial_jobs = len(scheduler._scheduler.get_jobs())
    
    scheduler.schedule_dynamic_meeting(15)
    
    new_jobs = len(scheduler._scheduler.get_jobs())
    assert new_jobs == initial_jobs + 1
    
    await scheduler.stop()

@pytest.mark.asyncio
async def test_execute_meeting(scheduler, mock_bot, mocker):
    """Test full orchestration logic without exceptions."""
    # Setup mocks for dependencies
    mocker.patch("bot.price_feed.price_feed.get_prices", new_callable=AsyncMock, return_value={"BTC": {"price": 50000.0}})
    mocker.patch("bot.price_feed.price_feed.get_market_state_summary", new_callable=AsyncMock, return_value="Market is stable")
    mocker.patch("bot.portfolio.portfolio.get_summary", return_value={"cash": 10000, "holdings": {"BTC": {"quantity": 1, "avg_cost": 40000}}, "total_pnl": 10000})
    mocker.patch("bot.ceo_handler.ceo_handler.format_directives_for_context", return_value="Test directive")
    mocker.patch("bot.ceo_handler.ceo_handler.get_pending_directives", return_value=[])
    
    mock_memory_semantic = mocker.patch("bot.memory.meeting_memory.get_semantic_context", new_callable=AsyncMock, return_value="Mock memory")
    mocker.patch("bot.memory.meeting_memory.get_recent_context", return_value="Recent context")
    mock_engine = mocker.patch("bot.meetings.meeting_engine.run_meeting", new_callable=AsyncMock)
    mock_rotation = mocker.patch("bot.meetings.meeting_rotation.get_next_meeting_type", return_value="morning_briefing")
    
    scheduler._bot = mock_bot
    await scheduler._execute_meeting(emergency_data=None)
    
    # Assertions
    mock_engine.assert_awaited_once()
    kwargs = mock_engine.call_args[1]
    assert kwargs["meeting_type_id"] == "morning_briefing"
    assert kwargs["price_data"] == "Market is stable"
    assert "BTC:** 1.0" in kwargs["portfolio_summary"]
    assert kwargs["ceo_directives"] == "Test directive"
    assert "Mock memory" in kwargs["memory_context"]
    
    # Assert system status post
    mock_bot.post_system_status.assert_awaited()
