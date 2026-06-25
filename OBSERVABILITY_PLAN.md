# OTTR — Observability & Transparency Plan (Tier 4)

**Scope:** Tier 4 of the improvement research: make the running system *legible* in real
time. Touches `discord-bridge` (event emission, trade rationale capture, a component-health
endpoint), `agent-gateway` (SSE broadcast, a health-aggregation endpoint), and `frontend`
(live execution feed, the rationale display, a system-health panel).

**Why now:** Tier 1 made it *measurable*, Tier 2 made decisions *grounded*, Tier 3 made
risk *enforced*. But the system is still opaque to the operator:

- **The live event pipeline is half-built.** The bridge can push events to the gateway's
  SSE (`bot/webhooks.py`: `sync_agent_state` is wired into meetings, but `sync_execution`
  and `sync_portfolio` are **defined and never called**). So the dashboard updates agent
  status live, but trades and portfolio changes only appear on a poll/refresh, and there's
  no real-time "the desk just did X."
- **Trades have no captured "why."** The trade record (`portfolio.py` buy/sell) carries no
  reasoning; the gateway execution-log maps a **hardcoded** `"Executed via Discord Meeting
  consensus."` (`agent-gateway/app/routers/api.py:242`); `journal_manager.record_journal_entry`
  accepts a `reasoning` arg but is never called. The frontend `ExecutionLogsTable` already
  has a "Decision Reasoning Chain" disclosure (`ExecutionLogsTable.tsx:112-124`) with nothing
  real to show. A meeting produces `decisions` (`meetings.py:_summarize_outcome`) but that
  link never reaches the trade.
- **There's no health view.** `/api/v1/health` is a static `{status: OK}`. Component checks
  exist but are scattered and Discord-only: LLM (`agents.check_health`), price feed and
  scheduler (meeting-abort paths in `scheduler.py`). The operator can't see at a glance
  whether LM Studio is up, prices are fresh, or the scheduler is ticking.

So the CEO can measure performance and trust the risk controls, but can't *watch* the desk
work, *understand* a given trade, or *tell if the plumbing is healthy*. Tier 4 closes that.

**Guiding rule:** Observability is **read-only and additive** — it must never change trading
behavior or the portfolio. Events are emitted *after* a state change, best-effort, and a
failure to emit can never break the trade path (the gateway being down must not stop a
trade). The bridge stays the **sole writer**; the gateway relays/aggregates, never writes.
**No secrets to clients** (same posture as the snapshot-leak fix). Gate every change on
`pytest -k "not live"` + `run_evals.py`; add a test per change.

**Status:** O0-O1 COMPLETE (O0 live execution + portfolio events; O1 trade rationale flows
to the record + audit + live feed). O2-O3 pending.

Status legend: `[ ]` todo · `[~]` in progress · `[x]` done.

---

## Current state (the gap, precisely)

| Area | Wired today | Missing |
|---|---|---|
| Live events | bridge→gateway→frontend SSE for `agent_state` | `execution` + `portfolio` never fired; no `meeting_outcome` |
| Trade rationale | trade dict shape; audit log; CoT UI disclosure | no "why" captured at trade time; hardcoded gateway fallback |
| Health | static `/health`; scattered Discord-only checks | no component aggregation endpoint; no frontend panel |

---

## Phase O0 — Complete the live event pipeline (the spine)

The plumbing exists; the calls don't. Fire the already-defined events so the dashboard is
live, then add the one missing event type. Everything else in Tier 4 rides this.

Concrete targets:
- `discord-bridge/bot/webhooks.py` — `sync_execution` / `sync_portfolio` (defined, unused).
- `discord-bridge/bot/tools.py` — after `execute_trade` BUY/SELL (~`:300-318`).
- `agent-gateway/app/routers/discord_webhooks.py` — `/api/internal/discord-sync` allow-list.
- `frontend/src/services/apiClient.ts:175-239` — `subscribeToAgentEvents` listeners.

Tasks:
- [x] **Fire execution + portfolio events** — after a trade fills and after a forced risk
  action, `webhooks.publish_trade(trade, portfolio, prices)` fires `execution` + `portfolio`
  **fire-and-forget** (the gateway already broadcasts both, no gateway/frontend change). A
  normalizing shaper maps the bridge trade dict to the frontend's expected shape (`symbol`/
  `price`); a 2s timeout + fail-soft push means a down/slow gateway never blocks a trade.
  _(Order-fill path in `alerts` is a small O3 follow-on.)_
- [~] **`meeting_outcome` event** — at meeting close, emit `{summary, decisions, consensus,
  votes}`. _(Deferred to O3: it needs new gateway allow-list + frontend plumbing, grouped with
  the other new live-event work there, unlike the execution/portfolio events the gateway already
  broadcasts.)_
