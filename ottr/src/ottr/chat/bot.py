"""
discord.py glue for `ottr chat`: slash commands over a gateway connection, and the process.

What it does: registers /status, /positions, /meeting, /ask, /kill and /release, checks
every interaction against the owner and allow-list before any command runs (strangers get
a private refusal), defers anything that reads the API so Discord's 3-second deadline
can't expire, asks for a button confirmation before /release, and splits long replies
across messages. `run_chat` is the process: a single-instance lock (the supervised bot
stands by while one started by hand holds it), a stop-file watcher (so `ottr service
stop` ends it cleanly), and exit code 78 for problems a restart can't fix, which tells
the supervisor to stop restarting it.

Why a gateway bot with slash commands: it connects outbound only (no public URL or
port), needs no privileged intents (no Message Content: commands arrive as structured
interactions), and Discord shows the commands and their options in its own UI. Every
decision lives in service.py; this module only translates.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any, Optional

import discord
from discord import app_commands

from ottr.chat import EXIT_CONFIG, api_base_url, render, unavailable
from ottr.chat.client import ApiClient, ApiError
from ottr.chat.log import Asker, ChatLog
from ottr.chat.service import OWNER_ONLY, ChatService, Outcome
from ottr.config import Config
from ottr.control import install_stop_signals, restore_signals, stop_requested
from ottr.council.models import make_adapter
from ottr.ledger.store import LockError, _FileLock

logger = logging.getLogger(__name__)

RELEASE_TIMEOUT = 60.0
STANDBY_POLL = 5.0    # how often a standing-by bot retries the single-instance lock
COMMANDS = ("status", "positions", "meeting", "screen", "ask", "kill", "release")


class ChatConfigError(RuntimeError):
    """Discord refused something only the owner can fix (scope, token): don't restart."""


def asker_of(interaction: Any) -> Asker:
    u = interaction.user
    return Asker(user_id=u.id, name=getattr(u, "display_name", None) or u.name,
                 channel_id=interaction.channel_id)


class Handlers:
    """One method per command, each taking the Discord interaction."""

    def __init__(self, service: ChatService) -> None:
        self.service = service

    async def check(self, interaction: Any) -> bool:
        cmd = getattr(getattr(interaction, "command", None), "name", None) or "?"
        refusal = self.service.gate(asker_of(interaction), cmd)
        if refusal is None:
            return True
        await interaction.response.send_message(refusal, ephemeral=True)
        return False

    async def send(self, interaction: Any, outcome: Outcome) -> None:
        chunks = render.split_message(outcome.text) or ["(empty reply)"]
        for i, chunk in enumerate(chunks):
            if i == 0 and not interaction.response.is_done():
                await interaction.response.send_message(chunk, ephemeral=outcome.ephemeral)
            else:
                await interaction.followup.send(chunk, ephemeral=outcome.ephemeral)

    async def _read(self, interaction: Any, fn) -> None:
        # Reads can take a few seconds (the book waits up to 5s for quotes): defer first.
        await interaction.response.defer(thinking=True)
        await self.send(interaction, Outcome(await fn(asker_of(interaction))))

    async def status(self, interaction: Any) -> None:
        await self._read(interaction, self.service.status)

    async def positions(self, interaction: Any) -> None:
        await self._read(interaction, self.service.positions)

    async def meeting(self, interaction: Any) -> None:
        await self._read(interaction, self.service.meeting)

    async def screen(self, interaction: Any) -> None:
        await self._read(interaction, self.service.screen)

    async def ask(self, interaction: Any, question: str) -> None:
        async def defer() -> None:
            await interaction.response.defer(thinking=True)
        await self.send(interaction, await self.service.ask(asker_of(interaction), question, defer))

    async def kill(self, interaction: Any, reason: Optional[str]) -> None:
        await interaction.response.defer(thinking=True)
        await self.send(interaction, Outcome(await self.service.kill(asker_of(interaction), reason or "")))

    async def release(self, interaction: Any) -> None:
        asker = asker_of(interaction)
        text, confirm = await self.service.release_check(asker)
        if not confirm:
            await interaction.response.send_message(text, ephemeral=True)
            return
        view = ReleaseView(self, asker)
        await interaction.response.send_message(text, view=view, ephemeral=True)
        view.origin = interaction

    async def release_confirmed(self, interaction: Any) -> None:
        await interaction.response.edit_message(content="Releasing the kill switch...", view=None)
        text = await self.service.release(asker_of(interaction))
        await interaction.edit_original_response(content="Done (the result is posted in the channel).")
        await interaction.followup.send(text)  # public, like /kill, so the channel keeps the record

    async def release_cancelled(self, interaction: Any) -> None:
        self.service.note_release_cancelled(asker_of(interaction), "cancelled")
        await interaction.response.edit_message(content="Cancelled: the kill switch stays engaged.", view=None)

    async def on_error(self, interaction: Any, error: Exception) -> None:
        if isinstance(error, app_commands.CommandNotFound):
            msg = "That command no longer exists here; Discord may take a minute to refresh its command list."
        else:
            logger.error("/%s failed", getattr(interaction.command, "name", "?"), exc_info=error)
            msg = "Something went wrong answering that (details are in logs/chat.log). Nothing changed."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass


