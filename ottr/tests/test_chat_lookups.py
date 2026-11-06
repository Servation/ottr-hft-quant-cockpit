"""
/ask's read-only lookups and tool loop (chat/lookup.py, chat/agent.py), with no Discord and
no real model: a scripted fake model drives the loop against a fake engine API, and the
real Anthropic SDK runs over a mock transport for the Claude API's request shape.

Covers: every lookup is one keyless GET (the model can't write); bad or unknown lookups
come back as errors the model can read; one turn's results go back in one message; the
lookup cap and the forced last turn; a meeting proposal is only a field of the answer, and
the service offers its button only when code would allow a call; the owner's call POSTs
with the key and a refusal says code's reason; timestamps in New York time; clipping;
the prompt's split into instructions and data; and which config turns the loop on.
"""

import asyncio
import dataclasses
import json

import anthropic
import httpx2
import pytest

from ottr.chat import agent, prompt
from ottr.chat.agent import ANSWER_SCHEMA, OVER_LIMIT, ClaudeTools, Step, parse_answer, tool_model
from ottr.chat.client import DeskContext
from ottr.chat.log import Asker, ChatLog
from ottr.chat.lookup import BY_NAME, LOOKUPS, MEETING_ID_RE, TOOLS, Arg, BadArgument, Lookups, clip_json, readable
from ottr.chat.service import MEETING_OWNER_ONLY, ChatService, button_life
from ottr.council.models import FALLBACK_BETA, ModelError
from tests.fakes import CHAT_KEY, FakeEngineApi, RecordingModel, ScriptedTools, asks, replies

OWNER, FRIEND = 111111111111111111, 222222222222222222
ME = Asker(OWNER, "Jeff", 555)
PAL = Asker(FRIEND, "pal", 555)
NOW = 1_793_978_103.0          # Fri 2026-11-06 10:15:03 ET
TEST_KEY = "test-key-never-shown"


@pytest.fixture
def engine_api():
    return FakeEngineApi()


@pytest.fixture
async def lookups(engine_api):
    client = engine_api.client()
    yield Lookups(client, lambda: NOW)
    await client.aclose()


# -- the lookups -----------------------------------------------------------------------------
async def test_every_lookup_is_one_keyless_get(engine_api, lookups):
    for lk in LOOKUPS:
        text, is_error, done = await lookups.run(lk.name, {"meeting_id": "m-1"} if lk.required else {})
        assert not is_error and done.ok and json.loads(text), lk.name
    assert engine_api.writes() == []
    assert len(engine_api.requests) == len(LOOKUPS)
    # The client holds the API key, but a lookup never sends it.
    assert all("authorization" not in h for _, _, h, _ in engine_api.requests)
    assert {p for _, p, _, _ in engine_api.requests} == {
        "/api/meetings/m-1", "/api/meetings", "/api/fills", "/api/audit", "/api/equity", "/api/council",
        "/api/screen", "/api/universe", "/api/health", "/api/meeting-call"}


def test_the_tool_definitions():
    names = [t["name"] for t in TOOLS]
    assert len(names) == len(set(names)) == len(LOOKUPS) and set(names) == set(BY_NAME)
    for t in TOOLS:
        s = t["input_schema"]
        assert s["type"] == "object" and s["additionalProperties"] is False and t["description"]
        assert set(s["required"]) <= set(s["properties"])
    # Nothing that sounds like a write is a lookup.
    assert not [n for n in names if any(w in n for w in ("kill", "release", "order", "trade", "call_meeting"))]


