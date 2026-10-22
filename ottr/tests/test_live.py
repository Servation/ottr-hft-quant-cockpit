"""
Live meeting tests: the council's progress events and their order, the progress state
that GET /api/council and the live Discord message share, one message edited in place
(not N posts) with coalesced and spaced edits and one event token for the post, failures
that leave the meeting and every other notification untouched, the engine's wiring
(risk gate outcomes, abort, cancel, shutdown, the config switch), the chat bot's /status
line built from real API JSON, and the Discord create/edit transport.
"""

import asyncio
import dataclasses
import json
import logging
import time

import aiohttp
import httpx
import pytest

from ottr import config as cfgmod
from ottr.api import ApiServer
from ottr.chat import render
from ottr.config import Alerts as Limits
from ottr.config import ConfigError
from ottr.council.council import Council
from ottr.council.models import ModelError
from ottr.engine.engine import Engine
from ottr.engine.progress import MeetingProgress
from ottr.health import Alarm
from ottr.market.types import MarketDataError
from ottr.notify import LIMIT, Alerts, DiscordWebhook, format_event
from tests.fakes import OPEN, LiveRecorder, ScriptedModel, analyst_reply, buy_council, market_rig, no_corporate_actions

FAST = Limits(live_edit_sec=0.05)
EM_DASH = chr(0x2014)
SECRET_URL = "https://discord.com/api/webhooks/123/SECRET-TOKEN"


async def until(cond, timeout: float = 5.0) -> None:
    t0 = time.monotonic()
    while not cond():
        assert time.monotonic() - t0 < timeout, "condition not met in time"
        await asyncio.sleep(0.005)


async def make_engine(cfg, store, council=None, limits=FAST):
    """An engine on the market rig (mid-session Wednesday) whose council buys TLT."""
    hook = LiveRecorder()
    cfg = dataclasses.replace(cfg, alerts=limits)
    no_corporate_actions(store, cfg.core)
    rig = await market_rig(cfg, store).ready()
    eng = Engine(cfg, store, rig.market, council=council or buy_council(cfg, "TLT"),
                 alerts=Alerts(hook, limits, live=hook), clock=rig.clock)
    return eng, hook


def held_council(cfg, who="gemini"):
    """buy_council with one analyst held mid-call until the returned event is set."""
    gate = asyncio.Event()
    council = buy_council(cfg, "TLT")
    council.analysts[who].gate = gate
    return council, gate


# -- council events ------------------------------------------------------------------------
async def test_council_reports_each_analyst_as_it_finishes(cfg):
    u = list(cfg.core)
    events = []
    analysts = {"claude": ScriptedModel("claude", [analyst_reply(u, {"TLT": ("BUY", 0.8)})], delay=0.1),
                "gpt": ScriptedModel("gpt", [analyst_reply(u)]),
                "gemini": ScriptedModel("gemini", ["not json", "still not json"], delay=0.15)}
    chair = ScriptedModel("claude", [{"summary": "s", "trades": [
        {"action": "BUY", "asset": "TLT", "conviction": 0.6, "reasoning": "r"}]}])
    rec = await Council(analysts, chair, u).run("ctx", held=set(), meeting_id="m-1",
                                                observe=lambda k, d: events.append((k, d.get("name"))))
    # Finish order, not config order: gpt answers first, gemini fails last.
    assert events == [("analysts_started", None), ("analyst_done", "gpt"), ("analyst_done", "claude"),
                      ("analyst_dropped", "gemini"), ("chair_started", None), ("chair_done", None)]
    assert rec["id"] == "m-1" and rec["status"] == "ok" and rec["trades"][0]["asset"] == "TLT"


async def test_too_few_analysts_never_convene_the_chair(cfg):
    u = list(cfg.core)
    events = []
    analysts = {"claude": ScriptedModel("claude", [analyst_reply(u)]),
                "gpt": ScriptedModel("gpt", [ModelError("signed out")]),
                "gemini": ScriptedModel("gemini", [ModelError("signed out")])}
    rec = await Council(analysts, ScriptedModel("claude", []), u).run(
        "ctx", held=set(), observe=lambda k, d: events.append(k))
    assert rec["status"].startswith("abstain: 1 analyst(s) responded")
    assert sorted(events) == ["analyst_done", "analyst_dropped", "analyst_dropped", "analysts_started"]


