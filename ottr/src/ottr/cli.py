"""
`ottr` command line.

  ottr init                                open the book (once) with config.toml's starting cash
  ottr run                                 start the engine (the single writer)
  ottr supervise                           run the engine (and the chat bot) and restart them if they exit
  ottr service install|uninstall|status|start|stop|restart
                                           the Windows scheduled task that runs `supervise`
  ottr chat [--check]                      the Discord slash-command bot (reads the API; can't trade)
  ottr data --check [--symbols A,B]        are the Alpaca, events and macro feeds usable? (reads only)
  ottr meeting [--execute]                 one council meeting now (propose-only by default)
  ottr status [--json]                     read-only health + book; exit code says what's wrong
  ottr check                               replay the fill log and verify the books balance
  ottr resume                              clear the drawdown halt and re-baseline the peak

Read-only commands work while the engine runs. Writing commands need the writer lock,
so they refuse to run alongside the engine instead of racing it. Secrets can live in a
gitignored ottr/.env (KEY=VALUE lines); real environment variables take precedence.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import logging.handlers
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple

from ottr import config as cfgmod
from ottr import health
from ottr.engine.engine import Engine
from ottr.engine.killswitch import KillSwitch
from ottr.events.refresh import Sources
from ottr.ledger import corporate
from ottr.ledger.book import Ledger
from ottr.ledger.marks import mark
from ottr.ledger.store import LockError, SchemaError, Store
from ottr.market.calendar import ET, MarketTime, SessionState
from ottr.market.history import load_calendar, raw_bars
from ottr.market.service import MarketData
from ottr.market.types import MarketDataError, Quote
from ottr.notify import make_alerts
from ottr.risk.monitor import RiskState

logger = logging.getLogger("ottr")
LOG_MAX_BYTES, LOG_BACKUPS = 5 * 1024 * 1024, 5
NO_KEYS = ("No Alpaca keys: set APCA_API_KEY_ID and APCA_API_SECRET_KEY in ottr/.env "
           "(Alpaca dashboard > Paper Trading > API Keys).")


def _db(cfg) -> Path:
    return cfg.data_dir / "ottr.db"


def _writer(cfg) -> Store:
    try:
        return Store(_db(cfg), writer=True)
    except LockError:
        sys.exit("The engine (or another ottr command) holds the writer lock. Stop it first, "
                 "or use a read-only command (status, check, meeting without --execute).")


def _alpaca(required: bool):
    """The Alpaca data client from the keys in the environment, or None (or exit) without them."""
    from ottr.market.alpaca import Alpaca, Credentials
    creds = Credentials.from_env()
    if creds is None:
        if required:
            sys.exit(NO_KEYS)
        return None
    return Alpaca(creds)


def _sources(alpaca) -> Sources:
    from ottr.events.earnings import AlphaVantage
    from ottr.events.fred import Fred
    from ottr.events.sec import Sec
    return Sources(fred=Fred(), alpha_vantage=AlphaVantage.from_env(), sec=Sec.from_env(), alpaca=alpaca)


def _et(ts: float, fmt: str = "%a %Y-%m-%d %H:%M") -> str:
    return datetime.fromtimestamp(ts, tz=ET).strftime(fmt) + " ET"


def _market_line(mt: MarketTime) -> str:
    if mt.state is SessionState.OPEN:
        return f"market OPEN until {_et(mt.session.close_ts, '%H:%M')}"
    nxt = mt.next_open
    return f"market closed; next open {_et(nxt.open_ts) if nxt else 'beyond the stored calendar'}"


async def cmd_init(cfg, args) -> int:
    store = _writer(cfg)
    try:
        lg = Ledger(store)
        if lg.initialized():
            print("already initialized; nothing changed")
            return 1
        lg.initialize(cfg.account.starting_cash)
        print(f"Book opened: ${cfg.account.starting_cash:,.2f} in a {cfg.account.type} account. The benchmarks "
              "(SPY and 60/40) start from the first close the engine records. Next: `ottr run` (or "
              "`ottr service start`).")
        return 0
    finally:
        store.close()


async def _marks(cfg, store: Store) -> Tuple[Dict[str, Quote], Optional[str]]:
    """Prices for the holdings, and why there are none when there aren't. With Alpaca keys:
    the engine's own rules (live in session). Without: the stored official closes only."""
    held = Ledger(store).positions()
    if not held:
        return {}, None
    alpaca = _alpaca(required=False)
    if alpaca is not None:
        try:
            return await MarketData(cfg, alpaca, store).marks(), None
        except MarketDataError as e:
            return {}, str(e)
    cal = load_calendar(store)
    if cal is None:
        return {}, "no stored calendar yet"
    try:
        mt = cal.state_at(time.time())
    except Exception as e:
        return {}, str(e)
    out = {s: m for s in held if (m := mark(s, mt, None, None, raw_bars(store, s))) is not None}
    if not mt.is_open and mt.last_close is not None:
        out = corporate.restate_marks(store, out, mt.last_close.day)
    return out, None if len(out) == len(held) else "live prices need the Alpaca keys"