async def test_arguments_map_to_the_query(engine_api):
    seen = []   # the query strings, as the fake engine saw them

    def record(request):
        seen.append(dict(request.url.params))
        return engine_api.handler(request)

    import httpx
    from ottr.chat.client import ApiClient
    client = ApiClient("http://127.0.0.1:8765/api", CHAT_KEY, transport=httpx.MockTransport(record))
    try:
        lk = Lookups(client, lambda: NOW)
        await lk.run("list_meetings", {"limit": 2, "before_ts": 1_762_000_000})
        await lk.run("get_fills", {"before_id": 7})
        await lk.run("get_audit", {"kind": "order_rejected", "hours": 2})
        await lk.run("get_equity", {"days": 7, "max_points": 20})
        await lk.run("get_audit", {})
    finally:
        await client.aclose()
    assert seen == [{"limit": "2", "before": "1762000000"}, {"limit": "20", "before": "7"},
                    {"since": str(NOW - 7200), "limit": "40", "kind": "order_rejected"},
                    {"since": str(NOW - 7 * 86400), "max_points": "20"},
                    {"since": str(NOW - 86400), "limit": "40"}]


async def test_the_packet_comes_only_when_asked(lookups):
    text, _, _ = await lookups.run("get_meeting", {"meeting_id": "m-1"})
    assert "packet" not in json.loads(text) and "Buy TLT" in text
    text, _, _ = await lookups.run("get_meeting", {"meeting_id": "m-1", "include_packet": True})
    assert json.loads(text)["packet"]["note"] == "the packet the models saw"


async def test_bad_lookups_come_back_as_errors_and_send_nothing(engine_api, lookups):
    cases = [
        ("place_order", {"asset": "TLT"}, "no lookup named place_order"),
        ("get_meeting", {}, "meeting_id is required"),
        ("get_meeting", {"meeting_id": ".."}, "meeting_id must match"),
        ("get_meeting", {"meeting_id": "../kill-switch"}, "meeting_id must match"),
        ("get_fills", {"limit": 500}, "limit must be between 1 and 50"),
        ("get_fills", {"limit": 2.5}, "limit must be a whole number"),
        ("get_fills", {"limit": True}, "limit must be a number"),
        ("get_fills", {"limit": "5"}, "limit must be a number"),
        ("get_fills", {"path": "/kill-switch"}, "unknown argument(s) path"),
        ("get_audit", {"kind": "DROP TABLE"}, "kind must match"),
        ("get_meeting", {"meeting_id": "m-1", "include_packet": 1}, "include_packet must be true or false"),
        ("get_health", ["x"], "arguments must be an object"),
    ]
    for name, args, want in cases:
        text, is_error, done = await lookups.run(name, args)
        assert is_error and not done.ok and want in text, (name, args, text)
    assert engine_api.requests == []
    # A failed read is an error result too, with the API's own message.
    text, is_error, done = await lookups.run("get_meeting", {"meeting_id": "m-404"})
    assert is_error and text == "the engine API said: not found" and done.note == "not found"
    engine_api.down = True
    text, is_error, _ = await lookups.run("get_health", {})
    assert is_error and "can't connect to the engine API" in text


def test_argument_checks():
    n = Arg("int", "how many", 1, 10)
    assert n.check("n", 5) == 5 and n.check("n", 5.0) == 5 and isinstance(n.check("n", 5.0), int)
    for bad in (0, 11, 2.5, True, None, "3", float("nan")):
        with pytest.raises(BadArgument):
            n.check("n", bad)
    assert Arg("number", "hours", 1, 720).check("h", 1.5) == 1.5
    assert MEETING_ID_RE.match("m-20261106T151500-a1b2c3") and MEETING_ID_RE.match("m-1")
    for bad in ("", ".", "..", "../x", "a/b", "-m", "m 1", "x" * 81):
        assert not MEETING_ID_RE.match(bad), bad


def test_timestamps_read_in_new_york_time():
    out = readable({"ts": NOW, "started_at": NOW, "as_of": NOW, "count": NOW, "retry_at": 3, "done": True,
                    "rows": [{"updated_ts": NOW, "ok_ts": False}], "since": None})
    et = "Fri 2026-11-06 10:15:03 ET (1793978103)"
    assert out["ts"] == out["started_at"] == out["as_of"] == out["rows"][0]["updated_ts"] == et
    assert out["count"] == NOW and out["retry_at"] == 3 and out["rows"][0]["ok_ts"] is False
    assert out["since"] is None and out["done"] is True


