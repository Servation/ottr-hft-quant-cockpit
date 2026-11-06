"""
Make-up meetings: a scheduled council meeting that fails, or that the engine was down
through, is made up the same day once live prices answer again, within the called-meeting
limits; one that can't run that day is dropped and said so.

The first test replays 2026-11-06 through the real council loop on the market rig: the
network (DNS) went down before the 10:15 meeting, which failed on "no price for any
asset", and came back at 13:55, while the next scheduled meeting was 15:15.
"""

import asyncio
import dataclasses
import json

import aiohttp
import pytest

from ottr.api import ApiServer
from ottr.chat import render
from ottr.config import Alerts as Limits
from ottr.council.models import ModelError
from ottr.engine import watcher
from ottr.engine.engine import NEXT_MEETING, Engine
from ottr.engine.watcher import WatchState
from ottr.notify import Alerts, format_event
from tests.fakes import (ScriptedModel, analyst_reply, buy_council, council_for, et, market_rig,
                         no_corporate_actions)
from tests.nyse import calendar_2026

CAL = calendar_2026()
DAY = (2026, 10, 21)                          # the rig's Wednesday
SLOT, NEXT = et(*DAY, 10, 15), et(*DAY, 15, 15)
DNS = "[Errno 11001] getaddrinfo failed"      # what Windows said on 2026-11-06
MISSED = "the 10:15 ET meeting failed (no price for any asset)"


class Recorder:
    def __init__(self):
        self.posts = []

    async def __call__(self, text):
        self.posts.append(text)
        return True


def drain(alerts):
    out = []
    while not alerts.queue.empty():
        out.append(alerts.queue.get_nowait().text)
        alerts.queue.task_done()
    return out


def watch_reply():
    return {"call_meeting": False, "why": "nothing material changed", "note": "n", "watch_for": []}


async def rigged(cfg, store, now=SLOT - 300, council=None):
    no_corporate_actions(store, cfg.core, now)
    rig = await market_rig(cfg, store, now=now).ready()
    eng = Engine(cfg, store, rig.market, council=council or buy_council(cfg, "TLT"),
                 alerts=Alerts(Recorder(), Limits()), clock=rig.clock,
                 watcher_model=ScriptedModel("watcher", [watch_reply() for _ in range(10)]))
    await eng.refresh_members()
    return eng, rig


def offline(rig, ts):
    """The router's DNS fails: every request to Alpaca fails to connect, and the quotes
    MarketData cached before it go stale."""
    rig.fake.offline = DNS
    rig.at(ts, requote=False)
    rig.market._snaps_ts = 0.0


def online(rig, ts):
    rig.fake.offline = None
    rig.at(ts)
    rig.market._snaps_ts = 0.0


def audits(store, kind):
    return [json.loads(d) for (d,) in store.conn.execute("SELECT data FROM audit WHERE kind=? ORDER BY id", (kind,))]


def council_beat(store):
    return tuple(store.conn.execute("SELECT ok, detail FROM heartbeats WHERE task='council'").fetchone())


def meetings(store):
    return [json.loads(r) for (r,) in store.conn.execute("SELECT record FROM meetings ORDER BY ts")]


async def until(cond, tries=300):
    for _ in range(tries):
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("the council loop never got there")


async def test_the_2026_11_06_outage_is_made_up_once_prices_answer(cfg, store, ledger):
    eng, rig = await rigged(cfg, store)
    assert await rig.market.live_quotes()                       # 10:10: quotes cached, all fine
    offline(rig, SLOT)
    store.set_state(NEXT_MEETING, SLOT)
    loop = asyncio.create_task(eng._council_loop())
    try:
        # 10:15: the scheduled meeting fails on stale quotes, and a make-up is held.
        await until(lambda: eng.watch_state().makeup is not None)
        assert eng.watch_state().makeup["why"] == MISSED
        assert council_beat(store) == (0, "MarketDataError: no price for any asset")
        assert f"**Meeting missed**: {MISSED}. A make-up runs once live prices answer" in "\n".join(drain(eng.alerts))

        # 11:00, still down: the make-up waits for prices, and says so.
        rig.at(et(*DAY, 11), requote=False)
        eng._call.set()                                         # wake the loop (it waits a minute)
        await until(lambda: eng.watch_state().makeup.get("waiting", "").startswith("no live prices yet"))
        assert meetings(store) == [] and eng.watch_state().makeup["tries"] == 0

        # 13:55: the network is back. The make-up runs within the minute.
        online(rig, et(*DAY, 13, 55))
        eng._call.set()
        await until(lambda: meetings(store) and eng.watch_state().makeup is None)
    finally:
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)
    (rec,) = meetings(store)
    assert rec["status"] == "ok" and rec["called"] == {"by": "makeup", "why": MISSED, "slot": SLOT}
    assert rec["context"].startswith(f"WHY THIS MEETING: a make-up, called by code at 13:55 ET: {MISSED}. "
                                     "It stands in for that scheduled meeting")
    assert council_beat(store) == (1, "called: ok")
    st = eng.watch_state()
    assert [c["by"] for c in st.called_on(rig.clock.time())] == ["makeup"]     # counts toward the cap
    assert audits(store, "makeup_done")[0]["meeting"] == rec["id"]
    assert store.get_state(NEXT_MEETING) == NEXT                               # the schedule is untouched
    posts = drain(eng.alerts)
    assert f"**Make-up meeting called**: {MISSED}" in posts
    assert any(p.startswith("**Council m-") and f"\nMake-up: {MISSED}" in p for p in posts)


