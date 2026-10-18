"""
Kill switch: the runtime "dry run" flag, persisted in the ledger.

What it does: effective dry run = config/env dry_run OR the operator flag set through
the API. The flag lives in SQLite, so an engaged switch survives a crash and restart; a
switch that reset on restart would silently resume trading after the supervisor
recovered the engine. Config wins in the safe direction only: the API can engage the
switch over a live config, but can't release it while config or OTTR_DRY_RUN says dry run.
"""

from __future__ import annotations

import time
from typing import Optional

from ottr.config import Config
from ottr.ledger.store import Store

STATE_KEY = "kill_switch"


class KillSwitch:
    def __init__(self, cfg: Config, store: Store) -> None:
        self.cfg, self.store = cfg, store
        self._state = dict(store.get_state(STATE_KEY) or {})

    @property
    def operator_engaged(self) -> bool:
        return bool(self._state.get("engaged", False))

    @property
    def engaged(self) -> bool:
        return self.cfg.trading.dry_run or self.operator_engaged

    def state(self) -> dict:
        return {"effective_dry_run": self.engaged, "config_dry_run": self.cfg.trading.dry_run,
                "operator_engaged": self.operator_engaged, "since": self._state.get("since"),
                "by": self._state.get("by"), "reason": self._state.get("reason")}

    def set(self, engaged: bool, by: str, reason: Optional[str] = None) -> dict:
        """Persist the operator flag (writer store only) and audit who flipped it."""
        new = {"engaged": bool(engaged), "since": time.time(), "by": by, "reason": reason or ""}
        with self.store.tx():
            self.store.set_state(STATE_KEY, new)
            self.store.audit("kill_switch", engaged=bool(engaged), by=by, reason=reason or "",
                             effective=self.cfg.trading.dry_run or bool(engaged))
        self._state = new
        return self.state()
