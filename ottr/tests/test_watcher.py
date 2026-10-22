"""
The desk watcher (engine/watcher.py): the reply's validation, which conditions code
accepts, when a call may go ahead, and through the real engine on the market rig: a
review sets conditions and posts them, a condition met on a live quote calls a meeting
that says why in its packet and record, the limits refuse the next one, the council loop
wakes for a call, a failed review is remembered, and the API, chat, Discord and CLI show
it all.
"""

import asyncio
import dataclasses
import json

import aiohttp
import pytest

from ottr.api import ApiServer
from ottr.chat import render
from ottr.cli import watch_report
from ottr.config import Alerts as Limits
from ottr.council.models import ModelError
from ottr.council.schemas import SchemaError
from ottr.engine import watcher
from ottr.engine.engine import NEXT_MEETING, Engine
from ottr.engine.watcher import Trigger, WatchState
from ottr.notify import Alerts, format_event
from tests.fakes import OPEN, ScriptedModel, buy_council, et, market_rig, no_corporate_actions
from tests.nyse import calendar_2026

CAL = calendar_2026()


def reply(call=False, why="nothing material changed", note="TLT support and the broad market", conds=None):
    return {"call_meeting": call, "why": why, "note": note, "watch_for": conds if conds is not None else []}


# -- the reply and the conditions ------------------------------------------------------------
def test_the_reply_is_validated():
    ok = watcher.validate_reply(reply(conds=[{"asset": "tlt", "kind": "price_below", "level": 88, "why": "support"}]))
    assert ok["watch_for"] == [{"asset": "TLT", "kind": "price_below", "level": 88.0, "why": "support"}]
    for bad, msg in [({**reply(), "call_meeting": "yes"}, "true or false"),
                     ({**reply(), "watch_for": None}, "must be a list"),
                     (reply(conds=[{"asset": "TLT", "kind": "price_near", "level": 1, "why": "x"}]), "kind must be"),
                     (reply(conds=[{"asset": "TLT", "kind": "move_pct", "level": float("nan"), "why": "x"}]), "positive"),
                     ({**reply(), "why": ""}, "why: must be a non-empty string")]:
        with pytest.raises(SchemaError, match=msg):
            watcher.validate_reply(bad)


def test_code_accepts_only_conditions_it_can_watch():
    rows = [{"asset": "TLT", "kind": "price_below", "level": 88.0, "why": "a"},
            {"asset": "TLT", "kind": "price_below", "level": 87.0, "why": "second"},
            {"asset": "SPY", "kind": "price_above", "level": 499.0, "why": "already above"},
            {"asset": "GLD", "kind": "price_above", "level": 400.0, "why": "too far"},
            {"asset": "QQQ", "kind": "move_pct", "level": 30, "why": "too big"},
            {"asset": "NOPE", "kind": "move_pct", "level": 2, "why": "no price"},
            {"asset": "SPY", "kind": "move_pct", "level": 1.5, "why": "b"},
            {"asset": "IEF", "kind": "price_above", "level": 101.0, "why": "over the cap"}]
    prices = {"TLT": 90.0, "SPY": 500.0, "GLD": 200.0, "QQQ": 400.0, "IEF": 100.0}
    got, problems = watcher.accept_triggers(rows, prices, max_n=2)
    assert [t.describe() for t in got] == ["TLT below $88.00", "SPY moving 1.5% from $500.00"]
    assert problems == [
        "TLT price_below 87.00: a second price_below condition on TLT",
        "SPY price_above 499.00: already met at $500.00",
        "GLD price_above 400.00: more than 25% from the price $200.00",
        "QQQ move_pct 30: a move must be 0.5 to 20 percent",
        "NOPE move_pct 2: no price for NOPE",
        "IEF price_above 101.00: over the limit of 2 conditions"]
    spy = got[1]
    assert not spy.hit(505.0) and spy.hit(507.5) and spy.hit(492.0)
    assert watcher.fired(got, {"TLT": 87.99, "SPY": 501.0}) == [(got[0], 87.99)]


