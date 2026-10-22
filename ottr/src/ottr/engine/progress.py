"""
Meeting progress: what a council meeting has done so far, as plain data.

What it does: folds the council's events (analysts started, one done or dropped out, the
chair deliberating or decided) and then the executor's order outcomes into one state,
and `to_dict()` renders it for GET /api/council and the live Discord message.

Why its own object: the council only reports what happened and never learns who is
watching, and the watchers only read this snapshot. So neither the API nor a Discord
outage can slow a meeting down, and both surfaces show the same facts.

Phases: market (building the packet) -> analysts -> chair -> orders (recorded; the risk
gate is deciding the trades) -> done. A meeting that raised, or was cancelled by an
engine stop, ends in "aborted" with the reason in `note`.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Iterable, List, Optional

FINAL_PHASES = ("done", "aborted")


def _trade(t: Dict[str, Any]) -> Dict[str, Any]:
    out = {"action": t.get("action"), "asset": t.get("asset"), "outcome": None}
    if t.get("action") == "BUY":
        out["conviction"] = t.get("conviction")
    else:
        out["fraction_of_position"] = t.get("fraction_of_position")
    return out


def _drop(x: Dict[str, Any]) -> Dict[str, Any]:
    return {"action": x["trade"].get("action"), "asset": x["trade"].get("asset"), "why": x.get("why")}


class MeetingProgress:
    def __init__(self, meeting_id: str, analysts: Iterable[str], chair: str,
                 now: Optional[float] = None) -> None:
        self.id = meeting_id
        self.started = time.time() if now is None else now
        self.ended: Optional[float] = None     # when the council part ended (adjourned or aborted)
        self.phase = "market"
        self.analysts: Dict[str, Dict[str, Any]] = {
            n: {"name": n, "status": "waiting", "latency_sec": None, "stances": [], "view": None,
                "error": None} for n in analysts}
        self.chair: Dict[str, Any] = {"name": chair, "status": "waiting", "reports": None,
                                      "latency_sec": None, "error": None}
        self.trades: List[Dict[str, Any]] = []
        self.dropped: List[Dict[str, Any]] = []
        self.status: Optional[str] = None      # the meeting record's status ("ok", "abstain: ...")
        self.executing = False                 # trades were handed to the executor
        self.note: Optional[str] = None        # why it was aborted, or left orders undecided
        self.called: Optional[str] = None      # why the watcher called it (None: scheduled)

    @property
    def final(self) -> bool:
        return self.phase in FINAL_PHASES

    # -- council events ----------------------------------------------------------------------
    def apply(self, kind: str, d: Dict[str, Any]) -> None:
        """Fold one council event (see ottr.council.council.Observer) into the state."""
        if kind == "analysts_started":
            self.phase = "analysts"
            for a in self.analysts.values():
                a["status"] = "thinking"
        elif kind in ("analyst_done", "analyst_dropped"):
            a = self.analysts.setdefault(d["name"], {"name": d["name"], "stances": [], "view": None,
                                                     "error": None})
            a["latency_sec"] = round(float(d.get("latency") or 0.0), 1)
            if kind == "analyst_done":
                rep = d.get("report") or {}
                a["status"], a["view"] = "done", rep.get("overall_view")
                # HOLD is the default answer; what the owner wants to see is where it leans.
                a["stances"] = [{"asset": s["asset"], "stance": s["stance"], "conviction": s["conviction"]}
                                for s in rep.get("assets", []) if s.get("stance") != "HOLD"]
            else:
                a["status"], a["error"] = "dropped", d.get("error")
        elif kind == "chair_started":
            self.phase = "chair"
            self.chair.update(status="thinking", reports=d.get("reports"))
        elif kind == "chair_done":
            self.chair.update(status="done", latency_sec=round(float(d.get("latency") or 0.0), 1))
            self.trades = [_trade(t) for t in d.get("trades") or []]
            self.dropped = [_drop(x) for x in d.get("dropped") or []]
        elif kind == "chair_failed":
            self.chair.update(status="failed", error=d.get("error"),
                              latency_sec=round(float(d.get("latency") or 0.0), 1))

    # -- meeting lifecycle -----------------------------------------------------------------
    def adjourn(self, rec: Dict[str, Any], executing: bool, now: Optional[float] = None) -> None:
        """The meeting `rec` is recorded. With trades to execute, wait for their outcomes.

        Trades and drops are taken from the record (not the events), so what the live view
        waits on is exactly what goes to the executor.
        """
        self.status, self.ended = rec["status"], time.time() if now is None else now
        self.trades = [_trade(t) for t in rec.get("trades") or []]
        self.dropped = [_drop(x) for x in rec.get("dropped") or []]
        if self.chair["status"] == "waiting":
            self.chair["status"] = "skipped"   # too few analysts answered to convene the chair
        self.executing = executing and bool(self.trades)
        self.phase = "orders" if self.executing else "done"

    def abort(self, why: str, now: Optional[float] = None) -> None:
        """The meeting raised or was cancelled before it was recorded."""
        if self.phase in ("orders", "done"):
            return  # already recorded: a later failure doesn't unmake the meeting
        self.ended, self.note, self.phase = time.time() if now is None else now, why, "aborted"
        for who in (*self.analysts.values(), self.chair):
            if who["status"] in ("waiting", "thinking"):
                who["status"] = "stopped"

    def order(self, asset: str, status: str, reason: str = "", notional: float = 0.0,
              price: float = 0.0, notes: Iterable[str] = ()) -> bool:
        """Record the executor's outcome for this meeting's trade on `asset`.

        Returns False when no trade of this meeting is waiting on that asset. Once every
        trade has an outcome the meeting is done.
        """
        for t in self.trades:
            if t["asset"] == asset and t["outcome"] is None:
                t["outcome"] = {"status": status, "reason": reason, "notional": notional,
                                "price": price, "notes": list(notes)}
                if self.phase == "orders" and all(x["outcome"] is not None for x in self.trades):
                    self.phase = "done"
                return True
        return False

    def close(self, why: str) -> None:
        """Stop waiting for order outcomes (the engine is stopping)."""
        if self.phase == "orders":
            self.phase, self.note = "done", why

    def to_dict(self, now: Optional[float] = None) -> Dict[str, Any]:
        now = time.time() if now is None else now
        end = self.ended if self.ended is not None else now
        return {
            "id": self.id, "phase": self.phase, "started_at": self.started, "ended_at": self.ended,
            "called": self.called,
            "elapsed_sec": round(end - self.started, 1), "status": self.status, "note": self.note,
            "analysts": [dict(a, stances=list(a["stances"])) for a in self.analysts.values()],
            "chair": dict(self.chair),
            "executing": self.executing,
            "trades": [dict(t, outcome=dict(t["outcome"]) if t["outcome"] else None) for t in self.trades],
            "dropped": [dict(x) for x in self.dropped],
        }