def _print_book(store: Store, marks: Dict[str, Quote]) -> None:
    lg = Ledger(store)
    v = lg.value(marks)
    acct = lg.account()
    if v:
        owed = f"  dividends owed ${v.receivable:,.2f}" if v.receivable else ""
        print(f"equity ${v.equity:,.2f}  cash ${v.cash:,.2f}  invested ${v.invested:,.2f}{owed}  "
              f"realized ${float(acct['realized_pnl']):+,.2f}  fees ${float(acct['fees']):,.2f}")
    else:
        print(f"equity unknown (a holding has no price)  cash ${float(acct['cash']):,.2f}  "
              f"realized ${float(acct['realized_pnl']):+,.2f}  fees ${float(acct['fees']):,.2f}")
    for a, p in lg.positions().items():
        q = marks.get(a)
        px = f"${q.bid:,.2f} {q.basis}  {(v.weights.get(a, 0) * 100 if v else 0):5.1f}%" if q else "no price"
        print(f"  {a:6} qty {p.qty:.4f}  avg ${float(p.avg_cost):,.2f}  {px}")


def _usd(x: Optional[float], unknown: str = "n/a") -> str:
    return unknown if x is None else f"${x:,.2f}"


async def cmd_status(cfg, args) -> int:
    try:
        store = Store(_db(cfg), writer=False)
    except FileNotFoundError as e:
        print(json.dumps({"ok": False, "exit_code": health.EXIT_DOWN, "error": str(e)}) if args.json else e)
        return health.EXIT_DOWN
    try:
        with store.snapshot():
            report = health.evaluate(cfg, store, inside_engine=False)
            kill = KillSwitch(cfg, store).state()
            engine_state = store.get_state("engine") or {}
        if args.json:
            print(json.dumps(dict(report.to_dict(), kill_switch=kill, engine=engine_state), default=str))
            return report.exit_code

        verdict = {health.EXIT_OK: "OK", health.EXIT_DEGRADED: "DEGRADED", health.EXIT_LEDGER: "LEDGER PROBLEM",
                   health.EXIT_DOWN: "ENGINE DOWN"}[report.exit_code]
        print(f"OTTR {verdict} (exit {report.exit_code})")
        for a in report.alarms:
            print(f"  ! [{a.severity}] {a.message}")
        if kill["effective_dry_run"]:
            src = "config/OTTR_DRY_RUN" if kill["config_dry_run"] else f"operator via {kill['by']}"
            print(f"kill switch ENGAGED ({src}): verdicts are recorded as dry run, nothing fills")
        if not Ledger(store).initialized():
            print("ledger not initialized (run `ottr init`)")
            return report.exit_code
        cal = load_calendar(store)
        if cal is not None and cal.covers_day(datetime.now(ET).date()):
            print(_market_line(cal.state_at(time.time())))
        marks, why = await _marks(cfg, store)
        if why:
            print(f"(prices unavailable here: {why})")
        _print_book(store, marks)
        st = RiskState.from_dict(store.get_state("risk"))
        print(f"risk: {'HALTED since ' + _et(st.halted_since) if st.halted else 'normal'}  "
              f"peak {_usd(st.peak_equity, 'n/a (set by the first in-session risk check)')}  cooldowns {list(st.cooldowns) or 'none'}")
        eq = store.conn.execute("SELECT ts,equity,bench_spy,bench_60_40 FROM equity ORDER BY ts DESC LIMIT 1").fetchone()
        if eq:
            later = "n/a until the first close"
            print(f"last equity point {_et(eq[0])}: ${eq[1]:,.2f}  vs SPY hold {_usd(eq[2], later)}  "
                  f"vs 60/40 {_usd(eq[3], later)}")
        for t in report.tasks:
            age = f"{health.fmt_age(report.ts - t.last_ts):>6} ago" if t.last_ts else "   never"
            state = "BAD" if not t.healthy else "ok" if t.last_ts else "wait"  # wait: still in its grace period
            print(f"  task {t.task:8} {state:4} {age}  ({t.expect})  {t.detail}")
        nxt = store.get_state("next_meeting_at")
        if nxt:
            print(f"next council meeting: {_et(nxt)}")
        return report.exit_code
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
    alpaca = _alpaca(required=True)
    if args.execute:
        store, alerts = _writer(cfg), make_alerts(cfg.discord_webhook_url, cfg.alerts)
    else:
        store, alerts = _scratch_copy(cfg), make_alerts(None)
        cfg = dataclasses.replace(cfg, trading=dataclasses.replace(cfg.trading, dry_run=True))
    try:
        async with alerts.running():
            market = MarketData(cfg, alpaca, store)
            engine = Engine(cfg, store, market, alerts=alerts)
            try:
                await market.refresh_calendar()
                await market.refresh_bars()
                await engine.refresh_members()
            except MarketDataError as e:
                print(f"market data unavailable: {e}")
                return 2
            if not market.calendar.state_at(time.time()).is_open:
                print("(the market is closed: any trade the council decides is deferred, then expires)")
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
        print(f"  {key.split(':')[-1]:6} {v.get('side', ''):4} {status:8} {v.get('reason', '')} "
              f"${v.get('notional', 0):,.2f}  {'; '.join(v.get('notes', []))}")
    if not rec["trades"]:
        print("  no trades")


