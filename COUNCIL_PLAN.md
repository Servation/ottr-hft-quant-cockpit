# COUNCIL_PLAN.md

Rewrite plan: replace the single weak local model with a three-model council
(Claude, GPT via Codex, Gemini via Antigravity). Why: the 4b model's tool calls were
unreliable, so large parts of the system (re-ask loops, raw-tag parsing, chair-prompt
sizing hints, vote regexes) exist only to compensate for it. Strong models can emit
structured JSON reliably, so the LLMs do judgment and deterministic code does everything else.

## Principles

1. **Models analyze, code acts.** Models never call tools. They return JSON that is
   schema-validated. Only the deterministic executor touches `execute_trade`.
2. **Independence before synthesis.** The three analysts see the same market packet and
   do not see each other. The chair sees their outputs anonymized (A/B/C) to avoid
   deference to a brand.
3. **Fail closed.** Fewer than 2 valid analyst responses means no trades. A chair failure
   means no trades. Invalid JSON gets one repair retry, then the response is dropped.
4. **All existing safety invariants stay.** Execution goes through `tools.handle_tool_call`
   so dry-run, `MAX_TRADE_USD`, drawdown halt, concentration clamp, vol-sizing, idempotency
   and audit all apply unchanged. `portfolio.py` stays the single writer.
5. **Votes are computed, not parsed.** The tally and per-asset stance table come from the
   analysts' JSON in code. This deletes `_VOTE_RE`, the chair's tally prompt and PR #44's
   workaround.

## Architecture

```
market packet (code: prices, indicators, vol, F&G, portfolio, watchlist)
        |
        +--> Analyst A (Claude, risk/regime lens)   --\
        +--> Analyst B (GPT,    quant/technical lens) ---+--> JSON x3
        +--> Analyst C (Gemini, sentiment/contrarian) -/
                                   |
                         tally + disagreement map (code)
                                   |
                         Chair (Claude) -> decision JSON
                                   |
                  validate + filter (universe, max trades, quorum) (code)
                                   |
                  executor -> handle_tool_call(execute_trade) (existing gates)
                                   |
                  audit + memory + Discord/dashboard post (existing)
```

## Where it runs (important)

Codex, `agy` and `claude` are host CLIs signed in with the user's accounts. The bridge
runs in Docker and cannot see them. Target design: a small **host-side model gateway**
(`bot/council/` served on the host, bound to localhost, `OTTR_API_KEY` protected) that the
container calls via `host.docker.internal`. The container keeps owning the portfolio.
Phase 1 (this prototype) runs the council on the host in **propose-only** mode and never
writes portfolio state.

## Phases

- **C0 (this PR):** `bot/council/` package: model adapters (subprocess, timeout, JSON
  extraction, schema validation, repair retry), schemas, tally, council runner, a
  `run_council.py` script that prints a proposed decision from live market data. No
  execution, no state writes. Tests use fake adapters.
- **C1:** host gateway + container client; executor wired to `handle_tool_call` behind a
  `council.enabled` flag (default off), with `TRADING_DRY_RUN` honored.
- **C2:** replace the persona meetings (`meetings.py`, 982 lines) with council meetings
  per type (strategy, risk review, etc.); keep Discord posting and memory.
- **C3:** delete compensation code that is no longer needed (raw-tag parsing, re-ask loop,
  vote regexes, 4b-specific prompt text). Keep the idempotency guard.
- **C4:** per-model scorecard (reuse `knowledge_graph.reputation_graph`) so each model's
  calls are tracked and the chair can weight by track record.

## Open risks

- Latency: CLIs take roughly 8-12 s per call in a trivial probe; a real packet will be
  slower. Analysts run in parallel, so a meeting is about analyst time + chair time.
- CLI behavior can change between versions; adapters isolate this and each has a test
  seam. Output is always validated, never trusted.
- Subscription rate limits: a 15-minute cadence across three providers needs checking.
- Cost of a bad chair call is bounded by the deterministic caps, not by the model.
