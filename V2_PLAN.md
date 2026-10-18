# OTTR v2 plan

A clean rewrite built around a three-model council (Claude, GPT via Codex, Gemini via
Antigravity). It does not reuse the v1 code. v1 (`discord-bridge`, `agent-gateway`,
`frontend`) stays in the repo until v2 replaces it, then gets deleted.

## Why rewrite

v1 was shaped by a weak local 4b model: persona chat, regex vote parsing, tool-call
re-ask loops, raw-tag parsing, and prompt hints to steer sizing. Strong models return
reliable structured JSON, so most of that machinery has no job left. v1 also had problems
that a rewrite fixes by design rather than by patch:

| v1 problem (found 2026-10-18) | v2 design answer |
|---|---|
| Importing `bot.portfolio` rewrote live state (save on every load) | SQLite ledger, one engine process holds the write lock, readers open read-only |
| Corrupt JSON state reset to a fresh $10k book | SQLite transactions; no hand-rolled JSON state |
| Failed volatility fetch became 0.0, which the sizer read as "calm" (about 40% of book per BUY) | Unknown vol is `None`, and sizing treats `None` as high vol |
| CoinGecko history calls hit 429s on every refresh | All candles from Kraken; vol and correlation computed from them |
| CoinCap fallback dead (DNS) | Kraken ticker for spot, one request for all pairs |
| BTC/ETH correlation computed on price levels | Correlation of daily log returns |
| SIGTERM handler on a never-run loop: `docker stop` hung, exit 137 | Signal handling on the running loop, tested |
| No restart policy: stack sat dead for 3 months unnoticed | Supervisor restart + heartbeat + a "last tick" health check |
| Model CLIs live on the host, bot lives in Docker | v2 runs natively on the host; Docker is optional |

## Principles

1. **Models judge, code acts.** Models return validated JSON only. Code does sizing,
   risk, execution, accounting and vote counting.
2. **Independence, then synthesis.** Analysts see one shared market packet and never see
   each other. The chair sees their reports anonymized (A/B/C).
3. **Fail closed.** No quorum (under 2 analysts), chair failure, stale market data or a
   failed check all mean no trade, with the reason recorded.
4. **Everything is recorded.** Every packet, prompt, raw reply, parsed decision, risk
   verdict and fill goes to SQLite, so any trade can be traced back to its inputs.
5. **Paper only.** There is no exchange order path. The broker is simulated.

## Architecture

```
ottr/src/ottr/
  config.py        typed config (config.toml + env overrides)
  market/          Kraken OHLC + ticker client; pure indicators (EMA, RSI, MACD,
                   efficiency ratio, regime, 30d vol, return correlation); MarketSnapshot
                   with per-field data-quality flags and timestamps
  ledger/          SQLite store (WAL): fills, cash events, meetings, audit, equity,
                   risk state. Positions and cash derived from fills (average cost).
                   Paper broker: fill = price +/- slippage, fee on notional.
  risk/            sizing (vol target x conviction x regime), pre-trade gate
                   (kill switch, halt latch, universe, staleness, cash, MAX_TRADE_USD
                   hard reject, concentration clamp, min trade), monitor (trailing
                   stop, concentration trim, drawdown breaker with hysteresis, cooldowns)
  council/         CLI model adapters, schemas, prompts, Council runner
  engine/          executor (decision -> gate -> broker -> ledger -> audit) and the
                   async loop (spot 60s, candles 1h, risk 60s, equity 15m, council on a
                   schedule), graceful shutdown, single-instance lock
  notify.py        optional Discord webhook posts (meeting summary, fills, risk actions)
  cli.py           ottr run | meeting [--execute] | status | import-legacy
```

### Council contract

- Analysts get one stance per watchlist asset: BUY / SELL / HOLD, conviction 0-1,
  thesis, key risk. The desk is long-only spot, so SELL only means reduce or exit.
- The chair returns at most N trades: `BUY {asset, conviction}` or
  `SELL {asset, fraction_of_position}`. **The chair never picks dollar sizes.** Code
  sizes BUYs: `size = risk_budget * equity / vol * conviction * regime_factor`, then
  applies the gate.
- Code computes the tally and passes it to the chair as a fact, not as something the
  chair counts.

### Risk defaults (carried over from the v1 backtest work)

