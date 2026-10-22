"""
US desk daily jobs: the close-of-day fee charge and the cash sweep into the T-bill ETF.

What it does:
- `close_of_day` charges the regulatory fees of every session that has closed (one cash
  event per day, rounded up), the way the broker does at the close. It is idempotent, so
  the engine can call it on every tick after the close and after any restart.
- `sweep_intent` returns, once per trading day, a code-sized BUY of the cash ETF (SGOV)
  for buying power above `keep_cash_pct` of equity. It fires at `sweep.at` (15:45 ET), or
  the same distance before an early close. Its idempotency key is the day, so a sweep that
  is deferred (a bad quote) is retried later that day, never doubled.

Why sweep: idle cash in a real brokerage account earns nothing unless it's invested, and
a desk that holds cash between trades would otherwise trail any benchmark by the T-bill
rate. The reverse direction (sell SGOV first when a BUY is short of cash) is the
executor's funding step, so cash is never stranded in either place.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import List, Mapping, Optional, Tuple

from ottr.broker.account import buying_power
from ottr.ledger.book import Ledger
from ottr.market.calendar import REGULAR_CLOSE, TradingCalendar
from ottr.market.types import Quote
from ottr.risk.types import Intent


def close_of_day(ledger: Ledger, cal: TradingCalendar, now: float) -> List[Tuple[object, Decimal]]:
    """Charge fees for every closed session up to now. Safe to call repeatedly."""
    last = cal.state_at(now).last_close
    return ledger.charge_fees(last.day, now=now) if last else []


def sweep_moment(cal: TradingCalendar, now: float, at_hm: str) -> Optional[float]:
    """When today's sweep fires, or None on a day with no session. `at_hm` is set against
    the regular 16:00 close; an early close moves it back by the same distance."""
    s = cal.state_at(now).session
    if s is None:
        return None
    h, m = (int(x) for x in at_hm.split(":"))
    minutes_before_close = (REGULAR_CLOSE.hour * 60 + REGULAR_CLOSE.minute) - (h * 60 + m)
    return (s.close - timedelta(minutes=minutes_before_close)).timestamp()


def sweep_intent(cfg, ledger: Ledger, cal: TradingCalendar, marks: Mapping[str, Quote],
                 now: float) -> Optional[Intent]:
    sw = cfg.sweep
    if not sw.enabled:
        return None
    mt = cal.state_at(now)
    moment = sweep_moment(cal, now, sw.at)
    if not mt.is_open or moment is None or now < moment:
        return None
    v = ledger.value(marks)
    if v is None or v.equity <= 0:
        return None
    bp = float(buying_power(cfg.account.type, ledger, cal, now).available)
    excess = bp - sw.keep_cash_pct / 100 * v.equity
    if excess < cfg.trading.min_trade_usd:
        return None
    day = mt.session.day.isoformat()
    return Intent(origin="sweep", asset=cfg.universe.cash_symbol, side="BUY", usd=round(excess, 2),
                  idem_key=f"sweep:{day}", ref=day, created_ts=now,
                  reason=f"sweep idle cash above {sw.keep_cash_pct:g}% of equity into {cfg.universe.cash_symbol}")
