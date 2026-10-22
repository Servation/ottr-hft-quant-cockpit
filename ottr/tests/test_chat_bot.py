"""
discord.py glue tests with fake interactions: no gateway connection, no real model.

Covers the registered command set and options, the owner check that runs before every
command, deferral before slow work, ephemeral refusals, splitting long answers across
follow-ups, the /release confirmation buttons (and their timeout), the error handler,
command-sync bookkeeping, and the `ottr chat` process: exit codes, the single-instance
lock and a clean stop through the stop file.
"""

import asyncio
import dataclasses
from types import SimpleNamespace

import discord
import pytest
from discord import app_commands

from ottr.chat import EXIT_CONFIG
from ottr.chat import bot as chatbot
from ottr.chat.bot import COMMANDS, Handlers, OttrBot, ReleaseView, build_tree, sync_commands
from ottr.chat.log import ChatLog
from ottr.chat.service import OWNER_ONLY, STRANGER, ChatService
from ottr.control import request_stop
from ottr.ledger.store import _FileLock
from tests.fakes import CHAT_KEY, FakeEngineApi, RecordingModel

OWNER, FRIEND, STRANGER_ID = 111111111111111111, 222222222222222222, 333333333333333333


class FakeResponse:
    def __init__(self, events):
        self.events, self._done = events, False

    def is_done(self):
        return self._done

    def _first(self):
        assert not self._done, "Discord allows one initial response per interaction"
        self._done = True

    async def defer(self, *, ephemeral=False, thinking=False):
        self._first()
        self.events.append(("defer", thinking))

    async def send_message(self, content=None, *, ephemeral=False, view=None, **kw):
        self._first()
        self.events.append(("send", content, ephemeral, view))

    async def edit_message(self, *, content=None, view=None, **kw):
        self._first()
        self.events.append(("edit", content, view))


class FakeFollowup:
    def __init__(self, events):
        self.events = events

    async def send(self, content, *, ephemeral=False, **kw):
        assert len(content) <= 2000
        self.events.append(("followup", content, ephemeral))


class FakeInteraction:
    def __init__(self, user_id=OWNER, name="Jeff", channel_id=555, command=None):
        self.user = SimpleNamespace(id=user_id, display_name=name, name=name)
        self.channel_id = channel_id
        self.command = SimpleNamespace(name=command) if command else None
        self.events = []
        self.response, self.followup = FakeResponse(self.events), FakeFollowup(self.events)

    async def edit_original_response(self, **kw):
        self.events.append(("edit_original", kw.get("content"), kw.get("view")))

    def kinds(self):
        return [e[0] for e in self.events]


@pytest.fixture
def chat_cfg(cfg):
    return dataclasses.replace(cfg, discord_bot_token="token", chat_owner_id=OWNER,
                               chat_allowed_ids=(FRIEND,), api_key=CHAT_KEY)


@pytest.fixture
async def rig(chat_cfg, tmp_path):
    engine_api = FakeEngineApi()
    model = RecordingModel()
    svc = ChatService(chat_cfg, engine_api.client(), model, ChatLog(tmp_path / "chat.db"))
    yield SimpleNamespace(api=engine_api, model=model, svc=svc, h=Handlers(svc))
    await svc.api.aclose()
    svc.log.close()


def new_client() -> discord.Client:
    intents = discord.Intents.none()
    intents.guilds = True  # as OttrBot runs; without it discord.py warns about state
    return discord.Client(intents=intents)


@pytest.fixture
async def client():
    c = new_client()
    yield c
    await c.close()


# -- the command set -----------------------------------------------------------------------
async def test_tree_registers_exactly_the_commands(rig, client):
    tree = build_tree(client, rig.h)
    cmds = {c.name: c.to_dict(tree) for c in tree.get_commands()}
    assert set(cmds) == set(COMMANDS)
    assert all(d["description"] for d in cmds.values())
    (q,) = cmds["ask"]["options"]
    assert (q["name"], q["required"], q["max_length"]) == ("question", True, 1500)
    assert "e.g." in q["description"]
    (r,) = cmds["kill"]["options"]
    assert r["name"] == "reason" and r["required"] is False
    assert not cmds["release"]["options"] and not cmds["status"]["options"]


