"""
Tax estimates for the book: wash sales, holding periods, short vs long term, collectibles,
dividends, and the tax a year's results would owe. ESTIMATES, not tax advice: they follow
the main federal rules with the rates set in config.toml, and say so wherever shown.

What it does: `replay` walks the ledger's history in order (fills, their lot closures, and
split adjustments, as `Ledger.check` does) and produces a Disposition for every piece of
every lot sold, in tax terms:
- Basis is the lot's cost (BUY fees included, as the ledger books it) plus any wash-sale
  adjustment the shares carry. Gain = proceeds - basis + any loss disallowed on this sale.
- Wash sales: a sale at a loss is matched against purchases of the same symbol within 30
  days before or after it (not the shares sold, each purchased share used once, oldest
  first, and only shares still held at the sale or bought after it). The matched part of
  the loss is disallowed now and added to the basis of the replacement shares, which also
  inherit the sold shares' holding period, so the loss comes back when they are sold.
- Long term: sold after the one-year anniversary of the (wash-adjusted) acquisition date.
  GLD and SLV hold physical metal, so their long-term gains are collectibles (taxed at up
  to 28%), set by `tax.collectibles`.
`estimate` nets one year's dispositions the way Schedule D does (short against long, a
net loss deducts up to $3,000 against ordinary income and carries the rest forward) and
adds dividends: qualified for the classes in `tax.qualified_dividend_classes`, ordinary
otherwise. `liquidation` is what selling everything now would add, for after-tax equity.

Why replay instead of storing adjustments: the economic book (cash, lots, P&L) is what the
ledger keeps exact; tax is a view of the same history. Replaying it means a corrected rule
or rate applies to the whole history at once, and nothing can drift out of step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from ottr.ledger.book import ZERO, D, anniversary, qty, usd
from ottr.ledger.store import Store
from ottr.market.types import Quote

WASH_DAYS = 30                       # either side of a loss sale
LOSS_DEDUCTION = Decimal("3000")     # a year's net capital loss deductible against ordinary income


@dataclass(frozen=True)
class Disposition:
    """One lot's shares sold by one fill, in tax terms."""
    closure_id: int
    fill_id: int
    lot_id: int
    asset: str
    qty: Decimal
    acquired: date                   # wash-adjusted: the holding period it carries
    sold: date
    proceeds: Decimal
    cost: Decimal                    # what the shares cost (the ledger's basis)
    adjustment: Decimal              # wash-sale losses added to the basis
    disallowed: Decimal              # this sale's loss disallowed by a wash sale (>= 0)
    term: str                        # short | long
    collectible: bool

    @property
    def basis(self) -> Decimal:
        return self.cost + self.adjustment

    @property
    def gain(self) -> Decimal:
        """The gain or loss to report: proceeds less basis, with a disallowed loss added back."""
        return self.proceeds - self.basis + self.disallowed


@dataclass(frozen=True)
class Wash:
    closure_id: int
    asset: str
    sold: date
    qty: Decimal                     # loss shares matched
    replacement_lot: int
    disallowed: Decimal


@dataclass
class _Tranche:
    qty: Decimal
    adj: Decimal                     # wash-sale basis carried by these shares
    acquired: date


@dataclass
class _Lot:
    id: int
    asset: str
    opened: date
    fill: Optional[int]              # the BUY that opened it (None: brought in at init)
    qty: Decimal                     # shares in today's share basis as the replay goes
    open_qty: Decimal = ZERO
    wash_used: Decimal = ZERO        # shares already used as a wash-sale replacement
    tranches: List[_Tranche] = field(default_factory=list)
    pending: List[_Tranche] = field(default_factory=list)   # adjustments for a lot not bought yet

    def plain(self) -> Decimal:
        return sum((t.qty for t in self.tranches if t.adj == 0 and t.acquired == self.opened), ZERO)


@dataclass
class Replay:
    dispositions: List[Disposition]
    washes: List[Wash]
    lots: Dict[int, _Lot]            # end state: the open shares, with their tranches


def _take(tranches: List[_Tranche], q: Decimal) -> List[Tuple[Decimal, Decimal, date]]:
    """Consume `q` shares from the front: [(shares, their wash adjustment, acquired)]."""
    out = []
    while q > 0 and tranches:
        t = tranches[0]
        n = min(q, t.qty)
        adj = t.adj if n == t.qty else usd(t.adj * n / t.qty)
        out.append((n, adj, t.acquired))
        t.qty -= n
        t.adj -= adj
        q -= n
        if t.qty == 0:
            tranches.pop(0)
    if q > 0:
        raise ValueError(f"the tax replay ran out of shares ({q} short)")
    return out


