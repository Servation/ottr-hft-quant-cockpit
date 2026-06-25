import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import json
import logging

from bot.meetings import MeetingEngine, MeetingRotation, MEETING_TYPES, ROTATION_ORDER
from bot.portfolio import portfolio
from bot.memory import meeting_memory

@pytest.fixture
def engine():
    return MeetingEngine()

@pytest.mark.asyncio
async def test_meeting_unknown_type(engine):
    with pytest.raises(ValueError, match="Unknown meeting type"):
        await engine.run_meeting("unknown_type", AsyncMock())

@pytest.mark.asyncio
async def test_memory_context_similarity(engine, mocker):
    mock_memory = mocker.patch("bot.meetings.meeting_memory")
    
    # Return 2 similar meetings, 1 with long summary to test truncation
    mock_memory.query_similar_meetings.return_value = [
        {"timestamp": "1", "type": "T", "summary": "Short"},
        {"timestamp": "2", "type": "T", "summary": "Long " * 200}
    ]
    
    mocker.patch.dict("bot.settings", {"token_budgets": {"meeting_history": 50}}) # budget
    
    mock_llm = mocker.patch("bot.meetings.agent_llm.generate_response", new_callable=AsyncMock)
    mock_llm.return_value = ("Hello", None)
    
    mocker.patch.object(engine, "_build_opening", return_value="opening")

    
    # We will raise an exception in the middle of run_meeting to abort early and just test memory context building
    mock_post = AsyncMock(side_effect=Exception("ABORT"))
    
    try:
        await engine.run_meeting("morning_briefing", mock_post, "price", "port", "")
    except Exception as e:
        assert str(e) == "ABORT"

@pytest.mark.asyncio
async def test_memory_context_exception(engine, mocker):
    mock_memory = mocker.patch("bot.meetings.meeting_memory")
    mock_memory.query_similar_meetings.side_effect = Exception("DB FAIL")
    
    mock_llm = mocker.patch("bot.meetings.agent_llm.generate_response", new_callable=AsyncMock)
    mock_llm.return_value = ("Hello", None)
    
    mock_post = AsyncMock(side_effect=Exception("ABORT"))
    
    try:
        await engine.run_meeting("morning_briefing", mock_post, "price", "port", "")
    except Exception as e:
        assert str(e) == "ABORT"

@pytest.mark.asyncio
async def test_full_meeting_run(engine, mocker):
    mock_post = AsyncMock()
    mock_llm = mocker.patch("bot.meetings.agent_llm.generate_response", new_callable=AsyncMock)
    
    # 1. Opening -> 2. Agent 1 -> 3. Agent 2 -> 4. Closing
    # Let's provide some responses
    mock_llm.return_value = ("Test response \n[TRADE: BUY BTC 5000]\nDecision: yes\nAction: yes", None)
    
    mocker.patch.object(meeting_memory, "save_meeting", new_callable=AsyncMock)
    
    record = await engine.run_meeting("risk_review", mock_post, memory_context="mem")
    
    assert "yes" in record["summary"]
    
    # Test exceptions in LLM
    mock_llm.side_effect = Exception("LLM DEAD")
    with pytest.raises(Exception, match="LLM DEAD"):
        await engine.run_meeting("risk_review", mock_post, memory_context="mem")



def test_summarize_outcome_from_votes():
    """Meeting records summarize the actual votes, not the chair's generic prose."""
    from bot.meetings import MeetingEngine

    # Unanimous abstain -> a distinct, recordable outcome (was boilerplate before).
    contribs = {f"a{i}": "report\n\n[DEBATE]: Final Vote: ABSTAIN SOL" for i in range(6)}
    summary, decisions = MeetingEngine._summarize_outcome(
        "Trade Execution", contribs, "I have reviewed the consensus."
    )
    assert "SOL" in summary and "ABSTAIN" in summary
    assert decisions == ["No position changes (no directional consensus)"]

    # Directional majority -> captured as a real decision with the vote split.
    contribs = {
        "a1": "Final Vote: SELL BTC", "a2": "Final Vote: SELL BTC",
        "a3": "Final Vote: SELL BTC", "a4": "Final Vote: HOLD BTC",
    }
    summary, decisions = MeetingEngine._summarize_outcome("Trade Execution", contribs, "")
    assert "BTC" in summary and "SELL" in summary
    assert any("SELL BTC" in d and "3/4" in d for d in decisions)

    # Belief revision: an agent's last vote supersedes the original.
    contribs = {"a1": "Final Vote: BUY ETH\n\n[DEBATE]: Final Vote: SELL ETH"}
    summary, _ = MeetingEngine._summarize_outcome("Trade Execution", contribs, "")
    assert "SELL" in summary and "BUY" not in summary

    # No votes (e.g. strategy session) -> falls back to the closing prose.
    summary, _ = MeetingEngine._summarize_outcome(
        "Strategy Session", {"a1": "no vote here"}, "Discussed allocation strategy."
    )
    assert summary.startswith("Discussed allocation strategy")

    # LLM error closing with no votes -> clean summary, not the raw error string.
    summary, decisions = MeetingEngine._summarize_outcome(
        "Strategy Session", {}, "[error] LLM call failed: Connection error."
    )
    assert "did not complete" in summary and "[error]" not in summary


def test_meeting_rotation(tmp_path, mocker):
    state_file = tmp_path / "rotation.json"
    mocker.patch("bot.meetings.ROTATION_STATE_PATH", state_file)
    
    rot = MeetingRotation()
    # peek returns the head of the rotation without advancing
    assert rot.peek_next_meeting_type() == ROTATION_ORDER[0]
    assert rot.peek_next_meeting_type() == ROTATION_ORDER[0]

    # get advances through the full order, then wraps back to the start
    for expected in ROTATION_ORDER:
        assert rot.get_next_meeting_type() == expected
    assert rot.peek_next_meeting_type() == ROTATION_ORDER[0]

    # Corrupt state file -> index resets to 0 on reload
    state_file.write_text("INVALID JSON")
    rot2 = MeetingRotation()
    assert rot2.peek_next_meeting_type() == ROTATION_ORDER[0]
