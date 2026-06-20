# Test & Eval Baseline (Phase 0)

Snapshot of the regression gate at the start of the audit. Re-run on your own
machine (Python 3.12, network up, LM Studio running) for the authoritative
numbers — several failures below are sandbox-only (no network / no LLM).

## How to run the gate
```
cd discord-bridge
pip install -r requirements.txt -r requirements-dev.txt
python run_evals.py            # eval suite (auto-runs LLM evals if backend is up)
pytest -k "not live" tests/    # unit tests (excludes network/LLM "live" tests)
```
CI runs the same in `.github/workflows/ci.yml`: the eval suite is the **blocking
gate**; unit tests are reported (non-blocking) until the failures below are cleared.

## Real blockers found and fixed in Phase 0
- **Missing dev dependency:** the suite uses the `mocker` fixture (`pytest-mock`)
  but it wasn't in `requirements-dev.txt`. Added.
- **Python version lock:** `bot/ceo_handler.py` had a backslash inside an f-string
  expression — valid only on 3.12+, a hard `SyntaxError` on 3.10/3.11. Hoisted the
  `"\n".join(...)` into a variable; the package now imports on 3.10–3.12.

## Status: GREEN
`pytest -k "not live"` -> **97 passed, 0 failed** (2 `live` tests need network/LLM
and are excluded). CI enforces both this suite and `run_evals.py` as blocking gates.

### Root causes that were fixed (all were stale tests / outdated expectations,
### not new bugs introduced by the audit)
- **ceo_handler routing (8):** `bot/__init__.py` loads `.env` on import, so the
  real `CEO_DISCORD_ID` rejected the mock author and `on_message` returned before
  routing. Tests now pin an authorized CEO identity (robust to Phase 1 auth work).
- **scheduler `run_meeting` (4):** `_execute_meeting` gained an `agent_llm.check_health()`
  gate; tests didn't mock it, so a real (failing) LLM ping aborted the meeting.
  Added an autouse "LLM online" fixture; added a test for the deliberate
  market-data abort path.
- **scheduler lifecycle (1):** `start()` now registers a 7th `evaluate_predictions`
  job. Assertion made precise (6 meeting crons + 1 background job).
- **meeting rotation (1):** `ROTATION_ORDER` changed to
  `[strategy_session, trade_execution]`; test now asserts against `ROTATION_ORDER`
  (mechanics, not hardcoded names).
- **price_feed (2):** `get_prices` now *raises* on total failure (safer than fake
  $0.00 prices) and `_fetch_derivatives` switched to the OKX API shape. Tests
  updated; added a stale-cache fallback test.

## Housekeeping note
`bot/meetings.py` is committed-vs-working different (CRLF line endings + ~38 lines
of uncommitted content changes that predate this audit). Consider a `.gitattributes`
to normalize line endings so diffs stay readable.
