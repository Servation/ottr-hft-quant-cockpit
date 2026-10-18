# OTTR v2

A paper-trading engine where three models analyze and code acts. Design and rationale:
[`../docs/DESIGN.md`](../docs/DESIGN.md).

- **Council:** Claude, GPT (Codex CLI) and Gemini (Antigravity CLI) each give a stance per
  asset as validated JSON. A Claude chair decides on anonymized reports. The chair never
  gives dollar sizes.
- **Code:** sizes BUYs (vol target x conviction x regime, topping up only to target), runs
  the pre-trade gate and the protective monitor (trailing stop, concentration trim,
  drawdown breaker), and books paper fills at bid/ask with fees in an exact Decimal
  SQLite ledger.
- **Operations:** a supervisor restarts the engine, a watchdog raises alarms on stale
  heartbeats, a blind risk monitor or a ledger mismatch, Discord gets rate-limited
  notifications, and a loopback-only JSON API serves the book and holds the kill switch.

## Setup (Windows host, where the model CLIs are signed in)

```bash
cd ottr
python -m venv .venv          # Python 3.11+
.venv/Scripts/pip install -e ".[dev]"
.venv/Scripts/ottr init --fresh   # or: init --import-v1 PATH (a v1 portfolio_state.json, marked at today's prices)
```

Secrets go in `ottr/.env` (gitignored; copy `.env.example`). The CLI reads it at start-up,
and real environment variables take precedence:

| Variable | What it does |
|---|---|
| `DISCORD_WEBHOOK_URL` | where meetings, fills, risk events and alarms are posted (optional) |
| `OTTR_API_KEY` | 16+ chars; enables `POST /api/kill-switch` (the GETs need no key) |
| `OTTR_DRY_RUN=1` | kill switch from config: record "would fill" verdicts, never fill |

## Commands

| Command | Writes? | What it does |
|---|---|---|
| `ottr run` | yes (holds the lock) | the engine: market + risk 60s, equity 15m, council every 4h, watchdog 60s, API |
| `ottr supervise` | no (its child does) | runs `ottr run`, restarts it with backoff (5s doubling to 5 min) |
| `ottr service install` | Task Scheduler | registers the supervisor to run at logon (see below); `--dry-run` prints the XML |
| `ottr service status\|start\|stop\|restart\|uninstall` | Task Scheduler | manage that task; `stop` lets queued orders finish first |
| `ottr meeting` | no | one council meeting on a scratch copy; prints dry-run verdicts |
| `ottr meeting --execute` | yes | one meeting that really paper-trades (engine must be stopped) |
| `ottr status [--json]` | no | health (alarms, heartbeats), book, risk state, benchmarks |
| `ottr check` | no | replays the fill log; exits 2 if the books don't balance |
| `ottr resume` | yes | clears a drawdown halt and re-baselines the peak |

`ottr status` exit codes, for scripts: **0** healthy, **1** degraded (an alarm is active),
**2** ledger problem (same as `ottr check`), **3** engine not running.

## Running unattended

```bash
.venv/Scripts/ottr service install --dry-run   # review the task first
.venv/Scripts/ottr service install             # registers "OTTR Engine" for the current user
.venv/Scripts/ottr service start               # or just sign out and back in
.venv/Scripts/ottr status                      # healthy within about a minute
```

Install from the checkout you'll keep (the main one, not a temporary worktree): the task
points at this checkout's `.venv\Scripts\pythonw.exe`. Re-run `service install` after
moving the checkout or recreating the venv.

What the task does: at your logon, and every 5 minutes after (a keepalive that does nothing
while it's already running), it starts `pythonw -m ottr.cli supervise` as you, with no
console window. It runs as you because the claude, codex and agy CLIs are signed in under
your profile. That means it starts at **logon, not boot**: turn on Windows automatic
sign-in if the PC has to recover from a reboot on its own. No time limit, and battery
power doesn't stop it.

Logs, all in `data/logs/` and rotated at 5 MB: `engine.log` (the engine),
`supervisor.log` (restarts), `engine.out.log` (anything the engine printed before it
could log, such as a crash traceback).

To update the code: `ottr service stop`, `git pull`, `pip install -e ".[dev]"`,
`ottr service start`.

## Alarms

The watchdog checks every minute. An alarm is raised when:

- a task's last *success* is older than 3x its interval (market and risk 3 min, equity
  45 min, council 12h), so a task that fails on every tick counts as stale too;
- the risk monitor is blind: a held asset has no fresh quote, so its stop isn't enforced;
- `ottr check` would fail (the books don't balance);
- the API couldn't bind its port.

An alarm resolves after it has been clear for 5 minutes. Discord gets one post per raise
and resolve, a reminder every 6h while an alarm stays active, and at most one post per
alarm per 30 min while it flaps (the next post says how many times it was raised).
Events and alarms have separate budgets (burst 10, then 30/h), so a burst of fills can't
crowd out an alarm. Every raise and clear is also in the audit log.

The engine can't report its own death. Restarts are covered by the supervisor (posts
"Engine exited ... restarts it in Ns"), and by the next start (posts that the previous
run didn't shut down cleanly). A machine that is simply off shows up as exit code 3 from
`ottr status`.

## HTTP API (for the dashboard)

Read-only JSON on `http://127.0.0.1:8765/api` (loopback only, enforced). It reads through
its own `mode=ro` SQLite connection, so it can't write to the ledger.

| Endpoint | Returns |
|---|---|
| `GET /api/health` | alarms, task heartbeats, ledger check, kill switch |
| `GET /api/heartbeats` | per-task heartbeat age vs its stale limit |
| `GET /api/book` | equity, cash, P&L, risk state, benchmarks |
| `GET /api/positions` | holdings marked at the bid, with trailing stop levels |
| `GET /api/equity?since=&until=&max_points=` | equity curve with BTC and equal-weight benchmarks |
| `GET /api/fills?limit=&before=` | fills, newest first, paginated by id |
| `GET /api/meetings?limit=&before=` | meetings with every analyst's stance and the gate's verdicts |
| `GET /api/meetings/{id}` | one meeting in full, including the packet the models saw |
| `GET /api/audit?kind=&since=&limit=` | the audit log |
| `GET/POST /api/kill-switch` | read, or engage/release the runtime dry run |

The kill switch needs `Authorization: Bearer $OTTR_API_KEY`. An empty POST engages it:

```bash
curl -X POST -H "Authorization: Bearer $OTTR_API_KEY" http://127.0.0.1:8765/api/kill-switch
curl -X POST -H "Authorization: Bearer $OTTR_API_KEY" -d '{"engaged": false}' http://127.0.0.1:8765/api/kill-switch
```

It is stored in the ledger, so it survives a restart. It can't release a dry run that
config or `OTTR_DRY_RUN` imposes. Money fields from the ledger are exact decimal strings;
marks and weights are numbers.

## Tests

```bash
.venv/Scripts/python -m pytest -q
.venv/Scripts/python -m pyflakes src tests
```