async def cmd_resume(cfg, args) -> int:
    store = _writer(cfg)
    try:
        marks, why = await _marks(cfg, store)
        v = Ledger(store).value(marks)
        st = RiskState.from_dict(store.get_state("risk"))
        st.halted, st.halted_since = False, None
        if v:
            st.peak_equity = v.equity
        store.set_state("risk", st.to_dict())
        store.audit("risk_resume", why="operator", new_peak=st.peak_equity)
        if v:
            print(f"halt cleared; peak re-baselined to ${st.peak_equity:,.2f}")
        else:
            print(f"halt cleared; peak unchanged (no price for the book: {why})")
        return 0
    finally:
        store.close()


async def cmd_run(cfg, args) -> int:
    alpaca = _alpaca(required=True)
    store = _writer(cfg)
    try:
        if not Ledger(store).initialized():
            sys.exit("ledger not initialized: run `ottr init` first")
        market = MarketData(cfg, alpaca, store)
        engine = Engine(cfg, store, market, alerts=make_alerts(cfg.discord_webhook_url, cfg.alerts),
                        sources=_sources(alpaca))
        return await engine.run()
    except Exception:
        logger.exception("engine crashed")  # into the log file; the supervisor restarts us
        raise
    finally:
        store.close()


def _console_python() -> str:
    """python.exe for the engine child even when the supervisor runs under pythonw.exe.

    A console-subsystem engine started with CREATE_NO_WINDOW owns a hidden console that
    the model CLIs inherit; under pythonw each CLI would open a visible window instead.
    """
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
        return str(exe.with_name("python.exe"))
    return str(exe)


def supervised(cfg, config_path: Path, verbose: bool = False):
    """(engine argv, companion children, notes) for `ottr supervise`.

    The chat bot joins only when it can run (enabled, token and owner set); otherwise a
    note says why, so a missing token is one log line rather than a crash loop. It runs
    with --standby, so a bot started by hand keeps the token until it exits.
    """
    from ottr.chat import EXIT_CONFIG, unavailable
    from ottr.supervisor import Child

    logs = cfg.data_dir / "logs"

    def argv(cmd: str, log_name: str, *extra: str) -> list:
        a = [_console_python(), "-m", "ottr.cli", "--config", str(Path(config_path).resolve()),
             "--log-file", str(logs / log_name)]
        return a + (["-v"] if verbose else []) + [cmd, *extra]

    companions, notes = [], []
    why = unavailable(cfg)
    if why:
        notes.append(f"chat bot not started: {why}")
    else:
        companions.append(Child("chat", argv("chat", "chat.log", "--standby"), logs / "chat.out.log",
                                give_up_codes=frozenset({EXIT_CONFIG})))
    return argv("run", "engine.log"), companions, notes


async def cmd_supervise(cfg, args) -> int:
    from ottr.supervisor import Supervisor
    engine, companions, notes = supervised(cfg, args.config, args.verbose)
    for n in notes:
        logger.info(n)
    sup = Supervisor(engine, cfg.data_dir, make_alerts(cfg.discord_webhook_url, cfg.alerts),
                     companions=companions)
    logger.info("supervisor starting for %s (%s)", cfg.data_dir,
                ", ".join(c.name for c in sup.children))
    return await sup.run()