- [x] **Tests** — `tests/test_webhooks.py` (5): payload shaping, a trade fires both events,
  publish is fire-and-forget (schedules the send, doesn't await), `execute_trade` wires it in.

**Exit:** trades, portfolio changes, and meeting outcomes stream to the dashboard live (no
poll); a gateway outage degrades silently and never affects trading.

---

## Phase O1 — Trade rationale ("why did we trade")

Capture the decision behind each trade and carry it all the way to the UI disclosure that's
already waiting for it.

Concrete targets:
- `discord-bridge/bot/tools.py` `execute_trade` + `bot/portfolio.py` buy/sell (trade dict).
- `discord-bridge/bot/audit.py` (`audit_event("trade", ...)`); `bot/meetings.py` decision path.
- `agent-gateway/app/routers/api.py:242` (the hardcoded reasoning fallback) + `journal_manager`.
- `frontend/src/components/ExecutionLogsTable.tsx` (the CoT disclosure, already built).

Tasks:
- [x] **Capture reasoning at trade time** — `execute_trade` takes an optional `reasoning` (the
  agent's own one-line justification, capped at 500 chars), stored on the trade record
  (`portfolio.buy/sell`) and the `audit_event("trade", ...)`. Pure annotation, kept out of the
  cost basis / P&L. _(Auto-enriching with the consensus tally / regime context is a follow-on.)_
- [x] **Surface it** — the `execution` SSE event (O0) carries the reasoning and the gateway
  execution-log already prefers `trade["reasoning"]` over the hardcoded fallback; the
  `ExecutionLogsTable` CoT box already renders it — so the whole display chain lights up with no
  gateway/frontend change. _(Wiring `journal_manager.record_journal_entry` to feed the LLM's own
  context is deferred — it would re-enter a prompt, so it needs sanitization/fencing first.)_
- [~] **Link to the meeting (optional)** — stamp the originating `meeting_id` on the trade.
- [x] **Tests** — `tests/test_rationale.py` (3): reasoning persists on the trade record,
  `execute_trade` threads it to the writer + audit, the execution payload carries it.

**Exit:** every executed trade shows a real, specific "why" in the dashboard's reasoning
disclosure and the audit log; the placeholder string is gone.

---

## Phase O2 — Health aggregation

One place that answers "is the system healthy?" across components, surfaced to the operator.

Concrete targets:
- New `discord-bridge/bot/api_server.py` `/api/health` (component status, read-only).
- New `agent-gateway` `/api/v1/health/detailed` aggregating bridge + its own view.
- New `frontend` `SystemHealthPanel.tsx`.

Tasks:
- [ ] **Bridge component-health endpoint** — `/api/health` reports, read-only and secret-free:
  LLM reachable (`agents.check_health` + latency), price-feed freshness (last-quote age),
  scheduler running + next-meeting time, portfolio-state readable + age. Degrades gracefully
  (a failed sub-check reports DOWN, never 500s the whole endpoint).
- [ ] **Gateway aggregation** — `/api/v1/health/detailed` best-effort-proxies the bridge health,
  adds bridge-reachability + latency, and rolls a top-line `OK | DEGRADED | DOWN`. Bridge down →
  the gateway still answers with `bridge: DOWN` (never hangs the dashboard).
- [ ] **Frontend `SystemHealthPanel`** — polls `/health/detailed` (~30s), renders a compact
  component grid (green/amber/red + last-checked), placed in the dashboard chrome. Hidden/raw
  values only; no secrets.
- [ ] **Tests** — bridge health endpoint reports per-component status and degrades on a failed
  sub-check; gateway aggregation rolls the top-line status and tolerates a down bridge; frontend
  health mapping (vitest).

**Exit:** `/health/detailed` returns a per-component status that rolls up to one verdict; the
dashboard shows live component health; a single component failing is visible, not silent.

---

## Phase O3 — Polish (stretch)

Tasks:
- [~] **Optimizer history events** — the frontend already listens for `optimization_history`
  and the endpoint returns `[]`; emit real events when a parameter is tuned/reverted.
- [~] **Health history** — keep a short rolling health log so flapping components are visible
  ("price feed DOWN 3x in the last hour"), not just the instantaneous state.
- [~] **SSE resilience** — verify the frontend `EventSource` reconnects cleanly and de-dupes on
  reconnect; confirm the bridge push has a timeout so a slow gateway can't back up the bot.

**Exit:** the optimizer feed is real; health flapping is visible; the live pipeline survives a
gateway blip without leaks or duplicates.

---

## Sequencing & invariants

**Order.** O0 is the spine (the event delivery O1/O2 lean on). O1 and O2 are largely
independent and can land in either order after O0. O3 is stretch. Recommended **O0 → O1 → O2**,
then O3.

**Do NOT regress (carry every prior invariant forward):**
- **Observability never changes trading.** Events fire *after* state changes, best-effort and
  fire-and-forget; a down/slow gateway can never block or alter a trade. The bridge stays the
  **sole portfolio writer**; the gateway relays/aggregates, never writes.
- **No secrets to clients.** SSE, execution, health, and rationale payloads carry portfolio-
  derived/operational data only — never LLM keys/config (the Phase-1 snapshot-leak posture).
- **Fail loud / degrade gracefully.** Health sub-checks report DOWN rather than 500; the gateway
  answers even when the bridge is unreachable.
- **Input safety.** Captured rationale is DATA — if it's ever fed back into an LLM prompt, it
  goes through `security.sanitize_user_input` and is fenced (Phase-2). Audit invariants intact.
- CI (`pytest -k "not live"` + `run_evals.py`) green **before and after** each phase; a test per
  change.

**Measured by Tier 1:** Tier 4 adds no trading edge by design — it's validated by the test/eval
gate and by the dashboard reflecting real, live state (trades, reasons, health) rather than
polled/placeholder data.

**Rough effort:** O0 ~1-1.5d · O1 ~1-1.5d · O2 ~1.5d · O3 ~1d (stretch).
