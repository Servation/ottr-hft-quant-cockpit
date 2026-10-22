# OTTR: the US-markets paper desk

A paper-trading desk for US markets where three models analyze and code acts. Design and
rationale: [`../docs/DESIGN.md`](../docs/DESIGN.md).

- **Universe:**
  - 16 core ETFs: stocks, bonds and cash, commodities, real estate.
  - Up to 6 single stocks: satellites a weekly code screener picks (40% of equity at most).
  - SGOV holds idle cash.
- **Council:** Claude, GPT (Codex CLI) and Gemini (Antigravity CLI) each give a stance per
  asset as validated JSON at 10:15 and 15:15 New York time on trading days. A Claude
  chair decides on anonymized reports. The chair never gives dollar sizes.
- **Code:**
  - **Sizing:** sizes BUYs (vol target x conviction x regime, topping up only to target).
  - **Gate:** runs the pre-trade gate (session, quote sanity, caps, sleeve, earnings blackout, corporate actions).
  - **Protective monitor:** volatility-scaled trailing stops set at entry, gap exits, concentration and sleeve trims, the drawdown breaker with persistent de-risking.
  - **Fills:** books simulated fills the way Alpaca would fill a marketable limit order, with the 2026 regulatory fees, in an exact Decimal SQLite ledger with FIFO tax lots.
- **Market data:** Alpaca's free feed (IEX quotes, SIP daily bars), the NYSE calendar,
  FRED macro series, Alpha Vantage earnings dates, the SEC's company lists and Alpaca's
  corporate actions (dividends, splits and cash mergers are booked; anything else is
  exited the session before it takes effect).
- **Operations:**
  - A supervisor restarts the engine.
  - A watchdog raises alarms.
  - Discord gets rate-limited notifications and a live view of each meeting.
  - A loopback-only JSON API serves the book and holds the kill switch.
- **Chat:** an optional Discord bot answers slash commands. It reads through the API and
  can't place a trade.

## Setup (Windows host, where the model CLIs are signed in)

```bash
cd ottr
python -m venv .venv          # Python 3.11+
.venv/Scripts/pip install -e ".[dev]"
.venv/Scripts/ottr data --check   # every feed answers with your keys
.venv/Scripts/ottr init           # opens the $10,000 book, loads the market data, runs the first screen
```

Secrets go in `ottr/.env` (gitignored; copy `.env.example`). The CLI reads it at start-up,
and real environment variables take precedence:

