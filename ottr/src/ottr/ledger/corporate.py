"""
Corporate actions applied to the book: dividends, splits, cash in lieu, cash mergers.

What it does: `apply_due` walks every stored action (`ottr/events`) that has taken effect
(its ex- or effective date is today or earlier) and that the ledger hasn't recorded yet,
oldest first, and writes one `corporate_entries` row for each, one transaction per action:
- Cash dividend or capital gains distribution: on the ex-date, the shares held at the end
  of the trade day before it (replayed from the log, so a late run gets the same answer)
  are owed rate x shares. That receivable counts in equity until the pay date, when it
  becomes cash. A rate Alpaca corrects before then is followed; a withdrawn one is
  cancelled.
- Forward or reverse split: every open lot's shares are multiplied by the ratio (its cost
  and holding period don't change) and the high-water mark is divided by it, so the stop
  moves with the price. Where the asset trades in whole shares only, the fraction left
  over is paid as cash in lieu at the last close restated to the new shares, booked as a
  SELL so lots, P&L and tax follow a sale's rules.
- Cash merger: the whole holding is sold at the deal's cash rate.
- Anything else: the book can't follow it, so nothing changes. A holding carried through
  one is recorded `unmodeled` and stays an alarm while it is held (the risk monitor exits
  such holdings the session before, see `exits_due`, so this means that exit failed).
A holding of zero is recorded `not_held`; its rate or ratio is still kept, because
`restate_marks` uses every entry to restate a symbol's last close to today's share basis.

An action that can't be applied (no rate, no close to price cash in lieu, or trades in
the symbol after it took effect) is left unrecorded and named in `FAILURES`; `unapplied`
lists every due action without an entry, which holds back trading in that symbol and the
equity curve until it is applied (`problems` turns both into alarms).

Why at the start of the effective date rather than at the open: that is when a broker
books them, and it keeps every reader consistent. From that moment the book's shares, its
receivables and its marks (the last close restated by `restate_marks`) all speak of the
same share basis: at night, at the open and in session.

Why the cash-in-lieu and merger SELLs skip the executor: they are not orders. There is no
quote to check, no limit to apply and nothing to refuse; the broker books them, and only
the engine (the one writer) runs this, in one transaction per action like a fill.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from ottr.events import db as events_db
from ottr.events.corporate import CASH_OUT, INCOME, SPLITS, CorporateAction
from ottr.events.view import sessions_until
from ottr.ledger.book import ZERO, D, Ledger, LedgerError, trade_day
from ottr.ledger.store import Store
from ottr.market.calendar import ET, TradingCalendar
from ottr.market.types import Quote

logger = logging.getLogger(__name__)

ORIGIN = "corporate_action"         # the origin of the SELL fills these book
FAILURES = "corporate_failures"     # risk_state key: action id -> why it couldn't be applied
UNAPPLIED_GRACE_SEC = 3600.0        # due but unrecorded this long (failure or not) is an alarm
PRICE_STEP = Decimal("0.0001")      # cash-in-lieu prices, per share
# Order within one day: a dividend going ex with a split is paid on the shares held before it.
_ORDER = {**{k: 0 for k in INCOME}, **{k: 1 for k in SPLITS}, **{k: 2 for k in CASH_OUT}}


class CorporateActionError(RuntimeError):
    """An action that can't be applied as stored. It stays unapplied (and alarmed)."""


@dataclass
class Applied:
    """One pass of `apply_due`: what changed for holdings, and what couldn't be applied."""
    notes: List[Tuple[str, str]] = field(default_factory=list)   # (symbol, what happened), held ones only
    failed: Dict[str, str] = field(default_factory=dict)          # action id -> why not
    recorded: int = 0                                              # entries written, held or not

    def summary(self) -> str:
        parts = [f"{s}: {n}" for s, n in self.notes]
        if self.failed:
            parts.append(f"{len(self.failed)} corporate action(s) not applied")
        return "; ".join(parts)