def test_code_decides_when_a_make_up_can_still_run(cfg):
    st = WatchState(makeup={"slot": SLOT, "why": MISSED, "tries": 0, "tried_ts": None})
    noon = et(*DAY, 12)

    def expired(ts, nxt=NEXT, state=st):
        return watcher.makeup_expired(cfg, CAL.state_at(ts), ts, state, nxt)

    assert expired(noon) is None
    assert expired(et(*DAY, 16, 30)) == "the session ended first"
    assert expired(et(2026, 10, 22, 11)) == "the session ended first"           # the next day's session
    assert expired(et(*DAY, 15, 30), nxt=et(2026, 10, 22, 10, 15)) == \
        "none in the last 45 minutes of the session"
    assert expired(et(*DAY, 14, 40)) == "the scheduled meeting at 15:15 ET comes first"
    used = dataclasses.replace(st, called=[{"ts": noon - 60 * i, "ran": True, "by": "watcher"} for i in range(4)])
    assert expired(noon, state=used) == "today's 4 called meetings are used"

    assert watcher.makeup_spaced(cfg, noon, st.makeup) is None
    assert watcher.makeup_spaced(cfg, noon, {**st.makeup, "tried_ts": noon - 600}) == \
        "the last make-up began 10 minutes ago (at least 45 apart)"
    assert watcher.makeup_spaced(cfg, noon, {**st.makeup, "tried_ts": noon - 46 * 60}) is None


async def test_a_make_up_that_cannot_run_before_the_next_meeting_is_dropped(cfg, store, ledger):
    """Still no prices at 14:40, 35 minutes before 15:15 and inside the gap: the scheduled
    meeting stands in for the make-up instead of two meetings back to back."""
    eng, rig = await rigged(cfg, store, now=et(*DAY, 14))
    store.set_state(NEXT_MEETING, NEXT)
    eng._hold_makeup(SLOT, MISSED)
    offline(rig, et(*DAY, 14))
    await eng._makeup_check()
    assert eng.watch_state().makeup["waiting"].startswith("no live prices yet")
    drain(eng.alerts)
    rig.at(et(*DAY, 14, 40), requote=False)
    await eng._makeup_check()
    assert eng.watch_state().makeup is None and meetings(store) == []
    assert audits(store, "makeup_dropped")[0]["reason"] == "the scheduled meeting at 15:15 ET comes first"
    assert drain(eng.alerts) == [f"**No make-up meeting**: {MISSED}, and the scheduled meeting at 15:15 ET "
                                 "comes first."]


async def test_a_failed_1515_meeting_says_once_that_it_cannot_be_made_up(cfg, store, ledger):
    """15:15 is 45 minutes before the close, inside the last 45 minutes no call may use:
    one post that says so, not "missed, a make-up will run" and then "no make-up"."""
    eng, rig = await rigged(cfg, store, now=NEXT - 300)
    assert await rig.market.live_quotes()
    offline(rig, NEXT + 20)                     # the loop is a moment late, and the retries take time
    store.set_state(NEXT_MEETING, et(2026, 10, 22, 10, 15))
    await eng._scheduled_meeting(NEXT)
    assert eng.watch_state().makeup is None and audits(store, "makeup_held") == []
    posts = [p for p in drain(eng.alerts) if "make-up" in p.lower() or "missed" in p.lower()]
    assert posts == ["**No make-up meeting**: the 15:15 ET meeting failed (no price for any asset), and none "
                     "in the last 45 minutes of the session."]


async def test_a_make_up_without_models_is_retried_after_the_gap(cfg, store, ledger):
    """Prices are back but the models aren't: the make-up abstains like the scheduled
    meeting did, counts against the daily limit, and is tried again min_gap_min later."""
    u = list(cfg.core)
    down = ModelError("claude: can't reach the API (network or DNS)")
    analysts = [[down, down, analyst_reply(u, {"TLT": ("BUY", 0.8)})], [down, down, analyst_reply(u)],
                [down, down, analyst_reply(u)]]
    council = council_for(cfg, [None, None, None], {"summary": "hold", "dissent_notes": "", "trades": []})
    for model, script in zip(council.analysts.values(), analysts):
        model.replies = script
    eng, rig = await rigged(cfg, store, now=SLOT, council=council)
    store.set_state(NEXT_MEETING, NEXT)

    # 10:15: the scheduled meeting abstains, so a make-up is held.
    await eng._scheduled_meeting(SLOT)
    abstained = "the 10:15 ET meeting ended without a decision (abstain: 0 analyst(s) responded, need 2)"
    assert eng.watch_state().makeup["why"] == abstained

    # 10:16: prices answer, but the abstained meeting began at 10:15: the gap applies.
    rig.at(SLOT + 60)
    await eng._makeup_check()
    assert eng.watch_state().makeup["waiting"] == "the last meeting began 1 minutes ago (at least 45 apart)"
    assert eng.watch_state().pending is None

    # 11:01: the make-up is called, and abstains too: the models are still down.
    rig.at(et(*DAY, 11, 1))
    await eng._makeup_check()
    eng._call.clear()
    await eng._called_meeting()
    m = eng.watch_state().makeup
    assert m["tries"] == 1 and m["tried_ts"] == et(*DAY, 11, 1)
    assert [r["status"][:7] for r in meetings(store)] == ["abstain", "abstain"]
    assert [c["by"] for c in eng.watch_state().called_on(rig.clock.time())] == ["makeup"]

    # 11:47: the models are back; the second try decides and settles the make-up.
    rig.at(et(*DAY, 11, 47))
    await eng._makeup_check()
    eng._call.clear()
    await eng._called_meeting()
    assert meetings(store)[-1]["status"] == "ok" and eng.watch_state().makeup is None
    assert len(eng.watch_state().called_on(rig.clock.time())) == 2


