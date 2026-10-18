"""
Chat bot core tests, no Discord and no real model: owner-only auth and roles, the /ask
guardrails (one call at a time, the hourly budget surviving a restart, silence during a
council meeting, an unreachable or outdated engine), deferral order, the kill switch
being the only write, prompt building, reply splitting, the [chat] config checks, and an
integration run against a real engine API on an ephemeral port.
"""

import asyncio
import dataclasses
import re
import time

import pytest

from ottr import config as cfgmod
from ottr.api import ApiServer
from ottr.chat import EXIT_CONFIG, api_base_url, prompt, render, unavailable
from ottr.chat.client import ApiClient, DeskContext
from ottr.chat.log import Asker, ChatLog, Turn
from ottr.chat.service import OWNER_ONLY, STRANGER, ChatService
from ottr.config import ConfigError
from ottr.council.models import ModelError
from ottr.engine.engine import Engine
from tests.fakes import CHAT_KEY, PRICES, FakeEngineApi, FakeMarket, RecordingModel, eth_buy_council

OWNER, FRIEND, STRANGER_ID = 111111111111111111, 222222222222222222, 333333333333333333
ME = Asker(OWNER, "Jeff", 555)


class Clock:
    def __init__(self) -> None:
        self.t = time.time()  # the fake API's timestamps are real ones

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def chat_cfg(cfg):
    return dataclasses.replace(cfg, discord_bot_token="token", chat_owner_id=OWNER,
                               chat_allowed_ids=(FRIEND,), api_key=CHAT_KEY)


@pytest.fixture
def engine_api():
    return FakeEngineApi()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
async def make(chat_cfg, engine_api, tmp_path, clock):
    made = []

    def _make(model=None, cfg=None, log_path=None, api=None):
        log = ChatLog(log_path or tmp_path / "chat.db", clock=clock)
        client = api or engine_api.client()
        svc = ChatService(cfg or chat_cfg, client, model or RecordingModel(), log, clock=clock)
        made.append(svc)
        return svc

    yield _make
    for svc in made:
        await svc.api.aclose()
        svc.log.close()


class Deferral:
    def __init__(self, engine_api=None):
        self.calls, self.requests_before = 0, None
        self.engine_api = engine_api

    async def __call__(self):
        self.calls += 1
        if self.engine_api is not None:
            self.requests_before = len(self.engine_api.requests)


# -- auth and roles ------------------------------------------------------------------------
async def test_strangers_are_refused_and_recorded(make):
    svc = make()
    assert svc.gate(Asker(STRANGER_ID, "rando", 9), "status") == STRANGER
    assert svc.gate(ME, "status") is None and svc.gate(Asker(FRIEND, "pal", 9), "ask") is None
    row = svc.log.recent(1)[0]
    assert (row["user_id"], row["status"], row["command"]) == (str(STRANGER_ID), "refused", "status")


async def test_only_the_owner_may_release_but_a_friend_may_kill(make, engine_api):
    svc = make()
    friend = Asker(FRIEND, "pal", 9)
    assert svc.may_release(friend) == OWNER_ONLY
    assert await svc.release(friend) == OWNER_ONLY and engine_api.writes() == []
    assert "ENGAGED" in await svc.kill(friend, "flash crash")
    assert engine_api.writes() == [("POST", "/api/kill-switch")]


async def test_no_owner_configured_means_nobody_is_obeyed(make, chat_cfg):
    svc = make(cfg=dataclasses.replace(chat_cfg, chat_owner_id=None, chat_allowed_ids=()))
    assert svc.gate(Asker(OWNER, "Jeff", 1), "status") == STRANGER


# -- plain reads ---------------------------------------------------------------------------
async def test_status_positions_and_meeting_read_the_api(make, engine_api):
    svc = make()
    s = await svc.status(ME)
    assert s.startswith("**OTTR OK**") and "$10,234.50" in s and "cooling down: ETH" in s
    assert "Council: next meeting <t:" in s and "Alarms: none" in s
    p = await svc.positions(ME)
    assert "```" in p and re.search(r"ETH\s+\$5,234\.50\s+51\.1%\s+\+0\.2%\s+8\.9%", p)
    m = await svc.meeting(ME)
    assert "Buy ETH on a clean trend." in m and "BUY ETH (conviction 0.70): filled $5,222.20" in m
    assert "dropped by code: SELL XRP" in m and "ETH reclaimed its 50-day EMA" in m
    assert engine_api.writes() == []
    assert [r["command"] for r in svc.log.recent(3)] == ["meeting", "positions", "status"]