def test_code_decides_whether_a_call_may_run(cfg):
    st, mt = WatchState(), CAL.state_at(OPEN)                                     # Wed 11:00
    assert watcher.call_blocked(cfg, mt, OPEN, st, None, None, False) is None
    assert watcher.call_blocked(cfg, CAL.state_at(et(2026, 10, 21, 8)), et(2026, 10, 21, 8), st, None, None,
                                False) == "the market is closed"
    assert watcher.call_blocked(cfg, mt, OPEN, st, None, None, True) == "a meeting is running"
    late = et(2026, 10, 21, 15, 30)
    assert "30 minutes to the close" in watcher.call_blocked(cfg, CAL.state_at(late), late, st, None, None, False)
    assert watcher.call_blocked(cfg, mt, OPEN, st, OPEN - 1200, None, False) == \
        "the last meeting began 20 minutes ago (at least 45 apart)"
    assert watcher.call_blocked(cfg, mt, OPEN, st, None, OPEN + 1800, False) == \
        "the scheduled meeting at 11:30 ET comes sooner"
    st.called = [{"ts": OPEN - 7200 + i, "ran": True, "by": "watcher", "why": "x"} for i in range(4)]
    assert watcher.call_blocked(cfg, mt, OPEN, st, None, None, False) == "today's 4 called meetings are used"
    st.called = [{"ts": OPEN - 86400 + i, "ran": True} for i in range(4)]          # yesterday's don't count
    assert watcher.call_blocked(cfg, mt, OPEN, st, None, None, False) is None


# -- through the engine ----------------------------------------------------------------------
class Recorder:
    def __init__(self):
        self.posts = []

    async def __call__(self, text):
        self.posts.append(text)
        return True


def drain(alerts):
    out = []
    while not alerts.queue.empty():
        out.append(alerts.queue.get_nowait())
    return out


async def rigged(cfg, store, replies, council=None):
    no_corporate_actions(store, cfg.core)
    rig = await market_rig(cfg, store).ready()
    model = ScriptedModel("watcher", replies)
    eng = Engine(cfg, store, rig.market, council=council or buy_council(cfg, "TLT"),
                 alerts=Alerts(Recorder(), Limits()), clock=rig.clock, watcher_model=model)
    await eng.refresh_members()
    return eng, rig, model


async def test_a_review_sets_conditions_and_a_met_one_calls_a_meeting(cfg, store, ledger):
    eng, rig, model = await rigged(cfg, store, [])
    tlt0 = rig.price("TLT")
    model.replies = [reply(conds=[
        {"asset": "TLT", "kind": "price_below", "level": round(tlt0 * 0.98, 2), "why": "duration risk is back"},
        {"asset": "SPY", "kind": "move_pct", "level": 2, "why": "risk appetite"},
        {"asset": "GLD", "kind": "price_above", "level": 1e6, "why": "nonsense"}])]
    out = await eng.review("routine check")
    assert [t.describe() for t in eng.watch_state().triggers] == [
        f"TLT below ${round(tlt0 * 0.98, 2):,.2f}", f"SPY moving 2% from ${out['triggers'][1].ref_price:,.2f}"]
    assert out["problems"][0].startswith("GLD price_above 1,000,000.00: more than 25%")
    prompt = model.prompts[0]
    assert "=== SINCE THE LAST MEETING ===\nNo meeting has run yet." in prompt and "DESK BOOK" in prompt
    (post,) = drain(eng.alerts)
    assert post.startswith("**Watching** (routine check): TLT support and the broad market\n- TLT below $")
    assert await eng.watch_tick() == "watching 2 condition(s)"                    # nothing met, review fresh

    # TLT trades through the level: the condition is used up and a meeting is called.
    rig.fake.add_quote("TLT", round(tlt0 * 0.97, 2), round(tlt0 * 0.9702, 2), age=2.0)
    rig.market._snaps_ts = 0.0
    assert "TLT below" in await eng.watch_tick()
    st = eng.watch_state()
    assert [t.kind for t in st.triggers] == ["move_pct"] and st.pending["by"] == "condition"
    assert "watched because duration risk is back" in st.pending["why"] and eng._call.is_set()
    assert drain(eng.alerts)[0].startswith("**Meeting called** (a watched condition was met): TLT below $")

    model.replies = [reply(note="after the call")]                                 # the post-meeting review
    eng._call.clear()
    await eng._called_meeting()
    await eng._review_task
    rec = json.loads(store.conn.execute("SELECT record FROM meetings").fetchone()[0])
    assert rec["called"]["by"] == "condition" and rec["context"].startswith("WHY THIS MEETING: called at 11:00 ET")
    assert len(eng.watch_state().called_on(OPEN)) == 1 and eng.watch_state().note == "after the call"
    assert "Moves since (price at the meeting -> now)" in model.prompts[-1]
    posts = drain(eng.alerts)
    assert any(p.startswith("**Council m-") and "\nCalled by the watcher: TLT below $" in p for p in posts)

    # The next call inside the gap is refused, and says why.
    assert eng.request_meeting("watcher", "SPY slid") is False
    assert drain(eng.alerts)[-1].startswith(
        "**The watcher asked for a meeting, but no meeting**: the last meeting began 0 minutes ago")


