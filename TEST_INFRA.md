# OTTR HFT Semantic Memory Test Infrastructure (TEST_INFRA.md)

This document defines the E2E test infrastructure, mocking architecture, and the 4-Tier test suite specification for the HFT Semantic Memory component of the OTTR Crypto Trading Bot.

---

## 1. Test Philosophy & Goals
- **Opaque-Box Verification**: Test the public contracts (`MeetingMemory.save_meeting`, `MeetingMemory.query_similar_meetings`, and `MeetingEngine.run_meeting`) without depending on the internal database choice or embedding model particulars.
- **Hermetic Executions**: Tests must run completely offline (satisfying the Code-Only Network Mode constraint) and must not modify production databases or files.
- **Deterministic Mocking**: Mocks will simulate LLM response latencies, embedding values, and pricing feeds.

---

## 2. Test Architecture & Environment

### Framework
- **Test Runner**: `pytest` and `pytest-asyncio` for async test orchestration.
- **File Layout**: Tests are co-located in `discord-bridge/test_semantic_memory.py`.
- **Temp Directories**: Pytest's `tmp_path` fixture is used to redirect vector database files and JSON logs to isolated paths for each test, ensuring zero cross-test contamination.

### Mocking Strategy
1. **Embedding Generator Mock**:
   - Intercepts calls to the embedding provider (e.g. OpenAI embeddings endpoint or local model call).
   - Generates deterministic mock vectors based on text hashing or lookup tables, ensuring consistent cosine similarities for testing.
2. **Agent LLM Mock**:
   - Mocks `agent_llm.generate_response` to return pre-configured responses, dynamic summaries, or structural tag blocks (`[TRADE: ...]`, `[PARAM: ...]`).
3. **Discord / Message Poster Callback Mock**:
   - A mock async callback `post_message_fn(agent_id, content)` to capture all message outputs and permit assertions on what was communicated during meetings.
4. **Price Feed Mock**:
   - A mock `PriceFeed` that returns static or dynamically fluctuating price frames (e.g., flash crash series) to drive the meetings under test.

---

## 3. The 4-Tier Test Suite Specification

### Tier 1: Feature Coverage (10 Test Cases)

#### Feature A: Vector Database Integration (Saving/Querying/Persistence)

1. **`test_vector_db_save_meeting_happy_path`**
   - **Objective**: Verify that a valid meeting record is successfully stored in the vector database and is persistent.
   - **Setup**: Instantiate `MeetingMemory` with temporary db path.
   - **Action**: Call `save_meeting(meeting_record)` with a standard meeting dictionary.
   - **Assertions**: Verify that the record exists in the database and metadata is fully populated.

2. **`test_vector_db_embedding_generation`**
   - **Objective**: Validate that embedding generation is called with the correct text combination (summary + decisions) and format.
   - **Setup**: Mock embedding generation API.
   - **Action**: Call `save_meeting(meeting_record)`.
   - **Assertions**: Verify the mock is called exactly once with the concatenated text and the returned vector is stored.

3. **`test_vector_db_query_similar_returns_ordered_results`**
   - **Objective**: Ensure that querying the vector database returns results sorted by similarity score in descending order.
   - **Setup**: Insert three meetings. Configure the embedding mock to return vectors producing similarities: $S_A = 0.9$, $S_B = 0.7$, $S_C = 0.4$ relative to the query.
   - **Action**: Call `query_similar_meetings(query_text, n=3)`.
   - **Assertions**: Verify the returned list is ordered: `[Meeting A, Meeting B, Meeting C]`.

4. **`test_vector_db_query_limit_n`**
   - **Objective**: Validate that `query_similar_meetings` honors the `n` parameter and returns at most `n` records.
   - **Setup**: Insert 5 distinct meetings.
   - **Action**: Call `query_similar_meetings(query_text, n=3)`.
   - **Assertions**: Assert that the length of the returned list is exactly 3.

5. **`test_vector_db_persistence_across_instances`**
   - **Objective**: Ensure that meeting records and vector embeddings persist on disk and can be reloaded by a new instance of `MeetingMemory`.
   - **Setup**: Save a meeting in a temporary directory. Re-instantiate `MeetingMemory` targeting that same directory.
   - **Action**: Query the database using the new instance.
   - **Assertions**: Verify the previously saved meeting is returned successfully.

#### Feature B: Semantic Context Injection

6. **`test_meeting_engine_retrieves_and_injects_context`**
   - **Objective**: Verify that when `run_meeting` is executed, it queries the vector DB and passes the top 3 results as `memory_context`.
   - **Setup**: Pre-populate the vector database with 3 historical meetings. Mock `agent_llm.generate_response` to capture input prompts.
   - **Action**: Execute `MeetingEngine.run_meeting("strategy_session", mock_post_fn, price_data="BTC 60000")`.
   - **Assertions**: Verify that the captured prompt contains the formatted summaries of the 3 historical meetings.