async def test_a_server_id_scopes_the_commands_to_that_server(rig, client):
    tree = build_tree(client, rig.h, guild_id=999999999999999999)
    assert tree.get_commands() == []
    assert {c.name for c in tree.get_commands(guild=discord.Object(id=999999999999999999))} == set(COMMANDS)


async def test_the_tree_checks_every_interaction_first(rig, client):
    tree = build_tree(client, rig.h)
    stranger = FakeInteraction(STRANGER_ID, "rando", command="ask")
    assert await tree.interaction_check(stranger) is False
    assert stranger.events == [("send", STRANGER, True, None)]  # private refusal, nothing else ran
    assert rig.api.requests == [] and rig.model.prompts == []
    assert await tree.interaction_check(FakeInteraction(OWNER, command="ask")) is True
    assert await tree.interaction_check(FakeInteraction(FRIEND, command="status")) is True


async def test_callbacks_are_wired_to_the_handlers(rig, client):
    tree = build_tree(client, rig.h)
    it = FakeInteraction()
    await tree.get_command("ask").callback(it, question="what do we hold?")
    assert it.kinds() == ["defer", "followup"] and "TLT is your only position." in it.events[1][1]


# -- reads and /ask ------------------------------------------------------------------------
async def test_reads_defer_then_answer_in_the_channel(rig):
    for name in ("status", "positions", "meeting"):
        it = FakeInteraction()
        await getattr(rig.h, name)(it)
        assert it.events[0] == ("defer", True) and it.kinds()[1:] == ["followup"] * (len(it.events) - 1)
        assert all(e[2] is False for e in it.events[1:])  # public


async def test_ask_defers_before_the_model_runs(rig):
    it = FakeInteraction()
    seen = []
    rig.model.on_call = lambda: seen.append(it.response.is_done())
    await rig.h.ask(it, "why TLT?")
    assert seen == [True] and it.events[0] == ("defer", True)


async def test_a_long_answer_is_split_across_followups(rig):
    rig.model.replies = ["\n\n".join(f"Point {i}: " + "detail " * 120 for i in range(8))]
    it = FakeInteraction()
    await rig.h.ask(it, "explain everything")
    followups = [e for e in it.events if e[0] == "followup"]
    assert len(followups) >= 3 and all(len(e[1]) <= 2000 for e in followups)
    assert followups[0][1].startswith("> **Jeff:** explain everything")
    assert "asks this hour" in followups[-1][1]


async def test_ask_refusals_are_private_and_skip_the_deferral(rig):
    rig.api.council.update(running=True, started_at=0.0)
    it = FakeInteraction()
    await rig.h.ask(it, "anything")
    (ev,) = it.events
    assert ev[0] == "send" and ev[2] is True and "council is in session" in ev[1]


# -- kill switch ---------------------------------------------------------------------------
async def test_kill_is_public_and_posts_once(rig):
    it = FakeInteraction(FRIEND, "pal")
    await rig.h.kill(it, "panic")
    assert it.kinds() == ["defer", "followup"] and "ENGAGED" in it.events[1][1]
    assert rig.api.writes() == [("POST", "/api/kill-switch")]


async def test_release_needs_the_owner(rig):
    rig.api.kill.update(operator_engaged=True, effective_dry_run=True)
    it = FakeInteraction(FRIEND, "pal")
    await rig.h.release(it)
    assert it.events == [("send", OWNER_ONLY, True, None)] and rig.api.writes() == []


