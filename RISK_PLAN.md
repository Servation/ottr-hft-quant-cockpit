# OTTR — Risk-Limit Enforcement Plan (Tier 3)

**Scope:** Tier 3 of the improvement research: turn *measured* risk into *enforced*
risk. Touches `discord-bridge` only on the runtime path: new `bot/risk.py` (pure
policies) + a thin enforcer wired into the existing 60s `bot/alerts.py` loop,
`bot/tools.py` (a drawdown-halt BUY gate + a STOP order tool), `bot/portfolio.py`
(the forced-exit path, already the sole writer), `config/settings.yaml`, and
`bot/backtest.py` (to prove the controls). Read-only surfacing in `agent-gateway`
+ `frontend`.

**Why now:** Tier 1 made risk *measurable* (equity curve, `metrics.max_drawdown`,
shown on the dashboard) and Tier 2 sized *entries* (vol/regime sizing, a
concentration cap). But every risk control today is either a number nobody acts on
or a **pre-trade BUY gate** that never touches an existing position:

- **Drawdown is measured, never enforced.** `metrics.max_drawdown` (`metrics.py:62`)
  feeds the dashboard; nothing halts or de-risks when it blows out.
- **Stop-loss is a dead config knob.** `thresholds.stop_loss_pct: 10.0`
  (`settings.yaml:17`) is read by **no code** (grep confirms a single hit). The
  dial exists; the control does not.
- **Stop infrastructure is latent but never armed.** `portfolio.check_orders`
  (`portfolio.py:269-316`) already *executes* `STOP`/`TAKE_PROFIT` SELLs, and
  `alerts._notify_executed_orders` (`alerts.py:231`) already convenes an emergency
  meeting when a STOP fills. But the only order tool, `place_limit_order`
  (`tools.py:128`), is **LIMIT-only**, and nothing ever places a stop. The exit
  path is wired; the trigger is missing.
- **Concentration is a buy-time gate only.** `tools.execute_trade` (`tools.py:225-258`)
  blocks a BUY that would push an asset over `max_asset_exposure_pct` (35%) /
  `max_sol_exposure_pct` (20%), but a position that **drifts** over the cap via
  price appreciation is never trimmed.

Trading is **active with no per-trade cap** (`TRADING_DRY_RUN=0`, `MAX_TRADE_USD=0`).
Between 4-hour meetings, a position can run to a deep loss or an outsized weight with
**no autonomous brake**. Tier 3 makes risk a *control that acts*, in the 60-second
loop, without waiting for an LLM meeting. (It complements, not replaces, the
`risk_auditor` persona and the `risk_review` meeting: today "risk" is an agent that
*talks*; Tier 3 adds a control that *acts*.)

**Guiding rule:** Same bar as Tiers 1-2. Every policy is a **pure, deterministic,
backtestable** function (mirrors `bot/sizing.py` / `bot/metrics.py`): given
positions + prices + curve, it returns *actions*, with zero I/O. The bridge stays
the **sole portfolio writer**; forced actions go through `portfolio.sell`, never a
second writer. Every forced action respects the **kill-switch**, is **latched +
cooldown'd** (no double-fire on a 60s tick), **fails loud on missing data**, and is
**audited**. Gate every change on `pytest -k "not live"` (225) + `run_evals.py
--no-llm` (3); add a test for every behavior change.

**Status:** R0-R1 COMPLETE (R0 pure core; R1 autonomous stop-loss enforcer wired into
the 60s loop, dark behind `enabled: false`). R2-R4 pending.

Status legend: `[ ]` todo · `[~]` in progress · `[x]` done.

---

## Current state (the gap, precisely)

| Risk control | Measured? | Enforced at BUY? | Enforced continuously? |
|---|---|---|---|
| Per-position stop-loss | no | no | **no** (config knob unused) |
| Portfolio drawdown | **yes** (`max_drawdown`) | no | **no** |
| Concentration | yes (cap %) | **yes** (block) | **no** (no trim on drift) |
| Per-trade notional | n/a | yes (`MAX_TRADE_USD`, off) | n/a |
| Kill-switch | n/a | yes (`TRADING_DRY_RUN`) | yes (global) |

Tier 3 fills the **bold "no" cells**: an auto stop-loss (R1), a drawdown circuit
breaker (R2), and continuous concentration trimming (R3) — all on the existing 60s
loop, all routed through the existing sole writer.

---