# -- reading an action ------------------------------------------------------------------
def _number(a: CorporateAction, key: str) -> Decimal:
    try:
        v = Decimal(str(a.detail[key]))
    except (KeyError, InvalidOperation, ValueError, TypeError):
        raise CorporateActionError(f"{a.kind} {a.id} has no usable {key!r}") from None
    if not v.is_finite() or v <= 0:
        raise CorporateActionError(f"{a.kind} {a.id} has {key} {v}")
    return v


def split_ratio(a: CorporateAction) -> Decimal:
    """New shares per old share: 4 for a 4-for-1, 0.1 for a 1-for-10 reverse split."""
    return _number(a, "new_rate") / _number(a, "old_rate")


def _ratio_text(a: CorporateAction) -> str:
    return f"{_number(a, 'new_rate').normalize():f}-for-{_number(a, 'old_rate').normalize():f}"


def _shares(q: Decimal) -> str:
    return f"{q.normalize():f}"


def _sort_key(a: CorporateAction) -> Tuple:
    return (a.effective or date.max, _ORDER.get(a.kind, 3), a.id)


def due_actions(store: Store, today: date) -> List[CorporateAction]:
    """Live actions in effect by `today` that the ledger hasn't recorded, in the order
    they must be applied."""
    rows = store.conn.execute(
        f"SELECT {events_db.ACTION_COLS} FROM corporate_actions a WHERE withdrawn_ts IS NULL "
        "AND effective_day IS NOT NULL AND effective_day<=? "
        "AND NOT EXISTS (SELECT 1 FROM corporate_entries e WHERE e.action_id=a.id)", (today.isoformat(),))
    return sorted((events_db.action_from_row(r) for r in rows), key=_sort_key)


def unapplied(store: Store, today: date) -> Dict[str, str]:
    """symbol -> the first action in effect that the ledger hasn't applied. Until it is,
    the book's shares or cash for that symbol are not current."""
    out: Dict[str, str] = {}
    for a in due_actions(store, today):
        out.setdefault(a.symbol, f"{a.describe()} not applied to the book yet")
    return out


# -- applying ---------------------------------------------------------------------------
def _unchanged_since(ledger: Ledger, a: CorporateAction, held: Decimal) -> None:
    """Shares and cash move on the holding as of the effective date. If the symbol traded
    since, that holding is gone and the change can't be placed: a person must look."""
    pos = ledger.positions().get(a.symbol)
    now_held = pos.qty if pos else ZERO
    if now_held != held:
        raise CorporateActionError(
            f"{a.symbol} traded on or after {a.effective} before this was applied ({_shares(held)} shares going "
            f"in, {_shares(now_held)} now): fix the book by hand")