7. **`test_context_injection_formatting`**
   - **Objective**: Ensure that retrieved historical meetings are formatted into the LLM context prompt according to a clean, readable template.
   - **Setup**: Mock vector DB to return 2 historical meetings with known details.
   - **Action**: Trigger agent context building.
   - **Assertions**: Verify the prompt includes exactly: `• [timestamp] type — summary` for each meeting.

8. **`test_context_injection_no_history`**
   - **Objective**: Verify system behavior when there is no historical data in the vector DB.
   - **Setup**: Keep the vector database completely empty.
   - **Action**: Run a meeting cycle.
   - **Assertions**: Verify the injected context contains the fallback string `"No prior meetings on record."` and no exceptions are raised.

9. **`test_context_injection_with_existing_memory_context`**
   - **Objective**: Verify that if `memory_context` is explicitly passed to `run_meeting`, the meeting engine respects and uses it directly instead of querying the vector database.
   - **Setup**: Mock `query_similar_meetings` to raise an error if called.
   - **Action**: Run a meeting passing a non-empty `memory_context` string.
   - **Assertions**: Verify the meeting runs successfully using the passed context, and that the database query was bypassed.

10. **`test_agent_response_incorporates_context`**
    - **Objective**: Verify that agents receive the context messages in their payload and that their generated responses are influenced by it.
    - **Setup**: Pre-inject a historical meeting where a specific parameter limit was suggested.
    - **Action**: Execute agent turn response generation.
    - **Assertions**: Verify that the payload contains the historical context, and the mock response is generated using that history.

---

## 2. Boundary / Edge Cases (10 Test Cases)

#### Feature A: Vector Database Integration (Saving/Querying/Persistence)

11. **`test_vector_db_empty_summary`**
    - **Objective**: Verify database behavior when saving a meeting with an empty summary or empty fields.
    - **Action**: Save a meeting record where `summary = ""`.
    - **Assertions**: Verify that the database stores the record, and querying for it handles empty summary without errors (or uses fallback text).

12. **`test_vector_db_very_long_summary`**
    - **Objective**: Test database performance and correctness when saving an exceptionally long summary.
    - **Action**: Save a meeting record with a 50,000 character summary.
    - **Assertions**: Verify that no buffer overflow, truncation, or DB syntax errors occur, and retrieval returns the complete string.

13. **`test_vector_db_dimension_mismatch`**
    - **Objective**: Ensure the database handles dimension mismatches gracefully (e.g. if the embedding provider changes dimensions).
    - **Setup**: Seed DB with 128-dim vectors. Mock embedding generator to return a 256-dim vector for the query.
    - **Action**: Call `query_similar_meetings`.
    - **Assertions**: Verify that a clean, descriptive exception (e.g., `DimensionMismatchError`) is raised and logged rather than a silent crash.

14. **`test_vector_db_file_lock_concurrent_writes`**
    - **Objective**: Test database integrity and safety when multiple concurrent processes write simultaneously.
    - **Action**: Run concurrent calls to `save_meeting` using `asyncio.gather` or multiple threads.
    - **Assertions**: Verify all meetings are stored correctly without file corruption, database locks, or lost updates.

15. **`test_vector_db_read_only_permission_error`**
    - **Objective**: Validate system behavior when the vector DB directory/file lacks write permissions.
    - **Setup**: Set temporary folder permissions to read-only (or mock OS operations to throw `PermissionError`).
    - **Action**: Call `save_meeting`.
    - **Assertions**: Verify that a clear `IOError` or permission exception is raised and logged, and the main thread is notified.

#### Feature B: Semantic Context Injection

16. **`test_context_injection_completely_unrelated_query`**
    - **Objective**: Verify that querying with a completely unrelated concept returns results with low similarity scores.
    - **Setup**: Seed DB with HFT crash records. Query with `"banana pancakes"`.
    - **Action**: Call `query_similar_meetings`.
    - **Assertions**: Verify similarity scores are below a threshold (e.g., $< 0.3$) and that the system functions correctly.

17. **`test_context_injection_empty_market_data`**
    - **Objective**: Ensure that if `price_data` is empty/null, the system doesn't query the vector DB or queries using a default query string.
    - **Action**: Call `MeetingEngine.run_meeting` with `price_data = ""`.
    - **Assertions**: Verify that `query_similar_meetings` is either skipped or executed with a default query, without throwing errors.

18. **`test_context_injection_exact_phrase_matching`**
    - **Objective**: Check if a query containing an exact phrase from a previous meeting prioritizes that specific meeting with high similarity.
    - **Setup**: Insert meetings containing distinct phrases (e.g., "May 2021 leverage squeeze"). Query using that exact phrase.
    - **Assertions**: Verify that the corresponding meeting is returned at index 0 (rank 1).

