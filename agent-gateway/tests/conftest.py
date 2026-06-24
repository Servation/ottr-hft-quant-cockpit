"""Shared gateway test fixtures."""
import pytest


@pytest.fixture(autouse=True)
def _bridge_unreachable_by_default(monkeypatch):
    """Point the bridge URL at a dead local port so snapshot tests that don't
    explicitly stub performance fail fast (connection refused) instead of doing
    slow DNS on the docker-only 'discord-bridge' host."""
    monkeypatch.setenv("DISCORD_BRIDGE_URL", "http://127.0.0.1:9")
