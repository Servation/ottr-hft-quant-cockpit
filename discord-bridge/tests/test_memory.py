import pytest
import os
import json
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock

from bot.memory import MeetingMemory, SemanticMeetingMemory

@pytest.fixture
def mock_memory_paths(tmp_path):
    # Patch the module-level paths inside bot.memory
    import bot.memory
    orig_log = bot.memory.LOG_PATH
    orig_data = bot.memory.DATA_DIR
    orig_vault = bot.memory.VAULT_DIR
    orig_index = bot.memory.INDEX_PATH

    bot.memory.DATA_DIR = tmp_path
    bot.memory.LOG_PATH = tmp_path / "meeting_log.json"
    bot.memory.VAULT_DIR = tmp_path / "vesper_vault"
    bot.memory.INDEX_PATH = tmp_path / "embeddings_index.json"

    yield tmp_path

    bot.memory.DATA_DIR = orig_data
    bot.memory.LOG_PATH = orig_log
    bot.memory.VAULT_DIR = orig_vault
    bot.memory.INDEX_PATH = orig_index

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
    """Save a meeting and retrieve it by semantic (embedding) similarity."""
    # Fake embeddings: anything mentioning ETH -> [1,0]; everything else -> [0,1].
    async def fake_embed(text):
        return [1.0, 0.0] if "ETH" in text else [0.0, 1.0]
    mocker.patch("bot.embeddings.embed", side_effect=fake_embed)

    sem_mem = SemanticMeetingMemory()
    record = {
        "id": "123", "type": "strategy_session", "timestamp": "2025-01-01T00:00:00Z",
        "summary": "We discussed buying ETH due to bullish momentum.",
        "decisions": ["BUY 1 ETH"], "actions": [], "agent_contributions": {},
    }
    await sem_mem.save_meeting(record)

    # Markdown vault still written for humans; embedding index persisted.
    assert (mock_memory_paths / "vesper_vault" / "123.md").exists()
    assert (mock_memory_paths / "embeddings_index.json").exists()

    # Query about ETH retrieves the ETH meeting.
    context_str = await sem_mem.get_semantic_context("ETH bullish entry", limit=3)
    assert "buying ETH" in context_str


@pytest.mark.asyncio
async def test_semantic_memory_handles_embed_failure(mock_memory_paths, mocker):
    """If the embedding model is unavailable, retrieval degrades gracefully."""
    mocker.patch("bot.embeddings.embed", new_callable=AsyncMock, return_value=None)

    sem_mem = SemanticMeetingMemory()
    await sem_mem.save_meeting({"id": "1", "summary": "x", "decisions": [], "actions": []})
    context = await sem_mem.get_semantic_context("anything")
    assert context == "No matching meeting context found."


def test_cosine_similarity():
    from bot.embeddings import cosine
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine([], [1.0]) == 0.0  # mismatched/empty -> 0