async def test_status_when_the_engine_is_down(make, engine_api):
    engine_api.down = True
    svc = make()
    text = await svc.status(ME)
    assert text.startswith("Can't reach the OTTR engine") and "ottr status" in text
    assert svc.log.recent(1)[0]["status"] == "error"


async def test_status_survives_a_partial_failure(make, engine_api):
    engine_api.missing.add("/api/book")
    text = await make().status(ME)
    assert text.startswith("**OTTR OK**") and "Book unavailable" in text


# -- /ask ----------------------------------------------------------------------------------
async def test_ask_defers_before_reading_and_answers(make, engine_api):
    deferred_at_call = []
    model = RecordingModel(["ETH is 51% of the book — the only position."])
    svc = make(model=model)
    defer = Deferral(engine_api)
    model.on_call = lambda: deferred_at_call.append(defer.calls)
    out = await svc.ask(ME, "  what do we hold?  ", defer)
    assert defer.calls == 1 and deferred_at_call == [1]
    # Only the meeting check ran before the deferral; the desk reads came after it.
    assert engine_api.requests[:defer.requests_before] and \
        [p for _, p, _, _ in engine_api.requests[:defer.requests_before]] == ["/api/council"]
    assert not out.ephemeral
    assert out.text.startswith("> **Jeff:** what do we hold?")
    assert "ETH is 51% of the book, the only position." in out.text and "—" not in out.text
    assert "-# claude, low effort | " in out.text and "1 of 20 asks this hour" in out.text
    row = svc.log.recent(1)[0]
    assert row["status"] == "ok" and row["model_call"] == 1 and "DESK DATA" in row["prompt"]
    assert engine_api.writes() == []


async def test_ask_replays_this_channels_history_only(make):
    model = RecordingModel(["first answer", "second answer", "elsewhere"])
    svc = make(model=model)
    await svc.ask(ME, "first question", Deferral())
    await svc.ask(ME, "second question", Deferral())
    assert "first question" in model.prompts[1] and "You answered: first answer" in model.prompts[1]
    await svc.ask(Asker(OWNER, "Jeff", 999), "other channel", Deferral())
    assert "first question" not in model.prompts[2]


async def test_one_model_call_at_a_time(make):
    model = RecordingModel()
    model.hold = asyncio.Event()
    svc = make(model=model)
    first = asyncio.create_task(svc.ask(ME, "slow one", Deferral()))
    await asyncio.wait_for(model.entered.wait(), 5)
    second_defer = Deferral()
    out = await svc.ask(ME, "impatient", second_defer)
    assert out.ephemeral and "still answering" in out.text and second_defer.calls == 0
    model.hold.set()
    assert not (await first).ephemeral
    assert len(model.prompts) == 1
    assert (await svc.ask(ME, "now free", Deferral())).ephemeral is False


async def test_hourly_budget_is_enforced_and_survives_a_restart(make, chat_cfg, clock, tmp_path):
    cfg = dataclasses.replace(chat_cfg, chat=dataclasses.replace(chat_cfg.chat, per_hour=2))
    svc = make(cfg=cfg)
    for q in ("one", "two"):
        assert not (await svc.ask(ME, q, Deferral())).ephemeral
        clock.t += 60
    defer = Deferral()
    out = await svc.ask(ME, "three", defer)
    assert out.ephemeral and "hourly limit: 2 of 2" in out.text and defer.calls == 0
    assert "frees up in 58m" in out.text
    # A restart (new process, same chat.db) must not reset the budget.
    svc2 = make(cfg=cfg)
    assert "hourly limit" in (await svc2.ask(ME, "after restart", Deferral())).text
    clock.t += 3600
    assert not (await svc2.ask(ME, "an hour later", Deferral())).ephemeral


async def test_a_call_cut_off_by_a_crash_still_counts(make, chat_cfg, clock, tmp_path):
    cfg = dataclasses.replace(chat_cfg, chat=dataclasses.replace(chat_cfg.chat, per_hour=1))
    log = ChatLog(tmp_path / "crash.db", clock=clock)
    log.start_call(ME, "ask", "q", model="claude", effort="low", prompt="p")  # then the process died
    log.close()
    svc = make(cfg=cfg, log_path=tmp_path / "crash.db")
    row = svc.log.recent(1)[0]
    assert row["status"] == "error" and "interrupted" in row["detail"]
    assert "hourly limit" in (await svc.ask(ME, "again", Deferral())).text


