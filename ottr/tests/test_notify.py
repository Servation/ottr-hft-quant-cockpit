"""
Notification tests: alarm reconciliation (raise, resolve, remind, flap damping), the
event and alarm token buckets, keyed coalescing, ordered delivery, holding posts through
a Discord outage (retry in order, the outage header, the "sent late" note, a bounded
shutdown), and the Discord transport's answers (delivered, refused, unreachable) without
leaking the webhook token into logs.
"""

import asyncio
import json
import logging
import time

import httpx

from ottr.config import Alerts as Limits
from ottr.engine.progress import MeetingProgress
from ottr.health import Alarm
from ottr.notify import LIMIT, Alerts, DiscordWebhook, TokenBucket, format_event

FAST_RETRY = (0.01, 0.02)


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def drained(alerts):
    out = []
    while not alerts.queue.empty():
        out.append(alerts.queue.get_nowait().text)
        alerts.queue.task_done()
    return out


def make(limits=Limits(), clock=None):
    async def send(text):
        return True
    clock = clock or Clock()
    return Alerts(send, limits, clock=clock), clock


A = Alarm("task:market", "critical", "market task failing for 4m: Kraken down")


def test_disabled_alerts_are_noops():
    a = Alerts(None)
    a.event("fill", {})
    a.sync_alarms({"x": A})
    assert a.queue.empty() and not a.enabled


def test_raise_once_then_resolve():
    a, clock = make()
    a.sync_alarms({A.key: A})
    a.sync_alarms({A.key: A})       # still active: no repeat
    clock.t += 60
    a.sync_alarms({})
    msgs = drained(a)
    assert len(msgs) == 2
    assert msgs[0].startswith("**ALARM** (critical): market task failing")
    assert msgs[1].startswith("**Resolved**: market task failing")


def test_reminder_while_alarm_stays_active():
    a, clock = make(Limits(alarm_remind_sec=3600))
    a.sync_alarms({A.key: A})
    for _ in range(59):
        clock.t += 60
        a.sync_alarms({A.key: A})
    assert len(drained(a)) == 1
    clock.t += 60
    a.sync_alarms({A.key: A})
    assert drained(a)[0].startswith("**Still alarming** (active 60m)")


def test_flapping_feed_is_bounded_and_ends_truthful():
    # Market feed flips bad/good every minute for 4 hours.
    lim = Limits(burst=10, per_hour=30, alarm_repeat_sec=1800)
    a, clock = make(lim)
    posts = []
    for minute in range(240):
        a.sync_alarms({A.key: A} if minute % 2 == 0 else {})
        posts += drained(a)
        clock.t += 60
    raises = [p for p in posts if p.startswith("**ALARM**")]
    # At most one raise per 30 min (8 in 4h, +1 for the boundary), each followed by a resolve.
    assert len(raises) <= 9 and len(posts) <= 18
    assert any("raised 15 times since the last report" in p for p in raises)
    # It ends clear, and the last word the user got says so.
    a.sync_alarms({})
    posts += drained(a)
    assert posts[-1].startswith("**Resolved**")


def test_deferred_raise_posts_when_window_passes_if_still_active():
    a, clock = make(Limits(alarm_repeat_sec=1800))
    a.sync_alarms({A.key: A})
    clock.t += 300
    a.sync_alarms({})
    clock.t += 60
    a.sync_alarms({A.key: A})       # back within the window: deferred, not dropped
    assert [m[:9] for m in drained(a)] == ["**ALARM**", "**Resolve"]
    clock.t += 1500
    a.sync_alarms({A.key: A})
    msgs = drained(a)
    assert len(msgs) == 1 and msgs[0].startswith("**ALARM**")


def test_event_flood_is_capped_and_summarized_later():
    a, clock = make(Limits(burst=5, per_hour=60))
    fill = {"side": "BUY", "asset": "TLT", "notional": "100", "price": "2000", "origin": "council",
            "realized_pnl": "0", "reason": "r"}
    for _ in range(50):
        a.event("fill", fill)
    assert len(drained(a)) == 5 and a.suppressed == 45
    clock.t += 120  # 2 tokens back
    a.sync_alarms({})
    msgs = drained(a)
    assert len(msgs) == 1 and "45 notification(s) were dropped" in msgs[0]


def test_alarms_are_not_starved_by_events():
    a, _ = make(Limits(burst=3, per_hour=1))
    for _ in range(10):
        a.event("meeting", {"id": "m", "status": "ok", "trades": [], "summary": "s"})
    drained(a)
    a.sync_alarms({A.key: A})
    assert drained(a)[0].startswith("**ALARM**")


def test_keyed_events_coalesce():
    a, clock = make(Limits(alarm_repeat_sec=1800))
    crash = {"rc": 1, "runtime": "3s", "delay": "5s", "tail": "Traceback..."}
    a.event("engine_crash", crash, key="engine_crash")
    for _ in range(4):
        clock.t += 60
        a.event("engine_crash", crash, key="engine_crash")
    assert len(drained(a)) == 1
    clock.t += 1800
    a.event("engine_crash", crash, key="engine_crash")
    assert "(4 more like this since the last report)" in drained(a)[0]


