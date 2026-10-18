"""
Health: one definition of "is OTTR OK?", shared by the engine's watchdog, `ottr status`
and the API.

What it does: turns the persisted heartbeats, the risk monitor's blind list and a ledger
replay into a list of Alarms plus an exit code. A task is unhealthy when its last
SUCCESS (not just its last report) is older than 3x its interval, because a task that
fails every tick still writes a heartbeat. The Watchdog adds clear-hysteresis on top so
a value hovering at a threshold can't raise and resolve an alarm every minute.

Why one module: v1 died for three months unnoticed. Every observer must agree on what
"stale" means, and the in-engine watchdog and the external `ottr status` both need it
(the engine can't report its own death, so status/the supervisor cover that case).
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from ottr.config import Config
from ottr.ledger.book import Ledger
from ottr.ledger.store import Store

STALE_FACTOR = 3.0

EXIT_OK = 0
EXIT_DEGRADED = 1   # one or more alarms
EXIT_LEDGER = 2     # the books don't balance (same code as `ottr check`)
EXIT_DOWN = 3       # the engine is not running

CRITICAL, WARNING = "critical", "warning"
# Risk and market feed the protective exits; equity and council only measure and propose.
SEVERITY = {"watchdog": CRITICAL, "market": CRITICAL, "risk": CRITICAL,
            "equity": WARNING, "council": WARNING}


def intervals(cfg: Config) -> Dict[str, float]:
    """Expected cadence of each engine heartbeat, in seconds."""
    return {
        "watchdog": cfg.engine.watchdog_interval_sec,  # the engine's own pulse
        "market": cfg.market.spot_ttl_sec,
        "risk": cfg.engine.risk_interval_sec,
        "equity": cfg.engine.equity_interval_sec,
        "council": cfg.council.interval_hours * 3600,
    }


def fmt_age(sec: float) -> str:
    sec = max(0.0, sec)
    if sec < 120:
        return f"{sec:.0f}s"
    if sec < 7200:
        return f"{sec / 60:.0f}m"
    if sec < 172800:
        return f"{sec / 3600:.1f}h"
    return f"{sec / 86400:.1f}d"


@dataclass(frozen=True)
class Alarm:
    key: str          # stable identity, e.g. "task:risk", "risk:blind", "ledger"
    severity: str     # critical | warning
    message: str


@dataclass(frozen=True)
class TaskHealth:
    task: str
    interval: float
    last_ts: Optional[float]      # last report, success or failure
    last_ok_ts: Optional[float]   # last success
    ok: Optional[bool]            # result of the last report (None: never reported)
    detail: str
    healthy: bool
    problem: str = ""

    def to_dict(self, now: float) -> dict:
        d = asdict(self)
        d["age_sec"] = None if self.last_ts is None else round(now - self.last_ts, 1)
        d["stale_after_sec"] = self.interval * STALE_FACTOR
        return d


@dataclass(frozen=True)
class HealthReport:
    ts: float
    engine_alive: Optional[bool]  # None when evaluated inside the engine itself
    tasks: List[TaskHealth]
    alarms: List[Alarm]
    ledger_problems: List[str] = field(default_factory=list)
    blind: Dict[str, float] = field(default_factory=dict)  # asset -> blind since

    @property
    def exit_code(self) -> int:
        if self.ledger_problems:
            return EXIT_LEDGER
        if self.engine_alive is False:
            return EXIT_DOWN
        return EXIT_DEGRADED if self.alarms else EXIT_OK

    @property
    def ok(self) -> bool:
        return self.exit_code == EXIT_OK

    def to_dict(self) -> dict:
        return {"ts": self.ts, "ok": self.ok, "exit_code": self.exit_code,
                "engine_alive": self.engine_alive,
                "alarms": [asdict(a) for a in self.alarms],
                "tasks": [t.to_dict(self.ts) for t in self.tasks],
                "ledger_problems": self.ledger_problems, "blind": self.blind}


def task_health(name: str, interval: float, hb: Optional[dict], now: float,
                epoch: Optional[float]) -> TaskHealth:
    """`epoch` = when the current engine run began (or resumed from a system sleep).

    Staleness is measured from max(last success, epoch): a restarted engine gets a fresh
    grace period instead of being blamed for heartbeats its previous run left behind.
    """
    limit = STALE_FACTOR * interval
    last_ts = hb["ts"] if hb else None
    last_ok = hb.get("ok_ts") if hb else None
    refs = [t for t in (last_ok, epoch) if t is not None]
    ref = max(refs) if refs else None
    healthy = ref is not None and now - ref <= limit
    problem = ""
    if not healthy:
        every = f"expected every {fmt_age(interval)}"
        if hb is None:
            since = f"since the engine started {fmt_age(now - epoch)} ago" if epoch else "yet"
            problem = f"{name} task has not reported {since} ({every})"
        elif now - hb["ts"] > limit:
            problem = f"{name} task stalled: last heartbeat {fmt_age(now - hb['ts'])} ago ({every})"
        else:
            problem = f"{name} task failing for {fmt_age(now - ref) if ref else 'ever'}: {hb['detail'] or 'no detail'}"
    return TaskHealth(task=name, interval=interval, last_ts=last_ts, last_ok_ts=last_ok,
                      ok=None if hb is None else bool(hb["ok"]), detail=(hb or {}).get("detail") or "",
                      healthy=healthy, problem=problem)


def evaluate(cfg: Config, store: Store, now: Optional[float] = None, *, inside_engine: bool,
             epoch: Optional[float] = None, extra: Iterable[Alarm] = ()) -> HealthReport:
    """Point-in-time health. Inside the engine the caller passes its own epoch; outside,
    the epoch comes from the engine's persisted state."""
    now = time.time() if now is None else now
    engine_state = store.get_state("engine") or {}
    if epoch is None:
        epoch = engine_state.get("epoch")
    hbs = store.heartbeats()
    tasks = [task_health(n, iv, hbs.get(n), now, epoch) for n, iv in intervals(cfg).items()
             if not (inside_engine and n == "watchdog")]
    alarms: List[Alarm] = []

    alive: Optional[bool] = None
    if not inside_engine:
        wd = next(t for t in tasks if t.task == "watchdog")
        alive = wd.healthy and bool(engine_state.get("running"))
        if not alive:
            last = f"last heartbeat {fmt_age(now - wd.last_ts)} ago" if wd.last_ts else "no heartbeat on record"
            how = "stopped cleanly" if engine_state and not engine_state.get("running") else last
            alarms.append(Alarm("engine:down", CRITICAL, f"engine is not running ({how})"))

    blind: Dict[str, float] = {}
    if alive is not False:  # a dead engine's task and blind state are stale, not news
        alarms += [Alarm(f"task:{t.task}", SEVERITY.get(t.task, WARNING), t.problem)
                   for t in tasks if not t.healthy and t.task != "watchdog"]
        risk = next((t for t in tasks if t.task == "risk"), None)
        if risk is not None and risk.healthy:
            held = set(Ledger(store).positions())
            blind = {a: since for a, since in (store.get_state("risk_blind") or {}).items() if a in held}
            if blind:
                # One alarm for the set: a feed outage blinds every holding at once, and
                # that is one problem to report, not one message per asset.
                oldest = min(blind.values())
                alarms.append(Alarm("risk:blind", CRITICAL,
                                    f"risk monitor blind on {', '.join(sorted(blind))} for up to "
                                    f"{fmt_age(now - oldest)}: no fresh quote, so trailing stops and "
                                    "trims are not enforced"))

    problems: List[str] = []
    lg = Ledger(store)
    if lg.initialized():
        problems = lg.check()
        if problems:
            alarms.append(Alarm("ledger", CRITICAL, "ledger check failed: " + "; ".join(problems[:3])))
    alarms += list(extra)
    return HealthReport(ts=now, engine_alive=alive, tasks=tasks, alarms=alarms,
                        ledger_problems=problems, blind=blind)