class ReleaseView(discord.ui.View):
    """Release / Cancel buttons on the private /release prompt. Times out as a cancel."""

    def __init__(self, handlers: Handlers, owner: Asker, timeout: float = RELEASE_TIMEOUT) -> None:
        super().__init__(timeout=timeout)
        self.handlers, self.owner = handlers, owner
        self.origin: Any = None  # the /release interaction, to edit its prompt on timeout

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner.user_id:
            return True
        await interaction.response.send_message(OWNER_ONLY, ephemeral=True)
        return False

    @discord.ui.button(label="Release", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await self.handlers.release_confirmed(interaction)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await self.handlers.release_cancelled(interaction)

    async def on_timeout(self) -> None:
        self.handlers.service.note_release_cancelled(self.owner, "no answer within a minute")
        if self.origin is not None:
            try:
                await self.origin.edit_original_response(
                    content="No answer within a minute: release cancelled, the kill switch stays engaged.",
                    view=None)
            except discord.HTTPException:
                pass


class OttrTree(app_commands.CommandTree):
    def __init__(self, client: discord.Client, handlers: Handlers) -> None:
        super().__init__(client)
        self.handlers = handlers

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.handlers.check(interaction)

    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        await self.handlers.on_error(interaction, getattr(error, "original", error))


def build_tree(client: discord.Client, handlers: Handlers, guild_id: Optional[int] = None) -> OttrTree:
    """The command tree. With `guild_id` the commands live in that one server only."""
    tree = OttrTree(client, handlers)
    where = {"guild": discord.Object(id=guild_id)} if guild_id else {}

    @tree.command(name="status", description="Engine health, the book and the council schedule", **where)
    async def status(interaction: discord.Interaction) -> None:
        await handlers.status(interaction)

    @tree.command(name="positions", description="Open positions, marked at the bid, with stop distances", **where)
    async def positions(interaction: discord.Interaction) -> None:
        await handlers.positions(interaction)

    @tree.command(name="meeting", description="The latest council meeting: decision and each analyst's stance",
                  **where)
    async def meeting(interaction: discord.Interaction) -> None:
        await handlers.meeting(interaction)

    @tree.command(name="screen", description="Other Kraken coins ranked by trend, strength vs BTC and liquidity",
                  **where)
    async def screen(interaction: discord.Interaction) -> None:
        await handlers.screen(interaction)

    @tree.command(name="ask", description="Ask about the book, positions or the last meeting (can't trade)",
                  **where)
    @app_commands.describe(question="Your question, e.g. why did the council buy ETH?")
    async def ask(interaction: discord.Interaction, question: app_commands.Range[str, 1, 1500]) -> None:
        await handlers.ask(interaction, question)

    @tree.command(name="kill", description="Engage the kill switch: stop all paper fills now", **where)
    @app_commands.describe(reason="Why (kept in the audit log)")
    async def kill(interaction: discord.Interaction,
                   reason: Optional[app_commands.Range[str, 1, 300]] = None) -> None:
        await handlers.kill(interaction, reason)

    @tree.command(name="release", description="Release the kill switch (owner only, asks to confirm)", **where)
    async def release(interaction: discord.Interaction) -> None:
        await handlers.release(interaction)

    return tree


async def sync_commands(tree: app_commands.CommandTree, log: ChatLog, guild_id: Optional[int]) -> str:
    """Register the commands with Discord only when they (or their target) changed.

    Why not on every start: command registration is rate-limited per day, and a crash loop
    would burn through it. Moving between one server and global clears the old target, so
    no stale copy of a command is left behind.
    """
    target = discord.Object(id=guild_id) if guild_id else None
    payload = [c.to_dict(tree) for c in tree.get_commands(guild=target)]
    digest = hashlib.sha256(json.dumps({"guild": guild_id, "commands": payload}, sort_keys=True,
                                       default=str).encode("utf-8")).hexdigest()
    prev = log.get_state("command_sync") or {}
    if prev.get("digest") == digest:
        return "unchanged"
    try:
        if prev and prev.get("guild") != guild_id:
            old = prev.get("guild")
            await tree.sync(guild=discord.Object(id=old) if old else None)  # the tree has none there now
        await tree.sync(guild=target)
    except discord.Forbidden as e:
        raise ChatConfigError(
            f"Discord refused to register slash commands ({e.status}: {e.text}). Re-invite the bot with "
            "the applications.commands scope" + (f", and check DISCORD_GUILD_ID={guild_id}" if guild_id else ""))
    log.set_state("command_sync", {"digest": digest, "guild": guild_id, "ts": time.time()})
    return "registered"


class OttrBot(discord.Client):
    def __init__(self, service: ChatService, guild_id: Optional[int] = None) -> None:
        # The bot never joins voice; skip discord.py's "PyNaCl/davey not installed" warnings.
        for flag in ("warn_nacl", "warn_dave"):
            if hasattr(discord.VoiceClient, flag):
                setattr(discord.VoiceClient, flag, False)
        intents = discord.Intents.none()
        intents.guilds = True  # not privileged; lets discord.py resolve servers and channels
        # No mentions ever: a model reply containing @everyone must not ping anyone.
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.service, self.guild_id = service, guild_id
        self.handlers = Handlers(service)
        self.tree = build_tree(self, self.handlers, guild_id)

    async def setup_hook(self) -> None:
        result = await sync_commands(self.tree, self.service.log, self.guild_id)
        logger.info("slash commands %s (%s)", result, f"server {self.guild_id}" if self.guild_id else "global")

    async def on_ready(self) -> None:
        logger.info("chat bot online as %s (id %s) in %d server(s)", self.user,
                    getattr(self.user, "id", "?"), len(self.guilds))


# -- the process ---------------------------------------------------------------------------
async def _watch_stop_file(cfg: Config, started: float, stop: asyncio.Event) -> None:
    while not stop.is_set():
        if stop_requested(cfg.data_dir, started):
            logger.info("stop requested via the stop file")
            stop.set()
            return
        await asyncio.sleep(1.0)


def _service(cfg: Config, log: ChatLog) -> ChatService:
    api = ApiClient(api_base_url(cfg), cfg.api_key)
    model = make_adapter(cfg.chat.model, cfg.council, effort=cfg.chat.effort)
    return ChatService(cfg, api, model, log)


async def _acquire(lock: _FileLock, standby: bool, stop: asyncio.Event) -> Optional[int]:
    """Take the single-instance lock: None once it is held, else the exit code to return.

    Why a standby: two bots on one token would both answer every command, so only one may
    run. A bot started by hand while another runs is told so and exits (2). The supervised
    bot (`--standby`) instead waits quietly and takes over when the other one exits: a
    held lock is not a crash, so it must not cost restarts, backoff or crash alerts.
    """
    try:
        lock.acquire()
        return None
    except LockError:
        if not standby:
            logger.error("another `ottr chat` is already running for %s (the service's, or one in another "
                         "terminal); stop that one first", lock.path.parent)
            return 2
    logger.warning("another `ottr chat` holds %s (probably one started by hand); standing by until it exits",
                   lock.path)
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=STANDBY_POLL)
            logger.info("stop requested while standing by")
            return 0
        except asyncio.TimeoutError:
            pass
        try:
            lock.acquire()
        except LockError:
            continue
        logger.info("the other chat bot exited; taking over")
        return None