def test_a_long_result_is_clipped_and_says_so():
    big = {"rows": ["x" * 100] * 50}
    whole = len(clip_json(big, limit=10**6))
    text = clip_json(big, limit=1000)
    assert text.startswith('{"rows":["xxx') and len(text) < 1200
    assert f"(cut at 1,000 of {whole:,} characters: ask for fewer rows" in text
    assert clip_json({"a": 1}) == '{"a":1}'


# -- the loop --------------------------------------------------------------------------------
async def test_one_turns_lookups_run_together_and_go_back_in_one_message(engine_api, lookups):
    first = asks(("get_meeting", {"meeting_id": "m-1"}), ("get_fills", {"limit": 3}))
    model = ScriptedTools(first, replies("The council bought TLT on falling yields."))
    seen = []
    ans = await agent.answer(model, lookups, "SYSTEM", "the question", timeout=30, on_lookup=seen.append)
    assert ans.text == "The council bought TLT on falling yields." and ans.proposal is None
    assert ans.rounds == 2 and ans.tokens == (2200, 130, 1800, 800) and ans.model == ""
    assert [d.describe().split(")")[0] for d in ans.lookups] == ["get_meeting(meeting_id=m-1", "get_fills(limit=3"]
    assert seen == ans.lookups and model.sessions == 1
    r1, r2 = model.requests
    assert r1.system == r2.system == "SYSTEM" and r1.tools is TOOLS and not r1.final and not r2.final
    assert r1.messages == [{"role": "user", "content": "the question"}]
    user, assistant, results = r2.messages
    assert assistant == {"role": "assistant", "content": first.content}   # sent back unchanged
    assert results["role"] == "user" and [b["tool_use_id"] for b in results["content"]] == \
        [c.id for c in first.calls]
    assert all(b["type"] == "tool_result" and "is_error" not in b for b in results["content"])
    meeting = json.loads(results["content"][0]["content"])
    assert meeting["id"] == "m-1" and "packet" not in meeting and meeting["ts"].endswith(")")
    assert engine_api.writes() == []


async def test_a_bad_lookup_is_an_error_result_the_model_can_correct(lookups):
    bad = asks(("get_fills", {"limit": 999}))
    model = ScriptedTools(bad, asks(("get_fills", {"limit": 50})), replies("Here are the fills."))
    ans = await agent.answer(model, lookups, "S", "fills?", timeout=30)
    (res,) = model.requests[1].messages[-1]["content"]
    assert res["is_error"] is True and "limit must be between 1 and 50" in res["content"]
    assert [d.ok for d in ans.lookups] == [False, True] and ans.rounds == 3


async def test_the_lookup_cap_then_a_last_turn_without_tools(engine_api, lookups):
    five = [("get_health", {})] * 5
    model = ScriptedTools(asks(*five), asks(*five), replies("What I could check."))
    ans = await agent.answer(model, lookups, "S", "everything", timeout=30)
    assert len(ans.lookups) == agent.MAX_LOOKUPS == 8 and len(engine_api.requests) == 8
    assert [r.final for r in model.requests] == [False, False, True]
    results = model.requests[2].messages[-1]["content"]
    assert len(results) == 5                                # every tool_use has its result
    assert [b.get("is_error", False) for b in results] == [False, False, False, True, True]
    assert results[-1]["content"] == OVER_LIMIT


async def test_the_last_round_is_final_however_few_lookups(lookups):
    turns = [asks(("get_council", {})) for _ in range(agent.MAX_ROUNDS - 1)] + [replies("Done.")]
    model = ScriptedTools(*turns)
    ans = await agent.answer(model, lookups, "S", "q", timeout=30)
    assert ans.rounds == agent.MAX_ROUNDS and [r.final for r in model.requests] == [False] * 4 + [True]