def _carve(lot: _Lot, m: Decimal, adj: Decimal, acquired: date) -> None:
    """Make `m` of an open lot's plain shares carry a wash adjustment (placed first, so a
    later sale of the lot uses them first)."""
    left = m
    for t in lot.tranches:
        if left == 0:
            break
        if t.adj == 0 and t.acquired == lot.opened:
            n = min(left, t.qty)
            t.qty -= n
            left -= n
    lot.tranches = [_Tranche(m - left, adj, acquired)] + [t for t in lot.tranches if t.qty > 0]


def replay(store: Store, collectibles: Sequence[str] = ()) -> Replay:
    """The book's history in tax terms. Raises ValueError if the history is inconsistent
    (which `Ledger.check` would also report)."""
    c = store.conn
    lots: Dict[int, _Lot] = {}
    by_fill: Dict[int, int] = {}
    for lid, asset, fid, day, q in c.execute("SELECT id, asset, open_fill_id, opened_day, qty FROM lots ORDER BY id"):
        lots[lid] = _Lot(lid, asset, date.fromisoformat(day), fid, D(q))
        if fid is None:
            lots[lid].open_qty = D(q)
            lots[lid].tranches = [_Tranche(D(q), ZERO, lots[lid].opened)]
        else:
            by_fill[fid] = lid
    closures: Dict[int, list] = {}
    for row in c.execute("SELECT id, lot_id, fill_id, day, qty, proceeds, cost FROM lot_closures ORDER BY id"):
        closures.setdefault(row[2], []).append(row)
    splits: Dict[int, list] = {}
    for lid, after_fill, before, after in c.execute(
            "SELECT a.lot_id, e.after_fill, a.qty_before, a.qty_after FROM lot_adjustments a "
            "JOIN corporate_entries e ON e.action_id=a.action_id ORDER BY a.id"):
        splits.setdefault(after_fill, []).append((lid, D(before), D(after)))
    by_asset: Dict[str, List[_Lot]] = {}
    for lot in lots.values():
        by_asset.setdefault(lot.asset, []).append(lot)
    collect = set(collectibles)
    out: List[Disposition] = []
    washes: List[Wash] = []

    def apply_splits(after_fill: int) -> None:
        for lid, before, after in splits.pop(after_fill, []):
            lot, r = lots[lid], after / before
            lot.qty, lot.wash_used = qty(lot.qty * r), qty(lot.wash_used * r)
            lot.open_qty = after
            left = after
            for t in lot.tranches[:-1]:
                t.qty = qty(t.qty * r)
                left -= t.qty
            if lot.tranches:
                lot.tranches[-1].qty = left

    def wash(piece: Disposition, fid: int, loss: Decimal) -> Decimal:
        """Match a loss piece to replacement purchases; returns the loss disallowed."""
        remaining, disallowed = piece.qty, ZERO
        lo, hi = piece.sold - timedelta(days=WASH_DAYS), piece.sold + timedelta(days=WASH_DAYS)
        held_days = (piece.sold - piece.acquired).days
        for lot in sorted(by_asset.get(piece.asset, []), key=lambda x: (x.opened, x.id)):
            if remaining == 0:
                break
            if lot.id == piece.lot_id or not lo <= lot.opened <= hi:
                continue
            bought = lot.fill is None or lot.fill < fid
            avail = min(lot.open_qty, lot.qty - lot.wash_used) if bought else lot.qty - lot.wash_used
            m = min(remaining, avail)
            if m <= 0:
                continue
            # The match that completes the piece takes the rounding remainder.
            amount = loss - disallowed if m == remaining else usd(loss * m / piece.qty)
            tranche = (m, amount, lot.opened - timedelta(days=held_days))
            if bought:
                _carve(lot, *tranche)
            else:
                lot.pending.append(_Tranche(*tranche))
            lot.wash_used += m
            remaining -= m
            disallowed += amount
            washes.append(Wash(piece.closure_id, piece.asset, piece.sold, m, lot.id, amount))
        return disallowed

    apply_splits(0)
    for fid, side, day in c.execute("SELECT id, side, day FROM fills ORDER BY id").fetchall():
        if side == "BUY":
            lot = lots[by_fill[fid]]
            lot.open_qty = lot.qty
            extra = sum((t.qty for t in lot.pending), ZERO)
            lot.tranches = lot.pending + ([_Tranche(lot.qty - extra, ZERO, lot.opened)] if lot.qty > extra else [])
            lot.pending = []
        else:
            for cid, lid, _, cday, cq, proceeds, cost in closures.get(fid, []):
                lot, cq, proceeds, cost = lots[lid], D(cq), D(proceeds), D(cost)
                sold = date.fromisoformat(cday)
                pieces = _take(lot.tranches, cq)
                lot.open_qty -= cq
                done_p = done_c = ZERO
                for i, (n, adj, acquired) in enumerate(pieces):
                    last = i == len(pieces) - 1
                    p = proceeds - done_p if last else usd(proceeds * n / cq)
                    k = cost - done_c if last else usd(cost * n / cq)
                    done_p, done_c = done_p + p, done_c + k
                    term = "long" if sold > anniversary(acquired) else "short"
                    piece = Disposition(cid, fid, lid, lot.asset, n, acquired, sold, p, k, adj, ZERO, term,
                                        lot.asset in collect)
                    loss = piece.basis - piece.proceeds
                    if loss > 0:
                        piece = Disposition(**{**piece.__dict__, "disallowed": wash(piece, fid, loss)})
                    out.append(piece)
        apply_splits(fid)
    return Replay(out, washes, lots)


