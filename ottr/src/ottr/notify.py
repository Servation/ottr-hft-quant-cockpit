"""
Notifications: Discord webhook posts for events (meetings, fills, risk) and alarms,
rate-limited and non-blocking.

What it does: `Alerts` formats each event, passes it through a token bucket and queues
it; one background sender posts to DISCORD_WEBHOOK_URL. Alarms are level-triggered: the
watchdog hands over the full set of active alarms every tick and `sync_alarms` reconciles
what the user has been told with the truth, so a lost post is retried and a flapping
alarm posts at most once per `alarm_repeat_sec` (with a flap count) instead of every tick.

Why a webhook and not a bot: v2 only reports; it has no chat commands, so it needs no
gateway connection, token or intents. Failures are logged and never raised, and posting
never blocks the caller: a Discord outage must not slow down a risk tick.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable, Dict, Mapping, Optional

import httpx

from ottr.config import Alerts as AlertLimits
from ottr.health import fmt_age

logger = logging.getLogger(__name__)
LIMIT = 1900          # Discord allows 2000 chars per message
QUEUE_MAX = 200
MAX_RETRY_AFTER = 30.0

Send = Callable[[str], Awaitable[bool]]


def _usd(x) -> str:
    return f"${float(x):,.2f}"


def format_event(kind: str, d: dict) -> str:
    if kind == "fill":
        pnl = f", realized ${float(d['realized_pnl']):+,.2f}" if d["side"] == "SELL" else ""
        return (f"**{d['side']} {d['asset']}** {_usd(d['notional'])} @ {float(d['price']):,.6g} "
                f"[{d['origin']}]{pnl}\n> {d.get('reason', '')}")
    if kind == "meeting":
        trades = ", ".join(f"{t['action']} {t['asset']}" for t in d["trades"]) or "no trades"
        dropped = "".join(f"\n- dropped {x['trade']['action']} {x['trade']['asset']}: {x['why']}"
                          for x in d.get("dropped", []))
        return f"**Council {d['id']}** ({d['status']}): {trades}\n{d.get('summary', '')}{dropped}"
    if kind == "risk_halt":
        return f"**Drawdown breaker tripped**: {d['drawdown'] * 100:.1f}% off peak. BUYs blocked."
    if kind == "risk_resume":
        return f"**Breaker resumed** ({d['why']})."
    if kind == "alarm":
        return f"**ALARM** ({d['severity']}): {d['message']}"
    if kind == "alarm_remind":
        return f"**Still alarming** (active {d['for']}): {d['message']}"
    if kind == "alarm_clear":
        return f"**Resolved**: {d['message']}"
    if kind == "engine_start":
        mode = ("in dry run: the kill switch is engaged, so nothing will fill" if d.get("dry_run")
                else "and paper trading")
        unclean = ("\nThe previous run did not shut down cleanly (crash, kill or power loss)."
                   if d.get("unclean_previous_exit") else "")
        prev = (f"\nAlarms open before the restart are re-checked from scratch and re-raised if they "
                f"persist: {', '.join(d['previous_alarms'])}." if d.get("previous_alarms") else "")
        return f"**Engine started** {mode}.{unclean}{prev}"
    if kind == "engine_stop":
        how = "cleanly" if d.get("code", 0) == 0 else f"with exit code {d['code']}: {d.get('why', '')}"
        return f"**Engine stopped** {how}."
    if kind == "engine_resumed":
        return (f"**Engine was suspended for {d['gap']}** (system sleep or a blocked loop). "
                "Heartbeat checks restart their grace period now.")
    if kind == "engine_crash":
        tail = f"\n> {d['tail']}" if d.get("tail") else ""
        if d.get("error"):
            return f"**Engine failed to start** ({d['error']}); the supervisor retries in {d['delay']}."
        return (f"**Engine exited** with code {d['rc']} after {d['runtime']}; the supervisor "
                f"restarts it in {d['delay']}.{tail}")
    if kind == "engine_stable":
        return f"**Engine stable again** (up {d['uptime']} after {d['crashes']} crash(es))."
    if kind == "kill_switch":
        why = f" ({d['reason']})" if d.get("reason") else ""
        if d["engaged"]:
            return f"**Kill switch engaged** by {d['by']}{why}: fills stop, verdicts are recorded as dry run."
        if d["effective"]:
            return (f"**Kill switch released** by {d['by']}{why}, but config or OTTR_DRY_RUN still "
                    "forces dry run: nothing fills.")
        return f"**Kill switch released** by {d['by']}{why}: paper fills resume."
    return f"**{kind}**: {d}"


class DiscordWebhook:
    """POST to a Discord webhook. Never raises; returns whether the post landed.

    Errors are logged by status code only: an httpx error message embeds the URL, and the
    webhook URL contains its secret token.
    """

    def __init__(self, url: str, transport: Optional[httpx.AsyncBaseTransport] = None,
                 timeout: float = 10.0) -> None:
        self.url, self.transport, self.timeout = url, transport, timeout

    async def __call__(self, content: str) -> bool:
        payload = {"content": content[:LIMIT], "allowed_mentions": {"parse": []}}
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as c:
                for attempt in range(2):
                    r = await c.post(self.url, json=payload)
                    if r.status_code == 429 and attempt == 0:
                        wait = _retry_after(r)
                        if wait is not None and wait <= MAX_RETRY_AFTER:
                            await asyncio.sleep(wait)
                            continue
                    if r.is_success:
                        return True
                    logger.warning("discord webhook returned HTTP %s", r.status_code)
                    return False
        except httpx.HTTPError as e:
            logger.warning("discord webhook unreachable (%s)", type(e).__name__)
        return False


def _retry_after(r: httpx.Response) -> Optional[float]:
    try:
        return float(r.json().get("retry_after"))
    except Exception:
        try:
            return float(r.headers.get("retry-after", ""))
        except ValueError:
            return None


class TokenBucket:
    """`capacity` posts back to back, refilling at `per_hour`."""

    def __init__(self, capacity: int, per_hour: float, now: float) -> None:
        self.capacity, self.rate = float(capacity), per_hour / 3600.0
        self.tokens, self.ts = float(capacity), now

    def _refill(self, now: float) -> None:
        self.tokens = min(self.capacity, self.tokens + max(0.0, now - self.ts) * self.rate)
        self.ts = now

    def available(self, now: float) -> bool:
        self._refill(now)
        return self.tokens >= 1.0

    def take(self, now: float) -> bool:
        if not self.available(now):
            return False
        self.tokens -= 1.0
        return True


@dataclass
class _Told:
    """What the user was last told about one alarm key."""
    active: bool = False
    last_raise: float = float("-inf")
    last_post: float = float("-inf")
    since: float = 0.0
    seen_active: bool = False   # state at the previous sync, to count edges
    flaps: int = 0              # raise edges since the last raise post
    message: str = ""


class Alerts:
    """Rate-limited, non-blocking front end for every notification.

    Callable as the executor/engine `Notifier` (kind, data). Events and alarms draw on
    separate buckets so a burst of fills can never starve an alarm.
    """

    def __init__(self, send: Optional[Send], limits: AlertLimits = AlertLimits(),
                 clock: Callable[[], float] = time.time) -> None:
        self.send, self.limits, self.clock = send, limits, clock
        now = clock()
        self.events_bucket = TokenBucket(limits.burst, limits.per_hour, now)
        self.alarm_bucket = TokenBucket(limits.burst, limits.per_hour, now)
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAX)
        self.told: Dict[str, _Told] = {}
        self.suppressed = 0                     # events dropped by the bucket, not yet reported
        self._keyed: Dict[str, tuple] = {}      # key -> (last post ts, suppressed count)
        self.posted = 0
        self.failed = 0

    @property
    def enabled(self) -> bool:
        return self.send is not None

    # -- events (edge-triggered) -----------------------------------------------------------
    async def __call__(self, kind: str, data: dict) -> None:
        self.event(kind, data)

    def event(self, kind: str, data: dict, key: Optional[str] = None) -> bool:
        """Queue one event. With `key`, at most one post per alarm_repeat_sec for that key;
        the ones in between are counted into the next post."""
        if not self.enabled:
            return False
        now = self.clock()
        text = format_event(kind, data)
        if key is not None:
            last, held = self._keyed.get(key, (float("-inf"), 0))
            if now - last < self.limits.alarm_repeat_sec:
                self._keyed[key] = (last, held + 1)
                return False
            if held:
                text += f"\n({held} more like this since the last report)"
        if not self.events_bucket.take(now):
            self.suppressed += 1
            return False
        if key is not None:
            self._keyed[key] = (now, 0)
        if self.suppressed:
            text += f"\n({self.suppressed} earlier notification(s) dropped by the rate limit)"
            self.suppressed = 0
        return self._enqueue(text)

    # -- alarms (level-triggered) ----------------------------------------------------------
    def sync_alarms(self, active: Mapping[str, "object"]) -> None:
        """Reconcile what the user was told with the current alarm set (call every tick).

        `active` maps key -> Alarm (anything with .severity and .message).
        """
        if not self.enabled:
            return
        now, lim = self.clock(), self.limits
        for key, alarm in active.items():
            st = self.told.setdefault(key, _Told())
            if not st.seen_active:
                st.flaps += 1  # a raise edge; counted even when its post is deferred
            st.seen_active = True
            if not st.active:
                if now - st.last_raise < lim.alarm_repeat_sec or not self.alarm_bucket.available(now):
                    continue  # deferred: retried on a later tick while it stays active
                text = format_event("alarm", {"severity": alarm.severity, "message": alarm.message})
                if st.flaps > 1:
                    text += f"\n(raised {st.flaps} times since the last report)"
                if self._post_alarm(text, now):
                    st.active, st.last_raise, st.last_post, st.since = True, now, now, now
                    st.flaps = 0
            elif now - st.last_post >= lim.alarm_remind_sec and self.alarm_bucket.available(now):
                if self._post_alarm(format_event("alarm_remind", {"for": fmt_age(now - st.since),
                                                                  "message": alarm.message}), now):
                    st.last_post = now
            st.message = alarm.message
        for key, st in self.told.items():
            if key in active:
                continue
            st.seen_active = False
            if st.active and self.alarm_bucket.available(now):
                if self._post_alarm(format_event("alarm_clear", {"message": st.message}), now):
                    st.active, st.last_post = False, now
        if self.suppressed and self.events_bucket.available(now):
            n, self.suppressed = self.suppressed, 0
            self.events_bucket.take(now)
            self._enqueue(f"**Rate limit**: {n} notification(s) were dropped. "
                          "`ottr status` and the audit log have the full record.")

    def _post_alarm(self, text: str, now: float) -> bool:
        return self.alarm_bucket.take(now) and self._enqueue(text)

    # -- delivery --------------------------------------------------------------------------
    def _enqueue(self, text: str) -> bool:
        try:
            self.queue.put_nowait(text)
            return True
        except asyncio.QueueFull:
            logger.warning("notification queue full; dropping: %s", text[:120])
            return False

    async def run(self) -> None:
        """Sender loop: one post at a time, in order."""
        while True:
            text = await self.queue.get()
            try:
                ok = await self.send(text) if self.send else False
                if ok:
                    self.posted += 1
                else:
                    self.failed += 1
            except Exception:  # the transport should never raise; keep the loop alive anyway
                self.failed += 1
                logger.warning("notification send crashed", exc_info=True)
            finally:
                self.queue.task_done()

    async def flush(self, timeout: float = 15.0) -> None:
        try:
            await asyncio.wait_for(self.queue.join(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("%d notification(s) not delivered before shutdown", self.queue.qsize())

    @asynccontextmanager
    async def running(self, flush_timeout: float = 15.0) -> AsyncIterator["Alerts"]:
        """Run the sender for the duration of the block, then deliver what is queued."""
        task = asyncio.create_task(self.run(), name="alerts")
        try:
            yield self
        finally:
            if self.enabled:
                await self.flush(flush_timeout)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def make_alerts(url: Optional[str], limits: AlertLimits = AlertLimits()) -> Alerts:
    """Alerts posting to `url`, or a disabled (no-op) Alerts when no webhook is set."""
    return Alerts(DiscordWebhook(url) if url else None, limits)
