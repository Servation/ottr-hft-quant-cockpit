"""
Notifications: Discord webhook posts for events (meetings, fills, risk) and alarms,
rate-limited and non-blocking.

What it does: `Alerts` formats each event, passes it through a token bucket and queues
it; one background sender posts to DISCORD_WEBHOOK_URL. Alarms are level-triggered: the
watchdog hands over the full set of active alarms every tick and `sync_alarms` reconciles
what the user has been told with the truth, so a lost post is retried and a flapping
alarm posts at most once per `alarm_repeat_sec` (with a flap count) instead of every tick.

Live messages (`Alerts.live`): one message that is posted once and then edited in place as
its subject changes, which is how a council meeting is shown while it runs. The post costs
one event token; the edits run on their own sender, coalesced to the newest text and
spaced `live_edit_sec` apart, so they never queue behind (or ahead of) an alarm.

Why a webhook even though `ottr chat` runs a bot: alerts must reach Discord when the bot
is down, misconfigured or crash-looping, so the engine and supervisor post through a
webhook that needs no gateway connection, token or intents. Failures are logged and never
raised, and posting never blocks the caller: a Discord outage must not slow down a risk tick.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator, Awaitable, Callable, Deque, Dict, Mapping, Optional, Protocol, Tuple

import httpx

from ottr.config import Alerts as AlertLimits
from ottr.health import fmt_age

logger = logging.getLogger(__name__)
LIMIT = 1900          # Discord allows 2000 chars per message
QUEUE_MAX = 200
MAX_RETRY_AFTER = 30.0
# Supervised processes: "<name>_crash", "<name>_stable" and "<name>_gave_up" events.
PROCESS_LABELS = {"engine": "Engine", "chat": "Chat bot"}
LIVE_MAX = 3            # live messages tracked at once (meetings run one at a time)
LIVE_MAX_EDITS = 60     # per message, so a bug upstream can't become an edit storm
LIVE_IDLE_SEC = 3600.0  # a live message nobody updates for this long is let go
LIVE_GIVE_UP = 3        # consecutive failed edits before a live message is let go
EM_DASH = "\u2014"

Send = Callable[[str], Awaitable[bool]]


class LiveTransport(Protocol):
    async def create(self, content: str) -> Optional[str]: ...

    async def edit(self, message_id: str, content: str) -> bool: ...


def _usd(x) -> str:
    return f"${float(x):,.2f}"


def no_em_dash(text: str) -> str:
    # The owner's house style; models drift into em dashes no matter what the prompt says.
    return text.replace(f" {EM_DASH} ", ", ").replace(EM_DASH, ", ")


def clip(text: Optional[str], n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n - 3].rstrip() + "..."


def _dur(sec: float) -> str:
    sec = max(0, int(round(sec)))
    return f"{sec // 60}m {sec % 60:02d}s" if sec >= 60 else f"{sec}s"


def _trade(t: dict) -> str:
    if t.get("action") == "BUY":
        return f"BUY {t.get('asset')} (conviction {float(t.get('conviction') or 0):.2f})"
    return f"SELL {t.get('asset')} ({float(t.get('fraction_of_position') or 0) * 100:.0f}% of the position)"


def _live_analyst(a: dict) -> str:
    name, st = a.get("name"), a.get("status")
    if st == "done":
        leans = ", ".join(f"{s['stance']} {s['asset']} {float(s['conviction']):.2f}" for s in a.get("stances") or [])
        return f"- {name}: done in {_dur(a.get('latency_sec') or 0)}: {leans or 'HOLD on every asset'}"
    if st == "dropped":
        return f"- {name}: dropped out after {_dur(a.get('latency_sec') or 0)} ({clip(a.get('error'), 160)})"
    return f"- {name}: " + {"waiting": "waiting for the packet", "thinking": "thinking"}.get(st, str(st))


def _live_chair(d: dict) -> str:
    c = d.get("chair") or {}
    rep = c.get("replaced")
    head = (f"- chair ({rep.get('name')}): failed after {_dur(rep.get('latency_sec') or 0)} "
            f"({clip(rep.get('error'), 160)}), so {c.get('name')} stands in\n") if rep else ""
    who, st = f"- chair ({c.get('name')})", c.get("status")
    if st == "thinking":
        return f"{head}{who}: deliberating on {c.get('reports')} reports"
    if st == "done":
        trades = ", ".join(_trade(t) for t in d.get("trades") or []) or "no trades"
        return f"{head}{who}: decided in {_dur(c.get('latency_sec') or 0)}: {trades}"
    if st == "failed":
        return f"{head}{who}: failed after {_dur(c.get('latency_sec') or 0)} ({clip(c.get('error'), 160)})"
    if st == "skipped":
        return f"{head}{who}: not convened"
    return f"{head}{who}: " + {"waiting": "waits for the analysts"}.get(st, str(st))


def _live_outcome(o: Optional[dict], d: dict) -> str:
    if o is None:
        return f"undecided ({d['note']})" if d.get("note") else "at the risk gate"
    st, why = o.get("status"), str(o.get("reason") or "").replace("_", " ")
    notes = "; ".join(o.get("notes") or [])
    detail = f"{why} ({clip(notes, 120)})" if notes else why
    if st == "filled":
        return f"filled {_usd(o.get('notional') or 0)} @ {float(o.get('price') or 0):,.6g}"
    if st == "dry_run":
        return f"dry run, would fill {_usd(o.get('notional') or 0)} @ {float(o.get('price') or 0):,.6g}"
    if st == "duplicate":
        return f"already decided ({clip(notes or why, 120)})"
    if st == "error":
        return f"executor error ({clip(notes, 120)})"
    return f"{st}: {detail}"  # rejected / deferred


def _live_meeting(d: dict) -> str:
    """The live council message: who is done, who is thinking, what the chair decided, and
    the risk gate's answer per trade. Model text is clipped so the message stays one post."""
    phase, mid = d.get("phase"), d.get("id")
    if phase == "aborted":
        head = f"**Council meeting aborted** after {_dur(d.get('elapsed_sec') or 0)} (`{mid}`): {d.get('note')}"
    elif phase in ("orders", "done"):
        head = f"**Council adjourned** after {_dur(d.get('elapsed_sec') or 0)} (`{mid}`): {d.get('status')}"
    else:
        head = f"**Council in session** (`{mid}`, started <t:{int(d.get('started_at') or 0)}:R>)"
    lines = [head]
    if d.get("called"):
        lines.append(f"Called by the watcher: {clip(no_em_dash(d['called']), 240)}")
    if phase == "market":
        lines.append("Reading the market and the book.")
    for a in d.get("analysts") or []:
        lines.append(_live_analyst(a))
        if a.get("status") == "done" and a.get("view"):
            lines.append(f"> {clip(no_em_dash(a['view']), 200)}")
    lines.append(_live_chair(d))
    for x in d.get("dropped") or []:
        lines.append(f"- code dropped {x.get('action')} {x.get('asset')}: {x.get('why')}")
    if d.get("executing") and d.get("trades"):
        lines.append("**Orders**")
        lines += [f"- {_trade(t)}: {_live_outcome(t.get('outcome'), d)}" for t in d["trades"]]
    return "\n".join(lines)