# -- a year's estimate ------------------------------------------------------------------------
@dataclass(frozen=True)
class TaxYear:
    year: int
    short_term: Decimal
    long_term: Decimal               # excluding collectibles
    collectibles: Decimal            # long-term gains on collectibles
    disallowed: Decimal              # wash-sale losses deferred to replacement shares
    qualified_dividends: Decimal
    ordinary_dividends: Decimal
    carry_in: Decimal                # capital loss carried in from earlier years
    deduction: Decimal               # net capital loss deducted against ordinary income
    carry_out: Decimal
    federal: Decimal
    state: Decimal
    niit: Decimal

    @property
    def total(self) -> Decimal:
        return self.federal + self.state + self.niit


def _pct(x) -> Decimal:
    return D(x) / 100


def _net(st: Decimal, lt: Decimal, col: Decimal) -> Tuple[Decimal, Decimal, Decimal]:
    """Schedule D netting, in outline. In: the year's short-term, long-term (other) and
    collectibles results. Out: (short, long, collectibles) where each long figure is a gain
    (>= 0) and short is a gain, or the year's net loss when negative.
    - A loss in one long-term group reduces the other group's gain.
    - A net short-term loss reduces the collectibles (28%) gain first, then the other.
    - A net long-term loss reduces the short-term gain."""
    if lt < 0 < col:
        col, lt = col + lt, ZERO
    elif col < 0 < lt:
        lt, col = lt + col, ZERO
    long_net = lt + col
    if long_net < 0:                     # a net long-term loss, all of it in one figure
        return st + long_net, ZERO, ZERO
    if st < 0:
        cut = min(-st, col)
        col, st = col - cut, st + cut
        cut = min(-st, lt)
        lt, st = lt - cut, st + cut
    return st, lt, col


def estimate(year: int, dispositions: Sequence[Disposition], dividends: Mapping[str, Decimal],
             carry_in: Decimal, rates) -> TaxYear:
    """One year's estimated tax on the book's results. `dividends`: "qualified" and
    "ordinary" totals. `carry_in`: capital loss carried from earlier years (counted as
    short term). `rates`: the config's [tax] section. A net capital loss deducts up to
    $3,000 against ordinary income (a negative tax: what it saves) and the rest carries
    forward."""
    st_raw = sum((d.gain for d in dispositions if d.term == "short"), ZERO)
    lt_raw = sum((d.gain for d in dispositions if d.term == "long" and not d.collectible), ZERO)
    col_raw = sum((d.gain for d in dispositions if d.term == "long" and d.collectible), ZERO)
    st, lt, col = _net(st_raw - carry_in, lt_raw, col_raw)
    deduction = carry_out = ZERO
    if st < 0:
        deduction = min(-st, LOSS_DEDUCTION)
        carry_out = -st - deduction
        st = ZERO
    ordinary, long_rate = _pct(rates.federal_ordinary_pct), _pct(rates.federal_long_term_pct)
    col_rate = min(_pct(rates.collectibles_max_pct), ordinary)
    q, o = dividends.get("qualified", ZERO), dividends.get("ordinary", ZERO)
    federal = st * ordinary + lt * long_rate + col * col_rate + q * long_rate + o * ordinary - deduction * ordinary
    income = st + lt + col + q + o
    return TaxYear(year, st_raw, lt_raw, col_raw, sum((d.disallowed for d in dispositions), ZERO), q, o, carry_in,
                   deduction, carry_out, usd(federal), usd((income - deduction) * _pct(rates.state_pct)),
                   usd(income * _pct(rates.niit_pct)))


