# OTTR design

How OTTR works and why. Usage and operations live in [`ottr/README.md`](../ottr/README.md).

## The idea

Three models analyze the market independently. A chair model decides. Deterministic code
does everything that has a right answer: sizing, risk limits, execution, accounting and
vote counting.

The previous version ran on a weak local model whose tool calls were unreliable, so much
of its code existed only to compensate (regex vote parsing, tool-call re-asks, prompt hints
to steer sizing). Strong models return reliable structured JSON, so OTTR gives them one job,
judgment, and keeps them away from anything that moves money.

## Principles

1. **Models judge, code acts.** Models return schema-validated JSON and never call tools.
   No model output can carry a dollar size.
2. **Independence, then synthesis.** Analysts see one shared market packet and never see
   each other. The chair sees their reports anonymized as A/B/C, so it weighs arguments,
   not brands.
3. **Fail closed.** No quorum (fewer than 2 analysts), a failed chair, stale market data or a
   failed check all mean no trade, and the reason is recorded.
4. **Unknown is never zero.** A missing price, volatility or indicator stays `None` and is
   labeled "n/a" for the models. Sizing treats unknown volatility as high volatility.
5. **Everything is recorded.** Every packet, raw reply, parsed decision, gate verdict and
   fill goes to SQLite, so any trade traces back to its inputs.
6. **Paper only.** There is no exchange order path. The broker is simulated.

## Architecture

```
ottr/src/ottr/
  config.py        typed config (config.toml + env + ottr/.env), validated at load
  market/          Kraken quotes (bid/ask/24h high) and closed daily candles, Coinbase
                   fallback quotes, pure-Python indicators (EMA, RSI, MACD, efficiency
                   ratio, regime, 30d vol, return correlation), MarketSnapshot + packet,
                   and the daily screener of other Kraken pairs (advisory)
  council/         CLI model adapters (claude -p, codex exec, agy -p), schemas, Council
  risk/            gate (pre-trade checks + sizing), monitor (trailing stop, trim,
                   drawdown breaker), Intent/Verdict types
  ledger/          SQLite store (WAL, single writer via OS lock) and Decimal ledger
  engine/          executor (the only path to a fill), engine loop, kill switch, meeting
                   progress (what a running meeting has done so far)
  health.py        alarms and status exit codes shared by watchdog, CLI and API
  notify.py        rate-limited Discord webhook posts, and the live message edited in place
  api.py           read-only JSON API on 127.0.0.1 + authenticated kill switch
  supervisor.py    restarts `ottr run` (and `ottr chat`) with backoff
  service.py       Windows Task Scheduler registration for the supervisor
  control.py       stop-request file (graceful stop for a windowless process)
  proc.py          hidden subprocesses, process-tree kills, kill-on-close Job Object
  chat/            `ottr chat`: Discord slash-command bot in its own process; reads the API,
                   one model per /ask, can only flip the kill switch; own record in chat.db
  cli.py           ottr init | run | supervise | service | chat | meeting | status | check | resume
```

One engine process is the only writer. Read-only commands, the API and propose-only
meetings open the database read-only or work on a scratch copy.

### A council meeting