async def _serve(cfg: Config, stop: asyncio.Event) -> int:
    """Connect and answer commands until `stop` is set or the connection fails for good."""
    log = ChatLog(cfg.data_dir / "chat.db")
    service = _service(cfg, log)
    bot = OttrBot(service, cfg.chat_guild_id)
    code = 0
    try:
        bot_task = asyncio.create_task(bot.start(cfg.discord_bot_token or ""), name="discord")
        stopper = asyncio.create_task(stop.wait(), name="stop")
        logger.info("chat bot starting: model %s (%s effort), %d asks/hour, API %s", cfg.chat.model,
                    cfg.chat.effort, cfg.chat.per_hour, api_base_url(cfg))
        await asyncio.wait({bot_task, stopper}, return_when=asyncio.FIRST_COMPLETED)
        if bot_task.done():
            exc = None if bot_task.cancelled() else bot_task.exception()
            if isinstance(exc, discord.LoginFailure):
                logger.error("Discord rejected DISCORD_BOT_TOKEN (%s): copy a fresh token from the Developer "
                             "Portal (Bot > Reset Token) into ottr/.env", exc)
                code = EXIT_CONFIG
            elif isinstance(exc, (discord.PrivilegedIntentsRequired, ChatConfigError)):
                logger.error("chat bot stopped: %s", exc)
                code = EXIT_CONFIG
            elif exc is not None:
                logger.error("Discord connection failed: %r", exc, exc_info=exc)
                code = 1
            else:
                logger.error("the Discord client stopped on its own")
                code = 1
        await bot.close()
        for t in (bot_task, stopper):
            t.cancel()
        await asyncio.gather(bot_task, stopper, return_exceptions=True)
    finally:
        await service.api.aclose()
        log.close()
    return code