async def test_the_council_loop_wakes_for_a_call(cfg, store, ledger):
    eng, rig, model = await rigged(cfg, store, [reply()])
    store.set_state(NEXT_MEETING, OPEN + 3 * 3600)                                # the 15:15 slot is far off
    loop = asyncio.create_task(eng._council_loop())
    try:
        assert eng.request_meeting("watcher", "a shock across assets") is True
        for _ in range(200):
            if store.conn.execute("SELECT COUNT(*) FROM meetings").fetchone()[0]:
                break
            await asyncio.sleep(0.02)
    finally:
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)
    rec = json.loads(store.conn.execute("SELECT record FROM meetings").fetchone()[0])
    assert rec["called"] == {"by": "watcher", "why": "a shock across assets"}
    assert eng.watch_state().pending is None


async def test_a_failed_review_is_remembered_and_closed_markets_are_left_alone(cfg, store, ledger):
    eng, rig, model = await rigged(cfg, store, [ModelError("claude: timed out")])
    with pytest.raises(ModelError):
        await eng.review("routine check")
    assert "claude: timed out" in eng.watch_state().last_error
    rig.at(et(2026, 10, 21, 18))
    assert await eng.watch_tick() == "market closed: watching resumes at the open"


async def test_the_api_chat_and_cli_show_the_watcher(cfg, store, ledger):
    eng, rig, model = await rigged(cfg, store, [reply(conds=[{"asset": "SPY", "kind": "move_pct", "level": 2,
                                                              "why": "risk appetite"}])])
    out = await eng.review("routine check")
    server = ApiServer(eng, "127.0.0.1", 0)
    await server.start()
    try:
        async with aiohttp.ClientSession(f"http://127.0.0.1:{server.port}") as s:
            async with s.get("/api/council") as r:
                c = await r.json()
    finally:
        await server.stop()
    w = c["watch"]
    assert w["enabled"] and w["called_today"] == 0 and w["max_called_per_day"] == 4
    assert w["conditions"][0]["condition"].startswith("SPY moving 2% from $")
    assert "Watcher: watching SPY moving 2% from $" in render.council_line(c)
    assert "0 of 4 called meetings used today" in render.council_line(c)
    text = watch_report(out, cfg)
    assert text.startswith("Watcher (claude (opus)): call a meeting now? no\n  why: nothing material changed")
    assert "(a review by hand changes nothing and calls no meeting)" in text


def test_the_discord_messages():
    assert format_event("meeting_called", {"by": "watcher", "why": "a shock across assets"}) == \
        "**Meeting called** (the watcher): a shock across assets"
    assert format_event("watch_set", {"reason": "after the meeting", "note": "", "triggers": []}) == \
        "**Watching** (after the meeting)\n- nothing: no condition set until the next meeting"
    live = format_event("meeting_live", {"phase": "analysts", "id": "m-1", "started_at": 0, "called": "TLT broke",
                                         "analysts": [], "chair": {"name": "claude", "status": "waiting"}})
    assert live.splitlines()[1] == "Called by the watcher: TLT broke"


def test_the_config_limits(cfg, tmp_path):
    from ottr import config
    with pytest.raises(config.ConfigError, match="watch.model"):
        config._validate(dataclasses.replace(cfg, watch=dataclasses.replace(cfg.watch, model="llama")))
    with pytest.raises(config.ConfigError, match="min_gap_min"):
        config._validate(dataclasses.replace(cfg, watch=dataclasses.replace(cfg.watch, min_gap_min=5)))
    assert Trigger("SPY", "price_above", 1.0, "x", 1.0).describe() == "SPY above $1.00"
