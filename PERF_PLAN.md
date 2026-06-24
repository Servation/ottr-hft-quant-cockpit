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

**Status:** Tier 1 COMPLETE (M0-M3). Bridge + gateway suites green, frontend data-layer unit-tested, offline eval suite (incl. backtest) green in CI.

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
- [x] **`bot/backtest.py`** — deterministic engine: an OHLC series + a `Strategy` (returns a target weight 0..1 per bar, no look-ahead) → rebalances using the same slippage+fee model as the live portfolio (rates read from `settings`, no disk-backed singleton) → equity curve scored by `metrics.py`. _(Target-weight proved cleaner/safer than the sketched `-> list[order]`.)_ No LLM, no live network on the hot path.
- [x] **Reference strategies** — `BuyAndHold` (benchmark), `SmaCross(20/50)`, `RsiMeanReversion(14,30/70)`. On 2y of real BTC daily: SMA 20/50 beat HODL by +18.9% (Sharpe 0.56 vs 0.31, MaxDD 30% vs 51%); RSI underperformed — a real bar for the agents to clear.
- [x] **Data** — `fetch_kraken_daily` pulls up to ~720 daily candles (used only to build the fixture); `tests/fixtures/btc_daily.csv` is the cached, committed series so the eval is offline + deterministic. `synth_candles` is a reproducible fallback.
- [x] **Output** — `format_table` prints each strategy vs buy-and-hold (Return, CAGR, Sharpe, MaxDD, alpha-vs-HODL), ASCII-only so it's console-encoding safe.
- [x] **CI** — `eval_backtest.py` registered in `run_evals.py` (`needs_llm=False`); asserts buy&hold tracks the asset minus costs, every strategy yields finite metrics, and re-runs are identical. Plus `tests/test_backtest.py` (6) under the pytest gate.

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
- [x] **Magnitude-aware, vol-scaled grading** — `_score_vote` returns a continuous score in [-1,1]: BUY `+ret/σ`, SELL `-ret/σ`, HOLD `1-|ret|/σ`, where σ is the asset's expected move over the horizon (annualized vol → per-horizon, default 2% when vol unknown). A flat tape now *rewards* HOLD; a big correct call outscores a marginal one. Scheduler passes `price_feed.get_volatility()` through.
- [x] **Horizon-correct resolution** — votes resolve **once**, at `RESOLVE_HORIZON_HOURS` (4h, matching meeting cadence), using the price then — no early-stop at the first cron tick that crosses a threshold (the old path-dependent bias toward short-term noise is gone).
- [x] **Sample-size guard** — `get_reputation_summary` flags `< _MIN_SAMPLES` (5) as `(n=1, low confidence)` instead of `100% (1/1)`; `get_agent_weights` shrinks the mean score toward 0 with `_PSEUDO_OBS` pseudo-observations, so a thin record can't swing consensus. Legacy HIT/MISS nodes still count via `_node_score`.
- [~] **Calibration (deferred)** — `record_vote` only receives direction/asset/price from the brittle `Final Vote:` regex (no confidence), so threading self-reported confidence + a Brier score is left as a follow-up to avoid touching the vote-parsing path here.
- [x] **Tests** — rewrote `tests/test_knowledge_graph.py` (15): HOLD wins on a flat tape, magnitude/vol scaling affect the score, no resolution before the horizon, low-n flagged (no un-shrunk `100%`), weights shrink with few samples, legacy nodes still count.

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
