import os
import logging
from pathlib import Path
from typing import Dict, Any

import yaml
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Resolve project root: discord-bridge/bot/__init__.py -> discord-bridge/
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Load .env — check local discord-bridge/ first, then repo root as fallback
load_dotenv(PROJECT_ROOT / ".env")
load_dotenv(PROJECT_ROOT.parent / ".env")  # repo root (d:\crypto-trading-bot\.env)

# Load YAML settings
_settings_path = PROJECT_ROOT / "config" / "settings.yaml"
try:
    with open(_settings_path, "r", encoding="utf-8") as f:
        settings: Dict[str, Any] = yaml.safe_load(f) or {}
    logger.info("Loaded settings from %s", _settings_path)
except FileNotFoundError:
    logger.warning("Settings file not found at %s, using defaults", _settings_path)
    settings = {}

# Merge environment variables into settings
settings["discord_bot_token"] = os.getenv("DISCORD_BOT_TOKEN", "")
settings["discord_trading_floor_channel_id"] = int(
    os.getenv("DISCORD_TRADING_FLOOR_CHANNEL_ID", "0")
)
settings["discord_system_status_channel_id"] = int(
    os.getenv("DISCORD_SYSTEM_STATUS_CHANNEL_ID", "0")
)
settings["llm_base_url"] = os.getenv("LLM_BASE_URL", "http://localhost:1234/v1")
settings["llm_model_id"] = os.getenv("LLM_MODEL_ID", "gemma-4-12b")