The trailing stop is 10% from the high-water mark. The drawdown breaker halts at 15%
and resumes at 10%, with an optional 25% de-risk. Concentration caps are BTC/ETH 35%,
SOL 20%, other alts 15%, with a 5% trim band. Each asset has a 900s cooldown. Regime
uses ER(20) with a 0.30 threshold. The risk budget is 2% of equity per position, the
CHOPPY factor is 0.25, the max position is 50%, and `MAX_TRADE_USD` is a hard reject.

## Design decisions from the GPT + Gemini review (2026-10-18)

Both models critiqued the first draft of this plan. These changes were adopted:

- **Idempotent, atomic execution.** Every action has an idempotency key. The fill,
  position, cash, order status, cooldown and audit row commit in one SQLite transaction,
  so a crash can never double-fill. All actions go through one serialized executor queue,
  and protective exits take priority over new entries.
- **Exposure-reducing actions are never blocked.** Halt, cooldown, caps and the trade
  ceiling only restrict BUYs. A SELL needs only a fresh price and a position to sell.
- **Target-position semantics.** A BUY's conviction maps to a target weight
  (vol-target x conviction x regime, capped). Code trades only the gap between target and
  current, so repeated meetings can't keep adding to an unchanged thesis.
- **Gross exposure cap** on total invested, so eight correlated alts can't add up to one
  oversized bet.
- **Decisions expire.** A decision older than its TTL is dropped. Before each fill, code
  refreshes the quote and re-checks holdings, cash and risk. A BUY is dropped if a risk
  action hit that asset during the meeting.
- **Realistic fills.** Buys fill at ask + slippage and sells at bid - slippage. The fee
  is set at Kraken's lowest taker tier (0.40%). Holdings are marked at the bid.
- **Exact accounting.** Decimal arithmetic. Buy fees are capitalized into cost basis,
  and sell fees reduce proceeds. Realized P&L uses average cost. Cash and inventory
  invariants are checked after every fill, and `ottr check` recomputes everything from
  the fill log.
- **No drawdown deadlock.** The halt blocks BUYs only. It resumes when drawdown recovers
  under resume_pct, or automatically after halt_max_hours with the peak re-baselined to
  current equity, or by `ottr resume`. The peak, latch and cooldowns persist in SQLite.
- **Candles:** the unfinished bar is dropped, gaps are detected, indicators need warm-up,
  and candles are archived locally. High-water marks also use Kraken's 24h high once a
  position is older than 24h.
- **Second price source** (Coinbase public ticker) for marking and protective exits when
  Kraken is stale.
- **Measurement from day 1.** Equity snapshots carry BTC buy-and-hold and equal-weight
  buy-and-hold benchmarks. Every analyst stance is stored with its price, for the P3
  scorecard.
- **Chair rules.** The chair may BUY an asset only if at least one analyst said BUY on
  it. It may always reduce. It gets holdings and cash context. Analyst identity is kept
  internally even though the chair sees A/B/C.
- **Temporal anchoring.** Every packet starts with the current UTC time, 7d/30d ranges and
  the distance from the 90d high/low, plus an instruction to judge only the given data.
- **Legacy import at day-0 marks.** v1 holdings come in at current spot (cost = market
  value, high-water mark = today), so the missing v1 history can't become invented profit.
- **Ops in P1.** Per-task heartbeats (market, risk, equity, council) plus crash,
  duplicate-execution, stale-data and accounting-invariant tests.

## Phases

- **P1: core (BUILT 2026-10-18).** 68 tests; a live propose-only meeting and a dry-run
  engine smoke both passed on real data and real models. config, market, ledger, risk, council (ported from C0, chair
  without sizing), executor, engine loop, CLI, legacy import of the v1 book. Tests with
  fake models and fake market. Live dry run against real data and real models.
- **P2: operate.** Host supervisor (Task Scheduler at logon, auto-restart), heartbeat and
  staleness alarms, Discord webhook notifications, read-only HTTP API + kill switch.
- **P3: measure.** Per-model scorecard (each analyst's stances graded at +24h and +7d),
  chair-vs-analyst comparison, deterministic backtest of the risk/sizing policy on Kraken
  candles.
- **P4: dashboard.** A new lean web UI on the P2 API.
- **P5: retire v1.** Delete `discord-bridge`, `agent-gateway`, `frontend` once v2 has run
  cleanly for a week.

## Open questions (sensible defaults chosen; easy to change)

- Starting book: import the v1 positions as the opening state (default) or start fresh
  at $10k.
- Cadence: a council every 4h. That is 24 CLI calls a day across three subscriptions.
- Discord: webhook notifications only, with no chat bot. CEO directives move to the CLI/API.
