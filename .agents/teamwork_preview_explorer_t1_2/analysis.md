# Analysis: HFT Semantic Memory Test Plan & Architecture

This analysis details the current implementation of the meeting memory and engine, explores the required vector database contract, and plans the E2E 4-Tier testing structure.

---

## 1. Findings on Current Codebase Status

After exploring the codebase under `d:\crypto-trading-bot\`, we observed the following state:

### A. Meeting Memory (`discord-bridge/bot/memory.py`)
- **Persistence**: Currently writes a list of meetings, decisions, and a `rolling_summary` string to `data/meeting_log.json`.
- **Concurrency**: Employs an atomic write mechanism using a temporary file in the same directory (`tempfile.mkstemp`), followed by an atomic rename (`os.replace`).
- **Data Pruning**: Trims meetings when they exceed `MAX_FULL_MEETINGS = 5`. Oldest meetings are popped and condensed into a flat `rolling_summary` text block.
- **Missing Features**: Currently does not generate or store vector embeddings, nor does it integrate with a vector database (e.g. ChromaDB). There is no implementation for `query_similar_meetings()`.

### B. Meeting Engine (`discord-bridge/bot/meetings.py`)
- **Orchestration**: Directs the meeting flow using facilitator prompt generation, Round 1 initial takes, a Debate Round, and a closing summary that executes structured trading tags (like `[TRADE: BUY BTC 500]`).
- **Context Injection**: Uses `_build_agent_context()` to construct LLM prompts. However, the `memory_context` argument is currently static/empty or passed directly from downstream code, with no dynamic querying of the vector database before starting the meeting.

### C. Testing Scope (`.agents/teamwork_preview_orchestrator_test/SCOPE.md`)
- Outlines the 4-tier test approach:
  - **Feature 1**: Vector DB Integration (Happy path insertion, extreme values, database locked, etc.)
  - **Feature 2**: Semantic Context Injection (Retrieval of top 3 matches, query scoring, low similarity, empty market data, etc.)
  - **Feature 3**: E2E Integration (Saving and scheduling flows, sequential meetings)
  - **Feature 4**: Real-world Scenarios (Flash Crash, Bull Run, Sideways Chop, Volatility alerts, etc.)
- Defines Milestone T1 (Test Design) which requires design of the test framework and producing `TEST_INFRA.md`.

---

## 2. Proposed Test Design & 4-Tier Test Cases

We have mapped out a comprehensive test suite of 28 test cases. They are structured into 4 tiers to isolate unit capabilities, test boundaries, verify stateful flow integration, and stress the system under realistic HFT scenarios.

### Tier 1: Feature Coverage (10 Test Cases)
Focuses on verifying that happy-path requirements function correctly under normal operating conditions.
- **Vector DB**:
  - `test_vector_db_save_meeting_happy_path`: Saving valid meeting records.
  - `test_vector_db_embedding_generation`: Generating embeddings for summaries.
  - `test_vector_db_query_similar_returns_ordered_results`: Returning results sorted by similarity.
  - `test_vector_db_query_limit_n`: Honoring the limit of returned records.
  - `test_vector_db_persistence_across_instances`: Verifying that saving data persists on disk and is reloadable.
- **Context Injection**:
  - `test_meeting_engine_retrieves_and_injects_context`: Querying vector DB and passing results to prompt.
  - `test_context_injection_formatting`: Ensuring clean template string formatting.
  - `test_context_injection_no_history`: Fallback handling when vector DB is empty.
  - `test_context_injection_with_existing_memory_context`: Bypassing DB query if manual context is provided.
  - `test_agent_response_incorporates_context`: Confirming agents receive history payload.

### Tier 2: Boundary & Edge Cases (10 Test Cases)
Ensures the system does not crash or corrupt state when presented with unexpected or extreme inputs.
- **Vector DB Boundaries**:
  - `test_vector_db_empty_summary`: Saving meeting with empty summary.
  - `test_vector_db_very_long_summary`: Storing exceptionally large summaries (e.g. 50k characters).
  - `test_vector_db_dimension_mismatch`: Graceful error when embedding sizes mismatch.
  - `test_vector_db_file_lock_concurrent_writes`: Handling concurrent writes without corruption.
  - `test_vector_db_read_only_permission_error`: Error handling when DB path is read-only.
- **Context Injection Boundaries**:
  - `test_context_injection_completely_unrelated_query`: Low similarity filtering when query doesn't match history.
  - `test_context_injection_empty_market_data`: Behavior when pricing data is null/empty.
  - `test_context_injection_exact_phrase_matching`: Ensuring exact phrase matches rank first.
  - `test_context_injection_exceeds_token_budget`: Truncation rules when memory details exceed the token budget.
  - `test_context_injection_special_characters_in_query`: Query robustness against special characters / SQL-like injections.

### Tier 3: Cross-Feature Combinations (3 Test Cases)
Validates interactions between saving, scheduling, and querying modules.
- `test_flow_save_then_immediate_query`: Saved meeting instantly searchable.
- `test_flow_multiple_sequential_meetings`: Sequential meetings updating vector DB and rolling JSON summary concurrently.
- `test_flow_concurrent_meeting_and_query`: Concurrent writes and searches without deadlock or file conflicts.

### Tier 4: Real-World HFT Scenarios (5 Test Cases)
Simulates realistic market conditions to verify that contextual memory successfully guides the trading agents' decision-making processes.
- `test_scenario_flash_crash`: Injects historical crash context under sharp drawdown, leading to risk-averse actions.
- `test_scenario_bull_run`: Injects breakout context under surging prices, leading to breakout trading recommendations.
- `test_scenario_sideways_chop`: Injects low-volatility chop context, prompting conservative range trading.
- `test_scenario_high_volatility_alert`: Triggers emergency volatility meetings, asserting proper context injection and subsequent risk mitigation tags (`[CANCEL: ALL]`).
- `test_scenario_funding_rate_squeeze`: Simulates long squeeze warnings under extreme perpetual funding rates.

---

## 3. Reconciled Test Infrastructure (TEST_INFRA.md)
The proposed test framework relies on `pytest` and `pytest-asyncio`. To maintain zero external dependencies and work within the Code-Only Network Mode, all network calls to embedding providers or LLMs will be mocked. Vector databases will target temporary directories created during test fixtures.

Details have been structured into the proposed `TEST_INFRA.md` design file located in this agent's directory.
