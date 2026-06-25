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

**Status:** S0-S2 done (incl. signal tuning, a **regime-aware strategy that beats HODL
on all three assets**, LLM behavior tuning, and **regime-driven position sizing**).
S3 done (belief-revision round). **S4 done** (embeddings memory — TF-IDF/vesper
replaced with local embeddings). **All of Tier 2 (S0-S4) complete.**

**Regime-aware edge (the win):** gating trend-following on the Kaufman efficiency
ratio (`bot/signals.efficiency_ratio` / `regime_label`; `bot/backtest.RegimeStrategy`)
only trend-follows when an asset is *actually* trending and stays flat in chop. On
real BTC/ETH/SOL daily it beats buy-and-hold on **all three** — BTC +6.6%, ETH +40.5%,
SOL +40.8% alpha — with far lower drawdown (17-40% vs 51-76%), where plain SMA blew up
on choppy SOL (-43%). It's now in `default_strategies` (permanent cross-asset eval), and
the live agent context shows each asset's **regime** (`TRENDING`/`CHOPPY` + ER), with an
explicit "in a CHOPPY regime trend signals are unreliable — favor defense" note. This is
the first robustly positive-alpha result of the whole exercise.

**LLM tuning (directly evaluated `gemma-4-12b-it`):** the model follows the persona
format and cites real data (9/9 in probes) and, once told to, acts on the regime.
Adjustments from the findings: (1) a shared **desk rule** prepended to every agent's
system prompt — an A/B flip from "SELL @0.75" to "ABSTAIN @0.35" on choppy SOL, and a
live eval where **all** agents went from the baseline's 6× unanimous `BUY SOL` to
unanimous `ABSTAIN` with explicit "CHOPPY regime → trend signals are noise" reasoning;
(2) the debate prompt no longer uses `[BRACKET]` placeholders the model copied
literally, and the vote parser (`_VOTE_RE`) tolerates `[HOLD]`/`**SELL**` so votes
aren't silently dropped; (3) consensus weights a vote by **credibility** (non-negative)
so a unanimous BUY by below-average agents can't tally as SELL. (4) Resilience: a
transient empty completion (a momentarily-busy local backend) used to drop an agent's
whole turn/vote; `generate_response` now **retries once** on an empty, no-side-effect
response — guarded so it can never re-execute a trade and can't loop. _(A 20-call
stress run came back 20/20 clean, so the degradation is rare/transient, not systematic;
the retry + its log line are cheap insurance plus the observability to learn the true
frequency.)_

**Signal-tuning finding (cross-asset: BTC/ETH/SOL, real daily):** signal weights are
now configurable (`DEFAULT`/`TREND`/`TREND_TILT`) and validated across three assets
(not curve-fit to one). Result: the **balanced default is a risk-reducer, not an alpha
source** — it trails HODL in the BTC bull (-14%) but beats HODL by **+28%** in the
ETH and SOL bears with **~half the drawdown** (15-32% vs 51-76%). The trend-only
variant was *worse* on ETH/SOL (hypothesis disproven by the cross-asset test — the
point of validating broadly). **SMA 20/50 is the real trend offense** (+18.9% BTC,
+92.5% ETH alpha) but collapses on choppy SOL (-43%). No static config beats HODL
everywhere → the next real edge is **regime-aware** (trend-follow when trending,
signal-defensive otherwise) or an SMA-offense + signal-defense combine. The backtest
eval now runs across all fixtures, so this stays measured.

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
- [x] `bot/signals.py` — pure `signal_from_indicators` (EMA20×EMA50, RSI zones, MACD histogram, funding extreme contrarian, F&G contrarian → `Signal {direction, score -1..1, strength, reasons}`), plus `signals_for_assets`, `format_signals`, and `consensus_from_signals`. Fully unit-tested (11).
- [x] Structured signals injected into the market-state summary every agent reads (`price_feed.get_market_state_summary`), e.g. `SOL: BULLISH (strength 0.67: EMA20>EMA50, MACD>signal)`.
- [~] `consensus_from_signals` produces the code-only BUY/SELL/HOLD baseline; persisting it next to the LLM consensus for over-time comparison is a light follow-on.
- [x] Backtestability: `SignalStrategy` in `bot/backtest.py` (lazy causal indicator series, no look-ahead), added to `default_strategies`, so `eval_backtest.py` shows it vs HODL/SMA/RSI. **Result: naive blend trails HODL (-14% alpha) but lowest drawdown — signals need weight-tuning, now measurable.**
- [x] Tests: `test_signals.py` (11) + `SignalStrategy` in `test_backtest.py`; appears in the eval table.

