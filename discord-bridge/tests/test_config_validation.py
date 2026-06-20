"""Startup config validation (Phase 6)."""
import pytest
from bot.security import validate_runtime_config

GOOD = {
    "DISCORD_BOT_TOKEN": "real-token-xyz",
    "CEO_DISCORD_ID": "424242424242",
    "OTTR_API_KEY": "a-long-random-secret-value",
    "DISCORD_TRADING_FLOOR_CHANNEL_ID": "987654321098765432",
}


@pytest.fixture
def good_env(monkeypatch):
    for k, v in GOOD.items():
        monkeypatch.setenv(k, v)


def test_clean_config_has_no_problems(good_env):
    assert validate_runtime_config() == []


def test_missing_ceo_id_flagged(good_env, monkeypatch):
    monkeypatch.delenv("CEO_DISCORD_ID", raising=False)
    probs = validate_runtime_config()
    assert any("CEO_DISCORD_ID" in p for p in probs)


def test_placeholder_api_key_flagged(good_env, monkeypatch):
    monkeypatch.setenv("OTTR_API_KEY", "change-me-to-a-long-random-secret")
    probs = validate_runtime_config()
    assert any("OTTR_API_KEY" in p for p in probs)