async def test_a_model_that_never_stops_asking_fails(lookups):
    turns = [asks(("get_council", {})) for _ in range(agent.MAX_ROUNDS)]
    with pytest.raises(ModelError, match="still asking for lookups after 5 turns"):
        await agent.answer(ScriptedTools(*turns), lookups, "S", "q", timeout=30)


async def test_empty_and_plain_replies(lookups):
    empty = Step([], "end_turn", [], "  ")
    with pytest.raises(ModelError, match="empty reply"):
        await agent.answer(ScriptedTools(empty), lookups, "S", "q", timeout=30)
    blank = replies("   ")
    with pytest.raises(ModelError, match="empty answer"):
        await agent.answer(ScriptedTools(blank), lookups, "S", "q", timeout=30)
    plain = Step([], "end_turn", [], "Just text, not JSON.", model="claude-sonnet-5-5")
    ans = await agent.answer(ScriptedTools(plain), lookups, "S", "q", timeout=30)
    assert ans.text == "Just text, not JSON." and ans.proposal is None
    assert ans.model == "claude-sonnet-5-5"                 # a fallback model answered: say so


def test_a_proposal_needs_the_flag_and_a_reason():
    def parse(**o):
        return parse_answer(json.dumps({"answer": "A", "propose_meeting": False, "meeting_reason": "", **o}))
    assert parse() == ("A", None)
    assert parse(propose_meeting=True, meeting_reason="") == ("A", None)
    assert parse(propose_meeting=True, meeting_reason="   ") == ("A", None)
    assert parse(propose_meeting="yes", meeting_reason="TLT broke") == ("A", None)
    assert parse(propose_meeting=False, meeting_reason="TLT broke") == ("A", None)
    text, p = parse(propose_meeting=True, meeting_reason=" TLT broke\n  its 50-day EMA ")
    assert text == "A" and p.why == "TLT broke its 50-day EMA"
    assert len(parse(propose_meeting=True, meeting_reason="x" * 999)[1].why) == agent.REASON_MAX
    assert parse_answer('{"text": "no answer field"}') == ('{"text": "no answer field"}', None)
    assert parse_answer("[1, 2]") == ("[1, 2]", None)


# -- the service -----------------------------------------------------------------------------
@pytest.fixture
def chat_cfg(cfg):
    return dataclasses.replace(cfg, discord_bot_token="token", chat_owner_id=OWNER,
                               chat_allowed_ids=(FRIEND,), api_key=CHAT_KEY)


@pytest.fixture
async def make(chat_cfg, engine_api, tmp_path):
    made = []

    def _make(tools, cfg=None):
        log = ChatLog(tmp_path / "chat.db")
        svc = ChatService(cfg or chat_cfg, engine_api.client(), RecordingModel(), log, tools=tools)
        made.append(svc)
        return svc

    yield _make
    for svc in made:
        await svc.api.aclose()
        svc.log.close()


async def no_defer():
    return None


async def test_ask_with_lookups_answers_and_records_them(make, engine_api, chat_cfg):
    model = ScriptedTools(asks(("get_fills", {"limit": 3})), replies("Three fills, all TLT."))
    svc = make(model)
    out = await svc.ask(ME, "show me the last three fills", no_defer)
    assert out.text.startswith("> **Jeff:** show me the last three fills\n\nThree fills, all TLT.\n\n-# ")
    assert "| 1 lookup | 1 of 20 asks this hour | chat can't trade" in out.text
    assert out.meeting_why is None and not out.ephemeral and svc.model.prompts == []
    system = model.requests[0].system
    assert system.startswith("You are the desk assistant for OTTR") and "Question from" not in system
    assert "at least 45 minutes from any other meeting" in system and "at most 4 called meetings" in system
    assert "show me the last three fills" in model.requests[0].messages[0]["content"]
    assert "show me the last three fills" not in system     # data and the question go in the user turn
    row = svc.log.recent(1)[0]
    assert (row["command"], row["status"], row["model"]) == ("ask", "ok", "claude (lookups)")
    assert "2 turn(s), 1 lookup(s); tokens 2,200 in, 130 out, 1,800 cache read, 800 cache write" in row["detail"]
    assert "get_fills(limit=3)" in row["detail"] and row["reply"] == "Three fills, all TLT."
    assert engine_api.writes() == []