class Watchdog:
    """Alarm state with clear-hysteresis.

    An alarm is active from the first evaluation that reports it and stays active until
    it has been absent for `clear_after` seconds. Raising stays immediate (the 3x-interval
    staleness rule already waited), so only the noisy direction is damped.
    """

    def __init__(self, clear_after: float) -> None:
        self.clear_after = clear_after
        self.active: Dict[str, Alarm] = {}
        self.since: Dict[str, float] = {}       # key -> first raised
        self._clean_since: Dict[str, float] = {}

    def update(self, alarms: Iterable[Alarm], now: float) -> Tuple[List[Alarm], List[Alarm]]:
        """Apply one evaluation; returns (newly raised, newly cleared)."""
        current = {a.key: a for a in alarms}
        raised = [a for k, a in current.items() if k not in self.active]
        for k, a in current.items():
            self.active[k] = a  # keep the freshest message (ages, details)
            self.since.setdefault(k, now)
            self._clean_since.pop(k, None)
        cleared = []
        for k in list(self.active):
            if k in current:
                continue
            clean = self._clean_since.setdefault(k, now)
            if now - clean >= self.clear_after:
                cleared.append(self.active.pop(k))
                self.since.pop(k, None)
                self._clean_since.pop(k, None)
        return raised, cleared
