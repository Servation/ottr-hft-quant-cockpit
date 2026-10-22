"""
Universe membership for the US desk: who may be bought, and on what terms.

What it does: a Member is one symbol the desk trades: its asset class, its state (active,
or sunset: being sold off, so SELL/HOLD only), and whether the broker allows fractional
shares in it. `core_members` builds the members the config defines; the satellites, the
stocks the weekly screener picks (`universe/run.py`), join from the database (`db.py`).
`last_slot` says when the weekly screen was last due.

Why a membership record rather than a list: "may we buy this, how much, and in what
units" depends on all three facts at once, and a model can never add a symbol. Anything
not a member can still be SOLD if held (the gate only needs a position for that), so a
symbol leaving the universe never traps a position.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Dict, Mapping

from ottr.market.calendar import ET

ACTIVE, SUNSET = "active", "sunset"
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(frozen=True)
class Member:
    symbol: str
    asset_class: str
    state: str = ACTIVE
    fractionable: bool = True

    @property
    def buyable(self) -> bool:
        return self.state == ACTIVE


def core_members(cfg, fractionable: Mapping[str, bool] = {}) -> Dict[str, Member]:
    """The config's core ETFs as members. `fractionable` comes from Alpaca's asset list;
    a symbol it doesn't mention is treated as whole-shares-only, the safe assumption."""
    return {sym: Member(sym, cls, ACTIVE, fractionable.get(sym, False))
            for sym, cls in cfg.universe.core.items()}


def last_slot(screen, ts: float) -> float:
    """The most recent weekly screen time (`screen.weekday` at `screen.at`, New York time)
    at or before `ts`."""
    now = datetime.fromtimestamp(ts, tz=ET)
    h, m = (int(x) for x in screen.at.split(":"))
    day = now.date() - timedelta(days=(now.weekday() - WEEKDAYS.index(screen.weekday)) % 7)
    slot = datetime.combine(day, time(h, m), tzinfo=ET)
    if slot > now:
        slot = datetime.combine(day - timedelta(days=7), time(h, m), tzinfo=ET)
    return slot.timestamp()
