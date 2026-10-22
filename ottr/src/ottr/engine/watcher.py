"""
The desk watcher: Claude watching the market between council meetings, and calling one
when something has changed enough to look again before the schedule would.

What it does:
- After every meeting, and every `watch.check_every_min` in session, the watcher model
  (Claude; the council's `claude_model`, Opus) reads what changed since the last meeting
  and answers with validated JSON: whether to call a meeting now, and up to
  `watch.max_triggers` conditions to watch until the next one. A condition is an asset
  trading below or above a level, or moving more than some percent from where it was
  when the condition was set.
- Code checks those conditions every minute on live quotes; one that is met calls a
  meeting and is used up.
- Code alone decides whether a call may go ahead (`call_blocked`): only in session, not
  within `schedule.min_before_close_min` of the close, at least `watch.min_gap_min` after
  the last meeting began and before the next scheduled one, at most
  `watch.max_called_per_day` a day, never while a meeting runs.

Why: the schedule meets at 10:15 and 15:15 whatever happens. A desk that holds for days
to weeks mostly needs only that, but a break, a gap or a shock in between deserves a look
sooner, and what is worth watching depends on what the desk holds and just decided, which
is judgment. So the model judges what to watch and when to meet; code watches, enforces
the limits, and a called meeting goes through the same council and risk gate as any
other. The watcher can't trade, size or add a symbol.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from ottr.council.schemas import SchemaError, _text
from ottr.ledger.book import trade_day
from ottr.market.calendar import ET, MarketTime

KINDS = ("price_below", "price_above", "move_pct")
LEVEL_BAND = 0.25                    # a price level within 25% of the current price
MOVE_PCT = (0.5, 20.0)               # a move_pct condition's range, in percent
CALLS_KEPT = 30                      # called meetings remembered (for the daily cap and the API)

WATCH_SHAPE = """{
  "call_meeting": false,
  "why": "one or two sentences: why the council should meet now, or why waiting is fine",
  "note": "one line: what you are watching for until the next meeting",
  "watch_for": [
    {"asset": "TLT", "kind": "price_below", "level": 88.5,
     "why": "a break of support would mean duration risk is back"},
    {"asset": "SPY", "kind": "move_pct", "level": 2.0,
     "why": "a 2% move either way changes the risk appetite read"}
  ]
}"""


@dataclass(frozen=True)
class Trigger:
    asset: str
    kind: str                        # price_below | price_above | move_pct
    level: float                     # a price, or a percent for move_pct
    why: str
    ref_price: float                 # the price when it was set (move_pct measures from it)

    def hit(self, price: float) -> bool:
        if self.kind == "price_below":
            return price < self.level
        if self.kind == "price_above":
            return price > self.level
        return abs(price / self.ref_price - 1) * 100 >= self.level - 1e-9   # 1.5% is a 1.5% move

    def describe(self) -> str:
        if self.kind == "price_below":
            return f"{self.asset} below ${self.level:,.2f}"
        if self.kind == "price_above":
            return f"{self.asset} above ${self.level:,.2f}"
        return f"{self.asset} moving {self.level:g}% from ${self.ref_price:,.2f}"


@dataclass
class WatchState:
    """What the watcher left for code to watch, persisted in the book (risk_state "watch")."""
    note: str = ""
    triggers: List[Trigger] = field(default_factory=list)
    set_ts: Optional[float] = None           # when the triggers were set
    last_review_ts: Optional[float] = None
    last_review: str = ""                    # the last review's answer, one line
    last_error: str = ""                     # the last review that failed, and why
    called: List[Dict[str, Any]] = field(default_factory=list)   # {"ts", "by", "why", "ran"}
    pending: Optional[Dict[str, Any]] = None                     # a call waiting for the council loop

    @classmethod
    def from_dict(cls, d: Optional[Mapping]) -> "WatchState":
        d = dict(d or {})
        return cls(note=d.get("note", ""), triggers=[Trigger(**t) for t in d.get("triggers") or []],
                   set_ts=d.get("set_ts"), last_review_ts=d.get("last_review_ts"),
                   last_review=d.get("last_review", ""), last_error=d.get("last_error", ""),
                   called=list(d.get("called") or []), pending=d.get("pending"))

    def to_dict(self) -> dict:
        d = asdict(self)
        d["triggers"] = [asdict(t) for t in self.triggers]
        return d

    def called_on(self, ts: float) -> List[Dict[str, Any]]:
        """Meetings the watcher called that ran on `ts`'s trade day."""
        day = trade_day(ts)
        return [c for c in self.called if c.get("ran") and trade_day(c["ts"]) == day]

    def record_call(self, call: Dict[str, Any]) -> None:
        self.called = (self.called + [call])[-CALLS_KEPT:]