async def test_a_proposal_gets_a_button_when_code_would_allow_a_call(make, engine_api):
    svc = make(ScriptedTools(replies("TLT broke support; worth a look.", True, "TLT broke its 50-day EMA")))
    out = await svc.ask(PAL, "anything urgent?", no_defer)
    assert out.meeting_why == "TLT broke its 50-day EMA"
    assert ("**Proposed: a council meeting.** The owner can call it with the button below (it works for "
            "10 minutes).\n> TLT broke its 50-day EMA") in out.text
    assert "proposed a meeting: TLT broke its 50-day EMA" in svc.log.recent(1)[0]["detail"]
    # Proposing wrote nothing: it only read whether a call is allowed.
    assert engine_api.writes() == [] and ("GET", "/api/meeting-call") in [(m, p) for m, p, _, _ in
                                                                           engine_api.requests]


def test_the_button_times_out_before_discords_token_does():
    # Discord's token for the answer lasts 15 minutes from the /ask: the button's window is
    # 10 minutes, less after a slow answer, so taking it away never fails.
    assert button_life(0) == button_life(240) == 600.0
    assert button_life(600) == 240.0                        # the slowest answer chat.timeout_sec allows
    assert button_life(10_000) == 60.0


async def test_no_button_when_code_would_refuse_the_call(make, engine_api):
    engine_api.call_state.update(allowed=False, blocked="the market is closed")
    svc = make(ScriptedTools(replies("Worth a look.", True, "TLT broke")))
    out = await svc.ask(ME, "call a meeting", no_defer)
    assert out.meeting_why is None
    assert "**Proposed: a council meeting**, but code won't allow one now: the market is closed." in out.text


async def test_the_button_is_offered_when_the_check_fails(make, engine_api):
    engine_api.missing.add("/api/meeting-call")             # an older engine: the press will say
    svc = make(ScriptedTools(replies("Worth a look.", True, "TLT broke")))
    assert (await svc.ask(ME, "q", no_defer)).meeting_why == "TLT broke"


async def test_only_the_owner_calls_and_the_call_carries_the_key(make, engine_api):
    svc = make(ScriptedTools())
    assert await svc.call_meeting(PAL, "TLT broke") == MEETING_OWNER_ONLY and engine_api.writes() == []
    assert svc.log.recent(1)[0]["detail"] == "owner only"
    text = await svc.call_meeting(ME, "TLT broke")
    assert text.startswith("**Council meeting called** by Jeff: TLT broke\nIt starts within a minute")
    assert text.endswith("1 of 4 called meetings were used today before this one.")
    ((method, path, headers, body),) = [r for r in engine_api.requests if r[0] == "POST"]
    assert (path, headers["authorization"], body) == ("/api/meeting-call", f"Bearer {CHAT_KEY}",
                                                      {"why": "TLT broke", "by": "discord"})
    # A second press before it starts: code refuses, and says why.
    text = await svc.call_meeting(ME, "TLT broke again")
    assert text == ("**No meeting**: a meeting called by the owner is about to start. Code's limits for "
                    "called meetings decide this, as for the watcher's.")
    assert [(r["status"], r["input"]) for r in svc.log.recent(2)] == [("refused", "TLT broke again"),
                                                                      ("ok", "TLT broke")]


async def test_a_call_with_a_wrong_key_is_an_error_not_a_refusal(make, engine_api):
    engine_api.key = "some-other-key-0123456789"
    svc = make(ScriptedTools())
    text = await svc.call_meeting(ME, "TLT broke")
    assert "missing or wrong API key" in text and svc.log.recent(1)[0]["status"] == "error"
    assert engine_api.call_state["pending"] is None