| Variable | What it does |
|---|---|
| `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY` | Alpaca market data (a free paper account; OTTR never sends Alpaca an order) |
| `ALPHAVANTAGE_API_KEY` | earnings dates (one call a day); without it no single stock can be bought |
| `SEC_USER_AGENT` | your name and email for the SEC (the screener's company lists and profiles) |
| `DISCORD_WEBHOOK_URL` | where meetings, fills, risk events, screens and alarms are posted (optional) |
| `OTTR_API_KEY` | 16+ chars; enables `POST /api/kill-switch` (the GETs need no key) |
| `OTTR_DRY_RUN=1` | kill switch from config: record "would fill" verdicts, never fill |
| `DISCORD_BOT_TOKEN`, `CEO_DISCORD_ID` | the chat bot (see [Discord chat](#discord-chat)) |
| `OTTR_CHAT_ALLOWED_IDS`, `DISCORD_GUILD_ID` | optional chat allow-list and server |

Set your tax rates in `config.toml` under `[tax]` (federal and state) for the after-tax
estimates.

## Commands

| Command | Writes? | What it does |
|---|---|---|
| `ottr run` | yes (holds the lock) | the engine (see [The trading day](#the-trading-day)) |
| `ottr supervise` | no (its child does) | runs `ottr run` (and `ottr chat` when it's set up), restarts each with backoff |
| `ottr init [--no-seed]` | yes | opens the book once, then loads the calendar, bars, events feeds and the first screen |
| `ottr data --check [--symbols A,B]` | no | checks Alpaca, FRED, Alpha Vantage, the SEC and corporate actions with your keys |
| `ottr screen [--top N]` | no | runs the stock screener now and shows what it would change (a few minutes) |
| `ottr tax [--year Y] [--detail]` | no | tax ESTIMATES: gains by term, wash sales, dividends, estimated tax, after tax |
| `ottr watch` | no | asks the desk watcher now what it would watch for and whether it would call a meeting (a scratch copy) |
| `ottr status [--json]` | no | health, the book (with after-tax equity), risk state, benchmarks, schedule |
| `ottr check` | no | replays the ledger; exits 2 if the books don't balance |
| `ottr meeting [--execute]` | no (yes with `--execute`) | one council meeting now; without `--execute` on a scratch copy |
| `ottr resume` | yes | clears a drawdown halt and re-baselines the peak |
| `ottr archive DEST [--retire]` | renames with `--retire` | a verified copy of the book (any version) with config, logs and a note; `--retire` then sets the original aside |
| `ottr chat [--check] [--standby]` | no (its own `chat.db`) | the Discord slash-command bot |
| `ottr service install\|status\|start\|stop\|restart\|uninstall` | Task Scheduler | the supervisor as a logon task; `stop` lets queued orders finish |

`ottr status` exit codes, for scripts: **0** healthy, **1** degraded (an alarm is active),
**2** ledger problem (same as `ottr check`), **3** engine not running.

## The trading day

Times are New York time; the engine follows Alpaca's NYSE calendar (holidays, early
closes, DST).

- **Overnight, from midnight:** corporate actions that take effect today are booked:
  - a dividend's receivable on its ex-date;
  - a split's new shares, with the stop restated;
  - a cash merger's payout.

  With the market shut the book is marked at the last official close, restated by what
  was applied since, so equity doesn't jump.
- **09:30 open:** quotes arrive; stops wait until 09:35, because the opening prints are
  noise. A holding that gapped through its stop overnight is sold at the bid.
- **10:15 and 15:15 meetings:** on an early close the later one moves to 45 minutes
  before the close. BUYs stop 10 minutes before the close.
- **In between, the watcher:** after every meeting and every 30 minutes, Claude reviews
  what changed and may call a meeting (see [The desk watcher](#the-desk-watcher)).
- **15:45 sweep:** cash above 5% of equity buys SGOV; a BUY short of cash sells SGOV first.
- **The close:** the day's regulatory fees are charged (rounded up, as Alpaca does). Once
  the official close is final (16 minutes later on the free plan), an equity point is
  recorded with the SPY and 60/40 benchmarks and the after-tax estimate.
- **Saturday 08:00:** the stock screener promotes and demotes satellites.

Engine tasks: market 60s, jobs 60s (fees, sweep, events feeds, deferred retries), risk 60s
in session, equity 15 min in session and once per close, council by the schedule (and
when the watcher calls one), watch 60s in session, the screen weekly, watchdog 60s.

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

At your logon, and every 5 minutes after (a keepalive that does nothing while it's
already running), the task starts `pythonw -m ottr.cli supervise` as you, with no console
window:
- It runs as you because the claude, codex and agy CLIs are signed in under your profile.
- It starts at **logon, not boot**: turn on Windows automatic sign-in if the PC has to
  recover from a reboot on its own.
- Keep the PC awake from 9:00 to 16:30 on trading days.

Logs, all in `data/logs/` and rotated at 5 MB: `engine.log`, `supervisor.log`,
`engine.out.log` (anything printed before logging started, such as a crash traceback),
and for the chat bot `chat.log` and `chat.out.log`.

To update the code: `ottr service stop`, `git pull`, `pip install -e ".[dev]"`,
`ottr service start`.

## Alarms

The watchdog checks every minute. An alarm is raised when:

- **A task is stale:** its last *success* is older than 3x its interval. The council is
  judged by its schedule (a missed meeting) and the screen by its weekly slot.
- **The risk monitor is blind:** a held asset has no usable live quote in session, so
  its stop isn't enforced.
- **An events feed is too old:** corporate actions or earnings dates count as unknown,
  which blocks the BUYs they guard.
- **A corporate action needs a person:**
  - it is in effect but not applied (and why);
  - a holding faces one the book can't model;
  - a holding was carried through one;
  - one was applied and then withdrawn by the source.
- **The books don't balance:** `ottr check` would fail.
- **The API couldn't bind its port.**

An alarm resolves after it has been clear for 5 minutes. Discord gets one post per raise
and resolve, a reminder every 6h while an alarm stays active, and at most one post per
alarm per 30 min while it flaps. Events and alarms have separate budgets, so a burst of
fills can't crowd out an alarm. Every raise and clear is also in the audit log.

The engine can't report its own death. Restarts are covered by the supervisor, and by
the next start (it posts that the previous run didn't shut down cleanly). A machine that
is simply off shows up as exit code 3 from `ottr status`.

## The desk watcher

Between the scheduled meetings, Claude (the council's `claude_model`, Opus) watches the desk.
After every meeting, and every 30 minutes in session, it reads what changed since the last
meeting and answers two questions:
- **Call a meeting now?** It answers yes only when something material changed that the last
  meeting didn't weigh: a holding breaking down, a level that mattered breaking, or a shock
  across assets.
- **What to watch until the next meeting?** Up to 8 conditions: an asset below or above a
  level, or moving more than some percent either way. Code checks them every minute on
  live quotes; one that is met calls a meeting and is used up.

**The limits are code:**
- in session only, and none in the last 45 minutes;
- at least 45 minutes after the last meeting and before the next scheduled one;
- at most 4 called meetings a day;
- never while a meeting runs.

A condition that is unpriced, more than 25% from the price, or already met is dropped and
said so.

The watcher can't trade. A called meeting runs the full council and the same risk gate;
its packet opens with why it was called, and the chair may well decide nothing. Discord
gets a post when the watched conditions change, when a meeting is called (and why), and
when a call is refused (and why). `/status`, `ottr status` and `GET /api/council` show
what it is watching. `ottr watch` asks it what it would do right now, changing nothing.
`[watch]` in config.toml holds every limit, the model and how often it reviews
(`enabled = false` turns it off).

## Watching a council meeting

A meeting takes a few minutes (the analysts in parallel, then the chair). When it starts,
the webhook posts one message and edits it as the meeting moves: each analyst's view and
calls as they finish, the chair's decision, the trades code dropped (a BUY in an earnings
blackout, a sunset satellite, an unmodeled corporate action) and, once recorded, the risk
gate's answer per trade (filled, dry run, deferred or rejected, and why). The usual
summary post follows, and so do fill posts. `/status` in the chat bot reads the same
progress from `GET /api/council`.

`[alerts]` in config.toml: `live_meeting = false` turns the live message off, and
`live_edit_sec` (1 to 60) sets the spacing between edits.

## HTTP API (for the dashboard)

Read-only JSON on `http://127.0.0.1:8765/api` (loopback only, enforced), through its own
`mode=ro` SQLite connection, so it can't write to the ledger.

| Endpoint | Returns |
|---|---|
| `GET /api/health` | alarms, task heartbeats, ledger check, kill switch |
| `GET /api/heartbeats` | per-task heartbeat age vs its stale limit |
| `GET /api/book` | equity, after-tax estimate, cash, dividends owed, P&L, risk state, the session, SPY and 60/40 benchmarks |
| `GET /api/positions` | holdings marked (live, delayed or at the close), with stop levels |
| `GET /api/equity?since=&until=&max_points=` | the equity curve with the benchmarks and after-tax equity |
| `GET /api/fills?limit=&before=` | fills, newest first |
| `GET /api/meetings?limit=&before=` | meetings with every analyst's stance and the gate's verdicts |
| `GET /api/meetings/{id}` | one meeting in full, including the packet the models saw |
| `GET /api/audit?kind=&since=&limit=` | the audit log |
| `GET /api/council` | a running meeting's progress, the next and the last meeting |
| `GET /api/universe` | the members (class, cap, buyable, fractional) and the satellites |
| `GET /api/screen` | the latest stock screen, the satellites and recent membership changes |
| `GET/POST /api/kill-switch` | read, or engage/release the runtime dry run |

The kill switch needs `Authorization: Bearer $OTTR_API_KEY`. An empty POST engages it:

```bash
curl -X POST -H "Authorization: Bearer $OTTR_API_KEY" http://127.0.0.1:8765/api/kill-switch
curl -X POST -H "Authorization: Bearer $OTTR_API_KEY" -d '{"engaged": false}' http://127.0.0.1:8765/api/kill-switch
```

An optional `"by"` labels who asked in the audit log. The state is stored in the ledger,
so it survives a restart, and it can't release a dry run that config or `OTTR_DRY_RUN`
imposes. Money fields from the ledger are exact decimal strings.

## Stock screener

Every Saturday at 08:00 the engine screens NYSE and Nasdaq stocks for the satellite
sleeve (`ottr init` runs the first screen; `ottr screen` shows one without changing
anything). It is deterministic code; no model can add a symbol.

- **Stages:**
  1. Alpaca's tradable stocks that the SEC lists as companies. Funds, ETFs, ETNs and
     leveraged or inverse names are excluded.
  2. Price of at least $10 and a 20-session median dollar volume of at least $50M; the
     1,200 most traded are kept.
  3. 13+ months of adjusted bars against the hard gates: history, no missing sessions,
     60-session vol of 8 to 70%, above the 200-session average.
  4. An SEC profile saying "operating company", checked only for stocks that passed. This
     is the slow step; profiles are cached 90 days.
- **Score:** percentile ranks of:
  - vol-adjusted 12-1 month momentum 30%
  - momentum net of SPY's 20%
  - efficiency ratio 15%
  - nearness to the 52-week high 10%
  - the size the risk model would allow 10%
  - low correlation with SPY 10%
  - rising volume 5%

  Overextended or pulling back halves it.
- **Membership:**
  - A hard-gate failure demotes at once.
  - A new satellite is kept for 14 days, then while it ranks in the top 12.
  - At most 2 rank-outs or swaps a week; a challenger must beat the weakest satellite by 15 points.
  - Candidates are passed over for:
    - correlation over 0.75 with a member
    - 28 days after a demotion
    - no positive momentum
    - a warning flag
    - a report within 5 sessions
  - A demoted stock that is held stays a member in "sunset" (SELL or HOLD only) until it is sold.

Each run goes to Discord as a digest, to `/screen`, `GET /api/screen` and `/ask`, and is
kept in the ledger. `[screen]` in config.toml holds every threshold.

## Tax estimates

`ottr tax`, the after-tax line in `ottr status`, and `after_tax` on every equity point
are ESTIMATES, not tax advice. They replay the ledger's FIFO lots:
- **Wash sales:** a loss is replaced by a purchase within 30 days either side; the loss
  moves into the new shares' basis with their holding period.
- **Terms:** short vs long term; GLD and SLV long-term gains are collectibles (at up to 28%).
- **Netting:** Schedule D netting, a $3,000 net-loss deduction and its carryforward.
- **Dividends:** qualified or ordinary by asset class.
- **Rates:** the ones in `[tax]`.

## Discord chat

`ottr chat` is a separate process that talks to you through slash commands. It connects
outbound to Discord (no public URL or open port), reads everything through the API above,
and keeps its own record in `data/chat.db`. It never opens the ledger, the API has no
trade endpoint, and its only write is the kill switch, so **no chat command can trade**.

| Command | Who | What it does |
|---|---|---|
| `/status` | owner, allow-list | health, alarms, kill switch, equity and P&L, benchmarks, risk, the session and council schedule |
| `/positions` | owner, allow-list | holdings with their marks, weights, unrealized P&L and distance to the stop |
| `/meeting` | owner, allow-list | the latest council meeting: the chair's decision, each order's result, every analyst's stances |
| `/screen` | owner, allow-list | the satellites, the latest screen's changes and the top of its ranking |
| `/ask <question>` | owner, allow-list | one model call over the book, positions, the last meeting, fills, the satellites and health |
| `/kill [reason]` | owner, allow-list | engages the kill switch at once (the safe direction) |
| `/release` | owner only | releases it, after a button confirmation |

Everyone else gets a private "only takes commands from its owner" reply, and the attempt is
recorded. `/status`, `/positions`, `/meeting` and `/screen` are plain code, so they work
when the models don't. `/ask` makes one model call at a time, at most `per_hour` in any
rolling hour, and stays quiet while a council meeting runs.

### Setting it up

1. **Bot application.** In the [Discord Developer Portal](https://discord.com/developers/applications),
   create an application (or reuse the existing OTTR bot). Under **Bot**, use **Reset
   Token** to copy a token. Leave every **Privileged Gateway Intent** off.
2. **Invite it.** Under **OAuth2 > URL Generator** tick the scopes `bot` and
   `applications.commands`, and the bot permissions **View Channels** and **Send Messages**.
3. **Your IDs.** In Discord, **User Settings > Advanced > Developer Mode**, then right-click
   your name > **Copy User ID** (and optionally the server > **Copy Server ID**).
4. **ottr/.env:** `DISCORD_BOT_TOKEN`, `CEO_DISCORD_ID`, optionally `DISCORD_GUILD_ID` and
   `OTTR_CHAT_ALLOWED_IDS`. `OTTR_API_KEY` must be set too, or `/kill` and `/release` are
   refused.
5. **Check, then run.** `ottr chat --check` validates all of this without connecting;
   `ottr service restart` starts the bot next to the engine when `[chat] enabled = true`.

The bot is a second child of `ottr supervise` with its own restart loop: a chat crash
restarts only the chat bot. A problem a restart can't fix (a rejected token, a missing
scope) exits with code 78; the supervisor then stops restarting the bot, posts one alert
and keeps the engine running. Only one bot runs at a time; a supervised bot waits for a
hand-started one to exit and then takes over.

## Tests

```bash
.venv/Scripts/python -m pytest -q -m "not live"   # the suite, offline
.venv/Scripts/python -m pyflakes src tests
.venv/Scripts/python -m pytest -q -m live         # smoke tests against the real feeds (needs the keys)
```
