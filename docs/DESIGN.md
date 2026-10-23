# OTTR design

How OTTR works and why. Usage and operations live in [`ottr/README.md`](../ottr/README.md).

## The idea

Three models analyze the market independently. A chair model decides. Deterministic code
does everything that has a right answer: sizing, risk limits, execution, accounting, tax
and vote counting.

OTTR is a paper desk for US markets: 16 core ETFs across stocks, bonds and cash,
commodities and real estate, plus up to six single stocks that a code screener picks
weekly, with a $10,000 book. It began as a crypto desk on Kraken; that book was archived
at the cutover rather than converted, because its positions, precision and fee model
don't map onto equities.

The version before that ran on a weak local model whose tool calls were unreliable, so
much of its code existed only to compensate. Strong models return reliable structured
JSON, so OTTR gives them one job, judgment, and keeps them away from anything that moves
money.

## Principles

1. **Models judge, code acts.** Models return schema-validated JSON and never call tools.
   No model output can carry a dollar size, and no model can add a symbol.
2. **Independence, then synthesis.** Analysts see one shared market packet and never see
   each other. The chair sees their reports anonymized as A/B/C.
3. **Fail closed.** No quorum, a failed chair and fallback chair, stale or insane market data, an unknown
   earnings date or corporate action, or a failed check all mean no trade, and the reason
   is recorded.
4. **Unknown is never zero.** A missing price, volatility or indicator stays `None`.
   Sizing treats unknown volatility as high volatility; a holding without a price makes
   equity unknown, not lower.
5. **Reductions are never blocked.** Limits stop risk going up, never down: a SELL needs
   only a position and a quote.
6. **Everything is recorded.** Every packet, raw reply, decision, gate verdict, fill,
   corporate action and screen goes to SQLite, so any trade traces back to its inputs.
7. **Paper only.** OTTR reads Alpaca's market data and never sends it an order. The
   broker is simulated behind a seam a live adapter could replace.

## Architecture

```
ottr/src/ottr/
  config.py        typed config (config.toml + env + ottr/.env), validated at load
  clock.py         an injectable clock: nothing reads the time inline
  market/          Alpaca client (quotes, SIP daily bars, assets, calendar, corporate actions),
                   the NYSE calendar and session model, quote sanity checks, marks, the
                   MarketData service, indicators, the council packet, benchmarks
  events/          FRED macro series, Alpha Vantage earnings dates, SEC lists and company
                   profiles, corporate actions: fetched, sanity-checked, stored, read fail-closed
  universe/        membership (core ETFs from config, satellites from the DB), the weekly
                   stock screener: measures and score, membership rules, the run
  council/         CLI model adapters (claude -p, codex exec, agy over stdin), schemas, Council
  risk/            gate (pre-trade checks and sizing), monitor (stops, trims, breaker,
                   corporate exits), Intent/Verdict types
  broker/          fee schedule (effective-dated), account model (buying power), SimBroker
  ledger/          SQLite store (WAL, one writer via an OS lock), the Decimal ledger with FIFO
                   tax lots, marks, corporate actions applied, tax estimates
  engine/          executor (the only path from an order to a fill), the Desk (US specifics),
                   daily jobs (fees, sweep), engine loops, kill switch, meeting progress
  health.py        alarms and status exit codes shared by watchdog, CLI and API
  notify.py        rate-limited Discord webhook posts, and the live meeting message
  api.py           read-only JSON API on 127.0.0.1 + the authenticated kill switch
  supervisor.py    restarts `ottr run` (and `ottr chat`) with backoff
  service.py       Windows Task Scheduler registration for the supervisor
  archive.py       verified copies of a book, at the cutover and after
  chat/            `ottr chat`: the Discord slash-command bot; reads the API only
  cli.py           init | run | supervise | service | chat | meeting | status | check | resume
                   | data | screen | tax | archive
```

One engine process is the only writer. Read-only commands, the API and propose-only
meetings open the database read-only or work on a scratch copy.

### Time and the session

Every task reads an injectable clock, and every rule that depends on the market reads the
NYSE calendar (Alpaca's, stored in the book so a restart or `ottr status` knows the session
without the network). Nights, weekends and holidays are normal states, not alarms: loops
keep fixed cadences, so heartbeats stay meaningful, and branch on the session. Windows
count sessions, not days, and volatility annualizes by sqrt(252).