# -- the model's reply ------------------------------------------------------------------
def validate_reply(obj: Any) -> Dict[str, Any]:
    """The reply's shape (the triggers' levels are checked against prices separately)."""
    if not isinstance(obj, dict):
        raise SchemaError("top level must be an object")
    if not isinstance(obj.get("call_meeting"), bool):
        raise SchemaError("call_meeting must be true or false")
    rows = obj.get("watch_for")
    if not isinstance(rows, list):
        raise SchemaError("watch_for must be a list (empty is fine)")
    out = []
    for i, r in enumerate(rows):
        if not isinstance(r, dict):
            raise SchemaError(f"watch_for[{i}] must be an object")
        kind = str(r.get("kind", "")).strip()
        if kind not in KINDS:
            raise SchemaError(f"watch_for[{i}]: kind must be one of {KINDS}")
        level = r.get("level")
        if isinstance(level, bool) or not isinstance(level, (int, float)) or not math.isfinite(level) or level <= 0:
            raise SchemaError(f"watch_for[{i}]: level must be a positive number")
        out.append({"asset": str(r.get("asset", "")).upper().strip(), "kind": kind, "level": float(level),
                    "why": _text(r.get("why"), f"watch_for[{i}].why", 300)})
    note = obj.get("note")
    return {"call_meeting": obj["call_meeting"], "why": _text(obj.get("why"), "why", 400),
            "note": note.strip()[:200] if isinstance(note, str) else "", "watch_for": out}


def accept_triggers(rows: Sequence[Mapping], prices: Mapping[str, float], max_n: int) -> Tuple[List[Trigger], List[str]]:
    """The conditions code can watch, and why each other one was dropped: the asset needs a
    price now; a level must be within 25% of it and not already met; a move between 0.5%
    and 20%; one condition per asset and kind; at most `max_n`."""
    out: List[Trigger] = []
    problems: List[str] = []
    seen: Set[Tuple[str, str]] = set()
    for r in rows:
        asset, kind, level = r["asset"], r["kind"], r["level"]
        price = prices.get(asset)
        label = f"{asset} {kind} {level:g}" if kind == "move_pct" else f"{asset} {kind} {level:,.2f}"
        if price is None or price <= 0:
            problems.append(f"{label}: no price for {asset or 'that asset'}")
            continue
        if (asset, kind) in seen:
            problems.append(f"{label}: a second {kind} condition on {asset}")
            continue
        if kind == "move_pct":
            if not MOVE_PCT[0] <= level <= MOVE_PCT[1]:
                problems.append(f"{label}: a move must be {MOVE_PCT[0]:g} to {MOVE_PCT[1]:g} percent")
                continue
        elif abs(level / price - 1) > LEVEL_BAND:
            problems.append(f"{label}: more than {LEVEL_BAND:.0%} from the price ${price:,.2f}")
            continue
        trig = Trigger(asset, kind, level, r["why"], price)
        if kind != "move_pct" and trig.hit(price):
            problems.append(f"{label}: already met at ${price:,.2f}")
            continue
        if len(out) >= max_n:
            problems.append(f"{label}: over the limit of {max_n} conditions")
            continue
        seen.add((asset, kind))
        out.append(trig)
    return out, problems