def format_event(kind: str, d: dict) -> str:
    if kind == "fill":
        pnl = f", realized ${float(d['realized_pnl']):+,.2f}" if d["side"] == "SELL" else ""
        return (f"**{d['side']} {d['asset']}** {_usd(d['notional'])} @ {float(d['price']):,.6g} "
                f"[{d['origin']}]{pnl}\n> {d.get('reason', '')}")
    if kind == "meeting":
        trades = ", ".join(f"{t['action']} {t['asset']}" for t in d["trades"]) or "no trades"
        dropped = "".join(f"\n- dropped {x['trade']['action']} {x['trade']['asset']}: {x['why']}"
                          for x in d.get("dropped", []))
        called = f"\nCalled by the watcher: {clip(no_em_dash(d['called']), 300)}" if d.get("called") else ""
        ch = d.get("chair") or {}
        stood = (f"\nChaired by {ch.get('name')}: {ch.get('replaced')} failed ({clip(ch.get('error'), 160)})"
                 if ch.get("replaced") else "")
        return (f"**Council {d['id']}** ({d['status']}): {trades}{called}{stood}\n"
                f"{no_em_dash(d.get('summary', ''))}{dropped}")
    if kind == "watch_set":
        head = f"**Watching** ({d.get('reason')})" + (f": {clip(no_em_dash(d['note']), 200)}" if d.get("note") else "")
        rows = [f"- {c}: {clip(no_em_dash(why), 160)}" for c, why in d.get("triggers") or []]
        return "\n".join([head, *(rows or ["- nothing: no condition set until the next meeting"])])
    if kind == "meeting_called":
        who = "a watched condition was met" if d.get("by") == "condition" else "the watcher"
        return f"**Meeting called** ({who}): {clip(no_em_dash(d.get('why', '')), 600)}"
    if kind == "meeting_call_refused":
        who = "A watched condition was met" if d.get("by") == "condition" else "The watcher asked for a meeting"
        return (f"**{who}, but no meeting**: {d.get('blocked')}.\n> {clip(no_em_dash(d.get('why', '')), 400)}")
    if kind == "meeting_live":
        return _live_meeting(d)
    if kind == "risk_halt":
        sells = f" Selling part of {', '.join(d['derisk'])} until it fills." if d.get("derisk") else ""
        return f"**Drawdown breaker tripped**: {d['drawdown'] * 100:.1f}% off peak. BUYs blocked.{sells}"
    if kind == "risk_gap":
        return (f"**{d['asset']} gapped through its stop**: bid {float(d['bid']):,.2f} vs stop "
                f"{float(d['stop']):,.2f} (last close {float(d['close']):,.2f}). Selling at the bid.")
    if kind == "risk_resume":
        return f"**Breaker resumed** ({d['why']})."
    if kind == "screen":
        k = d.get("counts") or {}
        title = "Weekly stock screen" if d.get("applied", True) else "Stock screen (dry run, nothing changed)"
        out = [f"**{title}** {d.get('day')}: {k.get('ranked', 0):,} of {k.get('measured', 0):,} measured stocks "
               f"passed the gates as operating companies ({k.get('liquid', 0):,} liquid)."]
        for label, key in (("Promoted", "promote"), ("Demoted", "demote"), ("Kept", "keep"), ("Passed over", "passed")):
            for sym, why in d.get(key) or []:
                out.append(f"- {label} {sym}: {why}")
        if not (d.get("promote") or d.get("demote")):
            out.append("No membership changes.")
        if d.get("errors"):
            out.append(f"({d['errors']} data error(s); see `ottr status` and the log)")
        return no_em_dash("\n".join(out))
    if kind == "corporate_action":
        return f"**{d['symbol']}** corporate action: {no_em_dash(d['note'])}"
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
    proc, _, what = kind.partition("_")
    if proc in PROCESS_LABELS and what in ("crash", "stable", "gave_up"):
        label = PROCESS_LABELS[proc]
        tail = f"\n> {d['tail']}" if d.get("tail") else ""
        if what == "stable":
            return f"**{label} stable again** (up {d['uptime']} after {d['crashes']} crash(es))."
        if what == "gave_up":
            return (f"**{label} stopped** with code {d['rc']}: a configuration problem a restart can't "
                    f"fix (see logs/{proc}.log). Fix it, then `ottr service restart`. The engine keeps "
                    f"running.{tail}")
        if d.get("error"):
            return f"**{label} failed to start** ({d['error']}); the supervisor retries in {d['delay']}."
        return (f"**{label} exited** with code {d['rc']} after {d['runtime']}; the supervisor "
                f"restarts it in {d['delay']}.{tail}")
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
    """Post to (and edit its own messages on) a Discord webhook. Never raises.

    Calling it posts and returns whether the post landed. `create` posts with ?wait=true
    and returns the new message's id; `edit` PATCHes that message
    (/webhooks/{id}/{token}/messages/{message_id}). Query parameters on the URL (a
    thread_id) are kept for both. Errors are logged by status code only: an httpx error
    message embeds the URL, and the webhook URL contains its secret token.
    """

    def __init__(self, url: str, transport: Optional[httpx.AsyncBaseTransport] = None,
                 timeout: float = 10.0) -> None:
        self.url, self.transport, self.timeout = url, transport, timeout

    async def __call__(self, content: str) -> bool:
        return await self._send("post", "POST", httpx.URL(self.url), content) is not None

    async def create(self, content: str) -> Optional[str]:
        r = await self._send("post", "POST", httpx.URL(self.url).copy_merge_params({"wait": "true"}), content)
        if r is None:
            return None
        try:
            mid = str(r.json()["id"])
        except Exception:
            mid = ""
        if not mid.isdigit():  # it goes into a URL path: only a Discord snowflake may
            logger.warning("discord webhook post returned no message id")
            return None
        return mid

    async def edit(self, message_id: str, content: str) -> bool:
        if not message_id.isdigit():
            return False
        url = httpx.URL(self.url)
        url = url.copy_with(path=url.path.rstrip("/") + f"/messages/{message_id}")
        return await self._send("edit", "PATCH", url, content) is not None

    async def _send(self, what: str, method: str, url: httpx.URL, content: str) -> Optional[httpx.Response]:
        payload = {"content": content[:LIMIT], "allowed_mentions": {"parse": []}}
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as c:
                for attempt in range(2):
                    r = await c.request(method, url, json=payload)
                    if r.status_code == 429 and attempt == 0:
                        wait = _retry_after(r)
                        if wait is not None and wait <= MAX_RETRY_AFTER:
                            await asyncio.sleep(wait)
                            continue
                    if r.is_success:
                        return r
                    logger.warning("discord webhook %s returned HTTP %s", what, r.status_code)
                    return None
        except httpx.HTTPError as e:
            logger.warning("discord webhook %s unreachable (%s)", what, type(e).__name__)
        return None


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


