# OTTR — Alpha & Decision-Quality Plan (Tier 2)

**Scope:** Tier 2 of the improvement research — make trading *decisions* quantitatively
grounded and properly sized, instead of an LLM reading a paragraph. Touches
`discord-bridge`: `price_feed`, new `signals`, `meetings`, `tools`/`portfolio`
(sizing), `agents`, and `backtest` (to prove edge).

**Why now:** Tier 1 made the system *measurable* (equity curve, metrics, BTC-HODL
benchmark, backtest, fixed reputation). Tier 2 acts on it. Today: the BUY/SELL/HOLD
call is whatever the local LLM emits after reading a text blob; technical indicators
are computed for **BTC/ETH only** (SOL is held + voted but has none, forcing the TA
agent to abstain or hallucinate); position size is a raw USD amount the LLM picks;
and the "debate" never changes a mind. Goal: compute real signals in code, inject
them as structured inputs, size by conviction × volatility, and let the backtest
prove whether it beats the SMA baseline Tier 1 set (+18.9% vs HODL).

**Guiding rule:** every change must be **measurable** via Tier 1 — the deterministic
signal layer is backtestable (plugs into `bot/backtest.py`), and live changes show up
in the equity metrics + reputation. Gate every change on `pytest -k "not live"` +
`run_evals.py --no-llm`. Preserve all audit invariants (idempotent tools, kill-switch,
sole portfolio writer, fail-loud, caps).

**Status:** S0 done. S1-S4 todo.

Status legend: `[ ]` todo · `[~]` in progress · `[x]` done.

---

## Phase S0 — Per-asset indicators (unblock everything)

Indicators exist for 2 of 9 assets. `get_technical_indicators` hard-loops
`for symbol in ["BTC", "ETH"]` (`price_feed.py:339`) and `_fetch_klines` maps only
`XBTUSD`/`ETHUSD` (`price_feed.py:299`). SOL — held and voted every meeting — gets
no RSI/MACD/EMA, so the technical_analyst persona (ordered to cite them) must abstain
or invent. Signals (S1) need indicators for every asset, so this is the prerequisite.

Tasks:
- [x] Added `_KRAKEN_PAIR_MAP` (BTC→XBTUSD, ETH→ETHUSD, SOL→SOLUSD, XRP→XRPUSD, ADA→ADAUSD, DOGE→XDGUSD, LINK→LINKUSD, AVAX→AVAXUSD; BNB has no Kraken USD pair → skipped). `_fetch_klines` uses it.
- [x] `get_technical_indicators` loops the mapped assets with a small inter-call sleep (rate-limit courtesy); unmapped/failed assets are omitted, never faked.
- [x] `get_market_state_summary` already rendered indicators per-asset when present — now populated for all of them.
- [x] Test: `test_technical_indicators_cover_all_mapped_assets` — SOL gets EMA/RSI/MACD from a mocked Kraken response; BNB (unmapped) yields none.

**Exit:** every traded asset with a Kraken pair has EMA/RSI/MACD; the TA agent is no
longer structurally forced to hallucinate on SOL.

---

## Phase S1 — Deterministic signal layer (the backtestable edge)

The indicators are computed and then only *described* to the LLM; nothing turns them
into a signal, so decisions are non-reproducible and the "strategy" can't be
backtested. This phase makes the edge explicit and testable.

Tasks:
- [ ] New `bot/signals.py` — pure functions mapping indicators + market data → a
  per-asset `Signal {direction: BULLISH|BEARISH|NEUTRAL, strength: 0..1, reasons: [...]}`.
  Rules: EMA20×EMA50 cross, RSI zones (oversold/overbought), MACD histogram sign,
  funding-rate extreme (contrarian), Fear & Greed contrarian. Fully unit-tested.
- [ ] Inject structured signals into the meeting context (e.g. `SOL: BULLISH (EMA+, RSI 41, MACD+)`) so agents reason over grounded inputs, not just raw numbers.
- [ ] Record a **deterministic baseline consensus** (tally the code signals → BUY/SELL/HOLD per asset) alongside the LLM consensus, so the two can be compared over time.
- [ ] Backtestability: a `SignalStrategy` in `bot/backtest.py` that trades off `bot/signals.py`; add it to `eval_backtest.py` so the comparison table shows signal-strategy vs HODL/SMA. **This is the proof the signals have edge.**
- [ ] Tests: `test_signals.py` (known indicator inputs → expected signal); the signal strategy appears in the backtest eval.