**Exit:** signals are computed in code, injected into meetings, and backtested; the
eval table shows the signal strategy's risk-adjusted return vs the baselines.

---

## Phase S2 — Conviction & volatility position sizing

`execute_trade` takes a raw LLM-chosen USD `amount` (`tools.py:197`); analyst
confidence (0..1) is emitted by every persona and consumed by nothing; size has no
link to volatility or conviction. The concentration cap is SOL-only and hardcoded
(`tools.py:226-254`).

Tasks:
- [x] `bot/sizing.py` (`max_buy_notional`): vol-targeted base size (risk budget / annualized vol), **regime-scaled** (×0.25 in a CHOPPY regime — the validated edge), optional conviction multiplier, hard-capped at `max_position_pct` of the book. Pure + unit-tested.
- [x] Wired into `execute_trade` as a guardrail: a BUY is **resized** to `max_buy_notional`; in a CHOPPY regime the size shrinks below the minimum and the trend-entry is **blocked** outright. Runs after the MAX_TRADE_USD gate so an injected oversized request is still blocked (not silently resized).
- [x] Generalized the concentration cap from SOL-only to **any asset** (`max_asset_exposure_pct`, with per-asset overrides like `max_sol_exposure_pct` staying stricter).
- [x] Tests: `test_sizing.py` (vol-target, regime shrink, conviction, per-trade cap) + `test_trade_gate.py` (resize in trend, sized-out in chop, BTC concentration cap, SOL stricter override). eval_trades stubs sizing for its deterministic e2e.

**Exit:** DONE. A BUY is sized by volatility × regime (not a free LLM pick); choppy
trend-entries are blocked; the concentration cap applies to every asset; covered by tests.
_(Conviction-from-consensus wiring deferred — `execute_trade` doesn't yet receive the
net consensus score; `max_buy_notional` already accepts a `conviction` arg for it.)_

---

## Phase S3 — Belief-revision debate + inference diversity

The debate is one pass of vote-emission; nobody revises after being challenged, and a
single model with personas stripped during debate collapses toward one view.

Tasks:
- [x] Revision round (`run_meeting` step 3b + `_prep_revision`): after the debate,
  agents who voted against the emerging consensus are shown the majority view + the
  strongest opposing argument and get a turn to confirm or change their `Final Vote`
  (appended as a `[DEBATE]:` segment so the closing tally uses the revised vote).
  Live-validated: a `SELL`-voting risk_auditor, given the trend counter-case, revised
  to `BUY` with reasoning ("I will yield to the majority's technical conviction").
- [~] Confidence capture deferred — the revision prompt asks for a vote; threading a
  numeric confidence through `_VOTE_RE` → `record_vote` (for M3 calibration) is the
  remaining piece.
- [~] Inference diversity: the M-phase LLM tuning already widened behavior via the
  desk rule + temperature spread; the explicit "argue against the room" contrarian
  framing is a small follow-on.
- [x] Tests: `test_belief_revision.py` (dissent detection — finds a dissenter vs a real
  majority, skips on unanimity/tie/no-votes, ignores ABSTAIN); meeting-flow tests still
  green with the revision round wired in.

**Exit:** agents can change their vote after rebuttal; votes carry confidence; the
panel is meaningfully less homogeneous.

---

## Phase S4 — Embeddings memory (stretch)

TF-IDF retrieval is keyword-only and queried with a price blob, so relevant past
meetings rarely surface (`vesper_engine.py`). There's already a TF-IDF-vs-embeddings
benchmark POC to validate against.

Tasks:
- [x] `bot/embeddings.py` (embed via the local `/v1/embeddings` model + cosine);
  `SemanticMeetingMemory` now keeps a persisted embedding index (`data/embeddings_index.json`)
  and ranks past meetings by cosine similarity. **Removed the external `d:\vesper-text`
  TF-IDF dependency entirely** and dropped the LLM query-expansion hack (embeddings
  capture meaning directly). Fails soft → "no context" if the embedder is down.
- [x] Live-validated on paraphrased queries (where TF-IDF fails): "shield the portfolio
  when prices fall" correctly retrieved the "protect capital in a downturn" meeting with
  zero keyword overlap; 2/3 tricky paraphrases hit the right precedent. (`test_memory.py`
  rewritten for embeddings + a cosine unit test.)

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