def dividends_by_year(store: Store, classes: Mapping[str, str],
                      qualified_classes: Sequence[str]) -> Dict[int, Dict[str, Decimal]]:
    """year -> {"qualified": ..., "ordinary": ...} from the dividends paid. Why by class: a
    broker's 1099-DIV says which dividends qualify; until there is one, stock and stock-ETF
    dividends count as qualified and the rest (bond, cash, commodity and REIT funds) as
    ordinary. A symbol that is no longer a member counts as a stock."""
    out: Dict[int, Dict[str, Decimal]] = {}
    for day, asset, amount in store.conn.execute("SELECT day, asset, amount FROM cash_events WHERE kind='dividend'"):
        kind = "qualified" if classes.get(asset, "stock") in qualified_classes else "ordinary"
        year = out.setdefault(int(day[:4]), {"qualified": ZERO, "ordinary": ZERO})
        year[kind] += D(amount)
    return out


def years(rep: Replay, dividends: Mapping[int, Mapping[str, Decimal]], rates, through: int,
          extra: Sequence[Disposition] = ()) -> List[TaxYear]:
    """Every year from the first result through `through`, carrying losses forward.
    `extra`: hypothetical dispositions (a liquidation) added to their year."""
    disp = list(rep.dispositions) + list(extra)
    first = min([d.sold.year for d in disp] + list(dividends) + [through])
    out, carry = [], ZERO
    for y in range(first, through + 1):
        t = estimate(y, [d for d in disp if d.sold.year == y], dividends.get(y, {}), carry, rates)
        out.append(t)
        carry = t.carry_out
    return out


def liquidation(store: Store, rep: Replay, marks: Mapping[str, Quote], today: date,
                collectibles: Sequence[str] = ()) -> Optional[List[Disposition]]:
    """What selling every open lot at its mark today would report, or None when a holding
    has no mark (unknown, never zero)."""
    cost = {r[0]: D(r[1]) for r in store.conn.execute("SELECT id, cost_remaining FROM lots WHERE closed_ts IS NULL")}
    out: List[Disposition] = []
    for lot in rep.lots.values():
        if lot.open_qty <= 0:
            continue
        q = marks.get(lot.asset)
        if q is None:
            return None
        value, lot_cost = usd(lot.open_qty * D(q.bid)), cost.get(lot.id, ZERO)
        done_v = done_c = ZERO
        for i, t in enumerate(lot.tranches):
            last = i == len(lot.tranches) - 1
            v = value - done_v if last else usd(value * t.qty / lot.open_qty)
            k = lot_cost - done_c if last else usd(lot_cost * t.qty / lot.open_qty)
            done_v, done_c = done_v + v, done_c + k
            term = "long" if today > anniversary(t.acquired) else "short"
            out.append(Disposition(0, 0, lot.id, lot.asset, t.qty, t.acquired, today, v, k, t.adj, ZERO, term,
                                   lot.asset in set(collectibles)))
    return out


def classes_of(cfg, store: Store) -> Dict[str, str]:
    """symbol -> asset class for every symbol the book has held: the core's from config,
    and anything else is a stock (a satellite, current or former)."""
    syms = {r[0] for r in store.conn.execute("SELECT DISTINCT asset FROM lots")}
    return {s: cfg.universe.core.get(s, "stock") for s in syms | set(cfg.universe.core)}


def after_tax_equity(cfg, store: Store, marks: Mapping[str, Quote], equity: float, today: date) -> Optional[float]:
    """Equity less every year's estimated tax so far and what selling everything at these
    marks today would add: what the book would be worth to its owner after tax. None when
    a holding has no mark (unknown, never a guess)."""
    t = cfg.tax
    rep = replay(store, t.collectibles)
    liq = liquidation(store, rep, marks, today, t.collectibles)
    if liq is None:
        return None
    divs = dividends_by_year(store, classes_of(cfg, store), t.qualified_dividend_classes)
    return equity - float(sum((y.total for y in years(rep, divs, t, today.year, liq)), ZERO))