1. Code builds the packet: time-anchored market data with data-quality flags, plus the
   book (equity, cash, holdings, risk-model target weights, caps, recent fills, last
   meeting's summary).
2. Claude, GPT and Gemini each return a stance for every asset (BUY / SELL / HOLD,
   conviction 0-1, thesis, key risk), each through its own lens: risk and regime, quant
   and technical, sentiment and contrarian.
3. Code tallies the stances and gives the tally to the chair as a fact.
4. The chair returns at most 3 trades: `BUY {asset, conviction}` or
   `SELL {asset, fraction_of_position}`.
5. Code enforces the rules: no BUY unless at least one analyst rated the asset BUY, and
   no SELL of an asset that isn't held (the desk is long-only spot).
6. Each surviving trade becomes an Intent for the executor.

**Watching it happen.** The council reports progress through a synchronous callback as
each step finishes: an analyst done (with its stances) or dropped out, the chair
deliberating, decided or failed. The engine folds those events, and then the executor's
verdict on each trade, into one `MeetingProgress` state. `GET /api/council` serves it,
and one Discord message shows it, posted when the meeting starts and edited as it moves.
The council never learns who is watching, and the callback swallows its own errors and
does no I/O, so a slow reader or a Discord outage can't delay or fail a meeting. Edits go
through their own sender, coalesced to the newest state and spaced at least
`live_edit_sec` apart (Discord allows a webhook about 5 requests per 2 seconds). Only
the first post costs an event token, so a meeting can't starve the fill and alarm posts.
A failed edit just leaves the last state showing. The summary post still goes out at
the end, because an edit notifies no one. If the engine stops mid-meeting, the message
says the meeting was cut short instead of showing "thinking" forever.

### From Intent to fill

The executor drains one priority queue. Protective exits (stop, de-risk, trim) run before
council orders. For each Intent it:

1. claims the idempotency key, so a repeated or retried Intent can never fill twice;
2. refreshes the quote and rebuilds the context from the live book;
3. runs the gate, which returns an exact Verdict or a rejection with its reason;
4. commits the fill, the order status, the cooldown and the audit row in **one** SQLite
   transaction (in dry run it records the verdict instead).

A transient rejection (stale or missing quote) stays retryable. A dry-run verdict doesn't
block real execution later.

## Risk policy

**Sizing (BUYs only).** A BUY's conviction maps to a target weight:
`2% / annualized vol x conviction`, quartered when the regime is CHOPPY or unknown, and
capped by the asset's cap and a 50% maximum position. Code buys only the gap between the
target and what is already held, so repeated meetings can't keep adding to an unchanged
thesis. The gap is then clamped by `max_trade_usd` ($2,500), the 90% gross-exposure cap
and a 2% cash reserve. Anything under $100 is dropped.

**The gate.** Every order needs a quote under 3 minutes old, and a council decision
older than 15 minutes is dropped. On top of that, the drawdown halt, a cooldown, or a risk
action on the asset during the meeting block a BUY, but **never a SELL**: limits exist to
stop risk going up, never to stop it going down. A SELL needs only a position and a fresh
price. A remainder too small to trade is sold with it.

**The monitor (every 60s).**

- *Trailing stop:* exit when the bid is 10% below the high-water mark. The high-water mark
  uses Kraken's 24h high only once the position is a day old, so the high can't predate
  the entry.
- *Concentration trim:* sell back to the cap when a weight exceeds it by 5 points. Caps
  are BTC/ETH 35%, SOL 20%, and 15% for the other alts. There is a 15-minute cooldown per
  asset.
- *Drawdown breaker:* at 15% off peak, block BUYs and sell 25% of each position. It
  resumes when drawdown recovers under 10%, or after 72h with the peak re-baselined to
  current equity (a book de-risked into cash may never recover on its own), or with
  `ottr resume`.

**Fills and accounting.** Buys fill at ask + 0.05% and sells at bid - 0.05%. The fee is
0.40%, Kraken's lowest taker tier. Holdings are marked at the bid. Amounts are Decimal and
use average cost. Buy fees go into cost basis, and sell fees reduce proceeds. Cash and
inventory are replayed from the event log after every fill, and `ottr check` does the same
on demand.

**Benchmarks.** Every equity point also records BTC buy-and-hold and equal-weight
buy-and-hold from day 0, so performance is never judged in a vacuum.

## Operations

- **Supervisor + Task Scheduler.** The task starts the supervisor at logon and checks it
  every 5 minutes. The supervisor restarts the engine with backoff (5s doubling to
  5 min). It runs as the user, because the model CLIs are signed in under that profile.
- **Watchdog alarms.** These fire when a task's last success is older than 3x its
  interval, the risk monitor is blind (a held asset has no fresh quote), the ledger
  doesn't balance, or the API can't bind. An alarm needs 5 minutes clear to resolve.
- **Notifications.** Discord webhook posts are queued and rate-limited, with alarms on a
  separate budget so a burst of fills can't hide one. Each council meeting also gets one
  live message, edited as analysts finish, the chair decides and the risk gate answers
  (`[alerts] live_meeting` turns it off).
- **Kill switch.** Set with `OTTR_DRY_RUN=1`, in config, or via
  `POST /api/kill-switch`. It's persisted, so it survives a restart, and the API can't
  release a dry run that config imposes.
- **Screener (advisory).** Once a day the engine ranks the other liquid Kraken USD pairs
  against the universe: a volatility-adjusted 14/30/90-day trend, strength vs BTC, the
  size the risk model would allow, smoothness, diversification and volume, as percentile
  ranks. A candidate qualifies only when every horizon is up, it beats BTC and it is near
  its 30-day high; pullbacks and spikes are penalized. Nothing is traded: the owner adds a
  coin to `universe` (with a cap and its Kraken pair) to make it tradable.
- **Discord chat.** `ottr chat` is a slash-command bot (outbound only, no privileged
  intents) that the supervisor runs as a second child with its own restart loop, so a chat
  crash never touches the engine. /status, /positions and /meeting are plain code over the
  API, and /status says how far a running meeting has got. /ask is one model call with
  guardrails: one call at a time, a rolling-hour budget kept in chat.db (a restart can't
  reset it), and silence while a council meeting runs (`GET /api/council`).
  /kill and /release (owner only, button confirmation) use the
  API's kill switch, the bot's only write, so no chat command can trade. Alerts stay on
  the webhook, because they must still arrive when the bot is down.

## Lessons carried over from the previous version

These problems were found in the old system and are designed out here:

| Problem | Design answer |
|---|---|
| Importing the portfolio module rewrote live state | One writer, enforced by an OS lock; everything else is read-only |
| A corrupt state file was reset to a fresh $10k book | SQLite transactions, no hand-rolled JSON state |
| A failed volatility fetch became 0, which the sizer read as "calm" | Unknown is `None`; unknown vol sizes small |
| History calls hit rate limits every refresh; the fallback API was dead | All candles from Kraken (archived locally); Coinbase as fallback quotes |
| Correlation was computed on price levels | Correlation of daily log returns |
| `docker stop` hung until SIGKILL; no restart policy; down 3 months unnoticed | Supervisor, Task Scheduler keepalive, heartbeats with alarms |
| The model CLIs live on the host, the bot lived in Docker | Runs natively on the host |

## Design review

The first draft of this design was critiqued by GPT and Gemini. The adopted points:
- idempotent atomic execution;
- reductions never blocked;
- target-position sizing;
- a gross exposure cap;
- decision expiry with a pre-fill re-check;
- bid/ask fills;
- Decimal accounting with replay;
- the non-deadlocking breaker;
- candle hygiene;
- a fallback quote source;
- benchmarks from day 1;
- chair support rules;
- time-anchored packets;
- importing the old book at day-0 marks.

## Status and roadmap

- **Done:** core engine and council (P1). Unattended operation: supervisor, alarms,
  notifications, API (P2). Discord chat, first slice: /status, /positions, /meeting,
  /ask, /kill, /release. A live view of each council meeting in Discord and the API.
  Removing the previous version (P5).
- **Next:**
  - **P3 measure:** a per-model scorecard grading each analyst's stances at +24h and +7d,
    chair vs analysts, and a backtest of the sizing and risk policy on Kraken candles.
  - **P4 dashboard:** a web UI on the API.
  - **Screener follow-ups:** grade past picks (did qualifying candidates beat BTC over
    the next 7 and 30 days?) before trusting the score more; then, optionally, let the
    council see candidates and propose promotions for the owner to approve.
  - **Chat follow-ups:**
    - CEO directives: owner free text ("stay out of DOGE this week") stored through an
      authenticated API endpoint and shown to the analysts and the chair as context. Code
      still enforces every rule, so a directive can never size or force a trade.
    - /council: all three models answer the same question side by side (three CLI calls,
      so it gets its own budget).
    - Adaptive cadence: the chair proposes when to meet next; code clamps it to a range,
      hard triggers (a stop-out, the drawdown breaker, a large move) pull a meeting
      earlier, and an invalid value falls back to the fixed interval.
    - Proactive check-ins: the bot posts on its own when something is worth a look, with
      its own rate limit.
    - Chat health in `ottr status` (a heartbeat in chat.db) and a retention policy for
      chat.db.
- **Open question:** sizing is conservative. Targets are roughly 0.4% to 4% per asset at
  full conviction, so the desk mostly holds cash. `sizing.target_risk_pct` is the knob.