@dataclass
class _Live:
    """One live message: the newest text wanted, and what Discord shows now."""
    text: str = ""
    final: bool = False
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    message_id: Optional[str] = None
    sent: Optional[str] = None
    edits: int = 0


class Alerts:
    """Rate-limited, non-blocking front end for every notification.

    Callable as the executor/engine `Notifier` (kind, data). Events and alarms draw on
    separate buckets so a burst of fills can never starve an alarm. With a `live`
    transport, `live()` keeps one message per key up to date (see the module docstring).
    """

    def __init__(self, send: Optional[Send], limits: AlertLimits = AlertLimits(),
                 clock: Callable[[], float] = time.time, live: Optional[LiveTransport] = None) -> None:
        self.send, self.limits, self.clock = send, limits, clock
        self.live_transport = live
        now = clock()
        self.events_bucket = TokenBucket(limits.burst, limits.per_hour, now)
        self.alarm_bucket = TokenBucket(limits.burst, limits.per_hour, now)
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAX)
        self.told: Dict[str, _Told] = {}
        self.suppressed = 0                     # events dropped by the bucket, not yet reported
        self._keyed: Dict[str, tuple] = {}      # key -> (last post ts, suppressed count)
        self._live: Dict[str, Tuple[_Live, asyncio.Task]] = {}
        self._live_seen: Deque[str] = deque(maxlen=64)  # keys that already had their one message
        self._sender: Optional[asyncio.Task] = None
        self.posted = 0
        self.failed = 0
        self.edited = 0
        self.edit_failed = 0

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

    # -- live messages ---------------------------------------------------------------------
    def live(self, key: str, kind: str, data: dict, final: bool = False) -> bool:
        """Show `data` in the one message for `key`: posted on the first call, then edited.

        Never blocks: it records the newest text and wakes the key's sender. `final` marks
        the last state; once it is shown the key is done and later calls are ignored. A key
        gets one message only: if its post is refused (budget spent) or fails, that key
        stays without a live view. Only works while `running()` is active.
        """
        if self.live_transport is None or self._sender is None:
            return False
        text = format_event(kind, data)  # first: a formatting bug must not leave a sender behind
        entry = self._live.get(key)
        if entry is None:
            if key in self._live_seen or len(self._live) >= LIVE_MAX:
                return False
            self._live_seen.append(key)
            if not self.events_bucket.take(self.clock()):  # a new message costs an event token
                logger.info("live message %s skipped: the notification budget is spent", key)
                return False
            st = _Live()
            self._live[key] = (st, asyncio.create_task(self._run_live(key, st), name=f"live:{key}"))
        else:
            st = entry[0]
        st.text, st.final = text, st.final or final
        st.wake.set()
        return True

    async def _run_live(self, key: str, st: _Live) -> None:
        loop = asyncio.get_running_loop()
        last, failures = float("-inf"), 0
        try:
            while True:
                try:
                    await asyncio.wait_for(st.wake.wait(), LIVE_IDLE_SEC)
                except asyncio.TimeoutError:
                    logger.info("live message %s had no update for %s; letting it go", key, fmt_age(LIVE_IDLE_SEC))
                    return
                # Discord rate-limits each webhook and only the newest state matters, so
                # updates that land while we wait are folded into a single edit.
                delay = last + self.limits.live_edit_sec - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                st.wake.clear()
                text, final = st.text, st.final
                if text != st.sent and (final or st.edits < LIVE_MAX_EDITS):
                    if st.message_id is None:
                        st.message_id = await self.live_transport.create(text)
                        ok = st.message_id is not None
                        self.posted, self.failed = self.posted + ok, self.failed + (not ok)
                        if not ok:
                            return  # nothing to edit: this key goes without a live view
                    else:
                        ok = await self.live_transport.edit(st.message_id, text)
                        st.edits += 1
                        self.edited, self.edit_failed = self.edited + ok, self.edit_failed + (not ok)
                    last = loop.time()
                    if ok:
                        st.sent, failures = text, 0
                    else:  # Discord keeps showing the last state that landed
                        failures += 1
                        if failures >= LIVE_GIVE_UP:
                            logger.warning("live message %s: %d edits failed in a row; giving up", key, failures)
                            return
                if final and st.text == text:
                    return
        except Exception:  # the transport should never raise; contain it to this one message
            logger.warning("live message %s crashed", key, exc_info=True)
        finally:
            self._live.pop(key, None)

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

    async def _flush_live(self, timeout: float) -> None:
        # Let final states (a meeting that just ended or was interrupted) reach Discord; a
        # live message still mid-story has nothing truthful left to say, so it is dropped.
        finals = [t for st, t in self._live.values() if st.final]
        if finals:
            _, pending = await asyncio.wait(finals, timeout=timeout)
            if pending:
                logger.warning("%d live message(s) not updated before shutdown", len(pending))
        tasks = [t for _, t in self._live.values()]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    @asynccontextmanager
    async def running(self, flush_timeout: float = 15.0) -> AsyncIterator["Alerts"]:
        """Run the senders for the duration of the block, then deliver what is queued."""
        task = asyncio.create_task(self.run(), name="alerts")
        self._sender = task
        try:
            yield self
        finally:
            self._sender = None  # no new live messages while shutting down
            t0 = time.monotonic()
            if self.enabled:
                await self.flush(flush_timeout)
            await self._flush_live(max(1.0, flush_timeout - (time.monotonic() - t0)))
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def make_alerts(url: Optional[str], limits: AlertLimits = AlertLimits()) -> Alerts:
    """Alerts posting to `url` (with live messages), or a disabled (no-op) Alerts when no
    webhook is set."""
    hook = DiscordWebhook(url) if url else None
    return Alerts(hook, limits, live=hook)
