"""Gateway discord-sync allow-list (Tier 4 / O3). The bridge can push a meeting_outcome
event, which is re-broadcast to the SSE stream; unknown events are ignored."""

from fastapi.testclient import TestClient

import app.routers.discord_webhooks as dw
from app.main import app

client = TestClient(app)


def test_meeting_outcome_is_broadcast(monkeypatch):
    sent = []

    async def _broadcast(event, data):
        sent.append(event)

    monkeypatch.setattr(dw.sse_manager, "broadcast", _broadcast)
    r = client.post("/api/internal/discord-sync",
                    json={"event": "meeting_outcome",
                          "data": {"name": "Risk Review", "summary": "HOLD BTC", "decisions": []}})
    assert r.status_code == 200
    assert "meeting_outcome" in sent


def test_unknown_event_not_broadcast(monkeypatch):
    sent = []

    async def _broadcast(event, data):
        sent.append(event)

    monkeypatch.setattr(dw.sse_manager, "broadcast", _broadcast)
    r = client.post("/api/internal/discord-sync", json={"event": "bogus_event", "data": {"x": 1}})
    assert r.status_code == 200
    assert sent == []      # unknown event type is ignored, not broadcast
