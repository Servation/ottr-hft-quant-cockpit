"""
/ask's read-only lookups: the tools its model may call, each one fixed GET on the engine API.

What it does: defines each lookup (its name, a description that tells the model when to
use it, and its input schema) and runs a call. Code checks every argument (type, range,
a meeting id's shape, no unknown names), builds the one GET it maps to, and hands back
the API's JSON with its timestamps also written in New York time, clipped to a size the
model can read. A bad argument or a failed read comes back as an error result the model
can read and correct; it never raises into the loop.

Why it can't change anything: the model names a lookup and a few arguments, never a path
or a method. Each lookup is one GET on the read-only API (which reads through a mode=ro
SQLite connection), and requests built here never carry the API key. So the model
chooses what to read, never what to change: models judge, code acts. Why not every
endpoint: /book and /positions are already in the snapshot /ask starts from, and the
writes (the kill switch, a meeting call) are not lookups at all.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from ottr.chat.client import ApiClient, ApiError
from ottr.market.calendar import ET

logger = logging.getLogger(__name__)

MAX_RESULT_CHARS = 30_000   # about 8k tokens: a meeting's full packet fits, a runaway listing doesn't
# The API's own rule, but starting with a letter or digit: the id becomes a path segment,
# and "." or ".." would walk up the URL.
MEETING_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
KIND_RE = re.compile(r"^[a-z_]{1,40}$")
TS_KEY_RE = re.compile(r"(^ts$|_ts$|_at$|^as_of$|^since$|^until$)")
EPOCH = (1e9, 1e11)          # a plausible Unix time, so a count named "*_at" isn't read as a date


class BadArgument(ValueError):
    """An argument the lookup can't use; its message goes back to the model."""


@dataclass(frozen=True)
class Arg:
    kind: str                     # "int" | "number" | "string" | "bool"
    description: str
    lo: Optional[float] = None    # inclusive range for numbers
    hi: Optional[float] = None
    pattern: Optional[re.Pattern] = None

    def schema(self) -> dict:
        t = {"int": "integer", "number": "number", "string": "string", "bool": "boolean"}[self.kind]
        return {"type": t, "description": self.description}

    def check(self, name: str, v: Any) -> Any:
        if self.kind == "bool":
            if not isinstance(v, bool):
                raise BadArgument(f"{name} must be true or false")
            return v
        if self.kind == "string":
            if not isinstance(v, str) or (self.pattern and not self.pattern.match(v)):
                raise BadArgument(f"{name} must match {self.pattern.pattern if self.pattern else 'a string'}")
            return v
        # Numbers: JSON has no int type of its own, so 5.0 is a fine 5; true is not 1.
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise BadArgument(f"{name} must be a number")
        if self.kind == "int":
            if v != int(v):
                raise BadArgument(f"{name} must be a whole number")
            v = int(v)
        if (self.lo is not None and v < self.lo) or (self.hi is not None and v > self.hi):
            raise BadArgument(f"{name} must be between {self.lo:g} and {self.hi:g}")
        return v


@dataclass(frozen=True)
class Lookup:
    name: str
    description: str
    args: Mapping[str, Arg]
    # (checked args, now) -> (API path, query params)
    request: Callable[[Dict[str, Any], float], Tuple[str, Dict[str, Any]]]
    required: Tuple[str, ...] = ()
    # Trims the API's answer to what the model needs (e.g. the packet only when asked).
    shape: Callable[[dict, Dict[str, Any]], Any] = lambda body, args: body

    def tool(self) -> dict:
        """The tool definition the Messages API takes."""
        return {"name": self.name, "description": self.description,
                "input_schema": {"type": "object", "properties": {k: a.schema() for k, a in self.args.items()},
                                 "required": list(self.required), "additionalProperties": False}}

    def checked(self, raw: Any) -> Dict[str, Any]:
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise BadArgument("arguments must be an object")
        unknown = sorted(set(raw) - set(self.args))
        if unknown:
            raise BadArgument(f"unknown argument(s) {', '.join(unknown)}; this lookup takes "
                               f"{', '.join(self.args) or 'none'}")
        missing = [k for k in self.required if raw.get(k) is None]
        if missing:
            raise BadArgument(f"{', '.join(missing)} is required")
        return {k: self.args[k].check(k, v) for k, v in raw.items() if v is not None}


