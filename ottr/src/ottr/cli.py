"""
`ottr` command line.

  ottr init [--fresh | --import-v1 PATH]   open the book (once)
  ottr run                                 start the engine (the single writer)
  ottr supervise                           run the engine (and the chat bot) and restart them if they exit
  ottr service install|uninstall|status|start|stop|restart
                                           the Windows scheduled task that runs `supervise`
  ottr chat [--check]                      the Discord slash-command bot (reads the API; can't trade)
  ottr screen [--json]                     rank other Kraken coins now (advisory; stores nothing)
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
from decimal import Decimal
from pathlib import Path
from typing import Optional

from ottr import config as cfgmod
from ottr import health
from ottr.engine.engine import Engine
from ottr.engine.killswitch import KillSwitch
from ottr.ledger.book import Ledger
from ottr.ledger.store import LockError, Store
from ottr.market.service import MarketData
from ottr.market.types import MarketDataError
from ottr.notify import make_alerts
from ottr.risk.monitor import RiskState

logger = logging.getLogger("ottr")
LOG_MAX_BYTES, LOG_BACKUPS = 5 * 1024 * 1024, 5


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
    else:
        print(f"equity unknown (a holding has no quote)  cash ${float(acct['cash']):,.2f}  "
              f"realized ${float(acct['realized_pnl']):+,.2f}  fees ${float(acct['fees']):,.2f}")
    for a, p in lg.positions().items():
        q = quotes.get(a)
        mark = f"bid {q.bid:,.6g}  {(v.weights.get(a, 0) * 100 if v else 0):5.1f}%" if q else "no quote"
        print(f"  {a:5} qty {p.qty:.6f}  avg {float(p.avg_cost):,.6g}  high {p.high_water:,.6g}  {mark}")


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
        try:
            quotes = await MarketData(cfg).quotes()
        except MarketDataError as e:
            print(f"(live quotes unavailable here: {e})")
            quotes = {}
        _print_book(store, quotes)
        st = RiskState.from_dict(store.get_state("risk"))
        print(f"risk: {'HALTED since ' + time.ctime(st.halted_since) if st.halted else 'normal'}  "
              f"peak {'$' + format(st.peak_equity, ',.2f') if st.peak_equity else 'n/a (no risk tick yet)'}  "
              f"cooldowns {list(st.cooldowns) or 'none'}")
        eq = store.conn.execute("SELECT ts,equity,bench_btc,bench_ew FROM equity ORDER BY ts DESC LIMIT 1").fetchone()
        if eq:
            print(f"last equity point {time.ctime(eq[0])}: ${eq[1]:,.2f}  vs BTC-hold ${eq[2] or 0:,.2f}  "
                  f"vs equal-weight-hold ${eq[3] or 0:,.2f}")
        for t in report.tasks:
            age = f"{health.fmt_age(report.ts - t.last_ts):>6} ago" if t.last_ts else "   never"
            state = "BAD" if not t.healthy else "ok" if t.last_ts else "wait"  # wait: still in its grace period
            print(f"  task {t.task:8} {state:4} {age}  (stale after {health.fmt_age(t.interval * health.STALE_FACTOR)})"
                  f"  {t.detail}")
        nxt = store.get_state("next_meeting_at")
        if nxt:
            print(f"next council meeting: {time.ctime(nxt)}")
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
    if args.execute:
        store, alerts = _writer(cfg), make_alerts(cfg.discord_webhook_url, cfg.alerts)
    else:
        store, alerts = _scratch_copy(cfg), make_alerts(None)
        cfg = dataclasses.replace(cfg, trading=dataclasses.replace(cfg.trading, dry_run=True))
    try:
        async with alerts.running():
            engine = Engine(cfg, store, MarketData(cfg, archive=store), alerts=alerts)
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
        engine = Engine(cfg, store, market, alerts=make_alerts(cfg.discord_webhook_url, cfg.alerts))
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


async def cmd_screen(cfg, args) -> int:
    from ottr.market import screen
    from ottr.market.kraken import Kraken
    if not args.json:  # one candle request per pair at Kraken's public rate: say why it's slow
        print(f"Scanning Kraken USD pairs (up to {cfg.screen.max_pairs}, about 1-2 minutes)...", flush=True)
    try:
        d = (await screen.run(Kraken(), cfg)).to_dict()
    except MarketDataError as e:
        print(f"screen failed: {e}")
        return 1
    if args.json:
        print(json.dumps(d, default=str))
        return 0
    rows = d["rows"]
    print(f"OTTR screener, live (not stored: the engine keeps its own daily runs). {d['listed']} Kraken USD "
          f"pairs listed, {len(rows)} ranked. * = in your universe. Advisory: nothing here is traded.")
    for r in rows:
        mark = "*" if r["in_universe"] else " "
        status = "QUALIFIES" if r["qualifies"] else screen.why_not(r)
        print(f" {mark}#{r['rank']:<3} {screen.line(r)} | {status}")
    top = screen.candidates(d, cfg.screen.top_n)
    print("\ncandidates: " + (", ".join(r["symbol"] for r in top) or "none qualifies right now"))
    if top:
        print(screen.promote_hint(top[0]))
    print("dropped before scoring: " + ", ".join(f"{k} {v}" for k, v in sorted(d["excluded"].items())))
    reasons: dict = {}
    for r in d["rejected"]:
        key = r["reason"].split(" (")[0]
        reasons[key] = reasons.get(key, 0) + 1
    if reasons:
        print("measured but ineligible: " + ", ".join(f"{k} {v}" for k, v in sorted(reasons.items())))
    for e in d["errors"]:
        print(f"  ! {e}")
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
    p = sub.add_parser("init")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--fresh", action="store_true")
    g.add_argument("--import-v1", metavar="PATH")
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
    p = sub.add_parser("screen", help="rank other Kraken coins now (advisory; prints, stores nothing)")
    p.add_argument("--json", action="store_true", help="the full result as JSON")
    sub.add_parser("check")
    sub.add_parser("resume")
    args = ap.parse_args(argv)

    cfgmod.load_env_file(Path(os.environ.get("OTTR_ENV_FILE") or cfgmod.ROOT / ".env"))
    cfg = cfgmod.load(args.config)
    log_file = args.log_file
    if args.cmd == "supervise" and log_file is None:
        log_file = cfg.data_dir / "logs" / "supervisor.log"
    # Under the supervisor the engine's stderr is a non-rotating capture file, so a
    # --log-file run logs to the rotating file only; `supervise` also echoes to a console.
    _setup_logging(args.verbose, log_file, console=log_file is None or args.cmd == "supervise",
                   errors_to_stderr=args.cmd == "chat")
    fn = {"init": cmd_init, "run": cmd_run, "supervise": cmd_supervise, "service": cmd_service,
          "chat": cmd_chat, "screen": cmd_screen, "meeting": cmd_meeting, "status": cmd_status, "check": cmd_check,
          "resume": cmd_resume}[args.cmd]
    return asyncio.run(fn(cfg, args))


if __name__ == "__main__":
    sys.exit(main())