**Exit:** signals are computed in code, injected into meetings, and backtested; the
eval table shows the signal strategy's risk-adjusted return vs the baselines.

---

## Phase S2 — Conviction & volatility position sizing

`execute_trade` takes a raw LLM-chosen USD `amount` (`tools.py:197`); analyst
confidence (0..1) is emitted by every persona and consumed by nothing; size has no
link to volatility or conviction. The concentration cap is SOL-only and hardcoded
(`tools.py:226-254`).

Tasks:
- [ ] New sizing model (`bot/sizing.py`): target notional = vol-target (size inversely to the asset's volatility to hit a fixed risk budget) × conviction (net weighted consensus score, optionally × analyst confidence), bounded by cash, `MAX_TRADE_USD`, and a per-asset concentration cap. Fractional-Kelly as an alternative knob.
- [ ] Wire it into the execution path so size is computed from conviction+vol (the LLM proposes direction/asset; the model sizes it), keeping the existing caps as hard guards.
- [ ] Generalize the concentration cap from SOL-only to any asset.
- [ ] Tests: higher vol → smaller size; higher conviction → larger; caps respected; backtest sizing is consistent.

**Exit:** position size is a function of conviction × volatility (not a free LLM pick);
the concentration cap applies to every asset; covered by tests.

---

## Phase S3 — Belief-revision debate + inference diversity

The debate is one pass of vote-emission; nobody revises after being challenged, and a
single model with personas stripped during debate collapses toward one view.

Tasks:
- [ ] Add a revision round: after initial votes, give each dissenter the strongest
  opposing argument and let them confirm or change their `Final Vote`.
- [ ] Capture self-reported **confidence** in the vote and thread it to `record_vote`
  (enables the M3 calibration that was deferred for lack of a confidence signal).
- [ ] Widen diversity: per-agent temperature spread; give the risk_auditor/contrarian
  an explicit "argue against the room" framing; stop stripping persona output formats
  in the round where differentiation matters.
- [ ] Tests: a meeting test asserts the revision round runs and a changed vote is recorded with its confidence.

**Exit:** agents can change their vote after rebuttal; votes carry confidence; the
panel is meaningfully less homogeneous.

---

## Phase S4 — Embeddings memory (stretch)

TF-IDF retrieval is keyword-only and queried with a price blob, so relevant past
meetings rarely surface (`vesper_engine.py`). There's already a TF-IDF-vs-embeddings
benchmark POC to validate against.

Tasks:
- [ ] Swap TF-IDF for local sentence embeddings (LM Studio can serve an embeddings
  model) over the same Markdown vault; query with a short LLM-written topic sentence
  instead of the raw price string.
- [ ] A/B retrieval quality vs TF-IDF using the existing benchmark.

**Exit:** memory retrieval returns semantically relevant precedents; measured better
than TF-IDF on the benchmark. _(Stretch — larger and depends on an embeddings endpoint;
defer until S0-S2 land.)_

---

## Sequencing & invariants

**Order:** S0 first (unblocks S1). S1 → S2 (sizing consumes the signals/conviction).
S3 is largely independent (agent reasoning). S4 is stretch. Recommended: **S0 → S1 → S2**,
then S3, with S4 optional.

**Do NOT regress:** idempotent tool execution, kill-switch (`TRADING_DRY_RUN`), sole
portfolio writer, fail-loud on missing data, `MAX_TRADE_USD` + concentration caps,
input fencing/auth. The signal layer stays **pure + deterministic** (so it's
backtestable); the LLM still makes the final call but now over grounded inputs.

**Measured by Tier 1:** every phase is validated against the backtest (does the signal
strategy beat HODL/SMA?), the live equity metrics, and the reputation loop.

**Rough effort:** S0 ~0.5-1d · S1 ~1.5-2d · S2 ~1-1.5d · S3 ~1-1.5d · S4 ~1.5d.