### Prices and marks

Quotes come from Alpaca's free IEX feed and must pass a sanity check: two-sided, not
crossed, a spread under 1%, fresh, near the last trade, and within 25% of the previous
close. A split explains a bigger gap on its ex-date. An order always needs such a quote.
The book is marked by session:
- In session: the live bid, else the newest delayed SIP minute close.
- Closed: the last official close, restated by any corporate action applied since.

Each equity point records which basis it used.

### A council meeting

1. Code builds the packet: the session, each asset's indicators and events (earnings,
   corporate actions), breadth from the sector ETFs, the macro block (VIX, rates, the
   curve, credit spreads), and the book (holdings, target weights, caps, the sleeve,
   blocked BUYs and why, recent fills, the last meeting's summary).
2. Claude, GPT and Gemini each return a stance for every asset (BUY / SELL / HOLD,
   conviction 0-1, thesis, key risk) through their lenses: risk and regime, quant and
   technical, cross-asset risk appetite.
3. Code tallies the stances and gives the tally to the chair as a fact.
4. The chair returns at most 3 trades: `BUY {asset, conviction}` or
   `SELL {asset, fraction_of_position}`. If the chair fails (its CLI signed out, a
   timeout, JSON it can't repair), the fallback chair (GPT) gets the same prompt, so one
   provider being down doesn't cost the meeting. The record and the Discord post say who
   chaired and why; if both fail, the meeting abstains.
5. Code enforces the rules: no BUY without an analyst's BUY, no SELL of what isn't held,
   no BUY of a blocked asset (sunset, earnings blackout, corporate action), each dropped
   with its reason.
6. Each surviving trade becomes an Intent for the executor.

Progress is reported through a callback that does no I/O, folded into one
`MeetingProgress` state that `GET /api/council` serves and one Discord message shows,
edited as the meeting moves. A slow reader or a Discord outage can't delay a meeting.

### The watcher, between meetings

The schedule meets twice a day whatever happens. A desk that holds for days to weeks
mostly needs only that, but a break, a gap or a shock in between deserves a look sooner,
and what is worth watching depends on what the desk holds and just decided.
- **Its role:** after every meeting, and every 30 minutes in session, a watcher model
  (Claude, Opus) reviews what changed. It returns validated JSON: whether to call a
  meeting now, and up to 8 conditions (a level broken or a percent moved).
- **Code's role:** it checks the conditions every minute and decides whether a call may
  run (in session, away from the close and the schedule, 45 minutes apart, 4 a day).
- **The meeting:** a called meeting is an ordinary meeting whose packet starts with why it
  was called.

The watcher can't trade, size or add a symbol: models judge, code acts.

### From Intent to fill

The executor drains one priority queue. Protective exits (stop, de-risk, trim, corporate
exit) run before council orders, and the cash sweep goes last. For each Intent it:

1. claims the idempotency key, so a repeated or retried Intent can never fill twice;
2. has the Desk gather a fresh context (session, sane quote, book, buying power, events);
3. runs the gate, which returns an exact Verdict or a rejection with its reason. A BUY
   short of cash while SGOV is held is funded by selling SGOV first;
4. has the SimBroker fill it as Alpaca would fill a marketable limit order (BUY at the
   ask, SELL at the bid, rounded to the tick against us, fractional to 1e-9 only where
   the asset is fractionable), and commits the fill, its lots, the order status, the
   cooldown and the audit row in **one** SQLite transaction (in dry run it records the
   verdict instead).

A transient rejection (a bad quote, the market closed, a corporate action not yet applied)
is deferred and retried; a dry-run verdict doesn't block real execution later.

## Risk policy

**Sizing (BUYs only).** Conviction maps to a target weight: `1.5% / annualized vol x
conviction`, with per-class volatility floors, quartered when the regime is CHOPPY or
unknown, and capped:
- broad ETFs at 20 to 25% (EEM, SLV and PDBC lower);
- single stocks at 8%, and all stocks together at 40% of equity;
- SGOV uncapped (it is cash, outside gross exposure).

Code buys only the gap to the target, clamped by `max_trade_usd`, 90% gross exposure and
a 2% cash reserve; anything under $100 is dropped. Single stocks also need a known
earnings date more than 5 sessions away.

**The gate.**
- **Any order:** a council decision older than 15 minutes is dropped. Every order needs
  the regular session and a sane quote.
- **BUYs only:** the drawdown halt, a cooldown, a risk action during the meeting, the last
  10 minutes of the session, a sunset member, an unknown earnings date or a pending
  corporate action each block a BUY, never a SELL.
- **Small SELLs:** a council SELL under the minimum is refused; a protective one never
  is.

**The monitor (every 60s in session, from 5 minutes after the open).**

- *Trailing stop:* each position gets its distance when it opens: 2x the move its
  volatility implies over 10 sessions, clamped to 3 to 15% (10% when vol is unknown).
  The distance never changes and the high-water mark only rises, so a stop never loosens.
  A gap through the stop overnight sells at the bid and says so.
- *Trims:* back to the cap when a weight exceeds it by 5 points, and the stock sleeve
  back to 40% pro rata.
- *Drawdown breaker:* at 15% off peak, block BUYs and sell 25% of each holding. Those
  sales are re-issued under one key until they fill, so a deferred one still happens the
  next session. It resumes under 10% drawdown, or after 72 hours with the peak
  re-baselined, or with `ottr resume`.
- *Corporate exits:* a holding facing an action the book can't model (a stock merger, a
  spin-off) is sold the session before it takes effect.

**Fills and accounting.** Amounts are Decimal: cash in cents, quantities to 1e-9.
- **Lots:** every BUY opens a FIFO tax lot and every SELL records which lots it closed,
  with short or long term.
- **Fees:** no commission. The 2026 regulatory fees (SEC section 31, FINRA TAF, CAT) are
  effective-dated in config, accrued exactly per fill and charged once a day rounded up,
  as Alpaca does.
- **Corporate actions**, booked at the start of their effective date:
  - Dividends: owed from the ex-date (a receivable in equity), paid on the pay date.
  - Splits: adjust each lot and restate the stop; whole-share assets get cash in lieu.
  - Cash mergers: pay out at the deal rate.
- **Checks:** `Ledger.check` rebuilds cash, every lot, position, dividend and fee from the
  event log after every change, and `ottr check` does the same on demand.

**Benchmarks.** Every equity point also records SPY buy-and-hold and 60/40 SPY/IEF
(rebalanced monthly), total return from the same day 0, and the after-tax estimate.

## The universe and the screener

The core ETFs live in `config.toml`; the satellites in the database.
- **Membership states:** active, sunset (demoted while held: SELL or HOLD until flat) and
  removed.
- **The screener**, every Saturday, works in stages:
  1. Alpaca's tradable NYSE and Nasdaq stocks that the SEC lists as companies.
  2. Liquidity floors; the 1,200 most traded go on.
  3. 13+ months of adjusted bars against hard gates.
  4. SEC profiles saying "operating company" for those that passed.
- **The score** ranks percentiles of vol-adjusted 12-1 month momentum, momentum net of
  SPY, path efficiency, nearness to the 52-week high, the size the risk model would
  allow, low correlation with SPY and rising volume.

Hysteresis keeps a stock near the cut line from being bought and sold every week:
- a 14-day tenure;
- kept while in the top 12;
- at most 2 changes a week;
- a 15-point margin for a challenger;
- a 28-day cooldown after a demotion.

A correlation filter (0.75 with any member) keeps the sleeve diversified, and a quote
filter keeps it tradable. The free feed is IEX's own book, so a stock whose IEX quote was
usable less than 75% of the sampled minutes over the last week is skipped, or demoted if
it is a satellite. Members are sampled every minute and the latest screen's top 40 every
15 minutes (the `quote_quality` table, schema v4).

## Tax

Tax is an estimate replayed from the lots, never stored in them, so the economic book
stays exact and a corrected rule applies to all history at once.
- **Wash sales:** 30 days either side; the loss moves into the replacement's basis and
  holding period.
- **Terms and collectibles:** short vs long term; GLD and SLV long-term gains are
  collectibles.
- **Netting:** Schedule D netting with the $3,000 deduction and its carryforward.
- **Dividends:** qualified or ordinary by class.
- **Rates:** the owner sets them in `[tax]`.

## Operations

- **Supervisor + Task Scheduler.** The task starts the supervisor at logon and checks it
  every 5 minutes. The supervisor restarts the engine with backoff, as the user, because
  the model CLIs are signed in under that profile. Claude can instead sign in with its own
  long-lived subscription token (`CLAUDE_CODE_OAUTH_TOKEN` in `ottr/.env`), so signing out
  of Claude Code on the PC doesn't sign the engine out.
- **Events feeds** refresh on their own cadences:
  - FRED: every 12h.
  - Earnings: daily (one Alpha Vantage call).
  - Corporate actions: every 4h, and at once for a new member.
  - SEC lists: weekly.

  A snapshot far smaller than the real thing is refused, so a bad response can't erase
  good data, and data older than its limit reads as unknown.
- **Watchdog alarms:** these fire on:
  - a stale task, judged by its schedule for the council and the screen;
  - a blind risk monitor;
  - a stale events feed;
  - a corporate action that needs a person;
  - a ledger that doesn't balance;
  - an API that can't bind.

  An alarm needs 5 minutes clear to resolve.
- **Notifications.** Discord webhook posts are queued and rate-limited, with alarms on a
  separate budget. Each council meeting gets one live message; screens and corporate
  actions get a digest.
- **Kill switch.** Set with `OTTR_DRY_RUN=1`, in config, or via `POST /api/kill-switch`
  (and `/kill` in chat). It's persisted, and the API can't release a dry run that config
  imposes.
- **Discord chat.** `ottr chat` is a slash-command bot the supervisor runs as a second
  child. /status, /positions, /meeting and /screen are plain code over the API; /ask is
  one model call with a rolling-hour budget. /kill and /release are its only write, so no
  chat command can trade. Alerts stay on the webhook, so they arrive when the bot is down.
- **Archive.** `ottr archive` makes a verified copy of any book version (SQLite backup
  API, integrity check, config, logs, a note with the commit), and with `--retire` sets
  the original aside for a new one.

## Lessons carried over

| Problem | Design answer |
|---|---|
| Importing the portfolio module rewrote live state | One writer, enforced by an OS lock; everything else is read-only |
| A corrupt state file was reset to a fresh $10k book | SQLite transactions, no hand-rolled JSON state |
| A failed volatility fetch became 0, which the sizer read as "calm" | Unknown is `None`; unknown vol sizes small |
| Correlation was computed on price levels, or across mismatched days | Correlation of daily log returns, aligned by date |
| A halt blocked the exit of a demoted asset | A SELL needs only a position |
| The drawdown breaker could deadlock in cash | It re-baselines after 72 hours |
| `docker stop` hung; no restart policy; down 3 months unnoticed | Supervisor, Task Scheduler keepalive, heartbeats with alarms |
| A split or a dividend would read as a crash | Adjusted history for signals; corporate actions booked; marks restated |

## Status and roadmap

- **Done:** the crypto desk (council, unattended operation, chat, the live meeting
  view), then its conversion to US markets: Alpaca data and the NYSE calendar, the FIFO
  ledger and fees, the gate and SimBroker, events feeds, the engine switch, the risk
  monitor, corporate actions, the stock screener, tax estimates and the cutover tooling.
- **Next:**
  - **Measure:** a per-model scorecard grading each analyst's stances at +1 and +5
    sessions, and grading the screener's picks against SPY before trusting the score more.
  - **Phase 2, options:** the model gives direction, horizon and a structure type (long
    call or put, covered call, cash-secured put, verticals). Code picks expiry and strikes
    by DTE and delta rules and sizes by maximum loss, with contract fees, exercise and
    assignment.
  - **Phase 3, live:** an Alpaca broker behind the seam, reconciliation at start-up, and
    broker-side stop orders so stops survive the PC being off.
  - **A dashboard** on the API.
- **Open question:** sizing is conservative (about 1.5% risk per position at full
  conviction), so the desk often holds SGOV. `sizing.target_risk_pct` is the knob.