def _meeting(body: dict, args: Dict[str, Any]) -> dict:
    if not args.get("include_packet"):
        body = {k: v for k, v in body.items() if k != "packet"}
    return body


def _int(lo: int, hi: int, what: str) -> Arg:
    return Arg("int", f"{what} ({lo} to {hi})", lo, hi)


LOOKUPS: Tuple[Lookup, ...] = (
    Lookup(
        "get_meeting",
        "One council meeting in full: the chair's decision and dissent, every analyst's stances with "
        "thesis and key risk, the trades code dropped and why, and each order's risk-gate verdict. Call "
        "it for questions about a meeting other than the latest (whose summary is in the snapshot) or "
        "for the latest one's full detail. include_packet adds the market packet the models saw "
        "(prices, indicators, events, the book), which is long: ask for it only when the question is "
        "about what the council was shown.",
        {"meeting_id": Arg("string", "the meeting id, e.g. m-20261106T151500-a1b2c3 (from the snapshot or list_meetings)",
                           pattern=MEETING_ID_RE),
         "include_packet": Arg("bool", "also return the market packet the models saw (default false)")},
        lambda a, now: (f"/meetings/{a['meeting_id']}", {}), required=("meeting_id",), shape=_meeting),
    Lookup(
        "list_meetings",
        "Council meetings, newest first, each with its status, summary, trades, dropped trades, orders "
        "and every analyst's stances. Call it for questions about past meetings (what the council "
        "decided yesterday, how a view changed, how often it traded). Page back with before_ts set to "
        "the oldest ts you got.",
        {"limit": _int(1, 10, "how many meetings, default 5"),
         "before_ts": Arg("number", "only meetings before this Unix time (for paging back)", 0, 1e11)},
        lambda a, now: ("/meetings", {"limit": a.get("limit", 5),
                                      **({"before": a["before_ts"]} if "before_ts" in a else {})})),
    Lookup(
        "get_fills",
        "Paper fills, newest first: time, side, asset, quantity, price, notional, fee, realized P&L, "
        "origin (council, stop, de-risk, sweep, corporate action) and the reason. The snapshot shows "
        "the last 10; call this for more, or to page back with before_id.",
        {"limit": _int(1, 50, "how many fills, default 20"),
         "before_id": Arg("int", "only fills with an id below this one (for paging back)", 1, 2**62)},
        lambda a, now: ("/fills", {"limit": a.get("limit", 20),
                                   **({"before": a["before_id"]} if "before_id" in a else {})})),
    Lookup(
        "get_audit",
        "The engine's audit log, newest first: every meeting, fill, rejected order, risk event "
        "(kinds starting risk_), watcher review (watch_review), met condition (watch_hit), called or "
        "refused meeting (meeting_called, meeting_call_refused), skipped slot (meeting_skipped), kill "
        "switch change (kill_switch), corporate action, alarm (alarm_raised, alarm_cleared) and engine "
        "start or stop. Call it for 'why did X happen' and 'what happened since' questions the "
        "snapshot doesn't answer.",
        {"kind": Arg("string", "only this kind, e.g. order_rejected or watch_review (default all)",
                     pattern=KIND_RE),
         "hours": Arg("number", "how far back, in hours (1 to 720, default 24)", 1, 720),
         "limit": _int(1, 100, "how many entries, default 40")},
        lambda a, now: ("/audit", {"since": round(now - a.get("hours", 24) * 3600, 3),
                                   "limit": a.get("limit", 40), **({"kind": a["kind"]} if "kind" in a else {})})),
    Lookup(
        "get_equity",
        "The equity curve: equity, cash, invested and after-tax equity over time, beside the SPY "
        "buy-and-hold and 60/40 benchmarks from the same start. Call it for performance over a period "
        "or against the benchmarks.",
        {"days": Arg("number", "how far back, in days (1 to 365, default 30)", 1, 365),
         "max_points": _int(2, 200, "points to return, spread evenly, default 60")},
        lambda a, now: ("/equity", {"since": round(now - a.get("days", 30) * 86400, 3),
                                    "max_points": a.get("max_points", 60)})),
    Lookup(
        "get_council",
        "The council's schedule and state: whether a meeting is running and how far it has got, the "
        "next and the last meeting, and the desk watcher: the conditions it watches between meetings, "
        "its last review, and the meetings called today against the daily limit. Call it for questions "
        "about the watcher or what is being watched.",
        {}, lambda a, now: ("/council", {})),
    Lookup(
        "get_screen",
        "The weekly stock screen: the latest run's counts, promotions, demotions and passed-over "
        "names with reasons, the top of its ranking, the current satellites and recent membership "
        "changes. Call it for questions about the single stocks and why they were picked.",
        {}, lambda a, now: ("/screen", {})),
    Lookup(
        "get_universe",
        "Every member the desk trades: asset class, cap, whether it is buyable now and fractionable, "
        "plus the satellites. Call it for questions about what the desk may hold and its limits.",
        {}, lambda a, now: ("/universe", {})),
    Lookup(
        "get_health",
        "Engine health in full: every task's heartbeat against its stale limit, active alarms with "
        "when they started, ledger checks, a blind risk monitor and the kill switch. Call it when "
        "the snapshot's health line isn't enough (which task is stale, since when).",
        {}, lambda a, now: ("/health", {})),
    Lookup(
        "check_meeting_call",
        "Whether code would allow a council meeting to be called right now, and if not why not "
        "(market closed, too near the close, too soon after the last meeting, today's called "
        "meetings used), with the calls used today. Call it before proposing a meeting when you "
        "aren't sure one is allowed.",
        {}, lambda a, now: ("/meeting-call", {})),
)
BY_NAME: Dict[str, Lookup] = {lk.name: lk for lk in LOOKUPS}
TOOLS: List[dict] = [lk.tool() for lk in LOOKUPS]   # a fixed order, so the prompt prefix caches


