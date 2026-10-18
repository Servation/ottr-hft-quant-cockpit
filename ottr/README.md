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
- **Chat:** an optional Discord bot (`ottr chat`) answers slash commands from you: status,
  positions, the last meeting, questions to one model, and the kill switch. It reads
  through the API and can't place a trade.

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
| `DISCORD_BOT_TOKEN` | the chat bot's token (see [Discord chat](#discord-chat)); chat runs only when set |
| `CEO_DISCORD_ID` | your Discord user ID: the one account the chat bot fully obeys |
| `OTTR_CHAT_ALLOWED_IDS` | optional, comma-separated user IDs that may read, `/ask` and `/kill` (not `/release`) |
| `DISCORD_GUILD_ID` | optional server ID: register the commands in that one server only (they appear at once) |

## Commands

| Command | Writes? | What it does |
|---|---|---|
| `ottr run` | yes (holds the lock) | the engine: market + risk 60s, equity 15m, council every 4h, watchdog 60s, API |
| `ottr supervise` | no (its child does) | runs `ottr run` (and `ottr chat` when it's set up), restarts each with backoff (5s doubling to 5 min) |
| `ottr chat` | no (its own `chat.db`) | the Discord slash-command bot; reads the API, can only flip the kill switch |
| `ottr chat --check` | no | checks the chat config, secrets, engine API and model CLI without connecting to Discord |
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
could log, such as a crash traceback), and for the chat bot `chat.log` and `chat.out.log`.

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
| `GET /api/council` | whether a meeting is running now, the next and the last meeting |
| `GET/POST /api/kill-switch` | read, or engage/release the runtime dry run |

The kill switch needs `Authorization: Bearer $OTTR_API_KEY`. An empty POST engages it:

```bash
curl -X POST -H "Authorization: Bearer $OTTR_API_KEY" http://127.0.0.1:8765/api/kill-switch
curl -X POST -H "Authorization: Bearer $OTTR_API_KEY" -d '{"engaged": false}' http://127.0.0.1:8765/api/kill-switch
```

An optional `"by"` (letters, digits, `_.:-`) labels who asked in the audit log; the chat
bot sends `"by": "discord"`. It is stored in the ledger, so it survives a restart. It can't release a dry run that
config or `OTTR_DRY_RUN` imposes. Money fields from the ledger are exact decimal strings;
marks and weights are numbers.

## Discord chat

`ottr chat` is a separate process that talks to you through slash commands. It connects
outbound to Discord (no public URL or open port), reads everything through the API above,
and keeps its own record in `data/chat.db`. It never opens the ledger, the API has no
trade endpoint, and its only write is the kill switch, so **no chat command can trade**.

| Command | Who | What it does |
|---|---|---|
| `/status` | owner, allow-list | health, alarms, kill switch, equity and P&L, benchmarks, risk, council schedule |
| `/positions` | owner, allow-list | holdings marked at the bid, weight, unrealized P&L, distance to the trailing stop |
| `/meeting` | owner, allow-list | the latest council meeting: the chair's decision, each order's result, every analyst's stances and theses |
| `/ask <question>` | owner, allow-list | one model call over the book, positions, the last meeting, recent fills and health, plus the channel's last few exchanges |
| `/kill [reason]` | owner, allow-list | engages the kill switch at once (the safe direction) |
| `/release` | owner only | releases it, after a button confirmation |

Everyone else gets a private "only takes commands from its owner" reply, and the attempt is
recorded. `/status`, `/positions` and `/meeting` are plain code, so they work when the
models don't. `/ask` has guardrails: one model call at a time, at most `per_hour` calls in
any rolling hour (counted in `chat.db`, so a restart doesn't reset it), and it stays quiet
while a council meeting runs. It uses `[chat] model` at `[chat] effort` (default: Claude at
low effort, about 15 seconds an answer) and ends each answer with the model, the time
taken and the hourly count.

### Setting it up

1. **Bot application.** In the [Discord Developer Portal](https://discord.com/developers/applications),
   create an application, or reuse the v1 OTTR bot (its token is `DISCORD_BOT_TOKEN` in
   the repo-root `.env`; v1 is retired, so one bot is enough). Under **Bot**, use **Reset
   Token** to copy a token. Leave every **Privileged Gateway Intent** off: slash commands
   don't need Message Content.
2. **Invite it.** Under **OAuth2 > URL Generator** tick the scopes `bot` and
   `applications.commands`, and the bot permissions **View Channels** and **Send Messages**.
   Open the URL and pick your server. A bot that is already in the server only needs this
   again if it was invited without `applications.commands`.
3. **Your IDs.** In Discord, **User Settings > Advanced > Developer Mode**, then right-click
   your name > **Copy User ID**. Optionally right-click the server > **Copy Server ID**.
4. **ottr/.env:**
   ```
   DISCORD_BOT_TOKEN=...
   CEO_DISCORD_ID=123456789012345678
   # optional:
   DISCORD_GUILD_ID=123456789012345678
   OTTR_CHAT_ALLOWED_IDS=
   ```
   `OTTR_API_KEY` must be set too (the engine and the bot read the same file), or `/kill`
   and `/release` are refused.
5. **Check, then run.** `ottr chat --check` validates all of this without connecting.
   `ottr chat` runs the bot in a terminal for a first try. For unattended use, run
   `ottr service restart`: the supervisor starts the bot next to the engine whenever
   `[chat] enabled = true` and the token and owner ID are set.

The first start registers the commands (later starts only when they change). Without
`DISCORD_GUILD_ID` they are global, which can take a while to appear in the slash menu.

### How it is supervised

The bot is a second child of `ottr supervise` with its own restart loop and crash alerts.
A chat crash restarts only the chat bot, never the engine. A problem a restart can't fix
(a rejected token, a missing `applications.commands` scope) exits with code 78. The
supervisor then stops restarting the bot, posts one alert with the reason, and keeps the
engine running. Fix it, then `ottr service restart`.

`[chat]` in config.toml: `enabled`, `model` (claude | gpt | gemini), `effort` (checked
against what that model's CLI accepts), `per_hour`, `history_turns` (earlier exchanges in
the channel replayed into the prompt; the model CLIs are stateless) and `timeout_sec`.

## Tests

```bash
.venv/Scripts/python -m pytest -q
.venv/Scripts/python -m pyflakes src tests
```
