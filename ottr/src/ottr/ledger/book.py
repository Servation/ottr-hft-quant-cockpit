"""
Ledger: cash, positions and fills with exact Decimal accounting.

What it does: applies paper fills (inside the caller's transaction), keeps the
materialized account/positions rows in step, and can recompute everything from the
event log to prove the books balance. Conventions: average cost; BUY fees are added to
cost basis; SELL fees reduce proceeds; realized P&L = net proceeds - cost removed.
Holdings are marked at the BID (what we could actually sell for).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, getcontext
from typing import Dict, List, Mapping, Optional, Tuple

from ottr.ledger.store import Store, dumps
from ottr.market.types import Quote

getcontext().prec = 34
USD = Decimal("0.00000001")
QTY = Decimal("0.000000000001")
DUST_USD = Decimal("0.01")  # a remainder worth less than this closes the position


def D(x) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def usd(x) -> Decimal:
    return D(x).quantize(USD, rounding=ROUND_HALF_EVEN)


def qty(x) -> Decimal:
    return D(x).quantize(QTY, rounding=ROUND_HALF_EVEN)


class LedgerError(RuntimeError):
    pass


@dataclass(frozen=True)
class Position:
    asset: str
    qty: Decimal
    cost: Decimal
    high_water: float
    opened_ts: float

    @property
    def avg_cost(self) -> Decimal:
        return self.cost / self.qty if self.qty > 0 else Decimal(0)


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


@dataclass(frozen=True)
class Valuation:
    equity: float
    cash: float
    invested: float
    weights: Dict[str, float]
    values: Dict[str, float]


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
        return {r[0]: Position(r[0], D(r[1]), D(r[2]), r[3], r[4]) for r in self.store.conn.execute(
            "SELECT asset,qty,cost,high_water,opened_ts FROM positions ORDER BY asset")}

    def value(self, quotes: Mapping[str, Quote]) -> Optional[Valuation]:
        """Mark to the bid. Returns None if any held asset has no quote (unknown, not 0)."""
        cash = float(self.cash())
        values: Dict[str, float] = {}
        for a, p in self.positions().items():
            q = quotes.get(a)
            if q is None:
                return None
            values[a] = float(p.qty) * q.bid
        invested = sum(values.values())
        equity = cash + invested
        weights = {a: (v / equity if equity > 0 else 0.0) for a, v in values.items()}
        return Valuation(equity=equity, cash=cash, invested=invested, weights=weights, values=values)

    # -- setup ----------------------------------------------------------------------------
    def initialize(self, cash, note: str = "initial deposit",
                   holdings: Optional[Mapping[str, Tuple[Decimal, float]]] = None) -> None:
        """Open the book once. `holdings` = {asset: (qty, mark_price)} imported at that mark,
        so their cost basis equals today's market value (no invented historical P&L)."""
        if self.initialized():
            raise LedgerError("ledger already initialized")
        now = time.time()
        with self.store.tx() as c:
            c.execute("INSERT INTO account(id,cash,realized_pnl,fees) VALUES(1,?,?,?)",
                      (str(usd(cash)), "0", "0"))
            c.execute("INSERT INTO cash_events(ts,kind,amount,note) VALUES(?,?,?,?)",
                      (now, "deposit", str(usd(cash)), note))
            for asset, (q, mark) in (holdings or {}).items():
                q = qty(q)
                if q <= 0:
                    continue
                cost = usd(q * D(mark))
                c.execute("INSERT INTO positions(asset,qty,cost,high_water,opened_ts) VALUES(?,?,?,?,?)",
                          (asset, str(q), str(cost), float(mark), now))
                c.execute("INSERT INTO cash_events(ts,kind,asset,qty,amount,note) VALUES(?,?,?,?,?,?)",
                          (now, "deposit_in_kind", asset, str(q), str(cost), f"{note} @ {mark}"))
            self.store.audit("ledger_initialized", cash=usd(cash),
                             holdings={a: str(h[0]) for a, h in (holdings or {}).items()})

    # -- writes (call inside store.tx()) -------------------------------------------------
    def apply_fill(self, *, idem_key: str, asset: str, side: str, quantity, price,
                   fee_pct: float, quote_source: str, origin: str, ref: str = "",
                   reason: str = "") -> Fill:
        """Book one fill. Raises LedgerError (rolling back the caller's tx) on any violation."""
        if not self.store._depth:
            raise LedgerError("apply_fill must run inside store.tx() so the fill commits atomically")
        side = side.upper()
        q, px = qty(quantity), D(price)
        if q <= 0 or px <= 0:
            raise LedgerError("fill needs positive quantity and price")
        notional = usd(q * px)
        fee = usd(notional * D(fee_pct) / 100)
        acct = self.account()
        pos = self.positions().get(asset)
        now = time.time()
        c = self.store.conn

        if side == "BUY":
            total = notional + fee
            if total > acct["cash"]:
                raise LedgerError(f"insufficient cash: need {total}, have {acct['cash']}")
            new_cash = acct["cash"] - total
            realized = Decimal(0)
            if pos:
                c.execute("UPDATE positions SET qty=?, cost=? WHERE asset=?",
                          (str(qty(pos.qty + q)), str(usd(pos.cost + total)), asset))
            else:
                c.execute("INSERT INTO positions(asset,qty,cost,high_water,opened_ts) VALUES(?,?,?,?,?)",
                          (asset, str(q), str(usd(total)), float(px), now))
        elif side == "SELL":
            if pos is None or q > pos.qty:
                raise LedgerError(f"cannot sell {q} {asset}: holding {pos.qty if pos else 0}")
            net = notional - fee
            cost_out = usd(pos.cost * q / pos.qty)
            realized = usd(net - cost_out)
            new_cash = acct["cash"] + net
            left_qty, left_cost = qty(pos.qty - q), usd(pos.cost - cost_out)
            if left_qty <= 0 or left_qty * px < DUST_USD:
                c.execute("DELETE FROM positions WHERE asset=?", (asset,))
            else:
                c.execute("UPDATE positions SET qty=?, cost=? WHERE asset=?",
                          (str(left_qty), str(left_cost), asset))
        else:
            raise LedgerError(f"bad side {side}")

        c.execute("UPDATE account SET cash=?, realized_pnl=?, fees=? WHERE id=1",
                  (str(usd(new_cash)), str(usd(acct["realized_pnl"] + realized)),
                   str(usd(acct["fees"] + fee))))
        c.execute("INSERT INTO fills(ts,idem_key,asset,side,qty,price,notional,fee,realized_pnl,"
                  "quote_source,origin,ref,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (now, idem_key, asset, side, str(q), str(px), str(notional), str(fee),
                   str(realized), quote_source, origin, ref, reason[:500]))
        fill = Fill(idem_key, asset, side, q, px, notional, fee, realized, quote_source, now)
        self.store.audit("fill", idem_key=idem_key, asset=asset, side=side, qty=q, price=px,
                         notional=notional, fee=fee, realized_pnl=realized, origin=origin)
        problems = self.check()
        if problems:
            raise LedgerError("invariant violated: " + "; ".join(problems))
        return fill

    def set_high_water(self, asset: str, price: float) -> None:
        with self.store.tx() as c:
            c.execute("UPDATE positions SET high_water=MAX(high_water, ?) WHERE asset=?", (price, asset))

    # -- audit ----------------------------------------------------------------------------
    def check(self) -> List[str]:
        """Recompute cash and holdings from the event log and compare to the live rows."""
        c = self.store.conn
        problems: List[str] = []
        cash = sum((D(r[0]) for r in c.execute("SELECT amount FROM cash_events WHERE kind='deposit'")), Decimal(0))
        held: Dict[str, Decimal] = {}
        for r in c.execute("SELECT asset,qty FROM cash_events WHERE kind='deposit_in_kind'"):
            held[r[0]] = held.get(r[0], Decimal(0)) + D(r[1])
        fees = Decimal(0)
        for side, asset, q, notional, fee in c.execute(
                "SELECT side,asset,qty,notional,fee FROM fills ORDER BY id"):
            q, notional, fee = D(q), D(notional), D(fee)
            fees += fee
            if side == "BUY":
                cash -= notional + fee
                held[asset] = held.get(asset, Decimal(0)) + q
            else:
                cash += notional - fee
                held[asset] = held.get(asset, Decimal(0)) - q
        acct = self.account()
        if abs(cash - acct["cash"]) > Decimal("0.000001"):
            problems.append(f"cash {acct['cash']} != replayed {cash}")
        if acct["cash"] < 0:
            problems.append(f"negative cash {acct['cash']}")
        if abs(fees - acct["fees"]) > Decimal("0.000001"):
            problems.append(f"fees {acct['fees']} != replayed {fees}")
        live = {a: p.qty for a, p in self.positions().items()}
        for a in set(held) | set(live):
            replay = held.get(a, Decimal(0))
            have = live.get(a, Decimal(0))
            if replay < -QTY:
                problems.append(f"{a} replayed qty negative ({replay})")
            # A dust remainder may have been closed out: allow the replay to exceed live by dust.
            if have > 0 and abs(replay - have) > QTY * 10:
                problems.append(f"{a} qty {have} != replayed {replay}")
        return problems

    def fills(self, limit: int = 50) -> List[Dict[str, str]]:
        cols = ("ts", "asset", "side", "qty", "price", "notional", "fee", "realized_pnl",
                "quote_source", "origin", "reason")
        return [dict(zip(cols, r)) for r in self.store.conn.execute(
            f"SELECT {','.join(cols)} FROM fills ORDER BY id DESC LIMIT ?", (limit,))]


__all__ = ["Ledger", "LedgerError", "Position", "Fill", "Valuation", "D", "usd", "qty", "dumps"]
