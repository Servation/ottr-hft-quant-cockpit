"""
`ottr` command line.

  ottr init [--fresh | --import-v1 PATH]   open the book (once)
  ottr run                                 start the engine (the single writer)
  ottr meeting [--execute]                 one council meeting now (propose-only by default)
  ottr status                              read-only snapshot: book, risk, heartbeats
  ottr check                               replay the fill log and verify the books balance
  ottr resume                              clear the drawdown halt and re-baseline the peak

Read-only commands work while the engine runs. Writing commands need the writer lock,
so they refuse to run alongside the engine instead of racing it.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import tempfile
import logging
import sys
import time
from decimal import Decimal
from pathlib import Path

from ottr import config as cfgmod
from ottr.engine.engine import Engine
from ottr.ledger.book import Ledger
from ottr.ledger.store import LockError, Store
from ottr.market.service import MarketData
from ottr.notify import make_discord_notifier
from ottr.risk.monitor import RiskState


def _db(cfg) -> Path:
    return cfg.data_dir / "ottr.db"


def _writer(cfg) -> Store:
    try:
        return Store(_db(cfg), writer=True)
    except LockError:
        sys.exit("The engine (or another ottr command) holds the writer lock. Stop it first, "
                 "or use a read-only command (status, check, meeting without --execute).")


async def cmd_init(cfg, args) -> int:
    store = _writer(cfg)
    try:
        lg = Ledger(store)
        if lg.initialized():
            print("already initialized; nothing changed")
            return 1
        market = MarketData(cfg, archive=store)
        if args.import_v1:
            v1 = json.loads(Path(args.import_v1).read_text(encoding="utf-8"))  # read only
            quotes = await market.quotes(force=True)
            holdings = {}
            for a, h in v1.get("holdings", {}).items():
                q = Decimal(str(h.get("quantity", 0)))
                if q > 0:
                    if a not in quotes:
                        sys.exit(f"no quote for v1 holding {a}; cannot mark it")
                    holdings[a] = (q, quotes[a].mid)
            lg.initialize(Decimal(str(v1["cash"])), note=f"import from {args.import_v1}",
                          holdings=holdings)
        else:
            lg.initialize(cfg.trading.starting_cash)
        await Engine(cfg, store, market).equity_tick()  # sets the day-0 benchmark baseline
        _print_book(store, await market.quotes())
        return 0
    finally:
        store.close()


def _print_book(store: Store, quotes) -> None:
    lg = Ledger(store)
    v = lg.value(quotes)
    acct = lg.account()
    if v:
        print(f"equity ${v.equity:,.2f}  cash ${v.cash:,.2f}  invested ${v.invested:,.2f}  "
              f"realized ${float(acct['realized_pnl']):+,.2f}  fees ${float(acct['fees']):,.2f}")
    for a, p in lg.positions().items():
        q = quotes.get(a)
        mark = f"bid {q.bid:,.6g}  {(v.weights.get(a, 0) * 100 if v else 0):5.1f}%" if q else "no quote"
        print(f"  {a:5} qty {p.qty:.6f}  avg {float(p.avg_cost):,.6g}  high {p.high_water:,.6g}  {mark}")


async def cmd_status(cfg, args) -> int:
    store = Store(_db(cfg), writer=False)
    try:
        market = MarketData(cfg)
        quotes = await market.quotes()
        _print_book(store, quotes)
        st = RiskState.from_dict(store.get_state("risk"))
        print(f"risk: {'HALTED since ' + time.ctime(st.halted_since) if st.halted else 'normal'}  "
              f"peak {'$' + format(st.peak_equity, ',.2f') if st.peak_equity else 'n/a (no risk tick yet)'}  "
              f"cooldowns {list(st.cooldowns) or 'none'}")
        eq = store.conn.execute("SELECT ts,equity,bench_btc,bench_ew FROM equity ORDER BY ts DESC LIMIT 1").fetchone()
        if eq:
            print(f"last equity point {time.ctime(eq[0])}: ${eq[1]:,.2f}  vs BTC-hold ${eq[2] or 0:,.2f}  "
                  f"vs equal-weight-hold ${eq[3] or 0:,.2f}")
        now = time.time()
        for task, hb in sorted(store.heartbeats().items()):
            age = now - hb["ts"]
            print(f"  task {task:8} {'ok ' if hb['ok'] else 'ERR'} {age:7.0f}s ago  {hb['detail'] or ''}")
        nxt = store.get_state("next_meeting_at")
        if nxt:
            print(f"next council meeting: {time.ctime(nxt)}")
        return 0
    finally:
        store.close()


async def cmd_check(cfg, args) -> int:
    store = Store(_db(cfg), writer=False)
    try:
        problems = Ledger(store).check()
        print("books balance" if not problems else "PROBLEMS:\n  " + "\n  ".join(problems))
        return 0 if not problems else 2
    finally:
        store.close()


def _scratch_copy(cfg) -> Store:
    """A throwaway writable copy of the live book in a temp dir.

    Propose-only meetings run the full pipeline (council, gate verdicts) against real
    holdings but write only to this copy, so the live DB is never touched and the engine
    can keep running.
    """
    live = Store(_db(cfg), writer=False)
    scratch = Store(Path(tempfile.mkdtemp(prefix="ottr-propose-")) / "ottr.db", writer=True)
    live.conn.backup(scratch.conn)
    live.close()
    return scratch


async def cmd_meeting(cfg, args) -> int:
    if args.execute:
        store, notify = _writer(cfg), make_discord_notifier(cfg.discord_webhook_url)
    else:
        store, notify = _scratch_copy(cfg), None
        cfg = dataclasses.replace(cfg, trading=dataclasses.replace(cfg.trading, dry_run=True))
    try:
        engine = Engine(cfg, store, MarketData(cfg, archive=store), notify=notify)
        rec = await engine.meeting(execute=True)
        await engine.executor.drain()
        _print_meeting(rec, store)
        if not args.execute:
            print(f"\n(propose-only: verdicts are dry-run; scratch copy at {store.path})")
        return 0 if rec["status"] == "ok" else 1
    finally:
        store.close()


def _print_meeting(rec, store: Store) -> None:
    print(f"\nmeeting {rec['id']}: {rec['status']}  latency {rec['latency']}")
    for n, e in rec["errors"].items():
        print(f"  ! {n} dropped: {e}")
    if rec["tally"]:
        from ottr.council.council import format_tally
        print(format_tally(rec["tally"]))
    d = rec["decision"]
    if d:
        print(f"\nCHAIR: {d['summary']}\nDISSENT: {d['dissent_notes']}")
    for x in rec["dropped"]:
        print(f"  dropped {x['trade']['action']} {x['trade']['asset']}: {x['why']}")
    rows = store.conn.execute("SELECT idem_key,status,verdict FROM orders WHERE idem_key LIKE ?",
                              (f"council:{rec['id']}:%",)).fetchall()
    for key, status, verdict in rows:
        v = json.loads(verdict or "{}")
        print(f"  {key.split(':')[-1]:5} {v.get('side', ''):4} {status:8} {v.get('reason', '')} "
              f"${v.get('notional', 0):,.2f}  {'; '.join(v.get('notes', []))}")
    if not rec["trades"]:
        print("  no trades")


async def cmd_resume(cfg, args) -> int:
    store = _writer(cfg)
    try:
        market = MarketData(cfg)
        v = Ledger(store).value(await market.quotes())
        st = RiskState.from_dict(store.get_state("risk"))
        st.halted, st.halted_since = False, None
        if v:
            st.peak_equity = v.equity
        store.set_state("risk", st.to_dict())
        store.audit("risk_resume", why="operator", new_peak=st.peak_equity)
        print(f"halt cleared; peak re-baselined to ${st.peak_equity or 0:,.2f}")
        return 0
    finally:
        store.close()


async def cmd_run(cfg, args) -> int:
    store = _writer(cfg)
    try:
        if not Ledger(store).initialized():
            sys.exit("ledger not initialized: run `ottr init` first")
        market = MarketData(cfg, archive=store)
        engine = Engine(cfg, store, market, notify=make_discord_notifier(cfg.discord_webhook_url))
        await engine.run()
        return 0
    finally:
        store.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ottr")
    ap.add_argument("--config", type=Path, default=cfgmod.DEFAULT_CONFIG)
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--fresh", action="store_true")
    g.add_argument("--import-v1", metavar="PATH")
    sub.add_parser("run")
    p = sub.add_parser("meeting")
    p.add_argument("--execute", action="store_true", help="act on the decision (else propose-only)")
    sub.add_parser("status")
    sub.add_parser("check")
    sub.add_parser("resume")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = cfgmod.load(args.config)
    fn = {"init": cmd_init, "run": cmd_run, "meeting": cmd_meeting, "status": cmd_status,
          "check": cmd_check, "resume": cmd_resume}[args.cmd]
    return asyncio.run(fn(cfg, args))


if __name__ == "__main__":
    sys.exit(main())
