# Portfolio / Journal State Inventory

> **Phase 4 update (resolved):** The gateway now reads the portfolio via
> `PORTFOLIO_STATE_PATH` and the journal via `JOURNAL_FILE` (both env-configurable,
> read at call time). `docker-compose.yml` points them at the shared
> `/discord-bridge/data` volume, so the bridge's authoritative file is the single
> source in Docker (no more brittle relative path / empty-portfolio-in-prod).
> The orphan `agent-gateway/portfolio_state.json` should be deleted (it is read by
> nothing). Original analysis below.


Maps every on-disk state file, who reads/writes it, and the risks. Feeds the
Phase 4 reconciliation. Bottom line: it's **not** two writers fighting over one
portfolio — it's one real source, one orphan copy, and fragile cross-service
coupling.

## The three files

| File | Writer(s) | Reader(s) | Status |
|---|---|---|---|
| `discord-bridge/data/portfolio_state.json` | `discord-bridge/bot/portfolio.py` (only) | the bot; the gateway (cross-dir, see below) | **Authoritative.** Fresh. All trades land here. |
| `agent-gateway/portfolio_state.json` | nothing | nothing | **Orphan / dead.** Stale (months old, ~415 B). No gateway code references it. |
| `agent-gateway/trade_journal.json` | `agent-gateway/app/services/journal_manager.py` | same | Live, but path is **CWD-relative** (`os.path.join(os.getcwd(), "trade_journal.json")`). |

## Risks

1. **Cross-service file coupling (real deploy bug).**
   `agent-gateway/app/routers/api.py` (lines ~72 and ~120) reads the bridge's
   portfolio by a hardcoded relative path:
   `../../../discord-bridge/data/portfolio_state.json`.
   This only works when both live in one checkout. `threat_model.md` documents
   them as **separate services** (`discord-bridge:8001`, gateway on :8000) — i.e.
   separate containers. In that deploy the gateway can't see the bridge's local
   filesystem, so `/portfolio/snapshot` silently returns the empty default
   (`cash: 0, holdings: {}`). The dashboard would show an empty portfolio in prod.

2. **Orphan file.** `agent-gateway/portfolio_state.json` is read/written by
   nothing. It's confusing (looks authoritative, isn't) and can mislead debugging.

3. **CWD-relative journal path.** `trade_journal.json` lands wherever the gateway
   process was started, so its location is non-deterministic across run methods
   (direct vs Docker vs systemd).

4. **No contract / no validation.** The gateway parses the bridge's raw JSON
   directly instead of calling a bridge API, so there's no schema, versioning, or
   validation between the two services.

## Recommended Phase 4 reconciliation

- **Single authority:** keep `discord-bridge` as the sole owner of portfolio truth.
- **Serve it over an API:** have the gateway fetch the snapshot from a
  `discord-bridge` HTTP endpoint (it already talks to `discord-bridge:8001` for
  `/api/directive`) instead of reading the file across directories. Removes the
  Docker breakage and adds a real contract.
- **Delete** the orphan `agent-gateway/portfolio_state.json`.
- **Make the journal path configurable** (env var / settings) instead of CWD.
- Add a reconciliation/health check that flags if the gateway's view diverges
  from the bridge's authoritative state.