async def test_ask_stays_quiet_while_the_council_meets(make, engine_api, clock):
    engine_api.council.update(running=True, started_at=clock.t - 130)
    model = RecordingModel()
    svc = make(model=model)
    defer = Deferral()
    out = await svc.ask(ME, "what's happening?", defer)
    assert out.ephemeral and "council is in session" in out.text and "2m ago" in out.text
    assert defer.calls == 0 and model.prompts == []
    assert svc.log.calls_since(0) == []  # a refusal costs no budget
    assert svc.log.recent(1)[0]["detail"] == "meeting"


async def test_ask_refusal_says_how_far_the_meeting_has_got(make, engine_api, clock):
    engine_api.council.update(running=True, started_at=clock.t - 130, progress={
        "phase": "analysts", "chair": {"status": "waiting"},
        "analysts": [{"name": "claude", "status": "done"}, {"name": "gemini", "status": "thinking"}]})
    out = await make(model=RecordingModel()).ask(ME, "what's happening?", Deferral())
    assert "(started 2m ago; claude done, gemini thinking), so I'm staying quiet" in out.text


async def test_ask_refuses_without_the_engine(make, engine_api):
    engine_api.down = True
    model = RecordingModel()
    svc = make(model=model)
    out = await svc.ask(ME, "anything", Deferral())
    assert out.ephemeral and "Can't reach the OTTR engine" in out.text and model.prompts == []


async def test_ask_against_an_engine_older_than_the_bot(make, engine_api):
    engine_api.missing.add("/api/council")
    out = await make().ask(ME, "anything", Deferral())
    assert out.ephemeral and "older than the chat bot" in out.text and "ottr service restart" in out.text


async def test_model_failure_is_reported_and_costs_budget(make):
    svc = make(model=RecordingModel([ModelError("timed out after 180s: claude")]))
    out = await svc.ask(ME, "q", Deferral())
    assert not out.ephemeral and "model call failed (timed out after 180s" in out.text
    row = svc.log.recent(1)[0]
    assert row["status"] == "error" and row["model_call"] == 1 and len(svc.log.calls_since(0)) == 1


async def test_chat_cannot_trade(make, engine_api):
    # Asking for a trade only produces an explanation: every request the bot makes is a read.
    svc = make(model=RecordingModel(["I can't trade; the council decides."]))
    out = await svc.ask(ME, "buy 1 BTC right now", Deferral())
    assert "can't trade" in out.text and engine_api.writes() == []
    assert "You cannot place, change or cancel trades" in svc.model.prompts[0]


async def test_empty_question_is_refused(make):
    out = await make().ask(ME, "   ", Deferral())
    assert out.ephemeral and "Ask me something" in out.text


# -- kill switch ---------------------------------------------------------------------------
async def test_kill_posts_with_the_key_and_records_who(make, engine_api):
    svc = make()
    text = await svc.kill(ME, "  going to bed  ")
    assert "Kill switch ENGAGED** by Jeff" in text
    method, path, headers, body = engine_api.requests[-1]
    assert (method, path) == ("POST", "/api/kill-switch") and headers["authorization"] == f"Bearer {CHAT_KEY}"
    assert body == {"engaged": True, "by": "discord",
                    "reason": f"/kill by Jeff ({OWNER}): going to bed"}
    assert "?" not in str(engine_api.requests[-1][1])  # the key never travels in the URL
    text, confirm = await svc.release_check(ME)  # engaged: /release asks to confirm
    assert confirm and text.startswith("Release the kill switch? Paper fills resume")
    text = await svc.release(ME)
    assert "Kill switch released** by Jeff: paper fills resume" in text
    assert engine_api.requests[-1][3]["engaged"] is False
    assert [r["command"] for r in svc.log.recent(2)] == ["release", "kill"]


async def test_release_check_skips_a_pointless_confirmation(make, engine_api):
    svc = make()
    text, confirm = await svc.release_check(ME)
    assert not confirm and "already on" in text
    engine_api.kill.update(effective_dry_run=True, config_dry_run=True)
    text, confirm = await svc.release_check(ME)
    assert not confirm and "OTTR_DRY_RUN forces dry run" in text
    # Both engaged: confirm, but don't promise fills that config will still block.
    engine_api.kill.update(operator_engaged=True)
    text, confirm = await svc.release_check(ME)
    assert confirm and "nothing will fill until that changes too" in text
    assert await svc.release_check(Asker(FRIEND, "pal", 1)) == (OWNER_ONLY, False)


