"""
Ledger: cash, FIFO tax lots and fills, with exact Decimal accounting in cents.

What it does: applies paper fills (inside the caller's transaction). A BUY opens a lot;
a SELL consumes the asset's open lots oldest first (FIFO) and records, per lot touched,
the proceeds and cost allocated to it, the realized P&L and whether the gain is short or
long term. The `positions` row per asset is the sum of its open lots, kept in step in the
same transaction. `check()` rebuilds cash, every lot and every position from the event
log (deposits + fills, in order) and reports any difference.

Conventions, chosen to match how a US broker reports a taxable account:
- Cash, cost, proceeds and fees are in cents. Quantities go to 1e-9 shares, the
  precision Alpaca uses for fractional shares.
- Fees charged at the fill (a commission): BUY fees are added to the lot's cost basis;
  SELL fees reduce the proceeds.
- Regulatory fees (SEC, TAF, CAT) are accrued per fill, exactly, and charged once per
  trading day, rounded up to the cent, as a 'fee' cash event: Alpaca's end-of-day charge.
  They are account costs, not part of any lot, so they reduce cash and equity but not a
  lot's basis or a closure's proceeds.
- Lots are consumed FIFO (the IRS default without specific identification). A partial
  lot keeps its remaining cost exactly: the consumed part's cost is rounded to the cent and
  the remainder is what's left, so a lot's pieces always add back to its total.
- A sale's net proceeds are split across lots by quantity, and the last lot takes the
  rounding remainder, so the pieces always add up to the fill exactly.
- Long term means sold after the one-year anniversary of the purchase date (held more
  than a year). Wash sales, which can move basis and holding period between lots, come in
  the tax PR and adjust these lots rather than replace them.
- Holdings are marked at the BID: what the position could actually be sold for.
- Corporate actions (`ledger/corporate.py` decides; the methods here book): a split
  multiplies each open lot's remaining shares (cost and holding period unchanged), one
  `lot_adjustments` row per lot, which `check()` replays in sequence with the fills; a
  dividend is owed from its ex-date (a receivable, counted in equity) and paid as cash on
  its pay date; cash in lieu and cash mergers are SELL fills with origin
  `corporate_action`, so lots, P&L and tax follow a sale's rules.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal, getcontext
from typing import Dict, List, Mapping, Optional, Tuple

from ottr.broker.fees import charge_for_day
from ottr.ledger.store import Store, dumps
from ottr.market.calendar import ET
from ottr.market.types import Quote

getcontext().prec = 34
USD = Decimal("0.01")
QTY = Decimal("0.000000001")      # 1e-9 shares: Alpaca's fractional precision
ZERO = Decimal(0)
# Cash event kinds that move cash. Others (deposit_in_kind) record holdings only.
CASH_KINDS = frozenset({"deposit", "fee", "dividend"})
EVENT_KINDS = CASH_KINDS | {"deposit_in_kind"}


def D(x) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def usd(x) -> Decimal:
    return D(x).quantize(USD, rounding=ROUND_HALF_EVEN)


def qty(x) -> Decimal:
    return D(x).quantize(QTY, rounding=ROUND_HALF_EVEN)


def trade_day(ts: float) -> date:
    """The trade date of an instant: the New York calendar date, as a broker reports it."""
    return datetime.fromtimestamp(ts, tz=ET).date()


def anniversary(d: date) -> date:
    """The same date one year later (Feb 29 maps to Feb 28)."""
    try:
        return d.replace(year=d.year + 1)
    except ValueError:
        return d.replace(year=d.year + 1, day=28)


def holding_term(opened: date, sold: date) -> str:
    """'long' once the asset is sold after its one-year anniversary, else 'short'."""
    return "long" if sold > anniversary(opened) else "short"


class LedgerError(RuntimeError):
    pass


@dataclass(frozen=True)
class Position:
    asset: str
    qty: Decimal
    cost: Decimal
    high_water: float
    opened_ts: float
    stop_pct: Optional[float] = None    # trailing-stop distance, a fraction; None: no stop (yet)

    @property
    def avg_cost(self) -> Decimal:
        return self.cost / self.qty if self.qty > 0 else Decimal(0)

    @property
    def stop_price(self) -> Optional[float]:
        """The trailing stop: the high-water mark less the stop distance. It only ever rises,
        because the high-water mark only rises and the distance never changes."""
        return self.high_water * (1 - self.stop_pct) if self.stop_pct else None


@dataclass(frozen=True)
class Lot:
    id: int
    asset: str
    open_fill_id: Optional[int]       # None: brought in at `ottr init`
    opened_ts: float
    opened_day: date
    qty: Decimal
    cost: Decimal
    qty_remaining: Decimal
    cost_remaining: Decimal
    closed_ts: Optional[float]

    @property
    def is_open(self) -> bool:
        return self.qty_remaining > 0


@dataclass(frozen=True)
class Closure:
    lot_id: int
    fill_id: int
    day: date
    qty: Decimal
    proceeds: Decimal
    cost: Decimal
    realized: Decimal
    term: str


@dataclass(frozen=True)
class Fill:
    idem_key: str
    asset: str
    side: str
    qty: Decimal
    price: Decimal
    notional: Decimal
    fee: Decimal
    realized_pnl: Decimal
    quote_source: str
    ts: float
    day: date
    id: int
    closures: Tuple[Closure, ...]
    accrued: Mapping[str, Decimal]          # regulatory fees owed, charged at the day's close


@dataclass(frozen=True)
class Valuation:
    equity: float
    cash: float
    invested: float
    weights: Dict[str, float]
    values: Dict[str, float]
    receivable: float = 0.0     # dividends owed (past the ex-date, not yet paid)


def _lot(r) -> Lot:
    return Lot(r[0], r[1], r[2], r[3], date.fromisoformat(r[4]), D(r[5]), D(r[6]), D(r[7]), D(r[8]), r[9])


_LOT_COLS = "id,asset,open_fill_id,opened_ts,opened_day,qty,cost,qty_remaining,cost_remaining,closed_ts"


class Ledger:
    def __init__(self, store: Store) -> None:
        self.store = store

    # -- reads ----------------------------------------------------------------------------
    def initialized(self) -> bool:
        return self.store.conn.execute("SELECT 1 FROM account WHERE id=1").fetchone() is not None

    def cash(self) -> Decimal:
        r = self.store.conn.execute("SELECT cash FROM account WHERE id=1").fetchone()
        if r is None:
            raise LedgerError("ledger not initialized (run `ottr init`)")
        return D(r[0])

    def account(self) -> Dict[str, Decimal]:
        r = self.store.conn.execute("SELECT cash,realized_pnl,fees FROM account WHERE id=1").fetchone()
        if r is None:
            raise LedgerError("ledger not initialized (run `ottr init`)")
        return {"cash": D(r[0]), "realized_pnl": D(r[1]), "fees": D(r[2])}

    def positions(self) -> Dict[str, Position]:
        return {r[0]: Position(r[0], D(r[1]), D(r[2]), r[3], r[4], r[5]) for r in self.store.conn.execute(
            "SELECT asset,qty,cost,high_water,opened_ts,stop_pct FROM positions ORDER BY asset")}

    def lots(self, asset: Optional[str] = None, open_only: bool = True) -> List[Lot]:
        """Lots oldest first: the order a SELL consumes them in."""
        q = f"SELECT {_LOT_COLS} FROM lots WHERE 1=1"
        args: list = []
        if asset is not None:
            q += " AND asset=?"
            args.append(asset)
        if open_only:
            q += " AND closed_ts IS NULL"
        return [_lot(r) for r in self.store.conn.execute(q + " ORDER BY id", args)]

    def closures(self, fill_id: Optional[int] = None) -> List[Closure]:
        q = "SELECT lot_id,fill_id,day,qty,proceeds,cost,realized,term FROM lot_closures"
        rows = (self.store.conn.execute(q + " WHERE fill_id=? ORDER BY id", (fill_id,)) if fill_id is not None
                else self.store.conn.execute(q + " ORDER BY id"))
        return [Closure(r[0], r[1], date.fromisoformat(r[2]), D(r[3]), D(r[4]), D(r[5]), D(r[6]), r[7]) for r in rows]

    def value(self, quotes: Mapping[str, Quote]) -> Optional[Valuation]:
        """Mark to the bid. Returns None if any held asset has no quote (unknown, not 0).
        Dividends owed count: from the ex-date the price no longer includes them."""
        cash = float(self.cash())
        values: Dict[str, float] = {}
        for a, p in self.positions().items():
            q = quotes.get(a)
            if q is None:
                return None
            values[a] = float(p.qty) * q.bid
        invested = sum(values.values())
        owed = float(self.receivable())
        equity = cash + invested + owed
        weights = {a: (v / equity if equity > 0 else 0.0) for a, v in values.items()}
        return Valuation(equity=equity, cash=cash, invested=invested, weights=weights, values=values,
                         receivable=owed)

    def receivable(self) -> Decimal:
        """Dividends owed: past their ex-date, not yet paid."""
        return sum((D(r[0]) for r in self.store.conn.execute("SELECT amount FROM dividends WHERE status='owed'")),
                   ZERO)

    def held_before(self, asset: str, day: date) -> Decimal:
        """Shares of `asset` held at the end of the last trade day before `day`, replayed
        from the log (deposits in kind, fills, split adjustments): what a dividend going ex
        on `day` is paid on, whenever it is asked."""
        d, c = day.isoformat(), self.store.conn
        n = sum((D(r[0]) for r in c.execute(
            "SELECT qty FROM cash_events WHERE kind='deposit_in_kind' AND asset=? AND day<?", (asset, d))), ZERO)
        for side, q in c.execute("SELECT side, qty FROM fills WHERE asset=? AND day<?", (asset, d)):
            n += D(q) if side == "BUY" else -D(q)
        for before, after in c.execute(
                "SELECT a.qty_before, a.qty_after FROM lot_adjustments a JOIN corporate_entries e "
                "ON e.action_id=a.action_id WHERE e.symbol=? AND e.day<?", (asset, d)):
            n += D(after) - D(before)
        return n

    def corporate_entries(self, symbol: Optional[str] = None) -> List[Dict[str, object]]:
        q = "SELECT action_id,kind,symbol,day,ts,outcome,qty,after_fill,detail FROM corporate_entries"
        rows = (self.store.conn.execute(q + " WHERE symbol=? ORDER BY day, action_id", (symbol,)) if symbol
                else self.store.conn.execute(q + " ORDER BY day, action_id"))
        cols = ("action_id", "kind", "symbol", "day", "ts", "outcome", "qty", "after_fill", "detail")
        return [{**dict(zip(cols, r)), "qty": D(r[6]), "day": date.fromisoformat(r[3]), "detail": json.loads(r[8])}
                for r in rows]

    def dividends(self, status: Optional[str] = None) -> List[Dict[str, object]]:
        q = "SELECT action_id,symbol,kind,ex_day,qty,rate,amount,status,cash_event_id FROM dividends"
        rows = (self.store.conn.execute(q + " WHERE status=? ORDER BY ex_day, action_id", (status,)) if status
                else self.store.conn.execute(q + " ORDER BY ex_day, action_id"))
        return [{"action_id": r[0], "symbol": r[1], "kind": r[2], "ex_day": date.fromisoformat(r[3]),
                 "qty": D(r[4]), "rate": D(r[5]), "amount": D(r[6]), "status": r[7], "cash_event_id": r[8]}
                for r in rows]

    # -- setup ----------------------------------------------------------------------------
    def initialize(self, cash, note: str = "initial deposit",
                   holdings: Optional[Mapping[str, Tuple[Decimal, float]]] = None,
                   now: Optional[float] = None) -> None:
        """Open the book once. `holdings` = {asset: (qty, mark_price)}: each becomes a lot
        whose cost is its value at that mark, so the book starts with no invented P&L."""
        if self.initialized():
            raise LedgerError("ledger already initialized")
        now = time.time() if now is None else now
        day = trade_day(now).isoformat()
        cash = usd(cash)
        if cash < 0:
            raise LedgerError("starting cash can't be negative")
        with self.store.tx() as c:
            c.execute("INSERT INTO account(id,cash,realized_pnl,fees) VALUES(1,?,?,?)", (str(cash), "0.00", "0.00"))
            c.execute("INSERT INTO cash_events(ts,day,kind,amount,note) VALUES(?,?,?,?,?)",
                      (now, day, "deposit", str(cash), note))
            for asset, (q, mark) in (holdings or {}).items():
                q = qty(q)
                if q <= 0:
                    continue
                cost = usd(q * D(mark))
                c.execute("INSERT INTO cash_events(ts,day,kind,asset,qty,amount,note) VALUES(?,?,?,?,?,?,?)",
                          (now, day, "deposit_in_kind", asset, str(q), str(cost), f"{note} @ {mark}"))
                c.execute(f"INSERT INTO lots({_LOT_COLS}) VALUES(NULL,?,NULL,?,?,?,?,?,?,NULL)",
                          (asset, now, day, str(q), str(cost), str(q), str(cost)))
                c.execute("INSERT INTO positions(asset,qty,cost,high_water,opened_ts) VALUES(?,?,?,?,?)",
                          (asset, str(q), str(cost), float(mark), now))
            self.store.audit("ledger_initialized", cash=cash,
                             holdings={a: str(h[0]) for a, h in (holdings or {}).items()})

    # -- writes (call inside store.tx()) -------------------------------------------------
    def apply_fill(self, *, idem_key: str, asset: str, side: str, quantity, price,
                   quote_source: str, origin: str, fee_pct: float = 0.0,
                   fees: Optional[Mapping[str, object]] = None,
                   accrued: Optional[Mapping[str, object]] = None, quote: Optional[Quote] = None,
                   ref: str = "", reason: str = "", now: Optional[float] = None,
                   day: Optional[date] = None) -> Fill:
        """Book one fill. Raises LedgerError (rolling back the caller's tx) on any violation.

        Fees: either `fees`, a breakdown in dollars ({"sec": 0.0021, "taf": 0.000195}) whose
        total is charged rounded to the cent, or `fee_pct` of the notional. The breakdown is
        stored with the fill either way. `accrued` is the regulatory fees the fill owes
        (exact, from broker.fees); they are not charged now but by `charge_fees` at the close.
        `day` is the trade date when it isn't now's: a corporate action booked late (the
        engine was down) is dated when it took effect.
        """
        if not self.store._depth:
            raise LedgerError("apply_fill must run inside store.tx() so the fill commits atomically")
        side = side.upper()
        if side not in ("BUY", "SELL"):
            raise LedgerError(f"bad side {side}")
        q, px = qty(quantity), D(price)
        if q <= 0 or px <= 0:
            raise LedgerError("fill needs positive quantity and price")
        now = time.time() if now is None else now
        day = day or trade_day(now)
        notional = usd(q * px)
        if fees is not None:
            detail = {k: D(v) for k, v in fees.items()}
            if any(v < 0 for v in detail.values()):
                raise LedgerError("fees can't be negative")
            fee = usd(sum(detail.values(), ZERO))
        else:
            fee = usd(notional * D(fee_pct) / 100)
            detail = {"fee": fee}
        owed = {k: D(v) for k, v in (accrued or {}).items()}
        if any(v < 0 for v in owed.values()):
            raise LedgerError("accrued fees can't be negative")
        acct = self.account()
        pos = self.positions().get(asset)
        quote_json = dumps({"bid": quote.bid, "ask": quote.ask, "last": quote.last, "ts": quote.ts,
                            "source": quote.source, "basis": quote.basis}) if quote else None
        c = self.store.conn

        def insert_fill(realized: Decimal) -> int:
            cur = c.execute(
                "INSERT INTO fills(ts,day,idem_key,asset,side,qty,price,notional,fee,fee_detail,realized_pnl,"
                "quote_source,quote,origin,ref,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (now, day.isoformat(), idem_key, asset, side, str(q), str(px), str(notional), str(fee),
                 dumps(detail), str(realized), quote_source, quote_json, origin, ref, reason[:500]))
            return cur.lastrowid

        closures: List[Closure] = []
        if side == "BUY":
            total = notional + fee
            if total > acct["cash"]:
                raise LedgerError(f"insufficient cash: need {total}, have {acct['cash']}")
            new_cash, realized = acct["cash"] - total, ZERO
            fill_id = insert_fill(realized)
            c.execute(f"INSERT INTO lots({_LOT_COLS}) VALUES(NULL,?,?,?,?,?,?,?,?,NULL)",
                      (asset, fill_id, now, day.isoformat(), str(q), str(total), str(q), str(total)))
            if pos:
                c.execute("UPDATE positions SET qty=?, cost=? WHERE asset=?",
                          (str(qty(pos.qty + q)), str(usd(pos.cost + total)), asset))
            else:
                c.execute("INSERT INTO positions(asset,qty,cost,high_water,opened_ts) VALUES(?,?,?,?,?)",
                          (asset, str(q), str(total), float(px), now))
        else:
            if pos is None or q > pos.qty:
                raise LedgerError(f"cannot sell {q} {asset}: holding {pos.qty if pos else 0}")
            net = notional - fee
            if net < 0:
                raise LedgerError(f"fees {fee} exceed the sale's notional {notional}")
            plan = self._consume_fifo(asset, q, net, day)
            realized = sum((r for *_, r, _ in plan), ZERO)
            new_cash = acct["cash"] + net
            fill_id = insert_fill(realized)
            cost_out = ZERO
            for lot, take, proceeds, cost, gain, term in plan:
                c.execute("INSERT INTO lot_closures(lot_id,fill_id,day,qty,proceeds,cost,realized,term) "
                          "VALUES(?,?,?,?,?,?,?,?)",
                          (lot.id, fill_id, day.isoformat(), str(take), str(proceeds), str(cost), str(gain), term))
                left_q, left_c = lot.qty_remaining - take, lot.cost_remaining - cost
                c.execute("UPDATE lots SET qty_remaining=?, cost_remaining=?, closed_ts=? WHERE id=?",
                          (str(left_q), str(left_c), now if left_q == 0 else None, lot.id))
                closures.append(Closure(lot.id, fill_id, day, take, proceeds, cost, gain, term))
                cost_out += cost
            left_qty = pos.qty - q
            if left_qty == 0:
                c.execute("DELETE FROM positions WHERE asset=?", (asset,))
            else:
                c.execute("UPDATE positions SET qty=?, cost=? WHERE asset=?",
                          (str(left_qty), str(pos.cost - cost_out), asset))

        c.execute("UPDATE account SET cash=?, realized_pnl=?, fees=? WHERE id=1",
                  (str(new_cash), str(acct["realized_pnl"] + realized), str(acct["fees"] + fee)))
        for name, amount in owed.items():
            c.execute("INSERT INTO fee_accruals(fill_id,day,fee,amount) VALUES(?,?,?,?)",
                      (fill_id, day.isoformat(), name, str(amount)))
        fill = Fill(idem_key, asset, side, q, px, notional, fee, realized, quote_source, now, day,
                    fill_id, tuple(closures), owed)
        self.store.audit("fill", idem_key=idem_key, asset=asset, side=side, qty=q, price=px,
                         notional=notional, fee=fee, realized_pnl=realized, origin=origin)
        problems = self.check()
        if problems:
            raise LedgerError("invariant violated: " + "; ".join(problems))
        return fill

    def _consume_fifo(self, asset: str, q: Decimal, net: Decimal, day: date):
        """Split a sale across the open lots, oldest first -> [(lot, qty, proceeds, cost, realized, term)]."""
        lots = self.lots(asset)
        left, takes = q, []
        for lot in lots:
            if left == 0:
                break
            take = min(left, lot.qty_remaining)
            takes.append((lot, take))
            left -= take
        if left > 0:
            raise LedgerError(f"{asset}: open lots hold less than the position ({left} short)")
        plan, proceeds_so_far = [], ZERO
        for i, (lot, take) in enumerate(takes):
            cost = lot.cost_remaining if take == lot.qty_remaining else usd(lot.cost_remaining * take / lot.qty_remaining)
            proceeds = net - proceeds_so_far if i == len(takes) - 1 else usd(net * take / q)
            proceeds_so_far += proceeds
            plan.append((lot, take, proceeds, cost, proceeds - cost, holding_term(lot.opened_day, day)))
        return plan

    # -- regulatory fees: accrued per fill, charged per day ------------------------------
    def pending_fees(self) -> Decimal:
        """What the uncharged accruals will cost when charged: each day's total rounded up.
        Buying power must hold this back, the way a broker deducts pending fees intraday."""
        days: Dict[str, List[Decimal]] = {}
        for day, amount in self.store.conn.execute("SELECT day, amount FROM fee_accruals WHERE charged=0"):
            days.setdefault(day, []).append(D(amount))
        return sum((charge_for_day(v) for v in days.values()), ZERO)

    def charge_fees(self, through: date, now: Optional[float] = None) -> List[Tuple[date, Decimal]]:
        """Charge every uncharged trading day up to `through`, one 'fee' cash event per day.

        Each day is charged separately, rounded up to the cent, so an engine that missed a
        close catches up without merging days (merged days would round differently).
        Returns [(day, amount charged)].
        """
        now = time.time() if now is None else now
        done: List[Tuple[date, Decimal]] = []
        with self.store.tx() as c:
            rows = c.execute("SELECT id, day, fee, amount FROM fee_accruals WHERE charged=0 AND day<=? "
                             "ORDER BY day, id", (through.isoformat(),)).fetchall()
            by_day: Dict[str, list] = {}
            for r in rows:
                by_day.setdefault(r[1], []).append(r)
            for day, accruals in by_day.items():
                amount = charge_for_day(D(r[3]) for r in accruals)
                event_id = None
                if amount > 0:
                    acct = self.account()
                    if amount > acct["cash"]:
                        raise LedgerError(f"fees of {amount} for {day} exceed cash {acct['cash']}")
                    detail: Dict[str, Decimal] = {}
                    for r in accruals:
                        detail[r[2]] = detail.get(r[2], ZERO) + D(r[3])
                    event_id = c.execute(
                        "INSERT INTO cash_events(ts,day,kind,amount,note) VALUES(?,?,?,?,?)",
                        (now, day, "fee", str(-amount), dumps(detail))).lastrowid
                    c.execute("UPDATE account SET cash=?, fees=? WHERE id=1",
                              (str(acct["cash"] - amount), str(acct["fees"] + amount)))
                c.executemany("UPDATE fee_accruals SET charged=1, charge_event_id=? WHERE id=?",
                              [(event_id, r[0]) for r in accruals])
                done.append((date.fromisoformat(day), amount))
            if done:
                self.store.audit("fees_charged", days={d.isoformat(): a for d, a in done})
                problems = self.check()
                if problems:
                    raise LedgerError("invariant violated: " + "; ".join(problems))
        return done

    def set_high_water(self, asset: str, price: float) -> None:
        with self.store.tx() as c:
            c.execute("UPDATE positions SET high_water=MAX(high_water, ?) WHERE asset=?", (price, asset))

    def set_stop(self, asset: str, pct: float) -> bool:
        """Give an open position its trailing-stop distance, once: a stop that is already set
        is never changed, so it can't be loosened later. True if this call set it."""
        if not 0 < pct < 1:
            raise LedgerError(f"stop distance {pct!r} for {asset} must be a fraction in (0, 1)")
        with self.store.tx() as c:
            return c.execute("UPDATE positions SET stop_pct=? WHERE asset=? AND stop_pct IS NULL",
                             (pct, asset)).rowcount == 1

    # -- corporate actions (ledger/corporate.py decides; call inside store.tx(), then check()) --
    def _in_tx(self, what: str) -> None:
        if not self.store._depth:
            raise LedgerError(f"{what} must run inside store.tx() so the action commits atomically")

    def record_corporate(self, action_id: str, kind: str, symbol: str, day: date, outcome: str,
                         held: Decimal, detail: Mapping[str, object], now: float) -> None:
        """Record what was done about one action, once. Its `after_fill` is the newest fill
        now, so split adjustments written after this replay right after that fill."""
        self._in_tx("record_corporate")
        c = self.store.conn
        if c.execute("SELECT 1 FROM corporate_entries WHERE action_id=?", (action_id,)).fetchone():
            raise LedgerError(f"corporate action {action_id} is already recorded")
        after = c.execute("SELECT COALESCE(MAX(id), 0) FROM fills").fetchone()[0]
        c.execute("INSERT INTO corporate_entries(action_id,kind,symbol,day,ts,outcome,qty,after_fill,detail) "
                  "VALUES(?,?,?,?,?,?,?,?,?)",
                  (action_id, kind, symbol, day.isoformat(), now, outcome, str(held), after, dumps(dict(detail))))
        self.store.audit("corporate_action", action_id=action_id, action=kind, symbol=symbol, day=day.isoformat(),
                         outcome=outcome, qty=held, detail=dict(detail))

    def split_lots(self, action_id: str, asset: str, ratio: Decimal) -> Tuple[Decimal, Decimal]:
        """Multiply each open lot of `asset` by `ratio` (shares to 1e-9; cost unchanged) and
        divide the high-water mark by it, so the stop moves with the price. The action must
        be recorded first. Returns the position's (shares before, shares after)."""
        self._in_tx("split_lots")
        if ratio <= 0:
            raise LedgerError(f"split ratio {ratio} for {asset} must be positive")
        pos = self.positions().get(asset)
        if pos is None:
            raise LedgerError(f"no {asset} position to split")
        c = self.store.conn
        if c.execute("SELECT 1 FROM corporate_entries WHERE action_id=?", (action_id,)).fetchone() is None:
            raise LedgerError(f"split {action_id} must be recorded before its lots are adjusted")
        total = ZERO
        for lot in self.lots(asset):
            new = qty(lot.qty_remaining * ratio)
            if new <= 0:
                raise LedgerError(f"lot {lot.id}: {lot.qty_remaining} shares x {ratio} rounds to nothing")
            c.execute("UPDATE lots SET qty_remaining=? WHERE id=?", (str(new), lot.id))
            c.execute("INSERT INTO lot_adjustments(action_id,lot_id,qty_before,qty_after) VALUES(?,?,?,?)",
                      (action_id, lot.id, str(lot.qty_remaining), str(new)))
            total += new
        c.execute("UPDATE positions SET qty=?, high_water=? WHERE asset=?",
                  (str(total), pos.high_water / float(ratio), asset))
        return pos.qty, total

    def owe_dividend(self, action_id: str, symbol: str, kind: str, ex_day: date, held: Decimal,
                     rate: Decimal) -> Decimal:
        """A dividend owed on `held` shares from its ex-date. Returns the amount (cents)."""
        self._in_tx("owe_dividend")
        if held <= 0 or rate <= 0:
            raise LedgerError(f"dividend {action_id}: {held} shares at {rate} owes nothing")
        amount = usd(held * rate)
        self.store.conn.execute(
            "INSERT INTO dividends(action_id,symbol,kind,ex_day,qty,rate,amount,status) VALUES(?,?,?,?,?,?,?,'owed')",
            (action_id, symbol, kind, ex_day.isoformat(), str(held), str(rate), str(amount)))
        return amount

    def restate_dividend(self, action_id: str, rate: Decimal) -> Tuple[Decimal, Decimal]:
        """Follow a rate the source corrected before payment. Returns (old, new) amounts."""
        self._in_tx("restate_dividend")
        r = self.store.conn.execute("SELECT qty, amount FROM dividends WHERE action_id=? AND status='owed'",
                                    (action_id,)).fetchone()
        if r is None or rate <= 0:
            raise LedgerError(f"dividend {action_id} isn't owed (or rate {rate} is bad)")
        amount = usd(D(r[0]) * rate)
        self.store.conn.execute("UPDATE dividends SET rate=?, amount=? WHERE action_id=?",
                                (str(rate), str(amount), action_id))
        return D(r[1]), amount

    def pay_dividend(self, action_id: str, pay_day: date, now: float) -> Decimal:
        """Turn an owed dividend into cash, as a 'dividend' cash event dated its pay date."""
        self._in_tx("pay_dividend")
        c = self.store.conn
        r = c.execute("SELECT symbol, kind, qty, amount FROM dividends WHERE action_id=? AND status='owed'",
                      (action_id,)).fetchone()
        if r is None:
            raise LedgerError(f"dividend {action_id} isn't owed")
        amount = D(r[3])
        event = c.execute("INSERT INTO cash_events(ts,day,kind,asset,qty,amount,note) VALUES(?,?,?,?,?,?,?)",
                          (now, pay_day.isoformat(), "dividend", r[0], r[2], str(amount),
                           f"{r[1]} {action_id}")).lastrowid
        c.execute("UPDATE account SET cash=? WHERE id=1", (str(self.cash() + amount),))
        c.execute("UPDATE dividends SET status='paid', cash_event_id=? WHERE action_id=?", (event, action_id))
        return amount

    def cancel_dividend(self, action_id: str) -> Decimal:
        """Drop an owed dividend whose action was withdrawn. Returns what was owed."""
        self._in_tx("cancel_dividend")
        r = self.store.conn.execute("SELECT amount FROM dividends WHERE action_id=? AND status='owed'",
                                    (action_id,)).fetchone()
        if r is None:
            raise LedgerError(f"dividend {action_id} isn't owed")
        self.store.conn.execute("UPDATE dividends SET status='cancelled' WHERE action_id=?", (action_id,))
        return D(r[0])

    # -- audit ----------------------------------------------------------------------------
    def check(self) -> List[str]:
        """Rebuild cash, fees, realized P&L, every lot and every position from the event log
        (cash events, then fills in order, with each split's lot adjustments replayed right
        after the fill it followed) and compare them to the stored rows.

        Also verifies the FIFO discipline: each SELL must consume the oldest open lots, in
        order, fully except possibly the last; and that every dividend paid is one cash
        event of exactly the amount owed.
        """
        c = self.store.conn
        problems: List[str] = []
        acct = self.account()
        cash = ZERO
        in_kind: Dict[str, Decimal] = {}
        for kind, asset, q, amount in c.execute("SELECT kind,asset,qty,amount FROM cash_events ORDER BY id"):
            if kind not in EVENT_KINDS:
                problems.append(f"unknown cash event kind {kind!r}")
            elif kind in CASH_KINDS:
                cash += D(amount)
            else:
                in_kind[asset] = in_kind.get(asset, ZERO) + D(q)

        lots = {r[0]: r for r in c.execute(f"SELECT {_LOT_COLS} FROM lots ORDER BY id")}
        by_open_fill: Dict[int, list] = {}
        rem: Dict[int, List[Decimal]] = {}   # lot id -> [qty, cost] replayed
        lot_asset: Dict[int, str] = {}
        for lid, r in lots.items():
            lot_asset[lid] = r[1]
            if r[2] is None:   # brought in at init: open from the start
                rem[lid] = [D(r[5]), D(r[6])]
            else:
                by_open_fill.setdefault(r[2], []).append(lid)
        init_lots: Dict[str, Decimal] = {}
        for lid, r in lots.items():
            if r[2] is None:
                init_lots[r[1]] = init_lots.get(r[1], ZERO) + D(r[5])
        if init_lots != in_kind:
            problems.append(f"init lots {init_lots} != deposit_in_kind events {in_kind}")

        # Split adjustments, by the fill they came after (0: before any fill).
        splits: Dict[int, list] = {}
        for lid, after_fill, before, after in c.execute(
                "SELECT a.lot_id, e.after_fill, a.qty_before, a.qty_after FROM lot_adjustments a "
                "LEFT JOIN corporate_entries e ON e.action_id=a.action_id ORDER BY a.id"):
            if after_fill is None:
                problems.append(f"lot {lid} adjusted by a split that has no corporate entry")
                continue
            splits.setdefault(after_fill, []).append((lid, D(before), D(after)))

        def replay_splits(after_fill: int) -> None:
            for lid, before, after in splits.pop(after_fill, []):
                if lid not in rem or rem[lid][0] <= 0:
                    problems.append(f"lot {lid} adjusted by a split while it wasn't open")
                elif rem[lid][0] != before:
                    problems.append(f"lot {lid}: a split adjusted {before} shares, the replay holds {rem[lid][0]}")
                elif after <= 0:
                    problems.append(f"lot {lid}: a split left {after} shares")
                else:
                    rem[lid][0] = after

        replay_splits(0)
        closures: Dict[int, list] = {}
        realized_all = ZERO
        for row in c.execute("SELECT id,lot_id,fill_id,qty,proceeds,cost,realized FROM lot_closures ORDER BY id"):
            closures.setdefault(row[2], []).append(row)
            realized_all += D(row[6])
            if D(row[4]) - D(row[5]) != D(row[6]):
                problems.append(f"closure {row[0]}: realized {row[6]} != proceeds - cost")

        fees = ZERO
        for fid, side, asset, q, notional, fee, realized in c.execute(
                "SELECT id,side,asset,qty,notional,fee,realized_pnl FROM fills ORDER BY id"):
            q, notional, fee, realized = D(q), D(notional), D(fee), D(realized)
            fees += fee
            opened = by_open_fill.pop(fid, [])
            mine = closures.pop(fid, [])
            if side == "BUY":
                cash -= notional + fee
                if len(opened) != 1:
                    problems.append(f"BUY fill {fid} opened {len(opened)} lots (expected 1)")
                for lid in opened:
                    r = lots[lid]
                    if r[1] != asset or D(r[5]) != q or D(r[6]) != notional + fee:
                        problems.append(f"lot {lid} doesn't match BUY fill {fid}")
                    rem[lid] = [D(r[5]), D(r[6])]
                if mine:
                    problems.append(f"BUY fill {fid} has lot closures")
                replay_splits(fid)
                continue
            cash += notional - fee
            if opened:
                problems.append(f"SELL fill {fid} opened lots")
            open_lots = [lid for lid in sorted(rem) if lot_asset[lid] == asset and rem[lid][0] > 0]
            if sum((D(r[3]) for r in mine), ZERO) != q:
                problems.append(f"SELL fill {fid}: closures cover {sum((D(r[3]) for r in mine), ZERO)}, fill is {q}")
            if sum((D(r[4]) for r in mine), ZERO) != notional - fee:
                problems.append(f"SELL fill {fid}: closure proceeds != net proceeds {notional - fee}")
            if sum((D(r[6]) for r in mine), ZERO) != realized:
                problems.append(f"SELL fill {fid}: closures realize {sum((D(r[6]) for r in mine), ZERO)}, fill says {realized}")
            for k, row in enumerate(mine):
                lid, cq, ccost = row[1], D(row[3]), D(row[5])
                if k >= len(open_lots) or open_lots[k] != lid:
                    problems.append(f"SELL fill {fid} broke FIFO: consumed lot {lid}, oldest open was "
                                    f"{open_lots[k] if k < len(open_lots) else 'none'}")
                    continue
                if k < len(mine) - 1 and cq != rem[lid][0]:
                    problems.append(f"SELL fill {fid} left lot {lid} partly open before moving to a newer lot")
                if cq == rem[lid][0] and ccost != rem[lid][1]:
                    problems.append(f"lot {lid}: fully consumed but cost {ccost} != remaining {rem[lid][1]}")
                rem[lid][0] -= cq
                rem[lid][1] -= ccost
            replay_splits(fid)
        for fid in by_open_fill:
            problems.append(f"lots opened by unknown fill {fid}")
        for fid in closures:
            problems.append(f"closures on unknown fill {fid}")
        for fid in splits:
            problems.append(f"split adjustments after unknown fill {fid}")

        held: Dict[str, List[Decimal]] = {}
        for lid, r in lots.items():
            q_rem, c_rem = rem.get(lid, [None, None])
            if q_rem is None:
                continue
            if q_rem < 0 or c_rem < 0:
                problems.append(f"lot {lid} went negative ({q_rem}, {c_rem})")
            if (D(r[7]), D(r[8])) != (q_rem, c_rem):
                problems.append(f"lot {lid}: stored remaining ({r[7]}, {r[8]}) != replayed ({q_rem}, {c_rem})")
            if (r[9] is None) != (q_rem > 0):
                problems.append(f"lot {lid}: closed_ts doesn't match remaining {q_rem}")
            if q_rem > 0:
                h = held.setdefault(r[1], [ZERO, ZERO])
                h[0] += q_rem
                h[1] += c_rem
        live = {a: (p.qty, p.cost) for a, p in self.positions().items()}
        for a in set(held) | set(live):
            want = tuple(held.get(a, (ZERO, ZERO)))
            if live.get(a, (ZERO, ZERO)) != want:
                problems.append(f"{a}: position {live.get(a)} != open lots {want}")
        if cash != acct["cash"]:
            problems.append(f"cash {acct['cash']} != replayed {cash}")
        if acct["cash"] < 0:
            problems.append(f"negative cash {acct['cash']}")
        problems += self._check_fee_accruals()
        problems += self._check_dividends()
        fees += sum((-D(r[0]) for r in c.execute("SELECT amount FROM cash_events WHERE kind='fee'")), ZERO)
        if fees != acct["fees"]:
            problems.append(f"fees {acct['fees']} != replayed {fees}")
        if realized_all != acct["realized_pnl"]:
            problems.append(f"realized {acct['realized_pnl']} != sum of lot closures {realized_all}")
        return problems

    def _check_fee_accruals(self) -> List[str]:
        """Every accrual belongs to a fill on its own trade day; every 'fee' event is exactly
        its accruals' total rounded up; a day charged without an event accrued exactly 0."""
        c = self.store.conn
        problems: List[str] = []
        fill_day = {r[0]: r[1] for r in c.execute("SELECT id, day FROM fills")}
        events = {r[0]: (r[1], D(r[2])) for r in c.execute("SELECT id, day, amount FROM cash_events WHERE kind='fee'")}
        by_event: Dict[int, List[Decimal]] = {}
        zero_days: Dict[str, Decimal] = {}
        for aid, fid, day, amount, charged, event in c.execute(
                "SELECT id, fill_id, day, amount, charged, charge_event_id FROM fee_accruals"):
            amount = D(amount)
            if fill_day.get(fid) != day:
                problems.append(f"fee accrual {aid}: day {day} != its fill's day {fill_day.get(fid)}")
            if amount < 0:
                problems.append(f"fee accrual {aid} is negative")
            if not charged and event is not None:
                problems.append(f"fee accrual {aid} points at a charge but isn't marked charged")
            if charged and event is None:
                zero_days[day] = zero_days.get(day, ZERO) + amount
            if event is not None:
                if event not in events:
                    problems.append(f"fee accrual {aid} points at missing fee event {event}")
                elif events[event][0] != day:
                    problems.append(f"fee accrual {aid} ({day}) charged on another day's event")
                by_event.setdefault(event, []).append(amount)
        for eid, (day, amount) in events.items():
            owed = charge_for_day(by_event.get(eid, []))
            if eid not in by_event or -amount != owed:
                problems.append(f"fee event {eid} ({day}) charged {-amount}, its accruals round up to {owed}")
        for day, total in zero_days.items():
            if total != 0:
                problems.append(f"{day}: fees of {total} marked charged without a charge")
        return problems

    def _check_dividends(self) -> List[str]:
        """Each dividend is its shares x rate; each paid one is exactly one 'dividend' cash
        event of that amount for that symbol; no dividend event pays nothing."""
        c = self.store.conn
        problems: List[str] = []
        events = {r[0]: (r[1], D(r[2])) for r in c.execute(
            "SELECT id, asset, amount FROM cash_events WHERE kind='dividend'")}
        used: set = set()
        for aid, sym, q, rate, amount, status, event in c.execute(
                "SELECT action_id,symbol,qty,rate,amount,status,cash_event_id FROM dividends ORDER BY action_id"):
            if D(amount) <= 0 or usd(D(q) * D(rate)) != D(amount):
                problems.append(f"dividend {aid}: {amount} isn't {q} shares x {rate}")
            if status != "paid":
                if event is not None:
                    problems.append(f"dividend {aid} is {status} but points at cash event {event}")
                continue
            if events.get(event) != (sym, D(amount)):
                problems.append(f"dividend {aid}: owes {amount} on {sym}, cash event {event} is {events.get(event)}")
            if event in used:
                problems.append(f"cash event {event} pays two dividends")
            used.add(event)
        for eid in sorted(set(events) - used):
            problems.append(f"dividend cash event {eid} pays no dividend")
        return problems

    def fills(self, limit: int = 50) -> List[Dict[str, str]]:
        cols = ("ts", "asset", "side", "qty", "price", "notional", "fee", "realized_pnl",
                "quote_source", "origin", "reason")
        return [dict(zip(cols, r)) for r in self.store.conn.execute(
            f"SELECT {','.join(cols)} FROM fills ORDER BY id DESC LIMIT ?", (limit,))]

    def fee_detail(self, fill_id: int) -> Dict[str, Decimal]:
        r = self.store.conn.execute("SELECT fee_detail FROM fills WHERE id=?", (fill_id,)).fetchone()
        return {k: D(v) for k, v in json.loads(r[0]).items()} if r else {}


__all__ = ["Ledger", "LedgerError", "Position", "Lot", "Closure", "Fill", "Valuation",
           "D", "usd", "qty", "dumps", "trade_day", "holding_term", "anniversary"]