async def test_a_broken_observer_cannot_fail_a_meeting(cfg, caplog):
    def boom(kind, d):
        raise RuntimeError("watcher bug")

    rec = await buy_council(cfg, "TLT").run("ctx", held=set(), observe=boom)
    assert rec["status"] == "ok" and rec["trades"][0]["asset"] == "TLT"
    assert "council observer failed" in caplog.text


# -- progress state ------------------------------------------------------------------------
def test_progress_folds_events_into_one_state():
    p = MeetingProgress("m-1", ["claude", "gpt", "gemini"], "claude", now=100.0)
    assert p.phase == "market" and {a["status"] for a in p.to_dict(100.0)["analysts"]} == {"waiting"}
    p.apply("analysts_started", {"analysts": ["claude", "gpt", "gemini"]})
    p.apply("analyst_done", {"name": "gpt", "latency": 30.04,
                             "report": analyst_reply(["SPY", "TLT"], {"TLT": ("BUY", 0.6)})})
    p.apply("analyst_dropped", {"name": "gemini", "latency": 300.0, "error": "timed out after 300s: agy"})
    d = p.to_dict(now=130.0)
    by = {a["name"]: a for a in d["analysts"]}
    assert d["phase"] == "analysts" and d["elapsed_sec"] == 30.0
    assert by["claude"]["status"] == "thinking"
    assert by["gpt"]["status"] == "done" and by["gpt"]["latency_sec"] == 30.0
    assert by["gpt"]["stances"] == [{"asset": "TLT", "stance": "BUY", "conviction": 0.6}]  # HOLDs left out
    assert by["gemini"]["status"] == "dropped" and "timed out" in by["gemini"]["error"]
    p.apply("chair_started", {"reports": 2})
    assert p.phase == "chair" and p.chair["status"] == "thinking" and p.chair["reports"] == 2
    trade = {"action": "BUY", "asset": "TLT", "conviction": 0.7, "reasoning": "r"}
    p.apply("chair_done", {"latency": 20.0, "trades": [trade], "dropped": [], "decision": {}})
    p.adjourn({"status": "ok", "trades": [trade],
               "dropped": [{"trade": {"action": "SELL", "asset": "SLV"}, "why": "not held"}]},
              executing=True, now=160.0)
    assert p.phase == "orders" and not p.final
    assert p.dropped == [{"action": "SELL", "asset": "SLV", "why": "not held"}]
    assert not p.order("GLD", "filled")  # not a trade of this meeting
    assert p.order("TLT", "filled", "approved", 1234.5, 2401.0)
    assert p.final and p.to_dict()["trades"][0]["outcome"]["status"] == "filled"
    p.abort("too late")  # a recorded meeting can't be unmade
    assert p.phase == "done" and p.note is None


def test_abort_marks_who_was_still_working():
    p = MeetingProgress("m-1", ["claude", "gpt"], "claude")
    p.apply("analysts_started", {})
    p.apply("analyst_done", {"name": "claude", "latency": 1.0, "report": analyst_reply(["SPY"])})
    p.abort("cancelled, the engine is stopping")
    d = p.to_dict()
    assert p.final and d["phase"] == "aborted" and d["note"].startswith("cancelled")
    assert [a["status"] for a in d["analysts"]] == ["done", "stopped"] and d["chair"]["status"] == "stopped"


def test_without_trades_or_execution_a_meeting_is_done_when_recorded():
    p = MeetingProgress("m-1", ["claude"], "claude")
    p.adjourn({"status": "abstain: 0 analyst(s) responded, need 1", "trades": [], "dropped": []}, executing=True)
    assert p.final and p.chair["status"] == "skipped"
    q = MeetingProgress("m-2", ["claude"], "claude")
    q.adjourn({"status": "ok", "trades": [{"action": "BUY", "asset": "TLT", "conviction": 0.5}]}, executing=False)
    assert q.final and not q.executing