async def test_kill_with_a_wrong_key_explains_the_fix(make, engine_api):
    svc = make(api=engine_api.client(key="wrong-key-wrong-key"))
    text = await svc.kill(ME)
    assert "refused the request" in text and "OTTR_API_KEY" in text
    assert svc.log.recent(1)[0]["status"] == "error"


# -- prompt --------------------------------------------------------------------------------
async def _ctx(engine_api) -> DeskContext:
    client = engine_api.client()
    try:
        return await client.desk_context()
    finally:
        await client.aclose()


async def test_prompt_carries_the_desk_and_the_rules(engine_api):
    ctx = await _ctx(engine_api)
    now = engine_api.data["/api/health"]["ts"]
    p = prompt.build(ctx, [Turn(now - 600, "Jeff", "old q", "old a")], "why ETH?", "Jeff", now)
    for needle in ("You cannot place, change or cancel trades", "Never use em dashes",
                   "HEALTH: OK", "KILL SWITCH: off", "BOOK: equity $10,234.50",
                   "ETH: qty 2.18 | value $5,234.50 (51.1% of book, cap 35.0%)",
                   "COUNCIL: meets every 4.0h", "Chair summary: Buy ETH on a clean trend.",
                   "ETH BUY conviction 0.80: ETH reclaimed its 50-day EMA Key risk: ETF outflows",
                   "Dropped by code: SELL XRP: not held", "RECENT FILLS", "in cooldown (no new BUYs): ETH",
                   "Jeff asked: old q", "You answered: old a"):
        assert needle in p, needle
    assert p.rstrip().endswith("Question from Jeff (now): why ETH?")
    assert "—" not in p


async def test_prompt_marks_missing_sections(engine_api):
    engine_api.missing.update({"/api/positions", "/api/fills"})
    ctx = await _ctx(engine_api)
    p = prompt.build(ctx, [], "q", "Jeff", 1.0)
    assert "POSITIONS: unavailable (no route /api/positions)" in p and "RECENT FILLS: unavailable" in p


async def test_prompt_fits_the_cap_dropping_old_history_first(engine_api):
    ctx = await _ctx(engine_api)
    history = [Turn(1000.0 + i, "Jeff", f"question {i}", "x" * 1100) for i in range(30)]
    p = prompt.build(ctx, history, "the real question", "Jeff", 5000.0, max_chars=12_000)
    assert len(p) <= 12_000 and "question 29" in p and "question 0 " not in p
    assert p.rstrip().endswith("the real question")
    # Each field is clipped on its own; only sheer volume can overflow the data section.
    rows = ctx.positions["positions"] * 400
    huge = DeskContext(**{**ctx.__dict__, "positions": dict(ctx.positions, positions=rows)})
    p = prompt.build(huge, history, "still asked", "Jeff", 5000.0, max_chars=6_000)
    assert len(p) <= 6_000 and "(desk data cut to fit)" in p and p.rstrip().endswith("still asked")


# -- splitting and rendering ---------------------------------------------------------------
def test_short_text_is_one_message():
    assert render.split_message("hello") == ["hello"] and render.split_message("  ") == []


def test_long_text_splits_at_paragraphs_under_the_limit():
    paras = [f"Paragraph {i}. " + "word " * 60 for i in range(40)]
    text = "\n\n".join(p.strip() for p in paras)
    chunks = render.split_message(text, max_chunks=50)
    assert len(chunks) > 1 and all(len(c) <= 2000 for c in chunks)
    assert all(c.startswith("Paragraph") for c in chunks)  # every cut fell on a paragraph break
    assert " ".join(" ".join(chunks).split()) == " ".join(text.split())


def test_one_enormous_word_is_hard_cut():
    chunks = render.split_message("x" * 4500)
    assert [len(c) for c in chunks] == [1996, 1996, 508]
    assert "".join(chunks) == "x" * 4500