def _apply(ledger: Ledger, a: CorporateAction, now: float, fractionable: Callable[[str], bool],
           last_close: Callable[[str, date], Optional[float]]) -> Optional[str]:
    """Record one action and book its effect. Returns a note when it touched a holding."""
    day = a.effective
    held = ledger.held_before(a.symbol, day)
    if a.kind in INCOME:
        rate = _number(a, "rate")
        if held <= 0:
            ledger.record_corporate(a.id, a.kind, a.symbol, day, "not_held", ZERO, {"rate": str(rate)}, now)
            return None
        ledger.record_corporate(a.id, a.kind, a.symbol, day, "owed", held, {"rate": str(rate)}, now)
        amount = ledger.owe_dividend(a.id, a.symbol, a.kind, day, held, rate)
        pay = a.pay or a.process
        return (f"{a.kind.replace('_', ' ')} of ${rate}/share on {_shares(held)} shares held into the "
                f"{day} ex-date: ${amount} owed, paid {pay or 'on a date not announced yet'}")
    if a.kind in SPLITS:
        ratio = split_ratio(a)
        detail = {"ratio": str(ratio)}
        if held <= 0:
            ledger.record_corporate(a.id, a.kind, a.symbol, day, "not_held", ZERO, detail, now)
            return None
        _unchanged_since(ledger, a, held)
        ledger.record_corporate(a.id, a.kind, a.symbol, day, "applied", held, detail, now)
        before, after = ledger.split_lots(a.id, a.symbol, ratio)
        note = (f"{_ratio_text(a)} {a.kind.replace('_', ' ')} effective {day}: {_shares(before)} -> "
                f"{_shares(after)} shares (cost basis and holding period unchanged, stop restated)")
        fraction = after - after.to_integral_value(rounding=ROUND_FLOOR)
        if fraction > 0 and not fractionable(a.symbol):
            close = last_close(a.symbol, day)
            if close is None or close <= 0:
                raise CorporateActionError(f"no close before {day} to price {a.symbol}'s cash in lieu")
            price = (D(close) / ratio).quantize(PRICE_STEP)
            fill = ledger.apply_fill(
                idem_key=f"ca:{a.id}:lieu", asset=a.symbol, side="SELL", quantity=fraction, price=price,
                quote_source="cash_in_lieu", origin=ORIGIN, ref=a.id, now=now, day=day,
                reason=f"cash in lieu of {_shares(fraction)} share after the {_ratio_text(a)} split, "
                       f"at the last close restated to the new shares (${price})")
            note += f"; {_shares(fraction)} share paid as cash in lieu, ${fill.notional}"
        return note
    if a.kind in CASH_OUT:
        rate = _number(a, "rate")
        detail = {"rate": str(rate)}
        if held <= 0:
            ledger.record_corporate(a.id, a.kind, a.symbol, day, "not_held", ZERO, detail, now)
            return None
        _unchanged_since(ledger, a, held)
        ledger.record_corporate(a.id, a.kind, a.symbol, day, "applied", held, detail, now)
        fill = ledger.apply_fill(
            idem_key=f"ca:{a.id}", asset=a.symbol, side="SELL", quantity=held, price=rate,
            quote_source="cash_merger", origin=ORIGIN, ref=a.id, now=now, day=day,
            reason=f"cash merger effective {day}: ${rate} a share")
        return (f"cash merger effective {day}: {_shares(held)} shares became ${fill.notional} "
                f"(${rate}/share), realized ${fill.realized_pnl:+}")
    ledger.record_corporate(a.id, a.kind, a.symbol, day, "unmodeled" if held > 0 else "not_held", held, {}, now)
    if held <= 0:
        return None
    return (f"{a.describe()}: held {_shares(held)} shares through an action the book can't model, so it "
            "may no longer match the account. Sell it, or fix the book by hand")


def _verify(ledger: Ledger) -> None:
    problems = ledger.check()
    if problems:
        raise LedgerError("invariant violated: " + "; ".join(problems))


def _settle(store: Store, ledger: Ledger, today: date, now: float, out: Applied) -> None:
    """Follow every owed dividend: corrected rates, withdrawals, and payment on its date."""
    for d in ledger.dividends("owed"):
        aid, sym = d["action_id"], d["symbol"]
        try:
            with store.tx():
                got = events_db.get_action(store, aid)
                if got is None or got[1] is not None:
                    amount = ledger.cancel_dividend(aid)
                    out.notes.append((sym, f"the ${amount} dividend owed ({aid}) was withdrawn by the source: cancelled"))
                else:
                    a = got[0]
                    rate = _number(a, "rate")
                    if rate != d["rate"]:
                        old, new = ledger.restate_dividend(aid, rate)
                        out.notes.append((sym, f"dividend {aid} corrected to ${rate}/share: ${old} -> ${new} owed"))
                    pay = a.pay or a.process
                    if pay is not None and pay <= today:
                        amount = ledger.pay_dividend(aid, pay, now)
                        out.notes.append((sym, f"{a.kind.replace('_', ' ')} paid: ${amount}"))
                _verify(ledger)
        except (CorporateActionError, LedgerError) as e:
            out.failed[aid] = f"{sym} dividend {aid}: {e}"


