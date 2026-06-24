# OTTR — Performance Measurement & Feedback Plan

**Scope:** Tier 1 of the improvement research — make the system *measurable* and fix
its *learning signal*. Touches `discord-bridge` (portfolio, price feed, reputation,
scheduler), `agent-gateway` (snapshot route), `frontend` (metrics widgets).

**Why this first:** Today the system cannot answer "does this beat just holding
BTC?" There is no persisted equity curve, no risk-adjusted metric, no benchmark,
and the one feedback signal (agent reputation) is mis-specified to the point of
being noise. Until measurement exists, no other improvement can be *proven* to
help. See the four-dimension research writeup for the full findings.

**Guiding rule:** It's a *trading* bot. Every change is gated by the eval/test
suite (`python run_evals.py` + `pytest -k "not live"`) so a measurement change
can't silently break trade execution. Add a test for every behavior change.

**Status:** M0 + M1 complete (bridge + gateway suites green; frontend data-layer unit-tested). M2-M3 todo.

Status legend: `[ ]` todo · `[~]` in progress · `[x]` done.

---

## Phase M0 — Metrics core + equity-curve persistence (the spine)

Everything else reads from here. Right now there is no time series of portfolio
value to compute anything from: `trade_history` is capped at 50
(`portfolio.py:23,361`) and the in-memory price history is a `deque(maxlen=60)`
lost on restart (`price_feed.py`). No metric functions exist anywhere (grep for
`sharpe|sortino|max_drawdown|benchmark` returns only an unrelated memory POC).

Concrete targets:
- `discord-bridge/bot/portfolio.py` — `_MAX_TRADE_HISTORY=50` truncation; no equity series; `buy`/`sell` model slippage but **no fee** (P&L is structurally optimistic).
- New `discord-bridge/bot/metrics.py` — pure functions, no I/O.
- New `discord-bridge/data/equity_curve.jsonl` — append-only, never truncated.

Tasks:
- [x] **`bot/metrics.py`** — pure functions over a `[(timestamp, value), ...]` series: `total_return`, `cagr`, `annualized_volatility`, `sharpe`, `sortino`, `max_drawdown`, `calmar`, and `summarize()` (resamples to daily; computes `benchmark_return`/`alpha`). No network/disk; returns `None` (not a fake `0.0`) on insufficient data, and guards a flat curve from an absurd Sharpe.
- [x] **Equity logger** — `bot/equity.py` appends `{ts, total_value, cash, holdings_value, realized_pnl, unrealized_pnl, btc_price, source}` to `data/equity_curve.jsonl`. **The bridge is the sole writer.** Sampled (a) on a new hourly APScheduler job and (b) post-meeting (prices already in hand — done at the meeting level rather than deep in the trade path, to avoid touching the trade-execution invariants). Append-only; never truncated.
- [x] **Benchmark series (BTC buy-and-hold)** — each row records BTC spot; `metrics.summarize()` derives BTC's return over the window and `alpha = strategy − benchmark` (return is scale-invariant, so no separate anchor file needed). The benchmark is anchored at the first logged sample.
- [x] **Model trading fees** — `portfolio.fee_pct` (default 0.1%/side, set in `settings.yaml`) charged to cash on `buy`/`sell`; recorded as `fee_usd`/`fee_pct` on the trade. Kept out of the cost basis so `avg_cost` stays the fill price; the drag still shows in total value (and the equity curve).
- [x] **Tests** — `tests/test_metrics.py` (13) + `tests/test_equity.py` (5); updated `eval_trades.py` (fee-aware asserts) and `test_scheduler.py` (new job). Added an autouse `conftest` fixture so no test writes the live equity curve. Full suite green.

**Exit:** `equity_curve.jsonl` grows hourly + on every trade; `metrics.py` computes
Sharpe/Sortino/max-drawdown/CAGR and alpha vs BTC-HODL; fees reduce P&L; all unit-tested and CI-green.

---

## Phase M1 — Surface live performance ("are we beating HODL?")