def test_a_code_block_cut_in_two_is_closed_and_reopened():
    code = "\n".join(f"line {i:04d} = {i * i}" for i in range(300))
    text = "Here is the table:\n```python\n" + code + "\n```\nDone."
    chunks = render.split_message(text, max_chunks=10)
    assert len(chunks) > 1 and all(len(c) <= 2000 for c in chunks)
    for c in chunks:
        assert c.count("```") % 2 == 0, c[-80:]
    assert chunks[1].startswith("```python\n")
    body = "".join(c for c in chunks)
    assert all(f"line {i:04d}" in body for i in range(300))


def test_too_long_replies_are_cut_with_a_note():
    text = "\n\n".join("para " + "w " * 400 for _ in range(30))
    chunks = render.split_message(text)
    assert len(chunks) == render.MAX_CHUNKS and all(len(c) <= 2000 for c in chunks)
    assert "reply cut here" in chunks[-1]


def test_rendered_views_have_no_em_dashes(engine_api):
    d = engine_api.data
    now = d["/api/health"]["ts"]
    texts = [render.status(d["/api/health"], d["/api/book"], engine_api.council, {}, now),
             render.positions(d["/api/positions"], now), render.meeting(d["/api/meetings"]["meetings"][0], now),
             render.kill_result({"effective_dry_run": True}, False, "Jeff")]
    assert all("—" not in t for t in texts)
    assert render.no_em_dash("a — b—c") == "a, b, c"
    m = dict(d["/api/meetings"]["meetings"][0], summary="Buy ETH \u2014 the trend holds.")  # model text
    assert "Buy ETH, the trend holds." in render.meeting(m, now)


def test_status_shows_an_engaged_kill_switch_and_alarms(engine_api):
    d = engine_api.data
    book = dict(d["/api/book"], kill_switch={"effective_dry_run": True, "config_dry_run": False,
                                             "operator_engaged": True, "since": 1.0, "by": "discord",
                                             "reason": "bed"})
    health = dict(d["/api/health"], exit_code=1,
                  alarms=[{"key": "task:risk", "severity": "critical", "message": "risk task stalled"}])
    t = render.status(health, book, dict(engine_api.council, running=True, started_at=5.0), {}, 10.0)
    assert t.startswith("**OTTR DEGRADED** | **KILL SWITCH ENGAGED** by discord <t:1:R>: bed. Nothing fills")
    assert "- [critical] risk task stalled" in t and "**in session**" in t
    both = dict(book["kill_switch"], config_dry_run=True)
    assert render.kill_line(both) == ("**KILL SWITCH ENGAGED** by discord <t:1:R>: bed; **DRY RUN** forced by "
                                      "config or OTTR_DRY_RUN. Nothing fills")


async def test_prompt_names_each_kill_switch_source(engine_api):
    engine_api.kill.update(effective_dry_run=True, operator_engaged=True, config_dry_run=True, by="discord",
                           since=1.0, reason="bed")
    p = prompt.build(await _ctx(engine_api), [], "q", "Jeff", 2.0)
    assert ("KILL SWITCH: ON. operator switch engaged by discord at 1970-01-01 00:00 UTC (reason: bed); the "
            "owner can release it with /release; config or OTTR_DRY_RUN forces dry run on the host; chat "
            "can't change that.") in p


def test_positions_when_flat():
    assert render.positions({"positions": []}, 0) == "No open positions: the book is all cash."


# -- config --------------------------------------------------------------------------------
def _cfg_with(tmp_path, old, new):
    p = tmp_path / "c.toml"
    text = cfgmod.DEFAULT_CONFIG.read_text(encoding="utf-8")
    assert old in text
    p.write_text(text.replace(old, new), encoding="utf-8")
    return cfgmod.load(p, data_dir=tmp_path)


@pytest.mark.parametrize("old,new,match", [
    ('model = "claude"', 'model = "llama"', "chat.model"),
    ('effort = "low"', 'effort = "minimal"', r"chat.effort for claude"),
    ("per_hour = 20", "per_hour = 0", "per_hour"),
    ("timeout_sec = 180", "timeout_sec = 900", "timeout_sec"),
    ("history_turns = 6", "histroy_turns = 6", r"\[chat\] unknown keys"),
])
def test_bad_chat_config_fails_loudly(tmp_path, old, new, match):
    with pytest.raises(ConfigError, match=match):
        _cfg_with(tmp_path, old, new)