async def cmd_data(cfg, args) -> int:
    from ottr.events import check as eventcheck
    from ottr.market import datacheck
    from ottr.market.symbols import normalize
    alpaca = _alpaca(required=False)
    if alpaca is None:
        print(NO_KEYS)
        return 3
    try:
        symbols = [normalize(s) for s in args.symbols.split(",")] if args.symbols else list(cfg.core)
    except ValueError as e:
        print(e)
        return 2
    print(f"Checking Alpaca for {len(symbols)} symbols (about 30 seconds)...", flush=True)
    code = await datacheck.run(alpaca, symbols, time.time())
    print()
    print("Checking the events and macro feeds (FRED, Alpha Vantage, SEC, corporate actions)...", flush=True)
    return max(code, await eventcheck.run(_sources(alpaca), cfg.events, symbols, time.time()))


def screen_report(res, top: int) -> str:
    """The screen as plain text for a terminal: where the pool shrank, the decision, and
    the top of the ranking with the numbers behind each score."""
    k, d = res.counts, res.decision
    lines = [f"Stock screen for {res.day} ({res.kind}): {k.get('listed', 0):,} listed stocks, "
             f"{k.get('liquid', 0):,} liquid (kept {k.get('pool', 0):,}), {k.get('measured', 0):,} measured, "
             f"{k.get('passed', 0):,} passed the gates, {k.get('ranked', 0):,} of them operating companies (ranked)."]
    for title, items in (("Promote", d.promote), ("Demote", d.demote), ("Keep", d.keep), ("Passed over", d.passed)):
        if items:
            lines.append(f"{title}:")
            lines += [f"  {s:6} {why}" for s, why in items]
    if not (d.promote or d.demote):
        lines.append("No membership changes.")
    lines.append(f"\n{'#':>4} {'symbol':6} {'score':>5} {'12-1':>6} {'resid':>6} {'vol':>4} {'high':>5} "
                 f"{'ER':>5} {'corr':>5} {'earn':>4}  flags")
    for r in res.ranked[:top]:
        def f(x, spec):
            return format(x, spec) if x is not None else "n/a"
        lines.append(f"{r.rank:>4} {r.symbol:6} {r.score:>5.1f} {f(r.momentum_return and r.momentum_return * 100, '+5.0f')}% "
                     f"{f(r.residual, '6.2f')} {f(r.vol and r.vol * 100, '3.0f')}% "
                     f"{f(r.near_high and r.near_high * 100, '4.0f')}% {f(r.efficiency, '5.2f')} "
                     f"{f(r.corr_spy, '5.2f')} {f(r.earnings_sessions, '>4')}  {', '.join(r.flags)}")
    if res.errors:
        lines.append("Errors: " + "; ".join(res.errors))
    return "\n".join(lines)


async def cmd_screen(cfg, args) -> int:
    from ottr.events.sec import Sec
    from ottr.universe.run import ScreenError, run_screen
    alpaca = _alpaca(required=True)
    try:
        store = Store(_db(cfg), writer=False)
    except FileNotFoundError as e:
        print(e)
        return 3
    cal = load_calendar(store)
    if cal is None:
        print("No stored trading calendar yet: start the engine once (`ottr run`) so it loads one.")
        return 3
    print("Screening NYSE and Nasdaq stocks (a few minutes; changes nothing)...", flush=True)
    try:
        res = await run_screen(cfg, store, alpaca, Sec.from_env(), cal, time.time(), kind="dry_run",
                               progress=lambda line: print(f"  {line}", flush=True))
    except (ScreenError, MarketDataError) as e:
        print(f"screen failed: {e}")
        return 1
    print(screen_report(res, args.top))
    return 0


async def cmd_chat(cfg, args) -> int:
    from ottr.chat.bot import check_chat, run_chat  # discord.py is only imported for chat
    return await (check_chat(cfg) if args.check else run_chat(cfg, standby=args.standby))


async def cmd_service(cfg, args) -> int:
    from ottr import service
    name = args.name
    if args.action == "install":
        spec = service.default_spec(args.config, name)
        warns = service.preflight(cfg.council.analysts, cfg.discord_webhook_url)
        if os.name != "nt" and not args.dry_run:
            print("Task Scheduler is Windows-only; use --dry-run to see the task XML")
            return 2
        return service.install(spec, cfg.data_dir, dry_run=args.dry_run, warnings=warns)
    if os.name != "nt":
        print("Task Scheduler is Windows-only")
        return 2
    if args.action == "uninstall":
        return service.uninstall(name, cfg.data_dir)
    if args.action == "status":
        rc = service.status(name)
        print("engine health: `ottr status`")
        return rc
    if args.action == "start":
        return service.start(name)
    if args.action == "stop":
        return service.stop(name, cfg.data_dir)
    rc = service.stop(name, cfg.data_dir)  # restart
    return rc or service.start(name)