The numbers exist after M0 but nothing shows them. The gateway **hardcodes**
`"drawdown": 0.0` (`agent-gateway/app/routers/api.py:163`), the allocation widget
is hardcoded 60/30/10 (`apiClient.ts:100-103`), and there is no Sharpe / return /
benchmark anywhere the CEO can see.

Concrete targets:
- `agent-gateway/app/routers/api.py` — `/portfolio/snapshot` (the `drawdown: 0.0` literal and the metric block).
- `frontend/src/services/apiClient.ts` + the dashboard metric widgets.

Tasks:
- [x] **Bridge `/api/performance` endpoint** (`api_server.py`) — read-only; computes the metric set via `metrics.py` over `equity_curve.jsonl`. Logic lives on the bridge (single source of truth); degrades to a generic 500 without leaking internals.
- [x] **Gateway** — `_fetch_performance()` best-effort-proxies the bridge into `/portfolio/snapshot`: the hardcoded `drawdown: 0.0` is replaced by real max drawdown and a `performance` block (return/CAGR/Sharpe/Sortino/benchmark/alpha) is added. Bridge-down → metrics null, snapshot still renders.
- [x] **Frontend** — `OverviewPanel` shows Return-vs-BTC-HODL (+alpha), Sharpe (+Sortino), and Max Drawdown, hidden until ≥2 samples and rendering `—` (not a fake 0) for null. `apiClient` now derives real allocation weights from holdings/prices (the old 60/30/10 array was dead data; the donut already used live holdings) and passes `performance` through. _Note: this checkout's frontend is missing 3 unrelated, never-tracked components (`ControlSidebar`/`MarketNewsFeed`/`OptimizerAuditTable`), so a full `tsc`/`vite build` can't pass here; my files typecheck clean and the data layer is vitest-green._
- [x] **Tests** — bridge `test_api_server.py` (2: empty curve → insufficient; populated → metrics + benchmark); gateway `test_snapshot.py` (2: real metrics when bridge up, graceful null when down) + a `conftest` pinning the bridge URL; frontend `apiClient.test.ts` (allocations + performance mapping). Bridge 9-test + gateway 19-test suites green.

**Exit:** the dashboard shows real max-drawdown, Sharpe, and return-vs-HODL; no
hardcoded performance values remain in the snapshot path.

---

## Phase M2 — Backtest harness (fast iteration + reference baselines)

No backtester exists; today the only way to learn whether a strategy works is to
wait through live 4-hour meeting cycles, so strategy-iteration speed is ~zero. The
historical OHLC is **already fetched** (`price_feed._fetch_klines`, 100 daily
Kraken candles) and unused for evaluation.

Concrete targets:
- `discord-bridge/bot/price_feed.py:297-329` — `_fetch_klines` (BTC/ETH, 100 daily candles).
- New `discord-bridge/bot/backtest.py` + a cached candle fixture + a new eval.

Tasks:
- [ ] **`bot/backtest.py`** — a deterministic engine: inputs are an OHLC series + a `Strategy` (callable `(state, candle) -> list[order]`); it simulates fills reusing the portfolio slippage+fee model, produces an equity curve, and runs `metrics.py`. No LLM, no live network.
- [ ] **Reference strategies** — `BuyAndHold` (the benchmark), `SmaCross(20/50)`, `RsiMeanReversion`. These are the deterministic baselines the LLM agents must beat.
- [ ] **Data** — parameterize the kline fetch beyond 100 candles (Kraken daily OHLC returns up to ~720) for a longer window; cache a CSV under `tests/fixtures/` so the backtest eval is deterministic and offline.
- [ ] **Output** — a comparison table: each strategy vs buy-and-hold (CAGR, Sharpe, max-drawdown).
- [ ] **CI** — `eval_backtest.py` registered in `run_evals.py` `EVALS` with `needs_llm=False`, deterministic against the fixture.

**Exit:** `python run_evals.py eval_backtest.py` prints a baseline-vs-HODL table
from the cached fixture, deterministically, green in CI.