async def test_a_slow_loop_times_out_and_says_how_far_it_got(make, chat_cfg):
    cfg = dataclasses.replace(chat_cfg, chat=dataclasses.replace(chat_cfg.chat, timeout_sec=0.2))
    model = ScriptedTools(asks(("get_health", {})), replies("never"))
    svc = make(model, cfg)
    model.hold = asyncio.Event()                            # the first turn never comes back
    out = await svc.ask(ME, "slow", no_defer)
    assert out.text == "No answer within 0s (0 lookups made). Nothing changed on the desk; try a narrower question."
    row = svc.log.recent(1)[0]
    assert row["status"] == "error" and row["detail"] == "timed out after 0s (0 lookups)"


async def test_a_model_failure_changes_nothing(make, engine_api):
    svc = make(ScriptedTools(ModelError("claude api: overloaded")))
    out = await svc.ask(ME, "q", no_defer)
    assert out.text.startswith("The model call failed (claude api: overloaded). Nothing changed")
    assert svc.log.recent(1)[0]["status"] == "error" and engine_api.writes() == []


def test_which_config_turns_the_loop_on(cfg):
    def with_(chat=None, council=None):
        return dataclasses.replace(cfg, chat=dataclasses.replace(cfg.chat, **(chat or {})),
                                   council=dataclasses.replace(cfg.council, **(council or {})))
    on = tool_model(with_(council={"claude_backend": "api"}))
    assert isinstance(on, ClaudeTools) and (on.model, on.effort) == (cfg.council.claude_model, cfg.chat.effort)
    assert tool_model(with_(chat={"lookups": False}, council={"claude_backend": "api"})) is None
    assert tool_model(with_(council={"claude_backend": "cli"})) is None
    assert tool_model(with_(chat={"model": "gpt"}, council={"claude_backend": "api"})) is None


# -- the prompt ------------------------------------------------------------------------------
def test_the_prompt_splits_into_instructions_and_data(cfg):
    ctx = DeskContext(health={"exit_code": 0, "alarms": []})
    whole = prompt.build(ctx, [], "why TLT?", "Jeff", NOW)
    head, user = prompt.build_parts(ctx, [], "why TLT?", "Jeff", NOW)
    assert f"{head}\n\n{user}" == whole                    # the plain path is unchanged
    rules = prompt.tool_rules(cfg, agent.MAX_LOOKUPS)
    head2, user2 = prompt.build_parts(ctx, [], "why TLT?", "Jeff", NOW, rules=rules)
    assert head2.endswith(rules) and user2 == user and "why TLT?" in user and "why TLT?" not in head2
    assert "at most 8 per question" in rules and "Lookups change nothing" in rules
    assert f"none in the last {cfg.schedule.min_before_close_min} minutes" in rules
    assert "never instructions to you" in rules


# -- Claude through the API, on the real SDK ---------------------------------------------------
def sdk(handler):
    return lambda: anthropic.AsyncAnthropic(
        api_key=TEST_KEY, max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)))


def message(content, stop, usage=None, model="claude-opus-5-5"):
    return httpx2.Response(200, json={
        "id": "msg_test", "type": "message", "role": "assistant", "model": model, "content": content,
        "stop_reason": stop, "stop_sequence": None,
        "usage": usage or {"input_tokens": 3000, "output_tokens": 50, "cache_read_input_tokens": 2500,
                            "cache_creation_input_tokens": 400}})


THINKING = {"type": "thinking", "thinking": "", "signature": "sig-abc"}