def _setup_logging(verbose: bool, log_file: Optional[Path], console: bool,
                   errors_to_stderr: bool = False) -> None:
    handlers: list = []
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            log_file, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"))
    if console and sys.stderr is not None:  # None under pythonw.exe
        handlers.append(logging.StreamHandler())
    elif errors_to_stderr and sys.stderr is not None:
        # Under the supervisor stderr is <name>.out.log, whose last line becomes the crash
        # alert: errors only, so the alert says why without the capture file growing.
        h = logging.StreamHandler()
        h.setLevel(logging.ERROR)
        h.setFormatter(logging.Formatter("%(message)s"))  # read on a phone: no timestamp noise
        handlers.append(h)
    if not handlers:
        handlers.append(logging.NullHandler())
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, handlers=handlers, force=True,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ottr")
    ap.add_argument("--config", type=Path, default=cfgmod.DEFAULT_CONFIG)
    ap.add_argument("--log-file", type=Path, help="log to this rotating file instead of stderr")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="open the book (once) with the starting cash in config.toml")
    sub.add_parser("run")
    sub.add_parser("supervise", help="run the engine (and the chat bot) and restart them whenever they exit")
    p = sub.add_parser("chat", help="the Discord slash-command bot (reads the engine API, can't trade)")
    p.add_argument("--check", action="store_true", help="check config, secrets, API and model CLI; don't connect")
    p.add_argument("--standby", action="store_true",
                   help="if another chat bot is running, wait for it to exit instead of quitting (the supervisor uses this)")
    p = sub.add_parser("service", help="manage the Windows scheduled task that runs `supervise`")
    p.add_argument("action", choices=["install", "uninstall", "status", "start", "stop", "restart"])
    p.add_argument("--name", default="OTTR Engine")
    p.add_argument("--dry-run", action="store_true", help="install: print the task XML, register nothing")
    p = sub.add_parser("meeting")
    p.add_argument("--execute", action="store_true", help="act on the decision (else propose-only)")
    p = sub.add_parser("status")
    p.add_argument("--json", action="store_true", help="machine-readable health report")
    p = sub.add_parser("data", help="US desk: check the Alpaca, events and macro feeds (reads only)")
    p.add_argument("--check", action="store_true", required=True, help="run the feed check")
    p.add_argument("--symbols", help="comma-separated symbols (default: the core universe in config.toml)")
    p = sub.add_parser("screen", help="run the stock screener now and show what it would change (changes nothing)")
    p.add_argument("--top", type=int, default=25, help="ranked stocks to show")
    sub.add_parser("check")
    sub.add_parser("resume")
    args = ap.parse_args(argv)

    try:
        cfgmod.load_env_file(Path(os.environ.get("OTTR_ENV_FILE") or cfgmod.ROOT / ".env"))
        cfg = cfgmod.load(args.config)
    except cfgmod.ConfigError as e:
        # A typo in config.toml or ottr/.env: say which, without a traceback. Exit 2, like
        # the other "fix this before running" errors.
        print(f"ottr: {args.config.name}: {e}", file=sys.stderr)
        return 2
    log_file = args.log_file
    if args.cmd == "supervise" and log_file is None:
        log_file = cfg.data_dir / "logs" / "supervisor.log"
    # Under the supervisor the engine's stderr is a non-rotating capture file, so a
    # --log-file run logs to the rotating file only; `supervise` also echoes to a console.
    _setup_logging(args.verbose, log_file, console=log_file is None or args.cmd == "supervise",
                   errors_to_stderr=args.cmd == "chat")
    fn = {"init": cmd_init, "run": cmd_run, "supervise": cmd_supervise, "service": cmd_service,
          "chat": cmd_chat, "data": cmd_data, "meeting": cmd_meeting, "status": cmd_status, "check": cmd_check,
          "resume": cmd_resume, "screen": cmd_screen}[args.cmd]
    try:
        return asyncio.run(fn(cfg, args))
    except SchemaError as e:
        # A book this version can't open (an archived crypto book, or a newer one): a
        # plain message for a person, not a traceback. Exit 2, like a ledger problem.
        print(f"ottr: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