## Phase R0 — Risk policy core + state + config (the spine)

Everything downstream calls this. Build the pure decision layer and the minimal
persisted latch *first*, wired into nothing yet (mirrors how `metrics.py`/`sizing.py`
landed as pure cores before their callers).

Concrete targets:
- New `discord-bridge/bot/risk.py` — pure functions, no I/O (template: `bot/sizing.py`).
- New `discord-bridge/data/risk_state.json` — the breaker latch only (bridge-owned).
- `config/settings.yaml` — a new `risk_limits` block.

Tasks:
- [x] **`bot/risk.py` pure policies**, each returning structured `RiskAction`s
  (`{kind, asset, qty|usd, reason, detail}`), never executing:
  - `stop_loss_breaches(holdings, prices, stop_pct, mode)` — positions where
    `(price - avg_cost)/avg_cost <= -stop_pct` (mode `avg_cost`; `trailing` reserved
    for R1 extension). Emits a full-liquidation SELL action per breach.
  - `drawdown_state(peak, current_value, halt_pct, resume_pct, was_halted)` — returns
    `{drawdown, halt, resume}` with **hysteresis** (trip at `halt_pct`, only clear
    below `resume_pct`) so it can't flap.
  - `concentration_breaches(holdings, prices, caps, band_pct)` — positions over
    `cap + band`; emits a partial SELL action sized to bring the weight back to the
    cap. Reads the **same** keys as the buy gate (`max_asset_exposure_pct`,
    per-asset overrides) so block and trim never disagree.
- [x] **Risk state (latch only)** — `bot/risk.py` (or a tiny `risk_state.py`) persists
  just `{halted, halted_since, last_action_ts: {asset: ts}}` to `risk_state.json` via
  the same atomic-write pattern as `portfolio.save()`. The drawdown **peak** is
  *derived from the equity curve* (`equity.load_curve()` max, vs the live value),
  **not** persisted — the curve stays the single source of truth; only the latch +
  per-asset cooldowns need to survive a restart.
- [x] **Config** — add `risk_limits` to `settings.yaml` (enable flags, `stop_loss_pct`
  promoted here, `max_drawdown_halt_pct`, `drawdown_resume_pct`,
  `concentration_trim_band_pct`, `action_cooldown_seconds`, `min_curve_points`,
  `drawdown_auto_derisk: false`). Keep the existing `thresholds.*` concentration caps
  where they are (R3 reads them); document that `risk_limits` holds the *new* knobs.
- [x] **Tests** — `tests/test_risk.py`: each policy on fixed inputs (stop trips at the
  threshold and not above it; drawdown hysteresis trips/holds/clears; trim sizes the
  excess correctly and ignores a within-band position; missing/`<=0` price yields **no**
  action, never a bad one).

**Exit:** `bot/risk.py` decides stop / halt / trim from numbers, deterministically and
I/O-free; the latch persists; config exists; fully unit-tested; **no runtime behavior
changed yet.**

---

## Phase R1 — Stop-loss enforcement (arm + auto-exit)

The highest-value first control: it directly bounds per-position loss, the most acute
gap given active trading with no per-trade cap.

Concrete targets:
- `bot/alerts.py:74-113` `_monitor_loop` (the existing 60s loop) + `_notify_executed_orders`.
- `bot/portfolio.py:179-236` `sell` (the sole-writer exit) and `:269-316` `check_orders`.
- `bot/tools.py:128-162` `place_limit_order`.

Tasks:
- [x] **Continuous auto-stop (the enforced control)** — add a risk-enforcement step to
  `_monitor_loop` (prices already fetched there for `check_orders`). Call
  `risk.stop_loss_breaches`; for each breach, force a SELL through a single shared
  helper `_execute_risk_action()` that: respects `TRADING_DRY_RUN` (log + `audit_event`,
  no execute), enforces the per-asset `action_cooldown` latch (no re-fire while a
  position is mid-exit), calls `portfolio.sell`, posts to the trading floor, and reuses
  the existing stop-loss emergency-meeting path so the agents are told *why* (and don't
  immediately re-enter). Stop is measured off the **live** avg_cost, so it adapts as a
  position is added to (no stale order to maintain).
- [~] **Let agents arm explicit stops too (secondary)** — extend `place_limit_order`
  (or add `place_protective_order`) to accept `STOP`/`TAKE_PROFIT`; `check_orders`
  already executes them. This gives the desk discretionary protective orders on top of
  the automatic floor.
