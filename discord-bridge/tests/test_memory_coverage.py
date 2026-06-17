import pytest
from unittest.mock import MagicMock, AsyncMock, patch
import json
import os
from pathlib import Path
from bot.memory import MeetingMemory, SemanticMeetingMemory

@pytest.fixture
def memory_obj(tmp_path, mocker):
    mocker.patch("bot.memory.LOG_PATH", tmp_path / "test_log.json")
    mocker.patch("bot.memory.DATA_DIR", tmp_path)
    return MeetingMemory()

def test_init_corrupt_json(tmp_path, mocker):
    log_path = tmp_path / "test_log.json"
    log_path.write_text("invalid json")
    mocker.patch("bot.memory.LOG_PATH", log_path)
    m = MeetingMemory()
    assert m._meetings == []
    
def test_save_exception(memory_obj, mocker):
    mocker.patch("os.fdopen", side_effect=Exception("Disk full"))
    with pytest.raises(Exception):
        memory_obj.save()

def test_get_recent_context(memory_obj):
    assert memory_obj.get_recent_context() == "No prior meetings on record."
    memory_obj.save_meeting({"timestamp": "1", "type": "A", "summary": "B"})
    assert "• [1] A — B" in memory_obj.get_recent_context()
    
def test_decision_log(memory_obj):
    memory_obj.save_decision({"id": 1})
    assert len(memory_obj.get_decision_log()) == 1
    
def test_get_rolling_summary(memory_obj):
    assert memory_obj.get_rolling_summary() == "No older meeting history yet."
    memory_obj._rolling_summary = "Old summary"
    assert memory_obj.get_rolling_summary() == "Old summary"

@pytest.mark.asyncio
async def test_semantic_context_vesper_error(tmp_path, mocker):
    mocker.patch("bot.memory.LOG_PATH", tmp_path / "test_log.json")
    mocker.patch("bot.memory.DATA_DIR", tmp_path)
    mocker.patch("bot.memory.VAULT_DIR", tmp_path / "vesper_vault")
    sm = SemanticMeetingMemory()
    
    # Mock LLM and vesper
    mock_llm = mocker.patch("bot.agents.agent_llm.generate_response", new_callable=AsyncMock)
    mock_llm.return_value = ("expanded query", 0.1)
    
    mocker.patch("vesper_engine.generate_context_packet", return_value=(None, None, "Disk error"))
    
    res = await sm.get_semantic_context("query")
    assert "No matching meeting context found." in res