> **Scope note:** backtesting the *LLM agent* strategy (replay candles, call the
> LLM at each step) is possible but slow and non-deterministic — out of scope for
> Tier 1. The engine is designed to accept it later as an optional mode.

---

## Phase M3 — Fix the reputation / feedback loop

The signature feature is statistically broken and actively misleading the agents.
Grading is a symmetric ±1.5% / 24h price move (`knowledge_graph.py:104-129`): for
volatile crypto a HOLD almost always "loses," magnitude is ignored (a +12% call
scores like a +1.6% one), it's decoupled from P&L, and the human-facing summary
prints `100% (1/1)` with no sample-size guard (`knowledge_graph.py:180-181`). The
live graph is ~95% MISS — it trains every agent to believe everyone is bad.

Concrete targets:
- `discord-bridge/bot/knowledge_graph.py` — grading (`evaluate_pending_votes`, 104-129), summary (180-181), weight priors (232-235).
- `discord-bridge/bot/meetings.py` — vote recording path (to thread self-reported confidence through).

Tasks:
- [ ] **Magnitude-aware, vol-scaled grading** — replace binary HIT/MISS with a continuous score: forward return in the vote's direction over the horizon, normalized by the asset's realized volatility (a 2% move in a calm asset ≠ 2% in a wild one). BUY scores `+ret`, SELL `-ret`, HOLD by closeness to zero within a vol-scaled band — so a genuinely flat tape *rewards* HOLD instead of the current guaranteed loss.
- [ ] **Horizon-correct resolution** — grade at the 4h/24h marks using the price at/near the horizon, not "whatever the 15-min cron last saw" (today's grade is path-dependent on cron timing). Sample/store the price at the horizon.
- [ ] **Sample-size guard** — apply Bayesian shrink (already used in `get_agent_weights`, priors at 232-235) to the *displayed* win-rate too, or annotate `(n=1, low confidence)`, so tiny samples can't masquerade as skill.
- [ ] **Calibration (stretch)** — capture each agent's self-reported confidence (already emitted by personas, consumed by nothing) at `record_vote` time, threaded from `meetings.py` vote parsing; track a per-agent Brier score (are 0.9 calls right 90%?).
- [ ] **Tests** — update `tests/test_knowledge_graph.py`: assert HOLD wins on a flat tape, a large correct call outweighs a marginal one, and the summary never shows an un-shrunk `100%` on tiny samples.

**Exit:** flat tape → HOLD wins; big correct call > marginal correct call in
weight; summary shows no un-shrunk small-sample rates; tests green.

---

## Sequencing & invariants

**Order.** M0 is the spine — M1 reads from it; M2 reuses its metrics+fill model;
M3 is **fully independent** and can land first as a quick win (it only needs
`knowledge_graph.py` + tests and immediately changes agent behavior). Suggested:
M0 → (M1 ‖ M3) → M2, or M3 first if you want the fastest visible improvement.

**Do NOT regress (carry the audit invariants forward):**
- `portfolio.py` stays the **sole writer** of portfolio truth; the equity log is
  written by the **bridge only**. The gateway **reads, never writes**.
- **Fail loud on missing data** — metrics/benchmark must degrade gracefully
  (empty curve → "insufficient data", never a fake `0.0` that reads as real).
- CI (`run_evals.py` + `pytest -k "not live"`) stays green **before and after**
  each phase; every change ships with a test.
- Kill-switch, idempotent tool execution, input fencing, auth — untouched.

**Out of scope here (later tiers, from the same research):**
- Tier 2 — deterministic signal layer replacing/augmenting the LLM decision; per-asset indicators; conviction/vol position sizing; belief-revision debate; embeddings memory.
- Tier 3 — *enforcing* stop-loss / drawdown / concentration limits (this plan only *measures* drawdown; fees are included only as measurement integrity, not as a risk control).
- Tier 4 — SSE pipeline, "why did we trade" rationale, health aggregation.

**Rough effort:** M0 ~1-1.5d · M1 ~1d · M2 ~1.5-2d · M3 ~1d.
