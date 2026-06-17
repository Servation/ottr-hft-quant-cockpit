import pytest
import os
import json
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock

from bot.memory import MeetingMemory, SemanticMeetingMemory

@pytest.fixture
def mock_memory_paths(tmp_path):
    # Patch LOG_PATH and DATA_DIR inside bot.memory
    import bot.memory
    orig_log = bot.memory.LOG_PATH
    orig_data = bot.memory.DATA_DIR
    orig_vault = bot.memory.VAULT_DIR
    
    bot.memory.DATA_DIR = tmp_path
    bot.memory.LOG_PATH = tmp_path / "meeting_log.json"
    bot.memory.VAULT_DIR = tmp_path / "vesper_vault"
    
    yield tmp_path
    
    bot.memory.DATA_DIR = orig_data
    bot.memory.LOG_PATH = orig_log
    bot.memory.VAULT_DIR = orig_vault

def test_meeting_memory_save_load(mock_memory_paths):
    mem = MeetingMemory()
    mem.save_meeting({"type": "test", "summary": "test meeting", "decisions": []})
    
    # Reload
    mem2 = MeetingMemory()
    assert len(mem2._meetings) == 1
    assert mem2._meetings[0]["type"] == "test"

def test_meeting_memory_condensation(mock_memory_paths):
    mem = MeetingMemory()
    # Save 6 meetings to trigger condensation (MAX_FULL_MEETINGS = 5)
    for i in range(6):
        mem.save_meeting({"type": "test", "summary": f"summary {i}", "decisions": []})
    
    assert len(mem._meetings) == 5
    # The oldest (summary 0) should be condensed
    assert "summary 0" in mem._rolling_summary
    assert "summary 5" == mem._meetings[-1]["summary"]

@pytest.mark.asyncio
async def test_semantic_memory_save_and_query(mock_memory_paths, mocker):
    """Test saving a meeting and querying it via Vesper semantic search."""
    
    # Mock LLM for query expansion
    mock_llm = mocker.patch("bot.agents.agent_llm.generate_response", new_callable=AsyncMock)
    mock_llm.return_value = ("ETH bullish", 0.1)
    
    # Mock Vesper Engine
    mock_packet = "<context_packet>We discussed buying ETH</context_packet>"
    mock_vesper = mocker.patch("vesper_engine.generate_context_packet", return_value=(mock_packet, {}, None))
    
    sem_mem = SemanticMeetingMemory()
    
    record = {
        "id": "123",
        "type": "strategy_session",
        "timestamp": "2025-01-01T00:00:00Z",
        "summary": "We discussed buying ETH due to bullish momentum.",
        "decisions": ["BUY 1 ETH"],
        "agent_contributions": {}
    }
    
    # save_meeting writes to json and creates a md file in vault
    await sem_mem.save_meeting(record)
    
    vault_file = mock_memory_paths / "vesper_vault" / "123.md"
    assert vault_file.exists()
    content = vault_file.read_text()
    assert "We discussed buying ETH" in content
    
    # Test context string retrieval
    context_str = await sem_mem.get_semantic_context("ETH bullish", limit=3)
    assert context_str == mock_packet
    mock_vesper.assert_called_once_with(str(mock_memory_paths / "vesper_vault"), "ETH bullish", top_k=3)

@pytest.mark.asyncio
async def test_semantic_memory_empty_query(mock_memory_paths, mocker):
    # Mock LLM to throw error
    mock_llm = mocker.patch("bot.agents.agent_llm.generate_response", new_callable=AsyncMock)
    mock_llm.return_value = ("[error] failed", 0.0)
    
    mock_vesper = mocker.patch("vesper_engine.generate_context_packet", return_value=(None, None, "No files"))
    
    sem_mem = SemanticMeetingMemory()
    
    context = await sem_mem.get_semantic_context("test")
    assert context == "No matching meeting context found."
    
    # The original query text should be used since LLM errored
    mock_vesper.assert_called_once_with(str(mock_memory_paths / "vesper_vault"), "test", top_k=3)
