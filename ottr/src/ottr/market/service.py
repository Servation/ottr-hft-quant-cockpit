"""
MarketData: the US desk's one view of the market, from Alpaca (implements the desk's
`Market`).

What it does:
- the trading calendar, refreshed from Alpaca once a day and kept in the book, so a
  restart knows the session at once;
- live IEX snapshots for every tracked symbol (core, reference, held, and any extra the
  caller adds), cached for `data.quote_ttl_sec`; `quote()` returns only a quote that
  passed `check_quote`, or why there is none;
- daily bars once each session's close is final (raw closes for marks, adjusted closes
  for indicators and benchmarks), kept in the book;
- `marks()` for every holding, by the session rules in `ledger/marks.py`;
- `vol_regime()` for sizing: 60-session realized vol (sqrt(252)) and the ER regime;
- `snapshot()`: the council's view of every asset (`market/packet.py`).

Why one service: the council, the gate, the risk monitor and the equity curve must agree
on the same numbers, and an outage must leave each of them saying "unknown" rather than
trading on a price that only looks current.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ottr.clock import SYSTEM, Clock
from ottr.events.view import MacroReading
from ottr.ledger import corporate
from ottr.ledger.book import Ledger, trade_day
from ottr.ledger.marks import SIP_FINAL_AFTER_SEC, mark
from ottr.ledger.store import Store
from ottr.market import benchmarks as bm
from ottr.market import history
from ottr.market import indicators as ind
from ottr.market.alpaca import Alpaca, AlpacaError, Asset, check_quote, completed_bars
from ottr.market.calendar import CalendarError, Session, TradingCalendar
from ottr.market.packet import UsSnapshot, build_snapshot
from ottr.market.types import Bar, MarketDataError, Quote

logger = logging.getLogger(__name__)

CALENDAR_AHEAD_DAYS = 30        # sessions known ahead: earnings blackouts and meeting slots
REFRESH_IF_OLDER_SEC = 10.0     # an order's quote is re-fetched if the cached one is older
MINUTE_TTL_SEC = 60.0           # delayed minute closes (a fallback mark) are re-read this often
ASSETS_STATE = "assets"         # risk_state key: Alpaca's asset records, refreshed daily


class MarketData:
    def __init__(self, cfg, alpaca: Alpaca, store: Store, clock: Clock = SYSTEM,
                 extra_symbols: Callable[[], Iterable[str]] = lambda: ()) -> None:
        self.cfg, self.alpaca, self.store, self.clock = cfg, alpaca, store, clock
        self.extra_symbols = extra_symbols
        self._cal: Optional[TradingCalendar] = history.load_calendar(store)
        self._cal_day = None                       # ET date of the last calendar refresh
        self._snaps: Dict[str, dict] = {}
        self._snaps_ts = 0.0
        self._minutes: Dict[str, Tuple[float, float]] = {}
        self._minutes_ts = 0.0
        self._bars_through = None                  # the last session whose final bars are stored
        self._lock = asyncio.Lock()

    # -- what we follow ----------------------------------------------------------------------
    def tracked(self) -> List[str]:
        u = self.cfg.universe
        held = Ledger(self.store).positions()
        return sorted(set(u.core) | set(u.reference) | set(held) | set(self.extra_symbols()))

    # -- calendar ----------------------------------------------------------------------------
    @property
    def calendar(self) -> TradingCalendar:
        if self._cal is None:
            raise MarketDataError("no trading calendar yet (Alpaca has not been reached)")
        return self._cal

    async def refresh_calendar(self, force: bool = False) -> TradingCalendar:
        """Once per New York day (or when forced). A failure keeps the stored calendar."""
        now = self.clock.time()
        today = trade_day(now)
        if not force and self._cal is not None and self._cal_day == today:
            return self._cal
        try:
            cal = await self.alpaca.calendar(today - timedelta(days=self.cfg.data.history_days),
                                             today + timedelta(days=CALENDAR_AHEAD_DAYS))
        except AlpacaError as e:
            if self._cal is None:
                raise MarketDataError(f"no trading calendar: {e}") from e
            logger.warning("calendar refresh failed (%s); keeping the stored one", e)
            return self._cal
        if self.store.writer:
            history.save_calendar(self.store, cal)
            cal = history.load_calendar(self.store) or cal
        self._cal, self._cal_day = cal, today
        return cal

    # -- quotes ------------------------------------------------------------------------------
    async def snapshots(self, force: bool = False) -> Dict[str, dict]:
        """Raw snapshots for every tracked symbol, cached for `quote_ttl_sec`. On an outage
        the cache is returned: each quote carries its own exchange timestamp, so
        `check_quote` still judges it stale rather than trusting it."""
        async with self._lock:
            now = self.clock.time()
            if not force and self._snaps and now - self._snaps_ts < self.cfg.data.quote_ttl_sec:
                return dict(self._snaps)
            try:
                self._snaps = await self.alpaca.snapshots(self.tracked(), feed=self.cfg.data.feed)
                self._snaps_ts = now
            except AlpacaError as e:
                if not self._snaps:
                    raise MarketDataError(f"no quotes: {e}") from e
                logger.warning("snapshots failed (%s); quotes judged from the last ones", e)
            return dict(self._snaps)

    async def quote(self, symbol: str) -> Tuple[Optional[Quote], str]:
        """A fresh, sane quote for an order, or (None, why not)."""
        if symbol not in set(self.tracked()):
            try:
                snaps = await self.alpaca.snapshots([symbol], feed=self.cfg.data.feed)
            except AlpacaError as e:
                return None, f"market_data_error ({e})"
        else:
            snaps = await self.snapshots(force=self.clock.time() - self._snaps_ts > REFRESH_IF_OLDER_SEC)
        now = self.clock.time()
        return check_quote(symbol, snaps.get(symbol), now, self.cfg.data.quote_limits(),
                           self._splits(now).get(symbol, 1.0))

    def _splits(self, now: float) -> Dict[str, float]:
        """symbol -> the share multiple of splits effective since the last close."""
        try:
            last = self.calendar.state_at(now).last_close
        except CalendarError:
            return {}
        return corporate.split_ratios(self.store, last.day, trade_day(now)) if last else {}

    async def live_quotes(self) -> Dict[str, Quote]:
        """Every tracked symbol that has a usable live quote right now."""
        now = self.clock.time()
        limits = self.cfg.data.quote_limits()
        splits = self._splits(now)
        out = {}
        for s, snap in (await self.snapshots()).items():
            q, _ = check_quote(s, snap, now, limits, splits.get(s, 1.0))
            if q is not None:
                out[s] = q
        return out

    # -- asset records -----------------------------------------------------------------------
    async def refresh_assets(self, symbols: Iterable[str], force: bool = False) -> Dict[str, Asset]:
        """Alpaca's record (tradable, fractionable) for each symbol, fetched once a New York
        day and kept in the book's state; a symbol whose lookup fails keeps its last record."""
        today = trade_day(self.clock.time()).isoformat()
        stored = dict(self.store.get_state(ASSETS_STATE) or {})
        known = {s: Asset(**r) for s, r in (stored.get("assets") or {}).items()}
        wanted = sorted(set(symbols))
        if not force and stored.get("day") == today and all(s in known for s in wanted):
            return {s: known[s] for s in wanted if s in known}
        for s in wanted:
            try:
                known[s] = await self.alpaca.asset(s)
            except AlpacaError as e:
                logger.warning("asset record for %s failed (%s); keeping the last one", s, e)
        if self.store.writer:
            self.store.set_state(ASSETS_STATE, {"day": today, "assets": {s: a.__dict__ for s, a in known.items()}})
        return {s: known[s] for s in wanted if s in known}

    # -- daily bars --------------------------------------------------------------------------
    def final_session(self, now: Optional[float] = None) -> Optional[Session]:
        """The newest session whose daily bar is final (closed, plus the SIP delay)."""
        return self._final_session(self.clock.time() if now is None else now)

    def _final_session(self, now: float) -> Optional[Session]:
        """The newest session whose daily bar is final (closed, plus the SIP delay)."""
        try:
            recent = self.calendar.completed_sessions(now, 2)
        except CalendarError as e:
            raise MarketDataError(str(e)) from e
        done = [s for s in recent if now >= s.close_ts + SIP_FINAL_AFTER_SEC]
        return done[-1] if done else None

    async def refresh_bars(self, force: bool = False) -> int:
        """Raw and adjusted daily bars for every tracked symbol, once per final session (or
        when forced). Returns how many raw bars were stored; 0 when there was nothing new."""
        now = self.clock.time()
        final = self._final_session(now)
        if final is None or (not force and self._bars_through == final.day):
            return 0
        start = trade_day(now) - timedelta(days=self.cfg.data.history_days)
        syms = self.tracked()
        try:
            raw = await self.alpaca.daily_bars(syms, start, now=now, adjustment="raw")
            adj = await self.alpaca.daily_bars(syms, start, now=now, adjustment="all")
        except AlpacaError as e:
            raise MarketDataError(f"daily bars: {e}") from e
        cal = self.calendar
        raw = {s: completed_bars(b, cal, now) for s, b in raw.items()}
        adj = {s: completed_bars(b, cal, now) for s, b in adj.items()}
        n = history.save_bars(self.store, raw) if self.store.writer else 0
        if self.store.writer:
            history.replace_adjusted(self.store, adj)
        self._bars_through = final.day
        missing = [s for s, b in raw.items() if not b or b[-1].day != final.day]
        if missing:
            logger.info("no %s bar yet for %s", final.day, ", ".join(missing))
        return n

    def raw_bars(self, symbol: str, since=None) -> List[Bar]:
        return history.raw_bars(self.store, symbol, since)

    def adjusted(self, symbol: str) -> List[Tuple]:
        return history.adjusted_closes(self.store, symbol)

    # -- marks -------------------------------------------------------------------------------
    async def _minute_closes(self, symbols: List[str]) -> Dict[str, Tuple[float, float]]:
        now = self.clock.time()
        missing = [s for s in symbols if s not in self._minutes]
        if missing or now - self._minutes_ts > MINUTE_TTL_SEC:
            try:
                self._minutes.update(await self.alpaca.latest_minute_closes(symbols, now=now))
                self._minutes_ts = now
            except AlpacaError as e:
                logger.warning("delayed minute closes failed: %s", e)
        return {s: self._minutes[s] for s in symbols if s in self._minutes}

    async def prices(self, symbols: Iterable[str]) -> Dict[str, Quote]:
        """The current price of each symbol that has one, by the session rules in
        `ledger/marks.py` (live quote, delayed minute close, or last official close). With
        the market shut, the last close is restated by the corporate actions the book has
        applied since (`ledger/corporate.restate_marks`)."""
        syms = sorted(set(symbols))
        if not syms:
            return {}
        now = self.clock.time()
        try:
            mt = self.calendar.state_at(now)
        except CalendarError as e:
            raise MarketDataError(str(e)) from e
        live = await self.live_quotes() if mt.is_open else {}
        bars = {s: self.raw_bars(s) for s in syms}
        need = [s for s in syms if (mt.is_open and s not in live) or
                (not mt.is_open and (mt.last_close is None or not bars[s] or bars[s][-1].day != mt.last_close.day))]
        delayed = await self._minute_closes(need) if need else {}
        out = {}
        for s in syms:
            m = mark(s, mt, live.get(s), delayed.get(s), bars[s])
            if m is not None:
                out[s] = m
        if not mt.is_open and mt.last_close is not None:
            out = corporate.restate_marks(self.store, out, mt.last_close.day)
        return out

    async def marks(self) -> Dict[str, Quote]:
        """A mark for each holding that has one (a missing one makes the book's value
        unknown: the ledger refuses to value a book with an unpriced holding)."""
        return await self.prices(Ledger(self.store).positions())

    # -- signals -----------------------------------------------------------------------------
    async def vol_regime(self, symbol: str) -> Tuple[Optional[float], str]:
        closes = [c for _, c in self.adjusted(symbol)]
        d = self.cfg.data
        vol = ind.annualized_vol(closes, d.vol_sessions, ind.TRADING_DAYS) if len(closes) > d.vol_sessions else None
        return vol, ind.regime(ind.efficiency_ratio(closes, d.er_sessions), d.er_threshold)

    def history(self) -> Mapping[str, List[Tuple]]:
        """Adjusted closes for every tracked symbol (for the packet and benchmarks)."""
        return {s: self.adjusted(s) for s in self.tracked()}

    async def snapshot(self, members: Mapping[str, str], events=None,
                       macro: Sequence[MacroReading] = ()) -> UsSnapshot:
        """The council's view: every traded member (symbol -> class) priced and measured,
        with breadth from the sector ETFs, the macro block and each asset's events."""
        now = self.clock.time()
        try:
            mt = self.calendar.state_at(now)
        except CalendarError as e:
            raise MarketDataError(str(e)) from e
        prices = await self.prices(members)
        return build_snapshot(self.cfg, now, mt, prices, self.history(), members, macro, events)

    async def benchmarks(self, base: Optional[Mapping]) -> Dict[str, Optional[float]]:
        """SPY and 60/40 equity from the day-0 baseline, carried to now by today's moves."""
        syms = (bm.SPY, bm.BONDS)
        closes = {s: self.adjusted(s) for s in syms}
        prices = await self.prices(syms)
        # A close-basis price on the last adjusted close's own day is that close: a 0 move.
        moves = {s: (prices[s].mid / closes[s][-1][1] - 1 if s in prices and closes[s] else None) for s in syms}
        return bm.benchmark_values(base, closes, moves)
