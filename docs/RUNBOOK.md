# OTTR Operations Runbook

Operational procedures for running OTTR safely. See `PROJECT_HANDBOOK.md` for
architecture and `AUDIT_PLAN.md` for the security work behind these controls.

## Required configuration (fail-closed)
Set these (same `OTTR_API_KEY` across both services). Startup logs a `CONFIG:`
error for anything missing/placeholder (`security.validate_runtime_config`).

| Var | Purpose |
|---|---|
| `DISCORD_BOT_TOKEN` | Discord bot login |
| `CEO_DISCORD_ID` | the only human allowed to issue directives (required) |
| `OTTR_API_KEY` | shared secret for all state-changing endpoints (required) |
| `DISCORD_TRADING_FLOOR_CHANNEL_ID` / `..._SYSTEM_STATUS_...` | channels |
| `LLM_BASE_URL` / `LLM_MODEL_ID` | LM Studio endpoint + model |

Safety / limits (optional, sensible defaults):
`TRADING_DRY_RUN`, `MAX_TRADE_USD`, `API_RATE_LIMIT`, `CEO_MIN_INTERVAL_SEC`,
`GATEWAY_ALLOWED_ORIGINS`, `PORTFOLIO_STATE_PATH`, `JOURNAL_FILE`, `AUDIT_LOG_FILE`.

## Emergency stop (kill-switch)
To immediately stop the bot from executing or changing anything while keeping it
running/observable:
1. Set `TRADING_DRY_RUN=1` (env) and restart the bridge.
2. `execute_trade` / `update_parameter` / `cancel_orders` become no-ops (logged +
   audited as `tool_blocked reason=dry_run`); read tools still work.
3. To resume, set `TRADING_DRY_RUN=0` and restart.

Tighter cap without a full stop: set `MAX_TRADE_USD` to a low value — any single
order above that notional is blocked (`trade_blocked reason=max_trade_usd`).

## Rotate the API key
1. Generate: `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
2. Update `OTTR_API_KEY` in `.env`, `discord-bridge/.env`, `agent-gateway/.env`,
   and `VITE_OTTR_API_KEY` in `frontend/.env` — all to the SAME value.
3. Restart bridge + gateway; rebuild the frontend (`npm run build`) or set
   `localStorage.ottr_api_key` in the browser.

Rotate the Discord bot token via the Discord developer portal, then update
`DISCORD_BOT_TOKEN` and restart.

## Docker deployment
Compose runs three services: `frontend` (:3000), `agent-gateway` (:8000), and
`discord-bridge` (API :8001, internal-only). Environment is interpolated from the
repo-root `.env`; a single `OTTR_API_KEY` is reused by all three and baked into the
frontend bundle at build time.

Rebuild + restart (data is preserved — it lives in the host bind-mount
`./discord-bridge/data`):
```
docker compose up -d --build        # or:  ./redeploy.ps1
docker compose logs -f discord-bridge
```

- **A rebuild (`--build`) is required** after pulling changes: the frontend bakes
  `VITE_OTTR_API_KEY` at build time and the gateway installs deps from pyproject.
- **Shared state:** the bridge mounts `./discord-bridge/data:/app/data`; the gateway
  mounts the same host dir at `/discord-bridge/data` (it reads `PORTFOLIO_STATE_PATH`
  and `JOURNAL_FILE` from there). One source of truth, survives restarts.
- **LLM on the host:** `LLM_BASE_URL` defaults to `host.docker.internal:1234`
  (LM Studio); `extra_hosts` makes that resolve on Linux too.
- **Never `docker compose down -v`** — `-v` deletes volumes. Plain `down` keeps your
  data (it's a host bind-mount regardless).
- Secrets stay out of images via `.dockerignore` (`.env`, `data/`, caches).
- Tests are NOT run on commit (the pre-commit hook is a no-op); CI is the gate.

A healthy bridge startup shows: a Discord login line, "Webhook ready" for all 7
agents, "Scheduler started — 6 meeting slots", "API Server running on port 8001",
and **no `CONFIG:` errors**.

## Deploy checklist
- [ ] `cd discord-bridge && pytest -k "not live" tests/` green.
- [ ] `cd agent-gateway && pytest tests/` green.
- [ ] `python discord-bridge/run_evals.py` green (LM Studio up).
- [ ] Required config set (no `CONFIG:` errors at startup).
- [ ] `OTTR_API_KEY` is a real random secret (not the placeholder).
- [ ] `GATEWAY_ALLOWED_ORIGINS` limited to your dashboard origin.
- [ ] Decide `TRADING_DRY_RUN` (1 for a dry first run) and `MAX_TRADE_USD`.

## Rollback
- App: `git revert <commit>` (or redeploy the previous image) and restart.
- Portfolio state: `discord-bridge/data/portfolio_state.json` is the single
  source of truth; keep periodic backups. To reset, stop the bridge, restore the
  backup file, restart.

## Audit trail
Every trade, parameter change, order cancel, blocked attempt, and accepted CEO
directive is appended as JSON to `AUDIT_LOG_FILE`
(default `discord-bridge/data/audit_log.jsonl`). Inspect with:
`tail -f discord-bridge/data/audit_log.jsonl`.

## Housekeeping
- Delete the orphan `agent-gateway/portfolio_state.json` (read by nothing).
- Clear stale `discord-bridge/data/portfolio_*.tmp` files if any accumulate.