def test_live_text_reads_like_a_meeting_and_fits_one_message(cfg):
    u = ["SPY", "TLT"]
    p = MeetingProgress("m-1", ["claude", "gpt", "gemini"], "claude", now=1000.0)
    texts = [format_event("meeting_live", p.to_dict(1000.0))]
    assert texts[0].startswith("**Council in session** (`m-1`, started <t:1000:R>)")
    assert "Reading the market and the book." in texts[0]
    p.apply("analysts_started", {})
    rep = analyst_reply(u, {a: ("BUY", 0.9) for a in u})
    rep["overall_view"] = f"Risk-on {EM_DASH} but careful. " * 40  # long, with the dash models love
    p.apply("analyst_done", {"name": "claude", "latency": 25.0, "report": rep})
    texts.append(format_event("meeting_live", p.to_dict(1025.0)))
    assert "- claude: done in 25s: BUY SPY 0.90, BUY TLT 0.90" in texts[-1]
    assert "- gpt: thinking" in texts[-1] and "- chair (claude): waits for the analysts" in texts[-1]
    p.apply("analyst_done", {"name": "gpt", "latency": 30.0, "report": analyst_reply(u)})
    p.apply("analyst_dropped", {"name": "gemini", "latency": 300.0, "error": "agy: timed out after 300s"})
    p.apply("chair_started", {"reports": 2})
    texts.append(format_event("meeting_live", p.to_dict(1300.0)))
    assert "- gpt: done in 30s: HOLD on every asset" in texts[-1]
    assert "- gemini: dropped out after 5m 00s (agy: timed out after 300s)" in texts[-1]
    assert "- chair (claude): deliberating on 2 reports" in texts[-1]
    buy = {"action": "BUY", "asset": "TLT", "conviction": 0.7}
    sell = {"action": "SELL", "asset": "GLD", "fraction_of_position": 0.5}
    p.apply("chair_done", {"latency": 20.0, "trades": [buy, sell], "dropped": []})
    p.adjourn({"status": "ok", "trades": [buy, sell], "dropped": [
        {"trade": {"action": "BUY", "asset": "QQQ"}, "why": "no analyst rated it BUY"}]},
        executing=True, now=1325.0)
    texts.append(format_event("meeting_live", p.to_dict()))
    assert texts[-1].startswith("**Council adjourned** after 5m 25s (`m-1`): ok")
    assert "- chair (claude): decided in 20s: BUY TLT (conviction 0.70), SELL GLD (50% of the position)" in texts[-1]
    assert "- code dropped BUY QQQ: no analyst rated it BUY" in texts[-1]
    assert "- BUY TLT (conviction 0.70): at the risk gate" in texts[-1]
    p.order("TLT", "filled", "approved", 1234.5, 2401.25)
    p.order("GLD", "rejected", "below_min_trade", notes=["$40.00"])
    texts.append(format_event("meeting_live", p.to_dict()))
    assert "- BUY TLT (conviction 0.70): filled $1,234.50 @ 2,401.25" in texts[-1]
    assert "- SELL GLD (50% of the position): rejected: below min trade ($40.00)" in texts[-1]
    for t in texts:
        assert EM_DASH not in t and len(t) <= LIMIT, t


def test_aborted_and_undecided_meetings_say_why():
    p = MeetingProgress("m-1", ["claude"], "claude", now=0.0)
    p.abort("no quotes for any asset", now=2.0)
    assert format_event("meeting_live", p.to_dict()).startswith(
        "**Council meeting aborted** after 2s (`m-1`): no quotes for any asset")
    q = MeetingProgress("m-2", ["claude"], "claude", now=0.0)
    q.adjourn({"status": "ok", "trades": [{"action": "BUY", "asset": "TLT", "conviction": 0.5}]}, executing=True)
    q.close("the engine stopped before the risk gate decided")
    t = format_event("meeting_live", q.to_dict())
    assert "- BUY TLT (conviction 0.50): undecided (the engine stopped before the risk gate decided)" in t