def fired(triggers: Sequence[Trigger], prices: Mapping[str, float]) -> List[Tuple[Trigger, float]]:
    """The conditions met at these prices (an asset without a price can't fire)."""
    return [(t, prices[t.asset]) for t in triggers if t.asset in prices and t.hit(prices[t.asset])]


# -- whether a call may go ahead --------------------------------------------------------
def call_blocked(cfg, mt: MarketTime, now: float, state: WatchState, last_meeting_ts: Optional[float],
                 next_meeting_ts: Optional[float], meeting_running: bool) -> Optional[str]:
    """Why a called meeting can't run now, or None when it can."""
    w = cfg.watch
    if not mt.is_open:
        return "the market is closed"
    if meeting_running:
        return "a meeting is running"
    left = mt.seconds_to_close()
    if left is not None and left < cfg.schedule.min_before_close_min * 60:
        return f"{left / 60:.0f} minutes to the close (none in the last {cfg.schedule.min_before_close_min})"
    gap = w.min_gap_min * 60
    if last_meeting_ts is not None and now - last_meeting_ts < gap:
        return f"the last meeting began {(now - last_meeting_ts) / 60:.0f} minutes ago (at least {w.min_gap_min:g} apart)"
    if next_meeting_ts is not None and 0 <= next_meeting_ts - now < gap:
        at = datetime.fromtimestamp(next_meeting_ts, tz=ET).strftime("%H:%M")
        return f"the scheduled meeting at {at} ET comes sooner"
    used = len(state.called_on(now))
    if used >= w.max_called_per_day:
        return f"today's {w.max_called_per_day} called meetings are used"
    return None


# -- the prompt -------------------------------------------------------------------------
def prompt(context: str, since: str, state: WatchState, cfg, now: float) -> str:
    w, s = cfg.watch, cfg.schedule
    used = len(state.called_on(now))
    current = "\n".join(f"- {t.describe()}: {t.why}" for t in state.triggers) or "- none"
    return (
        "You watch a US-markets paper-trading desk between its council meetings (scheduled at "
        f"{' and '.join(s.meeting_times)} New York time). Two decisions:\n"
        "1. call_meeting: should the council meet NOW, before the schedule? Only when something "
        "material changed that the last meeting didn't weigh: a holding breaking down or "
        "gapping, a level that mattered breaking, a shock across assets, a regime change. The "
        "desk holds for days to weeks, so most reviews should NOT call one; noise and drift are "
        "not reasons. You can't trade: a meeting you call runs the full council and the same "
        "risk checks, and may well decide to do nothing.\n"
        f"2. watch_for: up to {w.max_triggers} conditions code checks every minute until the next "
        "meeting; one that is met calls a meeting. kind price_below or price_above takes a "
        f"price level within {LEVEL_BAND:.0%} of the current price and not already met; "
        f"move_pct takes a percent ({MOVE_PCT[0]:g} to {MOVE_PCT[1]:g}), either direction from "
        "the current price. Pick levels that would change a decision (where a holding's "
        "thesis breaks, a breakout on a candidate, a market-wide move in SPY), not routine "
        "wiggles. Your list replaces the current one; an empty list watches nothing.\n"
        f"Limits enforced by code: at most {w.max_called_per_day} called meetings a day "
        f"({used} used today), at least {w.min_gap_min:g} minutes apart and from the schedule, "
        f"none in the last {s.min_before_close_min} minutes of the session.\n\n"
        f"=== WATCHING NOW ===\nNote: {state.note or 'none'}\n{current}\n=== END WATCHING ===\n\n"
        f"=== SINCE THE LAST MEETING ===\n{since}\n=== END SINCE ===\n\n"
        f"=== CONTEXT ===\n{context}\n=== END CONTEXT ===\n\n"
        f"Reply with ONLY a JSON object of this shape (no prose, no code fences):\n{WATCH_SHAPE}"
    )