def test_token_bucket_refills():
    b = TokenBucket(2, 3600, now=0)
    assert b.take(0) and b.take(0) and not b.take(0)
    assert b.take(1.0) and not b.take(1.0)


async def test_sender_delivers_in_order_and_drops_a_refused_post():
    got = []

    async def send(text):
        got.append(text)
        return text != "bad"  # False: Discord refused it for good, so it isn't retried

    a = Alerts(send, Limits(), retry_sec=FAST_RETRY)
    async with a.running():
        for t in ("one", "bad", "three"):
            a._enqueue(t)
    assert got == ["one", "bad", "three"] and a.posted == 2 and a.failed == 1


async def test_an_unreachable_post_is_retried_and_nothing_overtakes_it():
    got, misses = [], {"two": 2}

    async def send(text):
        got.append(text)
        if misses.get(text):
            misses[text] -= 1
            return None  # Discord not reached
        return True

    a = Alerts(send, Limits(), retry_sec=FAST_RETRY)
    async with a.running():
        for t in ("one", "two", "three"):
            a._enqueue(t)
    # A short blip: no header, no "sent late" note, nothing lost or reordered.
    assert got == ["one", "two", "two", "two", "three"] and a.posted == 3 and a.failed == 0


async def test_an_alarm_raised_while_discord_is_down_still_reaches_it():
    # 2026-11-06: the raises posted during a DNS outage were dropped; only "Resolved" landed.
    got, down = [], [True]

    async def send(text):
        if down[0]:
            return None
        got.append(text)
        return True

    a = Alerts(send, Limits(), retry_sec=FAST_RETRY)
    async with a.running():
        a.sync_alarms({A.key: A})
        await asyncio.sleep(0.05)
        down[0] = False
        a.sync_alarms({})
    assert [m[:10] for m in got] == ["**ALARM** ", "**Resolved"]


async def test_a_long_outage_is_announced_and_held_posts_say_when_they_happened():
    clock, got, down = Clock(), [], [True]
    t0 = int(clock.t)

    async def send(text):
        if down[0]:
            return None
        got.append(text)
        return True

    a = Alerts(send, Limits(), clock=clock, retry_sec=FAST_RETRY)
    async with a.running():
        a._enqueue("**ALARM** (critical): market task failing")
        a._enqueue("x" * 5000)
        await asyncio.sleep(0.05)  # the first post fails and waits at the head of the queue
        clock.t += 3 * 3600
        a._enqueue("**Resolved**: market task failing")
        down[0] = False
    assert got[0] == (f"**Discord was unreachable** for 3.0h (from <t:{t0}:f>). "
                      "3 held-back notification(s) follow.")
    assert got[1] == f"**ALARM** (critical): market task failing\n(sent late: this happened <t:{t0}:f>)"
    # A long post is clipped before the note, so Discord's limit can't cut the note off.
    assert len(got[2]) == LIMIT and got[2].endswith(f"(sent late: this happened <t:{t0}:f>)")
    assert got[3] == "**Resolved**: market task failing"  # it happened just now: no note
    assert a.posted == 4 and a.failed == 0


async def test_shutdown_while_discord_is_down_is_bounded(caplog):
    caplog.set_level(logging.WARNING)

    async def send(text):
        return None

    a = Alerts(send, Limits(), retry_sec=(5.0, 60.0))
    t = time.monotonic()
    async with a.running(flush_timeout=0.2):
        a._enqueue("one")
        a._enqueue("two")
        await asyncio.sleep(0.05)  # "one" is waiting out its backoff
    assert time.monotonic() - t < 2.0
    assert "2 notification(s) not delivered before shutdown" in caplog.text