# -- the live message ----------------------------------------------------------------------
async def test_a_live_message_is_posted_once_then_edited():
    hook = LiveRecorder()
    a = Alerts(hook, FAST, live=hook)
    async with a.running():
        assert a.live("k", "note", {"step": 0})
        await until(lambda: hook.calls)
        for i in range(1, 6):
            assert a.live("k", "note", {"step": i})
            await asyncio.sleep(0)
        a.live("k", "note", {"step": "final"}, final=True)
        await until(lambda: "k" not in a._live)
        assert not a.live("k", "note", {"step": "late"})  # a key's story ends with its final state
    kinds = hook.kinds()
    assert kinds[0] == "create" and kinds.count("create") == 1 and len(kinds) <= 3  # 6 updates, few edits
    assert hook.shown == "**note**: {'step': 'final'}" and hook.posts == []  # nothing went out as a post
    assert a.posted == 1 and a.edited == len(kinds) - 1


async def test_edits_are_coalesced_and_spaced():
    hook = LiveRecorder()
    a = Alerts(hook, Limits(live_edit_sec=0.2), live=hook)
    async with a.running():
        a.live("k", "note", {"i": 0})
        for i in range(1, 30):
            await asyncio.sleep(0.02)
            a.live("k", "note", {"i": i})
        a.live("k", "note", {"i": "end"}, final=True)
    times = [t for _, t, _ in hook.calls]
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert 2 <= len(hook.calls) <= 6  # 31 updates in ~0.6s
    assert all(g >= 0.2 - 0.03 for g in gaps), gaps  # the loop may fire a timer one clock tick early
    assert hook.shown == "**note**: {'i': 'end'}"


async def test_the_post_costs_an_event_token_but_edits_do_not():
    hook = LiveRecorder()
    a = Alerts(hook, Limits(burst=2, per_hour=1, live_edit_sec=0.05), live=hook)
    async with a.running():
        assert a.live("m1", "note", {"x": 1})        # token 1
        assert a.event("note", {"y": 1})             # token 2
        assert not a.event("note", {"y": 2}) and a.suppressed == 1
        assert not a.live("m2", "note", {"x": 1})    # budget spent: no live message
        a.events_bucket.tokens = 5.0
        assert not a.live("m2", "note", {"x": 2})    # and m2 doesn't appear halfway through
        await until(lambda: hook.calls)
        for i in range(3):
            a.live("m1", "note", {"x": 10 + i})
            await asyncio.sleep(0.07)
        a.live("m1", "note", {"x": "end"}, final=True)
        assert a.events_bucket.tokens >= 5.0         # edits drew nothing from the event budget
    assert hook.kinds().count("create") == 1 and hook.shown == "**note**: {'x': 'end'}"


async def test_failed_edits_leave_the_last_state_and_touch_nothing_else():
    hook = LiveRecorder()
    a = Alerts(hook, FAST, live=hook)
    async with a.running():
        a.live("k", "note", {"i": 0})
        await until(lambda: hook.calls)
        hook.fail_edits = True
        a.live("k", "note", {"i": 1})
        a.event("note", {"other": 1})
        await asyncio.sleep(0.1)
        a.live("k", "note", {"i": 2}, final=True)
    assert hook.shown == "**note**: {'i': 0}"        # Discord still shows the last state that landed
    assert hook.posts == ["**note**: {'other': 1}"]
    assert a.edit_failed == 2 and a.edited == 0 and not a._live


async def test_a_live_message_gives_up_after_repeated_failures():
    hook = LiveRecorder()
    a = Alerts(hook, FAST, live=hook)
    async with a.running():
        a.live("k", "note", {"i": 0})
        await until(lambda: hook.calls)
        hook.fail_edits = True
        for i in range(1, 7):
            a.live("k", "note", {"i": i})
            await asyncio.sleep(0.07)
        assert "k" not in a._live
    assert hook.kinds() == ["create", "edit", "edit", "edit"]


async def test_a_failed_post_means_no_live_view_for_that_key():
    hook = LiveRecorder()
    hook.fail_create = True
    a = Alerts(hook, FAST, live=hook)
    async with a.running():
        a.live("k", "note", {"i": 0})
        await until(lambda: "k" not in a._live)
        assert not a.live("k", "note", {"i": 1})
    assert hook.kinds() == ["create"] and a.failed == 1