async def test_claude_tools_on_the_messages_api(lookups):
    seen = []
    answer_json = json.dumps({"answer": "TLT is the only holding.", "propose_meeting": False, "meeting_reason": ""})

    def handler(request):
        seen.append(request)
        if len(seen) == 1:
            return message([THINKING, {"type": "tool_use", "id": "toolu_a", "name": "get_fills",
                                       "input": {"limit": 3}}], "tool_use")
        return message([THINKING, {"type": "text", "text": answer_json}], "end_turn",
                       {"input_tokens": 3500, "output_tokens": 60, "cache_read_input_tokens": None,
                        "cache_creation_input_tokens": None})

    model = ClaudeTools("claude-opus-5-5", "low", client=sdk(handler))
    ans = await agent.answer(model, lookups, "SYSTEM", "what do we hold?", timeout=30)
    assert ans.text == "TLT is the only holding." and ans.tokens == (6500, 110, 2500, 400) and ans.rounds == 2
    b1, b2 = (json.loads(r.content) for r in seen)
    assert seen[0].url.path == "/v1/messages" and seen[0].headers["authorization"] == f"Bearer {TEST_KEY}"
    assert FALLBACK_BETA in seen[0].headers["anthropic-beta"]
    assert (b1["model"], b1["max_tokens"], b1["system"]) == ("claude-opus-5-5", agent.ASK_MAX_TOKENS, "SYSTEM")
    assert b1["tools"] == TOOLS and b1["tool_choice"] == {"type": "auto"}
    assert b1["output_config"] == {"effort": "low", "format": {"type": "json_schema", "schema": ANSWER_SCHEMA}}
    assert b1["cache_control"] == {"type": "ephemeral"} and b1["fallbacks"] == "default"
    assert "thinking" not in b1                              # always on for Opus 5.5; effort sets how hard
    # The second turn: the assistant's content as it came (thinking signature and all), then
    # the lookup's result for its tool_use id.
    user, assistant, results = b2["messages"]
    assert user == {"role": "user", "content": "what do we hold?"}
    assert assistant["role"] == "assistant" and [c["type"] for c in assistant["content"]] == ["thinking", "tool_use"]
    assert assistant["content"][0]["signature"] == "sig-abc" and assistant["content"][1]["id"] == "toolu_a"
    (res,) = results["content"]
    assert res["type"] == "tool_result" and res["tool_use_id"] == "toolu_a" and '"fills"' in res["content"]


async def test_claude_tools_last_turn_turns_tools_off(lookups, monkeypatch):
    monkeypatch.setattr(agent, "MAX_ROUNDS", 2)
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        if len(seen) == 1:
            return message([{"type": "tool_use", "id": "toolu_b", "name": "get_health", "input": {}}], "tool_use")
        return message([{"type": "text", "text": '{"answer": "ok", "propose_meeting": false, '
                                                 '"meeting_reason": ""}'}], "end_turn")

    await agent.answer(ClaudeTools("claude-opus-5-5", "low", client=sdk(handler)), lookups, "S", "q", timeout=30)
    assert [b["tool_choice"] for b in seen] == [{"type": "auto"}, {"type": "none"}]
    assert seen[1]["tools"] == TOOLS                         # still declared: the history holds a tool_use


async def test_claude_tools_errors_never_show_the_key(lookups):
    def unauthorized(request):
        return httpx2.Response(401, json={"type": "error", "error": {"type": "authentication_error",
                                                                     "message": "invalid x-api-key"}})

    with pytest.raises(ModelError) as e:
        await agent.answer(ClaudeTools("claude-opus-5-5", "low", client=sdk(unauthorized)), lookups, "S", "q",
                           timeout=30)
    assert str(e.value).startswith("claude api:") and TEST_KEY not in str(e.value)

    def refused(request):
        return message([], "refusal")

    with pytest.raises(ModelError, match="declined to answer"):
        await agent.answer(ClaudeTools("claude-opus-5-5", "low", client=sdk(refused)), lookups, "S", "q",
                           timeout=30)

    def cut(request):
        return message([{"type": "text", "text": '{"answer": "trunc'}], "max_tokens")

    with pytest.raises(ModelError, match="cut off at 16000 tokens"):
        await agent.answer(ClaudeTools("claude-opus-5-5", "low", client=sdk(cut)), lookups, "S", "q", timeout=30)