def _et(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=ET).strftime("%a %Y-%m-%d %H:%M:%S ET")


def readable(o: Any) -> Any:
    """The API's JSON with each Unix timestamp also in New York time ("Fri 2026-11-06
    10:15:03 ET (1793978103)"): models misread raw epochs, and the desk speaks ET. The raw
    number stays, for paging with before_ts."""
    if isinstance(o, dict):
        out = {}
        for k, v in o.items():
            if (TS_KEY_RE.search(str(k)) and isinstance(v, (int, float)) and not isinstance(v, bool)
                    and EPOCH[0] < v < EPOCH[1]):
                out[k] = f"{_et(v)} ({v:.0f})"
            else:
                out[k] = readable(v)
        return out
    if isinstance(o, list):
        return [readable(v) for v in o]
    return o


def clip_json(o: Any, limit: int = MAX_RESULT_CHARS) -> str:
    text = json.dumps(o, separators=(",", ":"), ensure_ascii=False, default=str)
    if len(text) <= limit:
        return text
    return (text[:limit] + f" ... (cut at {limit:,} of {len(text):,} characters: ask for fewer rows, "
            "a shorter window, or leave out the packet)")


@dataclass
class Done:
    """One lookup as it ran, for the record in chat.db and the answer's footer."""
    name: str
    args: Dict[str, Any]
    ok: bool
    chars: int
    note: str = ""

    def describe(self) -> str:
        args = ", ".join(f"{k}={v}" for k, v in self.args.items())
        return f"{self.name}({args}) " + (f"{self.chars:,} chars" if self.ok else f"failed: {self.note}")


class Lookups:
    """Runs the model's lookups against the engine API."""

    def __init__(self, api: ApiClient, clock: Callable[[], float] = time.time) -> None:
        self.api, self.clock = api, clock

    async def run(self, name: str, raw_args: Any) -> Tuple[str, bool, Done]:
        """(the result text for the model, whether it is an error, the record)."""
        lk = BY_NAME.get(name)
        if lk is None:
            msg = f"no lookup named {name}; the lookups are {', '.join(BY_NAME)}"
            return msg, True, Done(str(name)[:40], {}, False, 0, "unknown lookup")
        try:
            args = lk.checked(raw_args)
        except BadArgument as e:
            return f"bad arguments: {e}", True, Done(name, {}, False, 0, str(e))
        path, params = lk.request(args, self.clock())
        try:
            body = await self.api.get(path, params)   # a GET: the client sends no key with it
        except ApiError as e:
            return f"the engine API said: {e.message}", True, Done(name, args, False, 0, e.message)
        text = clip_json(readable(lk.shape(body, args)))
        return text, False, Done(name, args, True, len(text))
