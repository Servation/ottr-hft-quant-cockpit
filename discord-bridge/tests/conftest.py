"""Shared pytest fixtures for the discord-bridge suite."""
import pytest
from unittest.mock import AsyncMock


@pytest.fixture(autouse=True)
def _no_real_api_server(mocker):
    """Stop on_ready() tests from binding the real aiohttp port (:8001).

    Several tests exercise TradingFloorBot.on_ready(), which calls
    start_api_server() and binds a real socket. Across a full run the socket
    isn't freed between tests, causing intermittent 'address already in use'
    failures. on_ready tests care about channel resolution, not the live server,
    so mock it out everywhere.
    """
    try:
        mocker.patch("bot.main.start_api_server", new_callable=AsyncMock)
    except (ImportError, AttributeError):
        pass