- [~] **Trailing option (extension)** — `stop_loss_mode: trailing` tracks a per-position
  high-water mark in `risk_state` and stops a fixed % off the high. Default stays
  `avg_cost` to keep R1 tight; trailing ships only if the backtest (R4) shows it earns
  its keep.
- [x] **Tests** — `tests/test_risk_enforcer.py`: a position below the stop force-sells
  through the (mocked) portfolio once and only once; cooldown blocks a second fire;
  `TRADING_DRY_RUN=1` makes it a logged no-op; a missing price is skipped, not sold.

**Exit:** a position breaching `stop_loss_pct` is auto-exited within one loop tick,
idempotently, audited, kill-switch-respecting; `stop_loss_pct` is finally a live
control, not dead config. _(Agent-placed explicit stops + trailing deferred to keep R1
fully dark; the autonomous floor is the R1 deliverable, behind `enabled`.)_

---

## Phase R2 — Portfolio drawdown circuit breaker

Drawdown is the one risk metric already computed and shown; R2 makes it act.

Concrete targets:
- `bot/alerts.py` loop (the enforcer) + `bot/equity.py:87-104` `load_curve` (peak source).
- `bot/tools.py:195-298` `execute_trade` — a new halt gate alongside the
  `MAX_TRADE_USD` (`:208-223`) and concentration (`:225-258`) gates.

Tasks:
- [ ] **Trip / latch** — each tick compute current drawdown = `(peak - live_value)/peak`
  where `peak = max(curve values, live_value)` and `live_value =
  portfolio.get_total_value(prices)` (responsive at 60s even though the curve samples
  hourly). On `drawdown >= max_drawdown_halt_pct`, latch `halted=true` in `risk_state`,
  post to the trading floor, and convene an emergency meeting (reuse the alert path). Skip
  entirely until the curve has `>= min_curve_points` (fail-loud: never trip on a thin
  series).
- [ ] **Enforce the halt** — `execute_trade` reads the latch and **blocks new BUYs**
  (read tools and SELLs stay allowed, so the desk can de-risk and reason). This is a
  *softer* auto-tripped sibling of the hard `TRADING_DRY_RUN` master kill: they compose
  (kill-switch off = nothing trades; halt = stop *adding* risk).
- [ ] **Resume** — auto-unlatch when drawdown recovers below `drawdown_resume_pct`
  (hysteresis from R0); also expose a manual reset (a tool / CEO directive, audited).
  The latch persists, so a restart mid-drawdown does **not** silently resume buying.
- [ ] **Optional auto-de-risk** — `drawdown_auto_derisk` (default **false**): when true,
  on trip also trim the most-concentrated / worst positions via the R3 path. Conservative
  default is halt-only; forced liquidation is opt-in.
- [ ] **Tests** — breaker trips at the halt threshold, holds through the hysteresis band,
  clears below resume; a halted state blocks a BUY in `execute_trade` but allows a SELL;
  the latch round-trips through `risk_state.json`; a thin curve never trips.

**Exit:** a portfolio drawdown past the limit auto-halts new risk (latched, persisted,
hysteresis-guarded), notifies the desk, and resumes only on recovery or audited manual
reset.

---

## Phase R3 — Continuous concentration enforcement (trim, not just block)

Close the "drifted over the cap and nothing trims it" gap; reuse the existing caps so
block (buy-time) and trim (continuous) agree by construction.

Concrete targets:
- `bot/alerts.py` loop (the enforcer) + `bot/risk.concentration_breaches` (R0).
- The existing caps in `settings.yaml:18-23` (`max_sol_exposure_pct`,
  `max_asset_exposure_pct`) — read by both the buy gate and the trim.