async def run_chat(cfg: Config, standby: bool = False) -> int:
    """Run the bot until a stop is requested. 0: stopped on request; 1: a fault worth a
    restart (network, Discord outage); 2: another chat bot holds the lock (only without
    `standby`, which waits for it instead); 78: config."""
    why = unavailable(cfg)
    if why:
        logger.error("chat bot can't start: %s", why)
        return EXIT_CONFIG
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    # Stop handling comes first, so a bot standing by can be stopped cleanly too.
    started = time.time()
    stop = asyncio.Event()
    undo = install_stop_signals(stop.set)
    watcher = asyncio.create_task(_watch_stop_file(cfg, started, stop), name="stopfile")
    lock = _FileLock(cfg.data_dir / "chat.lock")
    try:
        code = await _acquire(lock, standby, stop)
        if code is None:
            code = await _serve(cfg, stop)
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)
        restore_signals(undo)
        lock.release()  # a no-op when it was never taken
    logger.info("chat bot stopped (exit %s)", code)
    return code


async def check_chat(cfg: Config, out=print) -> int:
    """`ottr chat --check`: config, secrets, engine API and model CLI, without connecting
    to Discord. Exit 0 if the bot could start, 78 if config stops it."""
    c = cfg.chat
    out(f"chat: {'enabled' if c.enabled else 'DISABLED'} | model {c.model} ({c.effort} effort) | "
        f"{c.per_hour} asks/hour | {c.history_turns} history turns | timeout {c.timeout_sec:.0f}s")
    out(f"DISCORD_BOT_TOKEN: {'set' if cfg.discord_bot_token else 'MISSING'} | CEO_DISCORD_ID: "
        f"{cfg.chat_owner_id or 'MISSING'} | allow-list: {', '.join(map(str, cfg.chat_allowed_ids)) or 'none'}")
    out(f"commands: {'server ' + str(cfg.chat_guild_id) if cfg.chat_guild_id else 'global'} "
        f"({' '.join('/' + n for n in COMMANDS)})")
    out(f"OTTR_API_KEY: {'set' if cfg.api_key else 'MISSING: /kill and /release will be refused'}")
    api = ApiClient(api_base_url(cfg), cfg.api_key, timeout=5.0)
    try:
        h = await api.health()
        out(f"engine API {api.base_url}: reachable, health {render.VERDICTS.get(h.get('exit_code'), '?')}")
        council = await api.council()
        out(f"council: {'in session' if council.get('running') else 'idle'}")
    except ApiError as e:
        out(f"engine API {api.base_url}: {render.api_error(e)} (the bot still starts and says so when asked)")
    finally:
        await api.aclose()
    exe = make_adapter(c.model, cfg.council, effort=c.effort).locate()
    out(f"model CLI for {c.model}: {exe or 'NOT FOUND: /ask will fail'}")
    why = unavailable(cfg)
    out(f"not ready: {why}" if why else "ready: `ottr chat` connects to Discord (or let `ottr supervise` run it)")
    return EXIT_CONFIG if why else 0