def apply_due(store: Store, now: float, fractionable: Callable[[str], bool],
              last_close: Callable[[str, date], Optional[float]]) -> Applied:
    """Apply every action in effect, then settle the dividends owed. Safe to call on every
    tick: each action is recorded once, and a failure is retried on the next call.

    `fractionable(symbol)`: whether the broker keeps fractional shares of it (else a split's
    fraction is cash in lieu). `last_close(symbol, day)`: the last official close before
    `day`, raw (prices the cash in lieu)."""
    out = Applied()
    ledger = Ledger(store)
    if not ledger.initialized():
        return out
    today = trade_day(now)
    for a in due_actions(store, today):
        try:
            with store.tx():
                note = _apply(ledger, a, now, fractionable, last_close)
                _verify(ledger)
        except (CorporateActionError, LedgerError) as e:
            out.failed[a.id] = f"{a.symbol} {a.describe()}: {e}"
            continue
        out.recorded += 1
        if note:
            out.notes.append((a.symbol, note))
    _settle(store, ledger, today, now, out)
    if (store.get_state(FAILURES) or {}) != out.failed:
        store.set_state(FAILURES, out.failed)
    for s, n in out.notes:
        logger.info("corporate action, %s: %s", s, n)
    for why in out.failed.values():
        logger.error("corporate action not applied: %s", why)
    return out


# -- restating prices -------------------------------------------------------------------
def restatements(store: Store, symbols: Iterable[str], after: date) -> Dict[str, List[Tuple[str, Decimal]]]:
    """symbol -> what the recorded actions effective after `after` did to its price basis,
    in order: ("income", amount a share) or ("split", ratio)."""
    want = set(symbols)
    found: Dict[str, List[Tuple[Tuple, str, Decimal]]] = {}
    for aid, kind, sym, day, detail in store.conn.execute(
            "SELECT action_id, kind, symbol, day, detail FROM corporate_entries WHERE day>?", (after.isoformat(),)):
        if sym not in want:
            continue
        d = json.loads(detail)
        if kind in INCOME and d.get("rate"):
            change = ("income", D(d["rate"]))
        elif kind in SPLITS and d.get("ratio"):
            change = ("split", D(d["ratio"]))
        else:
            continue
        found.setdefault(sym, []).append(((day, _ORDER.get(kind, 3), aid), *change))
    return {s: [(k, v) for _, k, v in sorted(rows)] for s, rows in found.items()}


def restate(price: float, changes: Iterable[Tuple[str, Decimal]]) -> float:
    """A price from before `changes` in today's share basis: less each dividend that went
    ex since, divided by each split since."""
    for kind, v in changes:
        price = price - float(v) if kind == "income" else price / float(v)
    return price


def restate_marks(store: Store, marks: Mapping[str, Quote], since: date) -> Dict[str, Quote]:
    """Marks taken from the close of `since` (the market is shut), restated by the actions
    recorded after it. Why: from an ex-date's midnight the book holds the receivable (or
    the split's new shares), so the old close would count the dividend twice (or value
    the new shares at the old price) until the open."""
    out = dict(marks)
    for s, changes in restatements(store, marks, since).items():
        q = out[s]
        out[s] = dataclasses.replace(q, bid=restate(q.bid, changes), ask=restate(q.ask, changes),
                                     last=restate(q.last, changes))
    return out


def split_ratios(store: Store, after: date, through: date) -> Dict[str, float]:
    """symbol -> the share multiple of the known splits effective in (after, through]: on
    a split's ex-date the previous close is in the old shares, so a quote that far off it
    is the split, not a bad quote."""
    out: Dict[str, float] = {}
    marks = ",".join("?" * len(SPLITS))
    for r in store.conn.execute(
            f"SELECT {events_db.ACTION_COLS} FROM corporate_actions WHERE withdrawn_ts IS NULL AND kind IN ({marks}) "
            "AND effective_day>? AND effective_day<=?", (*sorted(SPLITS), after.isoformat(), through.isoformat())):
        a = events_db.action_from_row(r)
        try:
            out[a.symbol] = out.get(a.symbol, 1.0) * float(split_ratio(a))
        except CorporateActionError:
            continue
    return out