def test_chat_needs_the_api(tmp_path):
    text = cfgmod.DEFAULT_CONFIG.read_text(encoding="utf-8")
    p = tmp_path / "c.toml"
    p.write_text(text.replace("[api]                      # read-only JSON API for the dashboard + kill switch\n"
                              "enabled = true", "[api]\nenabled = false"), encoding="utf-8")
    with pytest.raises(ConfigError, match="chat.enabled needs api.enabled"):
        cfgmod.load(p, data_dir=tmp_path)


def test_effort_is_checked_against_the_chosen_model(tmp_path):
    c = _cfg_with(tmp_path, 'model = "claude"', 'model = "gpt"')
    assert (c.chat.model, c.chat.effort) == ("gpt", "low")
    p = tmp_path / "gpt-max.toml"
    p.write_text(cfgmod.DEFAULT_CONFIG.read_text(encoding="utf-8").replace('model = "claude"', 'model = "gpt"')
                 .replace('effort = "low"', 'effort = "max"'), encoding="utf-8")
    with pytest.raises(ConfigError, match="chat.effort for gpt"):  # claude's "max" isn't a codex level
        cfgmod.load(p, data_dir=tmp_path)


def test_discord_ids_come_from_the_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", " tok ")
    monkeypatch.setenv("CEO_DISCORD_ID", str(OWNER))
    monkeypatch.setenv("OTTR_CHAT_ALLOWED_IDS", f"{FRIEND}, 444444444444444444")
    c = cfgmod.load(data_dir=tmp_path)
    assert (c.discord_bot_token, c.chat_owner_id) == ("tok", OWNER)
    assert c.chat_allowed_ids == (FRIEND, 444444444444444444) and c.chat_guild_id is None
    assert "tok" not in repr(c)  # secrets stay out of repr
    monkeypatch.setenv("CEO_DISCORD_ID", "@jeff")
    with pytest.raises(ConfigError, match="CEO_DISCORD_ID must be a Discord"):
        cfgmod.load(data_dir=tmp_path)


def test_unavailable_says_why(chat_cfg):
    assert unavailable(chat_cfg) is None
    assert "disabled" in unavailable(dataclasses.replace(chat_cfg, chat=dataclasses.replace(chat_cfg.chat,
                                                                                          enabled=False)))
    assert "DISCORD_BOT_TOKEN" in unavailable(dataclasses.replace(chat_cfg, discord_bot_token=None))
    assert "CEO_DISCORD_ID" in unavailable(dataclasses.replace(chat_cfg, chat_owner_id=None))
    assert EXIT_CONFIG == 78
    assert api_base_url(dataclasses.replace(chat_cfg, api=dataclasses.replace(chat_cfg.api, host="::1",
                                                                             port=9))) == "http://[::1]:9/api"


# -- against a real engine API -------------------------------------------------------------
async def test_end_to_end_against_a_real_engine_api(chat_cfg, store, ledger, tmp_path, clock):
    market = FakeMarket(chat_cfg, PRICES)
    eng = Engine(chat_cfg, store, market, council=eth_buy_council(chat_cfg))
    await eng.meeting(execute=True)
    await eng.executor.drain()
    server = ApiServer(eng, "127.0.0.1", 0)
    await server.start()
    model = RecordingModel(["You hold ETH."])
    svc = ChatService(chat_cfg, ApiClient(f"http://127.0.0.1:{server.port}/api", CHAT_KEY), model,
                      ChatLog(tmp_path / "chat.db"))
    try:
        assert "**OTTR" in await svc.status(ME)
        assert "ETH" in await svc.positions(ME)
        m = await svc.meeting(ME)
        assert "buy eth" in m and "**claude**" in m and "ETH BUY 0.80" in m
        out = await svc.ask(ME, "what do we hold?", Deferral())
        assert "You hold ETH." in out.text
        assert "Chair summary: buy eth" in model.prompts[0] and "ETH BUY conviction 0.80" in model.prompts[0]
        # While a meeting runs, the API says so and /ask stays quiet.
        eng.meeting_started = clock.t
        out = await svc.ask(ME, "and now?", Deferral())
        assert out.ephemeral and "council is in session" in out.text
        eng.meeting_started = None
        assert "ENGAGED" in await svc.kill(ME, "test")
        assert eng.kill.engaged and store.audits("kill_switch")[0]["by"] == "discord"
        assert "released" in await svc.release(ME)
        assert not eng.kill.engaged
    finally:
        await svc.api.aclose()
        svc.log.close()
        await server.stop()