async def test_a_hung_edit_never_delays_alarms_or_events():
    hook = LiveRecorder()
    hook.hold = asyncio.Event()  # Discord accepts the edit and never answers
    a = Alerts(hook, FAST, live=hook)
    t0 = time.monotonic()
    async with a.running(flush_timeout=1.0):
        a.live("k", "note", {"i": 0})
        await until(lambda: hook.calls)
        a.live("k", "note", {"i": 1})
        await until(lambda: len(hook.calls) == 2)   # the edit is now stuck
        a.event("note", {"other": 1})
        a.sync_alarms({"x": Alarm("x", "critical", "risk task stalled")})
        await until(lambda: len(hook.posts) == 2)
    assert time.monotonic() - t0 < 3.0  # and shutdown doesn't wait on an unfinished live message
    assert hook.posts[1].startswith("**ALARM**")


async def test_shutdown_waits_for_a_final_state_but_not_for_an_unfinished_one():
    hook = LiveRecorder()
    a = Alerts(hook, Limits(live_edit_sec=0.3), live=hook)
    async with a.running():
        a.live("done", "note", {"i": 0})
        a.live("open", "note", {"i": 0})
        await until(lambda: len(hook.calls) == 2)
        a.live("done", "note", {"i": "final"}, final=True)  # has to wait out the spacing first
    assert [t for k, _, t in hook.calls if k == "edit"] == ["**note**: {'i': 'final'}"]
    assert not a._live  # "open" had nothing new to say: cancelled, not waited for


async def test_live_needs_a_running_sender_and_a_live_transport():
    hook = LiveRecorder()
    assert not Alerts(hook, FAST, live=hook).live("k", "note", {})
    async with Alerts(hook, FAST).running() as plain:
        assert not plain.live("k", "note", {})
    async with Alerts(hook, FAST, live=hook).running() as a:
        assert all(a.live(f"k{i}", "note", {}) for i in range(3))
        assert not a.live("k3", "note", {})  # meetings run one at a time; a pile-up is a bug
    assert hook.posts == []


# -- the engine ----------------------------------------------------------------------------
async def test_engine_meeting_runs_in_one_live_message_then_posts_the_summary(cfg, store, ledger):
    eng, hook = await make_engine(cfg, store)
    async with eng.alerts.running():
        rec = await eng.meeting(execute=True)
        await eng.executor.drain()
    assert rec["status"] == "ok"
    assert hook.kinds().count("create") == 1 and set(hook.kinds()) == {"create", "edit"}
    first, final = hook.calls[0][2], hook.shown
    assert first.startswith(f"**Council in session** (`{rec['id']}`")
    assert final.startswith(f"**Council adjourned** after 0s (`{rec['id']}`): ok")
    assert "- claude: done in 0s: BUY TLT 0.80" in final and "- gemini: done in 0s: HOLD on every asset" in final
    assert "- chair (chair): decided in 0s: BUY TLT (conviction 0.70)" in final
    assert "**Orders**\n- BUY TLT (conviction 0.70): filled $" in final
    # The summary still goes out as its own post (an edit doesn't notify anyone), then the fill.
    assert hook.posts[0].startswith(f"**Council {rec['id']}** (ok): BUY TLT")
    assert hook.posts[1].startswith("**BUY TLT**")
    assert eng.progress is None and eng._awaiting_orders == {}


async def test_the_live_message_shows_a_dry_run_verdict(cfg, store, ledger):
    eng, hook = await make_engine(cfg, store)
    eng.kill.set(True, by="test")
    async with eng.alerts.running():
        await eng.meeting(execute=True)
        await eng.executor.drain()
    assert "- BUY TLT (conviction 0.70): dry run, would fill $" in hook.shown


async def test_live_meeting_inside_the_running_engine(cfg, store, ledger):
    eng, hook = await make_engine(cfg, store)
    store.set_state("next_meeting_at", OPEN)  # due now (the rig's clock)
    stop = asyncio.Event()
    run = asyncio.create_task(eng.run(stop))
    await until(lambda: hook.shown and "filled $" in hook.shown)  # the executor task's verdict
    stop.set()
    assert await asyncio.wait_for(run, timeout=20) == 0
    assert hook.kinds().count("create") == 1


