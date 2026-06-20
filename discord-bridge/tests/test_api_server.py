"""Auth tests for the discord-bridge /api/directive endpoint."""
import pytest
from unittest.mock import AsyncMock, MagicMock

from bot import api_server


class FakeRequest:
    def __init__(self, headers, json_data, bot):
        self.headers = headers
        self._json = json_data
        self.app = {"bot": bot}

    async def json(self):
        return self._json


def make_bot():
    bot = MagicMock()
    bot._trading_floor_channel = MagicMock()
    bot._trading_floor_channel.send = AsyncMock()
    return bot


@pytest.mark.asyncio
async def test_directive_rejected_without_key(monkeypatch):
    monkeypatch.setenv("OTTR_API_KEY", "secret")
    bot = make_bot()
    resp = await api_server.handle_directive(FakeRequest({}, {"message": "SELL EVERYTHING"}, bot))
    assert resp.status == 401
    bot._trading_floor_channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_directive_rejected_with_wrong_key(monkeypatch):
    monkeypatch.setenv("OTTR_API_KEY", "secret")
    bot = make_bot()
    resp = await api_server.handle_directive(FakeRequest({"X-API-Key": "wrong"}, {"message": "x"}, bot))
    assert resp.status == 401
    bot._trading_floor_channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_directive_fails_closed_when_unconfigured(monkeypatch):
    monkeypatch.delenv("OTTR_API_KEY", raising=False)
    bot = make_bot()
    resp = await api_server.handle_directive(FakeRequest({"X-API-Key": "anything"}, {"message": "x"}, bot))
    assert resp.status == 503
    bot._trading_floor_channel.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_directive_accepted_with_valid_key(monkeypatch):
    monkeypatch.setenv("OTTR_API_KEY", "secret")
    bot = make_bot()
    resp = await api_server.handle_directive(FakeRequest({"X-API-Key": "secret"}, {"message": "check drawdown"}, bot))
    assert resp.status == 200
    bot._trading_floor_channel.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_directive_errors_are_not_leaked(monkeypatch):
    monkeypatch.setenv("OTTR_API_KEY", "secret")
    bot = make_bot()
    bot._trading_floor_channel.send = AsyncMock(side_effect=Exception("internal path /secret/x"))
    resp = await api_server.handle_directive(FakeRequest({"X-API-Key": "secret"}, {"message": "x"}, bot))
    assert resp.status == 500
    # The raw exception text must not be returned to the caller.
    import json
    body = json.loads(resp.body.decode())
    assert body["reason"] == "Internal server error"
