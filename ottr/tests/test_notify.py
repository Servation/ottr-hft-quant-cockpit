"""
Notification tests: alarm reconciliation (raise, resolve, remind, flap damping), the
event and alarm token buckets, keyed coalescing, ordered delivery, and the Discord
transport's 429 handling without leaking the webhook token into logs.
"""

import logging

import httpx

from ottr.config import Alerts as Limits
from ottr.health import Alarm
from ottr.notify import Alerts, DiscordWebhook, TokenBucket, format_event


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def drained(alerts):
    out = []
    while not alerts.queue.empty():
        out.append(alerts.queue.get_nowait())
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
    fill = {"side": "BUY", "asset": "ETH", "notional": "100", "price": "2000", "origin": "council",
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


async def test_sender_delivers_in_order_and_counts_failures():
    got = []

    async def send(text):
        got.append(text)
        return text != "bad"

    a = Alerts(send, Limits())
    async with a.running():
        for t in ("one", "bad", "three"):
            a._enqueue(t)
    assert got == ["one", "bad", "three"] and a.posted == 2 and a.failed == 1


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
        ("kill_switch", {"engaged": True, "by": "api", "reason": "test", "effective": True}),
        ("risk_halt", {"drawdown": 0.16}),
        ("risk_resume", {"why": "recovered"}),
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
    assert not await DiscordWebhook(SECRET_URL, transport=httpx.MockTransport(
        lambda r: httpx.Response(404)))("hi")

    def boom(req):
        raise httpx.ConnectError(f"cannot connect to {req.url}")

    assert not await DiscordWebhook(SECRET_URL, transport=httpx.MockTransport(boom))("hi")
    assert "HTTP 404" in caplog.text and "ConnectError" in caplog.text
    assert "SECRET-TOKEN" not in caplog.text


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
