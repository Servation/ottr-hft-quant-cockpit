"""
Discord chat for OTTR (`ottr chat`): a slash-command bot that reads the engine's API.

What it does: /status, /positions and /meeting render API reads as plain code; /ask
answers a question with one model call over the same data; /kill and /release flip the
kill switch through the API's authenticated endpoint. It answers only the owner
(CEO_DISCORD_ID) and an optional allow-list.

Why a separate process that only talks HTTP: the engine is the single ledger writer and
must never wait on Discord or a chat model, and the bot must not be able to change the
book. It has no ledger handle at all, the API has no trade endpoint, and the only write
it can ask for is the kill switch, so no chat command can place a trade. The bot keeps
its own record of every exchange in <data_dir>/chat.db (see log.py for why SQLite).

Layout: client.py (API calls), log.py (chat.db), render.py (Discord text, message
splitting), prompt.py (the /ask prompt), service.py (auth, guardrails, every decision),
bot.py (discord.py glue and the process). Only bot.py imports discord.
"""

from __future__ import annotations

import importlib.util
from typing import Optional

from ottr.config import Config

EXIT_CONFIG = 78  # sysexits EX_CONFIG: a restart can't fix this (bad token, chat disabled)


def unavailable(cfg: Config) -> Optional[str]:
    """Why the chat bot can't run here, or None if it can. The supervisor asks before
    starting it, so a missing token or package is one log line, not a crash loop."""
    if not cfg.chat.enabled:
        return "chat is disabled in config ([chat] enabled = false)"
    if not cfg.discord_bot_token:
        return "DISCORD_BOT_TOKEN is not set (ottr/.env)"
    if cfg.chat_owner_id is None:
        return "CEO_DISCORD_ID is not set (ottr/.env): the bot must know whom to obey"
    if importlib.util.find_spec("discord") is None:
        return "discord.py is not installed: run `pip install -e .` in ottr/"
    return None


def api_base_url(cfg: Config) -> str:
    host = cfg.api.host
    host = f"[{host}]" if ":" in host else host  # IPv6 loopback
    return f"http://{host}:{cfg.api.port}/api"