async def test_release_asks_then_releases_on_confirm(rig):
    rig.api.kill.update(operator_engaged=True, effective_dry_run=True)
    it = FakeInteraction()
    await rig.h.release(it)
    ((kind, text, ephemeral, view),) = it.events
    assert kind == "send" and ephemeral and text.startswith("Release the kill switch? Paper fills resume")
    assert isinstance(view, ReleaseView) and view.origin is it
    assert [c.label for c in view.children] == ["Release", "Cancel"] and rig.api.writes() == []
    # Someone else can't press it.
    other = FakeInteraction(FRIEND, "pal")
    assert await view.interaction_check(other) is False and other.events[0][1] == OWNER_ONLY
    press = FakeInteraction()
    await view.children[0].callback(press)
    assert press.kinds() == ["edit", "edit_original", "followup"]
    assert "released" in press.events[-1][1] and press.events[-1][2] is False
    assert rig.api.writes() == [("POST", "/api/kill-switch")] and rig.api.kill["operator_engaged"] is False


async def test_release_cancel_and_timeout_change_nothing(rig):
    rig.api.kill.update(operator_engaged=True, effective_dry_run=True)
    it = FakeInteraction()
    await rig.h.release(it)
    view = it.events[0][3]
    press = FakeInteraction()
    await view.children[1].callback(press)
    assert press.events == [("edit", "Cancelled: the kill switch stays engaged.", None)]
    await view.on_timeout()
    assert it.events[-1][0] == "edit_original" and "release cancelled" in it.events[-1][1]
    assert rig.api.writes() == []
    assert [r["detail"] for r in rig.svc.log.recent(2)] == ["no answer within a minute", "cancelled"]


async def test_release_with_nothing_engaged_says_so(rig):
    it = FakeInteraction()
    await rig.h.release(it)
    assert it.events[0][0] == "send" and "already on" in it.events[0][1] and it.events[0][3] is None


# -- errors --------------------------------------------------------------------------------
async def test_a_crashing_command_still_answers(rig, client):
    tree = build_tree(client, rig.h)
    it = FakeInteraction(command="status")
    await tree.on_error(it, app_commands.CommandInvokeError(tree.get_command("status"), RuntimeError("bug")))
    assert it.events == [("send", "Something went wrong answering that (details are in logs/chat.log). "
                                  "Nothing changed.", True, None)]
    late = FakeInteraction(command="ask")
    await late.response.defer(thinking=True)
    await rig.h.on_error(late, app_commands.CommandNotFound("gone", []))
    assert late.events[-1][0] == "followup" and "no longer exists" in late.events[-1][1]


# -- command sync --------------------------------------------------------------------------
async def test_commands_register_only_when_they_change(rig, client, monkeypatch):
    tree = build_tree(client, rig.h)
    synced = []

    async def fake_sync(*, guild=None):
        synced.append(getattr(guild, "id", None))
        return []

    monkeypatch.setattr(tree, "sync", fake_sync)
    assert await sync_commands(tree, rig.svc.log, None) == "registered" and synced == [None]
    assert await sync_commands(tree, rig.svc.log, None) == "unchanged" and synced == [None]
    # Moving to one server clears the old global copies, then registers in the server.
    other = new_client()  # a client holds one tree; the restarted bot is a new client anyway
    try:
        gtree = build_tree(other, rig.h, guild_id=999999999999999999)
        monkeypatch.setattr(gtree, "sync", fake_sync)
        assert await sync_commands(gtree, rig.svc.log, 999999999999999999) == "registered"
        assert synced == [None, None, 999999999999999999]
    finally:
        await other.close()


async def test_a_missing_scope_is_a_config_error(rig, client, monkeypatch):
    tree = build_tree(client, rig.h)

    async def forbidden(*, guild=None):
        raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), {"message": "Missing Access",
                                                                                 "code": 50001})

    monkeypatch.setattr(tree, "sync", forbidden)
    with pytest.raises(chatbot.ChatConfigError, match="applications.commands"):
        await sync_commands(tree, rig.svc.log, None)
    assert rig.svc.log.get_state("command_sync") is None  # retried on the next start


async def test_the_bot_never_pings_anyone(rig):
    b = OttrBot(rig.svc)
    try:
        am = b.allowed_mentions
        assert not am.everyone and not am.users and not am.roles and not am.replied_user
        assert not b.intents.message_content and b.intents.guilds
    finally:
        await b.close()


# -- the process ---------------------------------------------------------------------------
async def test_run_chat_without_a_token_is_a_config_exit(cfg):
    assert await chatbot.run_chat(cfg) == EXIT_CONFIG


