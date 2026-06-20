# OTTR Project Handbook

The single, complete reference for the OTTR multi-agent crypto trading system:
what it is, how it's built, how to run and test it, its current security and
correctness posture, the known issues, and the audit work done so far.

> Companion docs (don't duplicate — read on demand):
> `README.md` (user-facing overview), `CONTEXT.md` (domain glossary),
> `threat_model.md` (STRIDE security assessment), `AUDIT_PLAN.md` (phased
> hardening plan), `STATE_INVENTORY.md` (portfolio/journal state map),
> `discord-bridge/docs/TEST_BASELINE.md` (test/eval gate status).

---

## 1. What OTTR is

OTTR is a dashboard-driven, cooperative **multi-agent crypto trading system**.
Seven specialized AI agents "meet" in a Discord channel (`#trading-floor`),
reach a consensus (BUY / SELL / HOLD) on assets, and execute simulated trades
via native tools. A human "CEO" supervises from a React dashboard or directly in
Discord. It is a **paper/simulated** trading system (portfolio state is local
JSON), driven by a local LLM (LM Studio, OpenAI-compatible).

## 2. Architecture & components

Four top-level components (the 4th is effectively empty):

| Component | Stack | Port | Role |
|---|---|---|---|
| `discord-bridge` | Python, discord.py | aiohttp API :8001 | **The core bot.** Agents, tools, meetings, scheduler, portfolio, price feed, memory, reputation. |
| `agent-gateway` | Python, FastAPI | :8000 | API + SSE bridge between the dashboard and the bot; market/journal/SOPR services. |
| `frontend` | React + Vite (TS) | :3000 | Dashboard ("web terminal") into the trading floor. |
| `shopping-assistant` | — | — | Essentially empty (one skill file). Out of scope. |

Deploy topology (`docker-compose.yml`): frontend -> agent-gateway -> discord-bridge.
The gateway mounts `./discord-bridge/data` as a volume to read portfolio state
(see Section 5 for the caveat).

```
React UI (:3000) <-> FastAPI gateway (:8000) <-> discord-bridge bot (:8001) <-> Discord #trading-floor
                                                          |
                                                   LM Studio (:1234) + market APIs
```

## 3. The agents & meeting machinery (`discord-bridge/bot`)

**Seven personas** (`bot/agents.py`, prompts in `config/personas/*.txt`):
technical_analyst (Atlas), sentiment_analyst (Luna), trader (Mercury),
risk_auditor (Rogue), performance_optimizer (Zephyr), portfolio_manager (Midas),
meeting_chair (Athena, the facilitator).

**Meetings** (`bot/meetings.py`) run in strict phases:
1. *Independent Report* — each agent gives an initial assessment, no vote.
2. *Debate Round* — agents challenge each other and cast a mandatory
   `Final Vote: <BUY|SELL|HOLD|ABSTAIN> <ASSET>`.
3. *Closing* — the chair tallies a weighted "Algorithmic Consensus" (weights come
   from the reputation graph) and executes approved trades via native tools.

**Rotation** (`MeetingRotation`, persisted to `data/rotation_state.json`):
`ROTATION_ORDER = [strategy_session, trade_execution]`.

**Scheduler** (`bot/scheduler.py`): APScheduler with 6 cron meeting slots
(hours 0/4/8/12/16/20, `US/Pacific`) + 1 background `evaluate_predictions` job,
plus agent-invoked dynamic meetings. Total jobs after `start()` = 7.

**Reputation** (`bot/knowledge_graph.py`): tracks per-agent, per-asset win rates;
feeds vote weighting in the closing.

**Memory** (`bot/memory.py`): short-term = recent Discord messages; long-term =
"Vesper Text" TF-IDF semantic search over a Markdown vault of past meetings.

**CEO handler** (`bot/ceo_handler.py`): an LLM intent router classifies each CEO
message into `[IGNORE] / [QUEUE] / [EMERGENCY] / [DIRECT:agent] / [DISCUSSION:a,b]`.

## 4. Tool calling & safety (`bot/tools.py`, `bot/agents.py`)

Agents call tools two ways, both handled in `AgentLLM.generate_response`:
- **Native** OpenAI-style `tool_calls`.
- **Fallback** raw-tag parsing for local models (e.g. Gemma) that leak
  `<|tool_call>call:name{...}<|tool_call>` as text.

Tools: read-only (`get_asset_price`, `get_portfolio_summary`,
`get_historical_volatility`) and state-mutating (`execute_trade`,
`update_parameter`, `cancel_orders`, `schedule_meeting`, `start_meeting_now`).

**Safety invariants established (do not regress):**
- **Idempotent tool execution.** `generate_response` tracks executed
  (name+args) signatures per turn; a model repeating the same tool call (native
  or raw-tag) executes it **once**. Prevents double-trades.
- **Kill-switch.** `TRADING_DRY_RUN=1` blocks `execute_trade` /
  `update_parameter` / `cancel_orders` (no-op + audit log); read tools still work.
- **Sanitize + fence untrusted input** before the LLM; **`MAX_TRADE_USD`** caps single-trade notional.
- **Rate-limit the edges**: `API_RATE_LIMIT` (per-IP) and `CEO_MIN_INTERVAL_SEC` (per-CEO) throttle DoS/LLM-spam; generic error bodies; locked CORS.
- **Fail loud on missing market data.** `price_feed.get_prices()` *raises* when
  both APIs fail with no cache (never returns fake $0.00 prices). Meetings
  *abort* if market data is unavailable rather than trading blind.

## 5. Data & state model

| File | Owner | Notes |
|---|---|---|
| `discord-bridge/data/portfolio_state.json` | `bot/portfolio.py` (sole writer) | **Authoritative** portfolio (cash, holdings, P&L, orders). Atomic save (temp + `os.replace`). |
| `discord-bridge/data/rotation_state.json` | `MeetingRotation` | Current rotation index. |
| `agent-gateway/trade_journal.json` | `services/journal_manager.py` | Trade journal; path is **CWD-relative** (fragile). |
| `agent-gateway/portfolio_state.json` | nobody | **Orphan / stale** — read by nothing. |

**Split-brain caveat (see `STATE_INVENTORY.md`):** the gateway reads the bridge's
portfolio by a hardcoded relative path (`../../../discord-bridge/data/...`).
docker-compose mounts `./discord-bridge/data` into the gateway, but the relative
path may not resolve to the mount point, so the dashboard can show an empty
portfolio in some deploys. Phase 4 fix: serve portfolio truth over a bridge API.

## 6. Running, testing, and the CI gate

**LLM backend:** LM Studio (OpenAI-compatible) at `http://localhost:1234/v1`,
model id per `.env` (e.g. `gemma-4-12b`). In Docker the bridge defaults to
`host.docker.internal:1234`; for a **local** run use `localhost:1234`.

**Tests (deterministic):**
```
cd discord-bridge
pip install -r requirements.txt -r requirements-dev.txt
pytest -k "not live" tests/        # 97 passing; "live" tests need network/LLM
```

**Eval suite (agent/LLM behavior):**
```
cd discord-bridge
python run_evals.py                # auto-detects a reachable LLM; runs LLM evals if up
python run_evals.py --no-llm       # offline evals only (always green)
```
`run_evals.py` probes `localhost` / `127.0.0.1` / `host.docker.internal` and
**forces** the reachable endpoint onto every child eval, so a wrong `.env`
`LLM_BASE_URL` doesn't matter for a local run.

**CI** (`.github/workflows/ci.yml`, Python 3.12): runs `run_evals.py --no-llm`
and `pytest -k "not live"` as **blocking** gates.

## 7. Security posture

Full assessment in `threat_model.md`; hardening sequence in `AUDIT_PLAN.md`.

**Fixed:**
- LLM fallback **API key leak** removed from the gateway `/portfolio/snapshot`.
- **Authentication (Phase 1):** all state-changing endpoints (bridge
  `/api/directive` + gateway mutation routes) now require a shared `OTTR_API_KEY`
  (`X-API-Key` header); unauthenticated -> 401, unconfigured -> 503 (fail-closed).
- **CEO identity (Phase 1):** `CEO_DISCORD_ID` is required (fail-closed) and the
  dashboard-prefix spoofing bypass is closed (trust tied to `author.bot`).
- **Prompt-injection hardening (Phase 2):** all untrusted CEO/user text is
  sanitized (`security.sanitize_user_input`) and fenced in `<user_input>` with a
  data-not-instructions marker before reaching the LLM. A `MAX_TRADE_USD` notional
  cap blocks oversized single trades.

- **Edge hardening (Phase 3):** error responses are generic (no `str(e)`),
  CORS is restricted to `GATEWAY_ALLOWED_ORIGINS`, and per-IP rate limits
  (`API_RATE_LIMIT`) + a per-CEO cooldown (`CEO_MIN_INTERVAL_SEC`) throttle the
  HTTP surface and LLM dispatch.

**Status:** Phases 0-6 of `AUDIT_PLAN.md` are complete. Remaining residual risk:
read-only endpoints are intentionally unauthenticated (SSE limitation), mitigated
by CORS lockdown + localhost binding.

**Phase 4-6 additions:** portfolio source-of-truth reconciled via
`PORTFOLIO_STATE_PATH`/`JOURNAL_FILE` over the shared Docker volume; a tool-registry
test guards 'advertised but unhandled' tools; gateway tests are in CI; a JSONL
audit trail (`bot/audit.py`) records every state change + directive; startup
config validation (`security.validate_runtime_config`); ops procedures in
`docs/RUNBOOK.md`.

## 8. Engineering gotchas (read before you debug)

- **`.env` auto-loads on import.** `bot/__init__.py` calls `load_dotenv()` for both
  `discord-bridge/.env` and the repo-root `.env`. Tests inherit those vars — most
  notably `CEO_DISCORD_ID`, which makes `on_message` reject mock authors. Tests
  must pin identity (`tests/test_ceo_handler.py` does this).
- **Python version.** Code uses 3.12 in production; an f-string-with-backslash in
  `ceo_handler.py` (now fixed) had locked it to 3.12. It now imports on 3.10-3.12.
- **Dependencies.** `pandas-ta` is required by `price_feed.py`. `pytest-mock`
  (the `mocker` fixture) is required by the suite — both are now declared.
- **Line endings.** Some files are CRLF, others LF; diffs can look huge. Consider
  a `.gitattributes` to normalize.
- **Scheduler timezone.** Jobs live in `US/Pacific`; `get_next_meeting_info()`
  normalizes display so cron and dynamic jobs don't show mixed PDT/UTC.

## 9. What the audit changed (chronological summary)

1. **Evals made trustworthy.** Fixed a hidden **double-execution** bug (added the
   idempotency guard) that a too-weak assertion was masking; rewrote dead/stale
   validations in `eval_real_llm.py` (incl. a hardcoded-asset check); fixed the
   scheduler eval's timezone-flawed assertion; aligned `eval_meeting_phases` with
   the production `strip_output_format` path; added portfolio fixture isolation so
   evals never mutate live state.
2. **Single eval runner** (`run_evals.py`) with endpoint auto-detection, UTF-8
   handling, and real exit codes.
3. **`portfolio.save()` temp-file leak** fixed (cleanup on failed `os.replace`).
4. **Kill-switch** (`TRADING_DRY_RUN`) added with tests.
5. **Gateway API-key leak** fixed.
6. **Phase 0 safety net:** CI gate; declared missing `pytest-mock`; fixed the
   3.12-only f-string; documented the portfolio split-brain.
7. **Test suite turned green (97 passing).** All 16 failures were stale tests, not
   new bugs — root causes recorded in `TEST_BASELINE.md`. Two were cases where the
   *code* was correctly safer (raise on missing data; abort meeting without data);
   those behaviors were preserved and given dedicated tests.

## 10. Roadmap (from `AUDIT_PLAN.md`)

Phases 0 (safety net), 1 (auth & identity), 2 (input safety), and 3 (edge hardening) — **done**. Next, in order:
Phase 1 (auth & identity), Phase 2 (input safety / prompt injection), Phase 3
(CORS, error sanitization, rate limits), Phase 4 (agent/tool correctness +
portfolio reconciliation), Phase 5 (gateway/frontend test coverage), Phase 6
(audit logging, config validation, runbook).