async def test_live_view_off_posts_only_the_summary(cfg, store, ledger):
    eng, hook = await make_engine(cfg, store, limits=dataclasses.replace(FAST, live_meeting=False))
    async with eng.alerts.running():
        await eng.meeting(execute=True)
        await eng.executor.drain()
    assert hook.calls == [] and hook.posts[0].startswith("**Council")


async def test_a_broken_live_view_cannot_touch_the_meeting(cfg, store, ledger, caplog):
    eng, hook = await make_engine(cfg, store)
    hook.raise_on_edit = True
    async with eng.alerts.running():
        rec = await eng.meeting(execute=True)
        await eng.executor.drain()
    assert rec["status"] == "ok" and "TLT" in eng.ledger.positions()
    assert hook.posts[0].startswith("**Council") and "live message meeting:" in caplog.text

    eng2, hook2 = await make_engine(cfg, store)

    def boom(*a, **kw):
        raise RuntimeError("display bug")

    eng2.alerts.live = boom
    rec2 = await eng2.meeting(execute=False)
    assert rec2["status"] == "ok" and "live meeting view failed" in caplog.text


async def test_an_aborted_meeting_says_so(cfg, store, ledger):
    eng, hook = await make_engine(cfg, store)

    async def outage(*a, **kw):
        raise MarketDataError("scripted outage")

    eng.market.prices = outage
    async with eng.alerts.running():
        with pytest.raises(MarketDataError):
            await eng.meeting()
    assert hook.shown.startswith("**Council meeting aborted** after 0s") and "scripted outage" in hook.shown
    assert eng.progress is None


async def test_a_meeting_cut_short_by_a_stop_is_marked_aborted(cfg, store, ledger):
    council, _ = held_council(cfg)
    eng, hook = await make_engine(cfg, store, council)
    async with eng.alerts.running():
        task = asyncio.create_task(eng.meeting())
        await until(lambda: eng.progress is not None and eng.progress.analysts["gpt"]["status"] == "done")
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert hook.shown.startswith("**Council meeting aborted**") and "cancelled, the engine is stopping" in hook.shown
    assert "- gemini: stopped" in hook.shown and "- claude: done in" in hook.shown
    assert eng.progress is None and eng.meeting_started is None


async def test_orders_left_undecided_at_shutdown_are_said_so(cfg, store, ledger):
    eng, hook = await make_engine(cfg, store)
    async with eng.alerts.running():
        await eng.meeting(execute=True)  # submitted, never processed
        assert list(eng._awaiting_orders) and "at the risk gate" in eng.alerts._live[
            next(iter(eng.alerts._live))][0].text
        eng._close_awaiting("the engine stopped before the risk gate decided")
    assert "undecided (the engine stopped before the risk gate decided)" in hook.shown
    assert eng._awaiting_orders == {}


# -- API and chat --------------------------------------------------------------------------
async def test_api_council_shows_who_is_done_while_a_meeting_runs(cfg, store, ledger):
    council, gate = held_council(cfg)
    eng, _ = await make_engine(cfg, store, council)
    server = ApiServer(eng, "127.0.0.1", 0)
    await server.start()
    try:
        async with aiohttp.ClientSession(f"http://127.0.0.1:{server.port}") as s:
            task = asyncio.create_task(eng.meeting(execute=False))
            await until(lambda: eng.progress is not None and eng.progress.analysts["gpt"]["status"] == "done")
            async with s.get("/api/council") as r:
                d = await r.json()
            assert d["running"] is True and d["progress"]["id"] == eng.progress.id
            p = d["progress"]
            assert p["phase"] == "analysts" and p["chair"]["status"] == "waiting"
            by = {a["name"]: a for a in p["analysts"]}
            assert by["claude"]["status"] == "done"
            assert by["claude"]["stances"] == [{"asset": "TLT", "stance": "BUY", "conviction": 0.8}]
            assert by["gemini"]["status"] == "thinking" and by["gemini"]["latency_sec"] is None
            # What /status in the chat bot makes of that JSON:
            assert render.council_line(d).endswith(": claude and gpt done, gemini thinking")
            gate.set()
            rec = await task
            async with s.get("/api/council") as r:
                d = await r.json()
            assert d["running"] is False and d["progress"] is None
            assert d["last_meeting"]["id"] == rec["id"]
    finally:
        await server.stop()


