"""
Corporate actions from Alpaca, normalized and classified for the book and the gate.

What it does: `parse_actions` turns Alpaca's /v1/corporate-actions payload (one array per
action type) into CorporateActions, each naming the symbol whose holders it affects and
the day it takes effect. Each type is classified:
- income (cash dividends, capital gains distributions): cash to holders. Modeled (PR 9);
  never blocks a BUY.
- splits (forward, reverse): the share count changes, the value doesn't. Modeled (PR 9);
  never blocks a BUY.
- cash mergers: the shares become cash. Modeled (PR 9), but block BUYs: the price is
  pinned to the deal, so there is nothing left for the desk to trade.
- everything else (stock mergers, spin-offs, name changes, redemptions, unit splits, stock
  dividends, rights, reorganizations, worthless removals, partial calls, and any type
  Alpaca adds later): not modeled. They block BUYs; a held one raises an alarm and is
  exited before it takes effect (PR 8/9).

Facts this relies on (docs.alpaca.markets and live checks, 2026-10-21):
- `start`/`end` filter on process_date, the day Alpaca books the action. For a dividend
  that is the pay date, which can be six weeks after the ex-date (SPY: ex 09-18, paid
  10-30), so a window must reach well back to see dividends still owed.
- `symbols` matches any symbol in an action (an acquirer as well as its target), and
  `limit` counts across all types and symbols, with `next_page_token` paging.
- Arrays for types with no matches are left out. Splits show up about two weeks before
  their ex-date; dividends only around theirs.
- Mergers carry `effective_date` instead of `ex_date`; name changes, redemptions and
  removals carry only `process_date`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

from ottr.market.symbols import normalize

# Alpaca's array name -> (our kind, which field names the affected symbol, which date
# field says when it takes effect). The kind is Alpaca's `types` filter value.
TYPES: Dict[str, Tuple[str, str, str]] = {
    "cash_dividends": ("cash_dividend", "symbol", "ex_date"),
    "capital_gains_distributions": ("capital_gains_distribution", "symbol", "ex_date"),
    "forward_splits": ("forward_split", "symbol", "ex_date"),
    "reverse_splits": ("reverse_split", "symbol", "ex_date"),
    "cash_mergers": ("cash_merger", "acquiree_symbol", "effective_date"),
    "stock_mergers": ("stock_merger", "acquiree_symbol", "effective_date"),
    "stock_and_cash_mergers": ("stock_and_cash_merger", "acquiree_symbol", "effective_date"),
    "spin_offs": ("spin_off", "source_symbol", "ex_date"),
    "stock_dividends": ("stock_dividend", "symbol", "ex_date"),
    "unit_splits": ("unit_split", "old_symbol", "effective_date"),
    "rights_distributions": ("rights_distribution", "source_symbol", "ex_date"),
    "reorganizations": ("reorganization", "symbol", "effective_date"),
    "name_changes": ("name_change", "old_symbol", "process_date"),
    "redemptions": ("redemption", "symbol", "process_date"),
    "worthless_removals": ("worthless_removal", "symbol", "process_date"),
    "partial_calls": ("partial_call", "symbol", "process_date"),
}
INCOME = frozenset({"cash_dividend", "capital_gains_distribution"})
SPLITS = frozenset({"forward_split", "reverse_split"})
CASH_OUT = frozenset({"cash_merger"})
MODELED = INCOME | SPLITS | CASH_OUT
# Fields an unknown type might name its symbol with, tried in order (fail closed: a new
# action type still blocks BUYs of the symbol it touches).
_SUBJECT_FIELDS = ("symbol", "acquiree_symbol", "source_symbol", "old_symbol")
_DATE_FIELDS = ("ex_date", "effective_date", "process_date")


@dataclass(frozen=True)
class CorporateAction:
    id: str
    kind: str
    symbol: str                       # whose holders it affects (canonical)
    effective: Optional[date]         # ex-date, or effective date for mergers; None = unknown
    process: Optional[date]           # when Alpaca books it (a dividend's pay date)
    ex: Optional[date] = None
    record: Optional[date] = None
    pay: Optional[date] = None
    detail: Mapping[str, object] = field(default_factory=dict, compare=False)   # Alpaca's row

    @property
    def modeled(self) -> bool:
        return self.kind in MODELED

    @property
    def blocks_buy(self) -> bool:
        return self.kind not in INCOME | SPLITS

    def pending(self, today: date) -> bool:
        """Not yet in effect, or in effect but not yet booked. An unknown date counts as
        pending: fail closed."""
        if self.effective is None or self.effective >= today:
            return True
        return self.process is not None and self.process >= today

    def describe(self) -> str:
        when = self.effective.isoformat() if self.effective else "an unknown date"
        return f"{self.kind.replace('_', ' ')} effective {when}"

    def detail_json(self) -> str:
        return json.dumps(dict(self.detail), sort_keys=True, default=str)


def _day(raw) -> Optional[date]:
    try:
        return date.fromisoformat(str(raw)[:10]) if raw else None
    except ValueError:
        return None


def parse_actions(payload: Mapping[str, Iterable[Mapping]]) -> Tuple[List[CorporateAction], List[str]]:
    """(actions, problems) from Alpaca's `corporate_actions` object (array name -> rows).

    Rows whose symbol isn't a plain US ticker are dropped quietly (foreign lines, units);
    rows with no id, and types this code doesn't know, are reported as problems. An
    unknown type is still kept, classified as unmodeled, so it blocks BUYs."""
    actions: List[CorporateAction] = []
    problems: List[str] = []
    for array, rows in (payload or {}).items():
        known = TYPES.get(array)
        if known is None:
            problems.append(f"unknown corporate action type {array!r}: treated as unmodeled")
        for row in rows or ():
            if not isinstance(row, Mapping) or not row.get("id"):
                problems.append(f"{array}: a row with no id was skipped")
                continue
            if known:
                kind, subject, when = known
                raw_sym, eff = row.get(subject), _day(row.get(when))
            else:
                kind = array.rstrip("s") or array
                raw_sym = next((row[f] for f in _SUBJECT_FIELDS if row.get(f)), None)
                eff = next((_day(row[f]) for f in _DATE_FIELDS if row.get(f)), None)
            try:
                sym = normalize(str(raw_sym or ""))
            except ValueError:
                continue
            actions.append(CorporateAction(
                id=str(row["id"]), kind=kind, symbol=sym, effective=eff, process=_day(row.get("process_date")),
                ex=_day(row.get("ex_date")), record=_day(row.get("record_date")), pay=_day(row.get("payable_date")),
                detail=dict(row)))
    return actions, problems