async def test_run_chat_with_a_bad_token_is_a_config_exit(chat_cfg, monkeypatch):
    async def bad_login(self, token, *, reconnect=True):
        raise discord.LoginFailure("Improper token has been passed.")

    monkeypatch.setattr(OttrBot, "start", bad_login)
    assert await chatbot.run_chat(chat_cfg) == EXIT_CONFIG
    lock = _FileLock(chat_cfg.data_dir / "chat.lock")
    lock.acquire()  # released on the way out
    lock.release()


async def test_run_chat_network_failure_asks_for_a_restart(chat_cfg, monkeypatch):
    async def offline(self, token, *, reconnect=True):
        raise OSError("network is unreachable")

    monkeypatch.setattr(OttrBot, "start", offline)
    assert await chatbot.run_chat(chat_cfg) == 1


async def test_run_chat_refuses_a_second_instance(chat_cfg):
    chat_cfg.data_dir.mkdir(parents=True, exist_ok=True)
    held = _FileLock(chat_cfg.data_dir / "chat.lock")
    held.acquire()
    try:
        assert await chatbot.run_chat(chat_cfg) == 2
    finally:
        held.release()


async def test_a_standby_bot_waits_for_the_lock_then_takes_over(chat_cfg, monkeypatch):
    # Found live: a bot started by hand held the lock, and the supervised one exited 2 every
    # few seconds, backing off and posting crash alerts. With --standby it waits instead.
    monkeypatch.setattr(chatbot, "STANDBY_POLL", 0.05)
    connected = asyncio.Event()

    async def online(self, token, *, reconnect=True):
        connected.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(OttrBot, "start", online)
    chat_cfg.data_dir.mkdir(parents=True, exist_ok=True)
    by_hand = _FileLock(chat_cfg.data_dir / "chat.lock")
    by_hand.acquire()
    task = asyncio.create_task(chatbot.run_chat(chat_cfg, standby=True))
    try:
        await asyncio.sleep(0.5)
        assert not task.done() and not connected.is_set()  # waiting quietly, not exiting
    finally:
        by_hand.release()  # the bot started by hand exits
    await asyncio.wait_for(connected.wait(), 5)  # the standby takes over
    request_stop(chat_cfg.data_dir, "test")
    assert await asyncio.wait_for(task, 10) == 0


async def test_a_standby_bot_can_be_stopped_while_it_waits(chat_cfg, monkeypatch):
    monkeypatch.setattr(chatbot, "STANDBY_POLL", 0.05)

    async def never(self, token, *, reconnect=True):
        raise AssertionError("a bot standing by must not connect")

    monkeypatch.setattr(OttrBot, "start", never)
    chat_cfg.data_dir.mkdir(parents=True, exist_ok=True)
    by_hand = _FileLock(chat_cfg.data_dir / "chat.lock")
    by_hand.acquire()
    try:
        task = asyncio.create_task(chatbot.run_chat(chat_cfg, standby=True))
        await asyncio.sleep(0.3)
        request_stop(chat_cfg.data_dir, "test")  # `ottr service stop`
        assert await asyncio.wait_for(task, 10) == 0
        assert not (chat_cfg.data_dir / "chat.db").exists()  # it never opened its record
    finally:
        by_hand.release()


async def test_run_chat_stops_cleanly_on_the_stop_file(chat_cfg, monkeypatch):
    connected = asyncio.Event()

    async def online(self, token, *, reconnect=True):
        connected.set()
        await asyncio.Event().wait()  # a healthy gateway connection never returns

    monkeypatch.setattr(OttrBot, "start", online)
    task = asyncio.create_task(chatbot.run_chat(chat_cfg))
    await asyncio.wait_for(connected.wait(), 5)
    request_stop(chat_cfg.data_dir, "test")  # what `ottr service stop` does
    assert await asyncio.wait_for(task, 10) == 0
    lock = _FileLock(chat_cfg.data_dir / "chat.lock")
    lock.acquire()  # released on the way out
    lock.release()