def test_status_line_says_how_far_the_meeting_has_got():
    base = {"running": True, "started_at": 5.0}

    def at(phase, states, chair="waiting"):
        return dict(base, progress={"phase": phase, "chair": {"status": chair},
                                    "analysts": [{"name": n, "status": s} for n, s in states]})

    head = "Council: **in session** (started <t:5:R>)"
    assert render.council_line(at("market", [])) == f"{head}: reading the market"
    assert render.council_line(at("analysts", [("claude", "done"), ("gpt", "thinking"), ("gemini", "thinking")])) \
        == f"{head}: claude done, gpt and gemini thinking"
    after_drop = at("chair", [("claude", "done"), ("gpt", "done"), ("gemini", "dropped")], chair="thinking")
    assert render.council_line(after_drop) == f"{head}: claude and gpt done, gemini dropped out, chair deliberating"
    assert render.council_line(base) == head  # an engine older than the bot sends no progress


# -- config and transport ------------------------------------------------------------------
def test_live_view_config(tmp_path):
    cfg = cfgmod.load(data_dir=tmp_path)
    assert cfg.alerts.live_meeting is True and cfg.alerts.live_edit_sec == 3.0
    for text, ok in (("live_meeting = false", True), ("live_edit_sec = 0.2", False), ("live_edit_sec = 90", False)):
        p = tmp_path / "c.toml"
        line = "live_meeting = true" if text.startswith("live_meeting") else "live_edit_sec = 3"
        p.write_text(cfgmod.DEFAULT_CONFIG.read_text(encoding="utf-8").replace(line, text), encoding="utf-8")
        if ok:
            assert cfgmod.load(p, data_dir=tmp_path).alerts.live_meeting is False
        else:
            with pytest.raises(ConfigError, match="live_edit_sec"):
                cfgmod.load(p, data_dir=tmp_path)


async def test_webhook_create_and_edit_hit_discords_message_endpoints():
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path, dict(req.url.params), json.loads(req.content)))
        if req.method == "PATCH" and len(seen) == 2:
            return httpx.Response(429, json={"retry_after": 0.01})
        return httpx.Response(200, json={"id": "1234567890", "content": "x"})

    hook = DiscordWebhook(SECRET_URL + "?thread_id=42", transport=httpx.MockTransport(handler))
    mid = await hook.create("hello")
    assert mid == "1234567890"
    assert await hook.edit(mid, "x" * 5000)  # after one 429 and its retry_after
    assert seen[0][:3] == ("POST", "/api/webhooks/123/SECRET-TOKEN", {"thread_id": "42", "wait": "true"})
    assert [s[:3] for s in seen[1:]] == [("PATCH", "/api/webhooks/123/SECRET-TOKEN/messages/1234567890",
                                          {"thread_id": "42"})] * 2
    assert len(seen[2][3]["content"]) == LIMIT and seen[2][3]["allowed_mentions"] == {"parse": []}
    assert not await hook.edit("../../evil", "x") and len(seen) == 3  # only a snowflake goes in the path


async def test_webhook_edit_failures_are_quiet_and_never_leak_the_token(caplog):
    caplog.set_level(logging.WARNING)

    def handler(req):
        return httpx.Response(404 if req.method == "PATCH" else 204)

    hook = DiscordWebhook(SECRET_URL, transport=httpx.MockTransport(handler))
    assert await hook.create("hi") is None  # 204: no message came back to read an id from
    assert not await hook.edit("1", "hi")

    def boom(req):
        raise httpx.ConnectError(f"cannot connect to {req.url}")

    assert not await DiscordWebhook(SECRET_URL, transport=httpx.MockTransport(boom)).edit("1", "hi")
    assert "no message id" in caplog.text and "edit returned HTTP 404" in caplog.text
    assert "edit unreachable (ConnectError)" in caplog.text and "SECRET-TOKEN" not in caplog.text