19. **`test_context_injection_exceeds_token_budget`**
    - **Objective**: Ensure that if the formatted history exceeds the `meeting_history` token budget (e.g., 500 tokens), it is truncated or summarized.
    - **Setup**: Insert 3 extremely long summaries. Set the `token_budgets.meeting_history` configuration to a low value.
    - **Action**: Trigger meeting execution.
    - **Assertions**: Verify that the injected context is truncated at a safe boundary or only the top 1 is returned to fit the budget.

20. **`test_context_injection_special_characters_in_query`**
    - **Objective**: Verify that special/Unicode characters or injection attempts in the query do not break the database or vector search.
    - **Action**: Call `query_similar_meetings` with `SELECT * FROM memory; \u0000 \x00` or emoji.
    - **Assertions**: Verify query executes successfully without errors and returns the closest semantic matches.

---

## 3. Cross-Feature Combinations (3 Test Cases)

21. **`test_flow_save_then_immediate_query`**
    - **Objective**: Verify that a saved meeting is instantly searchable and retrievable via semantic query in the next cycle.
    - **Action**: Save meeting A -> Query similar to A.
    - **Assertions**: Verify that the query returns meeting A as the most similar result.

22. **`test_flow_multiple_sequential_meetings`**
    - **Objective**: Test that a series of consecutive meetings dynamically update the vector database and the rolling summary, and query results adapt.
    - **Action**: Run 6 meetings in a row.
    - **Assertions**: Verify that older meetings (beyond 5) are correctly transferred to the rolling summary in the JSON file, while all 6 remain indexed in the vector database and queryable.

23. **`test_flow_concurrent_meeting_and_query`**
    - **Objective**: Validate that the system handles a meeting being saved at the same time a query is executed.
    - **Action**: Call `save_meeting` and `query_similar_meetings` concurrently using asyncio tasks.
    - **Assertions**: Verify that neither call blocks indefinitely or throws database locks; the query returns consistent results.

---

## 4. Real-World Workloads/Scenarios (5 Test Cases)

24. **`test_scenario_flash_crash`**
    - **Objective**: Simulate a Flash Crash scenario (rapid price drop) and verify that the context injected reflects historical market crashes (e.g. May 2021 crash) and guides risk-averse behavior.
    - **Setup**: Seed DB with a meeting summarizing a past flash crash where the risk auditor recommended reducing leverage.
    - **Action**: Trigger a meeting with `price_data = "BTC drops 15% in 10 minutes"`.
    - **Assertions**: Verify the injected context contains the flash crash history and that the risk auditor's simulated response cites the historical crash.

25. **`test_scenario_bull_run`**
    - **Objective**: Simulate a Bull Run scenario (breakout, rising volume) and verify that the injected context contains past breakout phases and helps the altcoin screener identify narratives.
    - **Setup**: Seed DB with previous bull run meetings.
    - **Action**: Trigger a meeting with `price_data = "BTC breakouts through all-time high, volume up 3x"`.
    - **Assertions**: Verify the injected context matches bull run characteristics and that the screener suggests breakout plays.

26. **`test_scenario_sideways_chop`**
    - **Objective**: Simulate a Sideways Chop scenario (low volatility, range-bound) and verify that the injected context prompts conservative range-trading or capital preservation.
    - **Setup**: Seed DB with chop-market meetings.
    - **Action**: Trigger a meeting with `price_data = "BTC trades in $100 range, volatility drops to 10%"`.
    - **Assertions**: Verify the injected context displays sideways trading logs, and the trader recommends limit orders.

27. **`test_scenario_high_volatility_alert`**
    - **Objective**: Simulate a high volatility alert triggering an emergency meeting. Validate that the emergency meeting queries relevant historical high-volatility events and executes proper risk containment.
    - **Setup**: Seed DB with past high-volatility events.
    - **Action**: Trigger an emergency meeting type.
    - **Assertions**: Verify the injected context contains historical volatility events, and the facilitator closing message contains `[CANCEL: ALL BTC]` or `[PARAM: min_trade_usd=100]` due to high volatility risk.

28. **`test_scenario_funding_rate_squeeze`**
    - **Objective**: Simulate extreme funding rates (long squeeze scenario) and verify that the injected context alerts the team to unwind longs before a leverage flush.
    - **Setup**: Seed DB with past long squeeze events.
    - **Action**: Trigger a meeting with `price_data = "BTC perp funding rate is +0.1% (extremely high)"`.
    - **Assertions**: Verify the injected context contains past long squeeze warnings, and the portfolio manager recommends selling altcoins or hedging.

---

## 5. How to Run & Verify the Test Suite
Once the test suite is implemented in `discord-bridge/test_semantic_memory.py`, it can be verified with:

```bash
# From the project root or discord-bridge folder
pytest discord-bridge/test_semantic_memory.py -v
```