Tasks:
- [ ] **Detect + trim** — each tick, `risk.concentration_breaches` flags positions over
  `cap + concentration_trim_band_pct` (a tolerance band so normal wiggle doesn't churn);
  the shared `_execute_risk_action()` SELLs the **excess** back to the cap (partial),
  cooldown-latched, dry-run-respecting, audited, posted. Trim only ever *reduces* an
  oversized position (never opens or flips).
- [ ] **Coherence with the buy gate** — same config keys, same per-asset override
  precedence (SOL stricter), so a buy blocked at 20% and a trim back to 20% use one source
  of truth. No second definition of "the cap."
- [ ] **Tests** — a position above `cap + band` trims to the cap; a position within the
  band is untouched; the SOL override trims tighter than the general cap; cooldown
  prevents repeated trims within the window.

**Exit:** an asset that drifts past its concentration cap is trimmed back automatically,
in agreement with the buy-time gate, idempotently and audited.

---

## Phase R4 — Backtest the controls + surface state + ops

Prove the controls help (or measure their drag), make them visible, document them.

Concrete targets:
- `bot/backtest.py` (deterministic engine) + `eval_backtest.py`.
- `bot/api_server.py` `/api/performance` (read-only) → gateway `/portfolio/snapshot` → frontend.
- `docs/RUNBOOK.md`, `AUDIT_PLAN.md` invariants, `CLAUDE.md` safety section.

Tasks:
- [ ] **Backtest integration** — apply the stop-loss + drawdown-halt policies inside the
  backtest engine (they're pure, so they plug in) and report each strategy *with vs
  without* the risk overlay on the real BTC/ETH/SOL fixtures: does a 10% stop / 15% halt
  cut drawdown without giving back all the return? Register the comparison in
  `eval_backtest.py` so it stays measured (the Tier-1 rule applied to risk). Expected
  finding to validate: stops cap tail loss but can whipsaw in chop — quantify it.
- [ ] **Observability** — add a read-only `risk` block to the performance payload
  (`halted`, `current_drawdown` vs limit, stops armed, last forced action) so the CEO can
  see the controls are live. Gateway reads, never writes; no secrets (Phase-1 invariant).
- [ ] **Ops** — RUNBOOK section: what each control does, how to tune the `risk_limits`
  thresholds, how to manually reset a halt, how it composes with the kill-switch. Add
  "autonomous risk enforcement" to the AUDIT_PLAN / CLAUDE.md invariants so a later change
  can't silently regress it.
- [ ] **Tests** — backtest determinism with the overlay (re-runs identical); the
  performance endpoint exposes the risk block and degrades to nulls when state is absent.

**Exit:** the risk overlay is backtested on real fixtures and CI-green; the dashboard
shows live risk state; the runbook documents operation + manual reset; the invariant is
recorded.

---

## Sequencing & invariants

**Order.** R0 is the spine (pure policies + latch + config). R1, R2, R3 are
independent enforcers that all hook the same 60s loop and share `_execute_risk_action()`
from R0, so they can land in any order — recommended **R0 → R1 → R2 → R3 → R4**, with
R1 first for the fastest real risk reduction (per-position loss is the most acute gap).
R4 proves and surfaces them last. (R0 then R1‖R2‖R3 then R4 also works.)

**One loop, one enforcer.** Enforcement is a new *step* in the existing
`AlertMonitor._monitor_loop` (it already fetches prices, holds the bot handle, and has
cooldown machinery), calling pure `bot/risk.py` + the thin `_execute_risk_action()`
helper. No new background task, no second prices fetch — smallest change that works.

**Do NOT regress (carry every prior invariant forward):**
- **Sole portfolio writer** stays `portfolio.py`; all forced exits/trims call
  `portfolio.sell`. The gateway still reads, never writes.
- **Kill-switch** (`TRADING_DRY_RUN`) gates every forced action (dry-run → log + audit,
  no execute). The drawdown halt is a softer auto-trip that composes with it.
- **Idempotent / no double-fire**: every forced action is latch + cooldown guarded; the
  drawdown breaker is hysteresis-latched. A 60s tick never spams sells or re-trips.
- **Fail loud on missing data**: no action on `price <= 0` or a curve below
  `min_curve_points`; policies return *no action* (never a bad one) on absent inputs.
- **Audit (Phase 6)**: `stop_loss_fill`, `drawdown_halt` / `drawdown_resume`,
  `concentration_trim`, and manual reset each call `bot.audit.audit_event`.
- Input fencing, auth, idempotent tool execution, `MAX_TRADE_USD` — untouched.

**Measured by Tier 1:** the stop / halt overlay is validated in `bot/backtest.py`
against the fixtures, and shows up live in the equity metrics + the new risk block. No
control ships without a backtest number on whether it helps.

**Forced actions feed the agents.** Every autonomous action posts to the trading floor
and (for stops/halts) convenes an emergency meeting, so the desk learns *why* and does
not immediately undo it — preventing enforce/re-enter thrash.

**Rough effort:** R0 ~1d · R1 ~1-1.5d · R2 ~1-1.5d · R3 ~0.5-1d · R4 ~1-1.5d.
