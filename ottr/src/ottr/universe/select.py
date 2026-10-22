"""
Which stocks become, stay or stop being satellites. Pure: the screen's ranking and the
current membership in, the decision out.

The rules, in order, and why:
1. A satellite that fails a hard gate (or is no longer tradable, an operating company, or
   measurable) is demoted at once: the desk shouldn't hold it. These don't count as
   changes: they aren't a choice.
2. In its first `min_tenure_days` a satellite is kept whatever its rank, so a new pick
   isn't ranked out by one noisy week.
3. After that it is kept while it ranks within `keep_rank` (12 for 6 slots): a stock
   doesn't have to stay the best, only good enough. The worst-ranked beyond that are
   demoted, at most `max_changes` changes a week.
4. Empty slots are filled with the best candidates that pass the entry checks, aren't
   cooling off (`cooldown_days` after a demotion), and don't move with any member (core
   ETFs and satellites: correlation over `max_correlation`, 120 sessions). Filling an
   empty slot isn't a change.
5. With every slot full, a candidate replaces the weakest satellite past its tenure only
   if it scores at least `challenger_margin` points more, within the weekly limit.
Rules 2, 3 and 5 are the hysteresis: without them a stock near the cut line would be
bought and sold every week, paying the spread for nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from ottr.universe.db import Satellite
from ottr.universe.screen import Row

PASSED_SHOWN = 10            # the best candidates not promoted, each with its reason


@dataclass
class Decision:
    promote: List[Tuple[str, str]] = field(default_factory=list)   # (symbol, why)
    demote: List[Tuple[str, str]] = field(default_factory=list)
    keep: List[Tuple[str, str]] = field(default_factory=list)
    passed: List[Tuple[str, str]] = field(default_factory=list)
    changes: int = 0                                                # rank-outs and swaps

    def to_dict(self) -> dict:
        return {"promote": self.promote, "demote": self.demote, "keep": self.keep, "passed": self.passed,
                "changes": self.changes}


def _place(r: Row) -> str:
    return f"#{r.rank}, score {r.score:.0f}"


def decide(cfg, ranked: Sequence[Row], measured: Mapping[str, Row], current: Mapping[str, Satellite],
           cooling: Mapping[str, Satellite], today: date, correlation: Callable[[str, str], Optional[float]],
           core: Sequence[str], unfit: Mapping[str, str], untradable: Optional[Mapping[str, str]] = None) -> Decision:
    """`ranked`: rows that passed the gates, best first. `measured`: every row, by symbol.
    `current`: the active satellites. `cooling`: demoted ones still in their cooldown.
    `unfit`: symbol -> why it can't be a member at all (not tradable, not an operating
    company, a quote too rarely usable), for satellites and candidates alike.
    `untradable`: candidate -> why its live quote can't be relied on yet."""
    untradable = untradable or {}
    sc, slots_max = cfg.screen, cfg.universe.satellites_max
    out = Decision()
    by_rank = {r.symbol: r for r in ranked}
    kept: Dict[str, Row] = {}
    tenured: List[Row] = []
    for sym, sat in sorted(current.items()):
        r = by_rank.get(sym)
        why = unfit.get(sym) or (measured[sym].gate if sym in measured else "no price history to measure")
        if why or r is None:
            out.demote.append((sym, f"hard gate: {why}"))
            continue
        kept[sym] = r
        if (today - sat.promoted_day).days < sc.min_tenure_days:
            out.keep.append((sym, f"{_place(r)}; in its first {sc.min_tenure_days} days"))
        else:
            tenured.append(r)
    for r in sorted(tenured, key=lambda x: -x.rank):    # worst first, so the limit spares the best
        if r.rank > sc.keep_rank and out.changes < sc.max_changes:
            out.demote.append((r.symbol, f"ranks {_place(r)}, outside the top {sc.keep_rank}"))
            out.changes += 1
            del kept[r.symbol]
    for r in sorted(tenured, key=lambda x: x.rank):
        if r.symbol in kept:
            note = f"; outside the top {sc.keep_rank}, but this week's changes are used" if r.rank > sc.keep_rank else ""
            out.keep.append((r.symbol, _place(r) + note))

    members = list(core) + list(kept)
    slots = slots_max - len(kept)
    for r in ranked:
        if len(out.passed) >= PASSED_SHOWN and slots == 0 and out.changes >= sc.max_changes:
            break   # nothing further down can be promoted, and enough are explained
        sym = r.symbol
        if sym in kept or sym in current or sym in core:
            continue
        why = unfit.get(sym) or r.entry_problem() or untradable.get(sym, "")
        if not why and sym in cooling:
            why = f"cooling off since its demotion on {cooling[sym].demoted_day}"
        if not why:
            for m in members:
                c = correlation(sym, m)
                if c is not None and abs(c) > sc.max_correlation:
                    why = f"moves with {m} (correlation {c:.2f})"
                    break
        if not why:
            if slots > 0:
                out.promote.append((sym, f"ranks {_place(r)}: fills an open slot"))
                slots -= 1
                members.append(sym)
                kept[sym] = r
                continue
            weakest = min((kept[s] for s in kept if s in current and kept[s] in tenured),
                          key=lambda x: (x.score or 0, x.symbol), default=None)
            if weakest is None:
                why = "no slot: every satellite is in its first weeks"
            elif out.changes >= sc.max_changes:
                why = f"no slot: this week's {sc.max_changes} changes are used"
            elif (r.score or 0) < (weakest.score or 0) + sc.challenger_margin:
                lead = (r.score or 0) - (weakest.score or 0)
                why = (f"beats the weakest satellite {weakest.symbol} by {lead:.0f} points (needs "
                       f"{sc.challenger_margin:.0f})" if lead > 0 else
                       f"scores no better than the weakest satellite {weakest.symbol} ({weakest.score:.0f})")
            else:
                out.demote.append((weakest.symbol, f"replaced by {sym} ({_place(r)} vs {_place(weakest)})"))
                out.promote.append((sym, f"ranks {_place(r)}: replaces {weakest.symbol}"))
                out.keep = [k for k in out.keep if k[0] != weakest.symbol]
                out.changes += 1
                members.remove(weakest.symbol)
                del kept[weakest.symbol]
                members.append(sym)
                kept[sym] = r
                continue
        if len(out.passed) < PASSED_SHOWN:
            out.passed.append((sym, why))
    return out
