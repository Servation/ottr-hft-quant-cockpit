---
name: redeploy-ottr
description: >-
  Redeploy the OTTR Docker stack (discord-bridge + agent-gateway + frontend) and run
  the post-deploy health verification. Use this whenever the user wants to redeploy,
  rebuild, "sync the deploy", ship merged PRs to the live container, restart the stack,
  or run redeploy.ps1 in THIS repo, and also whenever you need to confirm a deploy is
  healthy (gateway health, bridge "fully operational", embeddings working). Prefer this
  over invoking redeploy.ps1 directly, because that script ends in a blocking
  `docker compose logs -f` tail that hangs an unattended/agent run.
---

# Redeploy OTTR + verify

Rebuild and recreate the three-service stack, then prove it came up healthy. This is
the agent-safe version of `redeploy.ps1`: it runs the same `down -> build -> up -d`
but drops the trailing `logs -f` (which follows forever and would block), and adds a
4-point health check with a PASS/FAIL summary.

## When to use
Redeploying, "syncing" main to the live container after merges, restarting the stack,
or just verifying the running deploy is healthy. For the broader ops context (rollback,
key rotation, the kill-switch) see `docs/RUNBOOK.md`.

## Why a rebuild is always required
The frontend bakes `VITE_OTTR_API_KEY` at **build** time and the gateway installs deps
at build time, so a plain restart would ship stale code/config. The script therefore
always `build`s. Data lives in `./discord-bridge/data` (host bind-mount) and is never
touched: the script uses plain `down`, **never `down -v`** (which deletes volumes).

## How to run it
The build takes minutes, so run the bundled script **in the background** and wait for
the completion notification, then read its summary. From the repo root:

```
pwsh .claude/skills/redeploy-ottr/scripts/redeploy-and-verify.ps1
```

- `-VerifyOnly` runs just the 4 checks against the already-running stack (no rebuild).
- `-TimeoutSec <n>` extends how long each check polls (default 120s).

The script resolves the repo root itself, so the working directory does not matter.

## Before deploying
Confirm local `main` is synced with origin so the build includes what you expect:

```
git fetch origin --quiet; git rev-list --left-right --count origin/main...HEAD
```

`0 0` means in sync. (The build uses the working tree, so any merged PR must be pulled
into local `main` first.)

## What the 4 checks mean
1. **gateway_health** — `GET http://localhost:8000/api/v1/health` returns
   `{"status":"OK"}`. The FastAPI gateway is up.
2. **bridge_operational** — discord-bridge logs contain `fully operational` (Discord
   login + scheduler + AlertMonitor started). Polled, since startup takes ~10-20s.
3. **embeddings** — runs `bot.embeddings.embed()` inside the bridge container; a 768-dim
   vector means the local LM Studio embeddings endpoint (`host.docker.internal:1234`)
   is reachable. This is the check most likely to fail, because it depends on LM Studio
   running on the host.
4. **embedding_index** (informational) — current size of `data/embeddings_index.json`;
   it is capped at 50 (PR #12), so a count at or below 50 is expected.

Exit code is 0 only when checks 1-3 pass.

## Troubleshooting
- **embeddings FAIL** — LM Studio is almost certainly not running (or no embedding model
  loaded) on the host at port 1234. Start it; the bridge degrades to "no semantic
  context" meanwhile but everything else works.
- **bridge_operational FAIL** — inspect `docker compose logs --tail=80 discord-bridge`.
  A `CONFIG:` error means a missing/placeholder env var (e.g. `CEO_DISCORD_ID`); the
  bridge fails fast on bad config by design.
- **gateway_health FAIL** — `docker compose ps` to confirm the container is up, then
  `docker compose logs --tail=80 agent-gateway`.

## After a successful deploy
Update the **Deploy state** line in `CLAUDE.md`'s Active-context to record what is now
live (which PRs / commit the running container reflects), so the next session knows the
live state without re-deriving it.
