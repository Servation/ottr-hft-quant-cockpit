# CLAUDE.md

Guidance for AI agents working in this repo. Keep this file lean; the full story
lives in `PROJECT_HANDBOOK.md`.

## What this is
OTTR — a simulated multi-agent crypto trading system. Four parts:
`discord-bridge` (the core bot: agents, tools, meetings, scheduler, portfolio,
price feed, memory; aiohttp API :8001), `agent-gateway` (FastAPI :8000),
`frontend` (React/Vite :3000), `shopping-assistant` (empty). LLM backend is local
LM Studio (OpenAI-compatible) at `http://localhost:1234/v1`.

## Commands
```
cd discord-bridge
pip install -r requirements.txt -r requirements-dev.txt
pytest -k "not live" tests/     # unit tests (97 pass; "live" tests need network/LLM)
python run_evals.py             # agent/eval suite (auto-detects the LLM; --no-llm to skip)
```
CI (`.github/workflows/ci.yml`, Python 3.12) runs both as blocking gates. Run the
full suite green before and after changes.

## Deploy (Docker)
`docker compose up -d --build` (or `./redeploy.ps1`). **A rebuild is required** —
the frontend bakes `VITE_OTTR_API_KEY` at build time and the gateway installs deps.
Env is interpolated from the repo-root `.env`; one `OTTR_API_KEY` feeds all three
services. Bridge + gateway share `./discord-bridge/data` (single source of truth,
persists across restarts). Never `down -v`. Full procedure in `docs/RUNBOOK.md`.

## Safety invariants — do NOT regress
- **Idempotent tool execution** (`bot/agents.py`): a repeated tool call (native or
  raw `<|tool_call>` tag) executes once. Prevents double-trades.
- **Kill-switch**: `TRADING_DRY_RUN=1` blocks `execute_trade` / `update_parameter`
  / `cancel_orders` (no-op + audit log); read tools still work.
- **Fail loud on missing data**: `price_feed.get_prices()` raises (never returns
  $0.00) when both APIs fail with no cache; meetings abort without market data.
- **Never send secrets to clients** (the gateway `/portfolio/snapshot` leak is
  fixed — don't reintroduce LLM keys/config in responses).
- **Auth (Phase 1)**: state-changing endpoints require the shared `OTTR_API_KEY`
  (`X-API-Key`); fail-closed (503) if unset. `CEO_DISCORD_ID` is required and the
  dashboard-prefix bypass is closed — don't loosen these.
- **Input safety (Phase 2)**: untrusted CEO/user text must be passed through
  `security.sanitize_user_input` and fenced in `<user_input>` before any LLM call.
  `MAX_TRADE_USD` caps single-trade notional. Don't inject raw user text into prompts.
- **Edge hardening (Phase 3)**: never return `str(e)` to clients (log it, return a
  generic message); keep CORS limited to `GATEWAY_ALLOWED_ORIGINS`; preserve the
  per-IP `API_RATE_LIMIT` and per-CEO `CEO_MIN_INTERVAL_SEC` throttles.
- Portfolio truth has **one writer**: `discord-bridge/bot/portfolio.py`. The gateway
  reads it via `PORTFOLIO_STATE_PATH` (shared volume). Don't add a second writer.
- **Audit (Phase 6)**: mutating tools + directives must call `bot.audit.audit_event`.

## Gotchas
- `bot/__init__.py` auto-loads `.env` (bridge + repo root) on import; tests inherit
  vars like `CEO_DISCORD_ID`, which gates `on_message`. Tests must pin identity.
- Required deps: `pandas-ta` (price feed), `pytest-mock` (tests).
- Avoid backslashes inside f-string expressions (locks Python to 3.12+).
- For a local (non-Docker) run, set `LLM_BASE_URL=http://localhost:1234/v1`.
- The pre-commit hook is a deliberate **no-op** — tests run in CI, not on commit
  (pytest needs the project env, which pre-commit's shell doesn't share).

## Conventions
- Make file edits, then verify with `pytest`/`run_evals.py`. Add a test for any
  bug fix or behavior change.
- Security/hardening work follows `AUDIT_PLAN.md` (Phases 0-6 done). Ops procedures in `docs/RUNBOOK.md`; every state change is audited to `AUDIT_LOG_FILE`.

## Read next
`PROJECT_HANDBOOK.md` (full reference) · `AUDIT_PLAN.md` · `threat_model.md` ·
`STATE_INVENTORY.md` · `docs/RUNBOOK.md` · `discord-bridge/docs/TEST_BASELINE.md` · `CONTEXT.md`
