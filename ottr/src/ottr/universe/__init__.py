"""
Universe membership for the US desk: who may be bought, and on what terms.

What it does: a Member is one symbol the desk trades: its asset class, its state (active,
or sunset: being sold off, so SELL/HOLD only), and whether the broker allows fractional
shares in it. `core_members` builds the members the config defines; screener-picked
satellites join from the database in the screener PR.

Why a membership record rather than a list: "may we buy this, how much, and in what
units" depends on all three facts at once, and a model can never add a symbol. Anything
not a member can still be SOLD if held (the gate only needs a position for that), so a
symbol leaving the universe never traps a position.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Mapping

ACTIVE, SUNSET = "active", "sunset"


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