def test_every_event_kind_formats():
    for kind, d in [
        ("alarm", {"severity": "critical", "message": "m"}),
        ("alarm_clear", {"message": "m"}),
        ("alarm_remind", {"for": "6h", "message": "m"}),
        ("engine_start", {"dry_run": True, "unclean_previous_exit": True}),
        ("engine_stop", {"code": 1, "why": "task risk ended"}),
        ("engine_resumed", {"gap": "2.0h"}),
        ("engine_crash", {"rc": 1, "runtime": "3s", "delay": "5s", "tail": "x"}),
        ("engine_stable", {"uptime": "10m", "crashes": 2}),
        ("chat_crash", {"rc": 1, "runtime": "3s", "delay": "5s", "tail": "x"}),
        ("chat_crash", {"rc": None, "error": "FileNotFoundError: python.exe", "delay": "5s"}),
        ("chat_stable", {"uptime": "10m", "crashes": 2}),
        ("chat_gave_up", {"rc": 78, "tail": "Discord rejected DISCORD_BOT_TOKEN"}),
        ("screen", {"rows": [], "settings": {"top_n": 5}}),
        ("kill_switch", {"engaged": True, "by": "api", "reason": "test", "effective": True}),
        ("risk_halt", {"drawdown": 0.16}),
        ("risk_resume", {"why": "recovered"}),
        ("meeting", {"id": "m-1", "status": "ok", "trades": [], "summary": "Hold \u2014 nothing clean."}),
        ("meeting_live", MeetingProgress("m-1", ["claude"], "claude").to_dict()),
    ]:
        text = format_event(kind, d)
        assert text.startswith("**") and "\u2014" not in text  # no em dashes, per house style
    assert "did not shut down cleanly" in format_event("engine_start", {"unclean_previous_exit": True})
    assert format_event("chat_crash", {"rc": 1, "runtime": "3s", "delay": "5s"}).startswith("**Chat bot exited**")
    assert "logs/chat.log" in format_event("chat_gave_up", {"rc": 78})


SECRET_URL = "https://discord.com/api/webhooks/123/SECRET-TOKEN"


async def test_webhook_retries_once_on_429():
    calls = []

    def handler(req):
        calls.append(req)
        if len(calls) == 1:
            return httpx.Response(429, json={"retry_after": 0.01})
        return httpx.Response(204)

    ok = await DiscordWebhook(SECRET_URL, transport=httpx.MockTransport(handler))("hi")
    assert ok and len(calls) == 2
    body = calls[1].read().decode()
    assert '"parse": []' in body or '"parse":[]' in body  # no @everyone pings, ever


async def test_webhook_failure_never_logs_the_token(caplog):
    caplog.set_level(logging.WARNING)
    assert await DiscordWebhook(SECRET_URL, transport=httpx.MockTransport(
        lambda r: httpx.Response(404)))("hi") is False

    def boom(req):
        raise httpx.ConnectError(f"cannot connect to {req.url}")

    assert await DiscordWebhook(SECRET_URL, transport=httpx.MockTransport(boom))("hi") is None
    assert "HTTP 404" in caplog.text and "ConnectError" in caplog.text
    assert "SECRET-TOKEN" not in caplog.text


async def test_webhook_says_whether_a_failed_post_is_worth_retrying():
    def raises(exc):
        def handler(req):
            raise exc("boom")
        return handler

    def status(code, **kw):
        return lambda req: httpx.Response(code, **kw)

    cases = [
        (status(204), True),
        (status(400), False),  # a malformed post: refused for good
        (status(404), False),  # the webhook was deleted
        (status(401), False),
        (status(500), None),   # Discord's side: try again
        (status(503), None),
        (status(429, json={"retry_after": 0.01}), None),  # still limited after the one wait
        (status(429, json={"retry_after": 600}), None),   # too long to wait inline
        (raises(httpx.ConnectError), None),               # DNS down, the 2026-11-06 outage
        (raises(httpx.ReadTimeout), None),
        (raises(httpx.RemoteProtocolError), None),
        (raises(httpx.UnsupportedProtocol), False),       # a bad URL: no retry can fix it
    ]
    for handler, want in cases:
        got = await DiscordWebhook(SECRET_URL, transport=httpx.MockTransport(handler))("hi")
        assert got is want, (handler, got)


async def test_held_posts_reach_a_real_webhook_after_an_outage():
    # The real transport end to end: a webhook that fails to connect, then answers.
    seen, up = [], [False]

    def handler(req):
        if not up[0]:
            raise httpx.ConnectError("getaddrinfo failed")
        seen.append(json.loads(req.read())["content"])
        return httpx.Response(204)

    hook = DiscordWebhook(SECRET_URL, transport=httpx.MockTransport(handler))
    a = Alerts(hook, Limits(), retry_sec=FAST_RETRY)
    async with a.running():
        a.sync_alarms({A.key: A})
        await asyncio.sleep(0.05)
        up[0] = True
    assert seen == [f"**ALARM** (critical): {A.message}"] and a.posted == 1


async def test_webhook_truncates_to_discord_limit():
    seen = []

    def handler(req):
        seen.append(len(__import__("json").loads(req.read())["content"]))
        return httpx.Response(204)

    await DiscordWebhook(SECRET_URL, transport=httpx.MockTransport(handler))("x" * 5000)
    assert seen == [1900]


def test_kill_switch_wording_says_what_actually_happens():
    base = {"by": "api", "reason": ""}
    assert "fills stop" in format_event("kill_switch", dict(base, engaged=True, effective=True))
    assert "paper fills resume" in format_event("kill_switch", dict(base, engaged=False, effective=False))
    still = format_event("kill_switch", dict(base, engaged=False, effective=True))
    assert "still forces dry run: nothing fills" in still
    assert format_event("engine_start", {"dry_run": False}) == "**Engine started** and paper trading."