# -- what the desk must do about actions it can't model ---------------------------------
def exits_due(store: Store, cal: TradingCalendar, today: date, held: Iterable[str]) -> Dict[str, Tuple[str, str]]:
    """Holdings to sell now, ahead of an action the book can't model: asset -> (action id,
    why). Due from the last session before it takes effect, or at once when its date is
    unknown (fail closed)."""
    out: Dict[str, Tuple[str, str]] = {}
    for s in sorted(set(held)):
        for a in events_db.pending_actions(store, s, today):
            if a.modeled:
                continue
            if a.effective is None or sessions_until(cal, today, a.effective) <= 1:
                out[s] = (a.id, f"{a.describe()}, which the book can't model: exiting the session before")
                break
    return out


def _due_since(a: CorporateAction, first_seen: float) -> float:
    start = datetime.combine(a.effective, dtime(0), tzinfo=ET).timestamp() if a.effective else first_seen
    return max(start, first_seen)


def problems(store: Store, now: float) -> List[Tuple[str, bool, str]]:
    """(alarm key, critical, message) for every corporate action that needs a person."""
    ledger = Ledger(store)
    if not ledger.initialized():
        return []
    today = trade_day(now)
    positions = ledger.positions()
    failures = store.get_state(FAILURES) or {}
    out: List[Tuple[str, bool, str]] = []
    seen = {r[0]: r[1] for r in store.conn.execute("SELECT id, first_seen_ts FROM corporate_actions")}
    for a in due_actions(store, today):
        if a.id in failures:
            out.append((f"corporate:{a.id}", True, f"corporate action not applied: {failures[a.id]}. Trading in "
                        f"{a.symbol} waits until it is"))
        elif now - _due_since(a, seen.get(a.id, now)) > UNAPPLIED_GRACE_SEC:
            out.append((f"corporate:{a.id}", True, f"{a.symbol} {a.describe()} is in effect but not applied to "
                        "the book (is the engine running?)"))
    for aid, why in failures.items():
        if not any(k == f"corporate:{aid}" for k, _, _ in out):
            out.append((f"corporate:{aid}", True, f"corporate action not settled: {why}"))
    for s, p in positions.items():
        for a in events_db.pending_actions(store, s, today):
            if not a.modeled:
                out.append((f"corporate:{a.id}", False, f"{s}: {a.describe()} can't be modeled. BUYs are "
                            "blocked and the risk monitor sells it the session before"))
    for e in ledger.corporate_entries():
        if e["outcome"] == "unmodeled":
            p = positions.get(e["symbol"])
            if p is not None and p.opened_ts <= e["ts"]:
                out.append((f"corporate:{e['action_id']}", True,
                            f"{e['symbol']} was held through a {e['kind'].replace('_', ' ')} on {e['day']}, which "
                            "the book can't model: it may not match the account. Sell it or fix it by hand"))
        elif e["outcome"] == "applied":
            got = events_db.get_action(store, e["action_id"])
            if got is None or got[1] is not None:
                change = "withdrawn"
            else:
                try:
                    now_is = split_ratio(got[0]) if e["kind"] in SPLITS else _number(got[0], "rate")
                    same = str(now_is) == e["detail"].get("ratio" if e["kind"] in SPLITS else "rate")
                except CorporateActionError:
                    same = False
                if same:
                    continue
                change = "changed (new terms)"
            out.append((f"corporate:{e['action_id']}", True,
                        f"{e['symbol']} {e['kind'].replace('_', ' ')} on {e['day']} was applied to the book, then "
                        f"{change} by the source: check the book by hand"))
    return out