async def test_a_slot_the_engine_was_down_through_is_made_up_while_the_session_lasts(cfg, store, ledger):
    eng, rig = await rigged(cfg, store, now=et(*DAY, 11))
    store.set_state(NEXT_MEETING, SLOT)                                  # the engine slept through 10:15
    loop = asyncio.create_task(eng._council_loop())
    try:
        await until(lambda: meetings(store))
    finally:
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)
    assert audits(store, "meeting_skipped") == [{"slot": SLOT}]
    (rec,) = meetings(store)
    assert rec["called"]["why"] == "the engine was down through the 10:15 ET meeting"
    assert rec["context"].startswith("WHY THIS MEETING: a make-up, called by code at 11:00 ET")


async def test_no_make_up_for_a_slot_missed_while_the_market_was_closed(cfg, store, ledger):
    eng, rig = await rigged(cfg, store, now=et(*DAY, 18))
    store.set_state(NEXT_MEETING, NEXT)                                  # down through 15:15, back after the close
    loop = asyncio.create_task(eng._council_loop())
    try:
        await until(lambda: audits(store, "meeting_skipped"))
    finally:
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)
    assert eng.watch_state().makeup is None and audits(store, "makeup_held") == []


async def test_a_meeting_that_decides_settles_a_held_make_up(cfg, store, ledger):
    """The 15:15 meeting decides: it stands in for the 10:15 one that couldn't be made up."""
    eng, rig = await rigged(cfg, store, now=NEXT)
    eng._hold_makeup(SLOT, MISSED)
    await eng._scheduled_meeting(NEXT)
    assert eng.watch_state().makeup is None
    assert audits(store, "makeup_done")[0]["meeting"] == meetings(store)[0]["id"]


async def test_make_ups_can_be_turned_off(cfg, store, ledger):
    off = dataclasses.replace(cfg, schedule=dataclasses.replace(cfg.schedule, make_up_missed=False))
    eng, rig = await rigged(off, store)
    eng._hold_makeup(SLOT, MISSED)
    assert eng.watch_state().makeup is None and drain(eng.alerts) == []


async def test_the_api_chat_and_status_show_a_pending_make_up(cfg, store, ledger, capsys):
    eng, rig = await rigged(cfg, store)
    eng._hold_makeup(SLOT, MISSED)
    offline(rig, et(*DAY, 11))
    await eng._makeup_check()
    server = ApiServer(eng, "127.0.0.1", 0)
    await server.start()
    try:
        async with aiohttp.ClientSession(f"http://127.0.0.1:{server.port}") as s:
            async with s.get("/api/council") as r:
                c = await r.json()
    finally:
        await server.stop()
    assert c["makeup"]["why"] == MISSED and c["makeup"]["waiting"].startswith("no live prices yet")
    line = render.council_line(c)
    assert f"\nMake-up pending: {MISSED} (waiting: no live prices yet" in line


def test_the_discord_messages():
    assert format_event("makeup_held", {"why": MISSED}) == (
        f"**Meeting missed**: {MISSED}. A make-up runs once live prices answer again, within the "
        "called-meeting limits.")
    assert format_event("meeting_called", {"by": "makeup", "why": MISSED}) == f"**Make-up meeting called**: {MISSED}"
    assert format_event("meeting_call_refused", {"by": "makeup", "why": MISSED, "blocked": "a meeting is running"}) \
        .startswith("**A make-up meeting was due, but no meeting**: a meeting is running.")
    live = format_event("meeting_live", {"phase": "analysts", "id": "m-1", "started_at": 0, "called": MISSED,
                                         "called_by": "makeup", "analysts": [],
                                         "chair": {"name": "claude", "status": "waiting"}})
    assert live.splitlines()[1] == f"Make-up: {MISSED}"


def test_the_config_flag_is_a_real_boolean(cfg):
    from ottr import config
    with pytest.raises(config.ConfigError, match="make_up_missed must be true or false"):
        config._validate(dataclasses.replace(cfg, schedule=dataclasses.replace(cfg.schedule, make_up_missed="no")))
