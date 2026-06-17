# OTTR HFT Semantic Memory: 4-Tier E2E Test Suite Analysis & Design

## 1. Codebase Exploration & Architecture Analysis

Based on our exploration of the codebase at `d:\crypto-trading-bot\`, focusing on `discord-bridge/bot/memory.py`, `discord-bridge/bot/meetings.py`, `PROJECT.md`, and the testing scope in `.agents/teamwork_preview_orchestrator_test/SCOPE.md`, we have analyzed the target implementation architecture.

### 1.1 `bot/memory.py` Analysis
- **Current Behavior**:
  - The `MeetingMemory` class manages raw meeting JSON logs in `data/meeting_log.json`.
  - It maintains a list of the last 5 full meetings (`MAX_FULL_MEETINGS = 5`).
  - When the count exceeds 5, the oldest meeting is popped and condensed into `self._rolling_summary` as a plain text string.
  - The chronological context is loaded via `get_recent_context(n=3)` which formatted the last `n` meetings.
- **Proposed Vector Memory Integration**:
  - A local SQLite database (`SQLiteVectorStore`) will be implemented using `sqlite3` to store vector embeddings and metadata. E.g.:
    - Table: `meeting_vectors`
    - Columns: `id` (TEXT PRIMARY KEY), `vector` (TEXT/JSON float list), `metadata` (TEXT/JSON dict), `timestamp` (TEXT).
  - Cosine similarity will be calculated in pure Python to find matching meetings:
    $$\text{Similarity}(A, B) = \frac{A \cdot B}{\|A\| \|B\|}$$
  - A `SemanticMeetingMemory` coordinator class will orchestrate indexing of raw meeting records (generating embeddings of type, summary, decisions, and actions) and retrieval (generating query embedding and executing SQLite similarity search).

### 1.2 `bot/meetings.py` Analysis
- **Current Behavior**:
  - `MeetingEngine.run_meeting()` orchestrates meetings. It accepts a `memory_context: str = ""` parameter.
  - In Round 1 and Round 2 (debate), the engine calls `_build_agent_context(..., memory_context, ...)` to inject chronological history:
    ```python
    if memory_context:
        user_content_parts.append(f"### Recent Meeting History\n{memory_context}")
    ```
  - The facilitator closing prompt builder `_build_closing` currently has **no** historical context.
- **Proposed Context Injection**:
  - Before a meeting starts, the scheduler/engine embeds the current market conditions and CEO directives as a query string:
    ```python
    query_text = f"Meeting Type: {mt.name}. Focus: {mt.focus}. CEO Directives: {ceo_directives}"
    ```
  - Retrieves the top 3 semantically relevant meetings via `SemanticMeetingMemory.get_semantic_context(query_text, limit=3)`.
  - Injects this under `### Relevant Historical Context (Semantic Search)` in `_build_agent_context` (replacing the chronological sliding window).
  - Injects this under `### Relevant Prior Decisions & Actions` in `_build_closing` to prevent the facilitator from repeating decisions.

### 1.3 `SCOPE.md` Alignment
- The testing scope outlines an opaque-box, requirement-driven philosophy.
- It defines the tiers:
  - **Tier 1: Feature Coverage** (Vector DB Integration & Semantic Context Injection).
  - **Tier 2: Boundary/Edge Cases** (Extreme values, empty inputs, dimensions mismatch, DB locks, etc.).
  - **Tier 3: Cross-Feature Combinations** (Pairwise combination of database insertion and meeting scheduling).
  - **Tier 4: Real-World Scenarios** (Flash Crash, Bull Run, Sideways Chop, volatile markets).

---

## 2. Test Infrastructure Design (`TEST_INFRA.md` Reconciliation)

The proposed E2E testing framework is structured to run under local, offline (`CODE_ONLY`) constraints. Since external APIs are unavailable, the test infrastructure will rely on standard library features and a mock embedding provider.

### 2.1 Test Framework & Directory Layout
- **Test Runner**: Python standard `unittest` or `pytest`.
- **Test File Location**: `discord-bridge/test_semantic_memory.py` (as specified in `PROJECT.md`).
- **Data Isolation**: During test execution, database operations must target a temporary SQLite database (e.g. `:memory:` or `data/test_meeting_vectors.db`) and a mock JSON log file (`data/test_meeting_log.json`) to avoid corrupting production data.

### 2.2 Mocking Strategy
1. **Mock Embedding Service**:
   - Instead of calling the live LM-Studio `/v1/embeddings` endpoint which requires a running LLM server, the test runner will inject a `MockEmbeddingService`.
   - The mock service generates deterministic 768-dimensional vectors based on hash values of the input text (or pre-defined mapping tables for testing scenarios).
2. **Mock Discord API / Webhooks**:
   - `post_message_fn` will be mocked using an async function that captures all posts in an in-memory list for assertion.
3. **Mock Price Feed & Portfolio**:
   - Isolated paper portfolio state will be loaded from a test JSON configuration.

---

## 3. 4-Tier E2E Test Suite (33 Test Cases)

Below is the detailed design of our E2E test cases, meeting and exceeding the 27-case requirement.

### Tier 1: Feature Coverage (12 Test Cases)

#### Feature 1: Vector Database Integration
1. **T1_F1_01: SQLite Database Initialization & Schema Creation**
   - *Objective*: Verify that `SQLiteVectorStore` creates the `meeting_vectors` table with the correct schema if the DB file does not exist.
   - *Inputs*: Non-existent file path `data/test_temp.db`.
   - *Expected Result*: File is created and table `meeting_vectors` has columns `id`, `vector`, `metadata`, `timestamp`.
   - *Verification*: Connect directly via `sqlite3` and inspect the table schema.

2. **T1_F1_02: Document Insertion**
   - *Objective*: Verify that `add_document` inserts a vector and metadata.
   - *Inputs*: ID `uuid-1`, vector `[0.1]*768`, metadata `{"type": "risk_review", "summary": "Test summary"}`.
   - *Expected Result*: A single record is saved to the SQLite table.
   - *Verification*: Direct SQL select asserts row count is 1.

3. **T1_F1_03: Document Upsert / Overwrite**
   - *Objective*: Verify that inserting a document with an existing `doc_id` overwrites the existing record.
   - *Inputs*: ID `uuid-1`, initial vector `[0.1]*768`, new vector `[0.9]*768`.
   - *Expected Result*: Database contains only 1 record with the new vector.
   - *Verification*: Direct SQL select verifies updated vector.

4. **T1_F1_04: Cosine Similarity Exact Match**
   - *Objective*: Verify that querying with the exact vector of an inserted document returns a score of 1.0.
   - *Inputs*: Insert `doc-1` with vector `[0.5]*768`. Query with `[0.5]*768`.
   - *Expected Result*: Returns `doc-1` with similarity score close to `1.0`.
   - *Verification*: Assert `results[0]["score"]` is equal to `1.0` (with delta of 1e-6).

5. **T1_F1_05: Cosine Similarity Orthogonal Search**
   - *Objective*: Verify that searching with orthogonal vectors returns a similarity score of 0.0.
   - *Inputs*: Insert `doc-1` with vector `[1.0, 0.0, ...]`. Query with `[0.0, 1.0, ...]`.
   - *Expected Result*: Score is `0.0`.
   - *Verification*: Assert `results[0]["score"]` is `0.0` (with delta of 1e-6).

6. **T1_F1_06: Vector DB Persistence Across Instances**
   - *Objective*: Verify that database records persist after the `SQLiteVectorStore` instance is reloaded.
   - *Inputs*: Insert record, close connection/delete instance, instantiate new `SQLiteVectorStore` pointing to the same file.
   - *Expected Result*: Querying the new instance returns the inserted record.
   - *Verification*: Call `search` on new instance and verify count.

#### Feature 2: Semantic Context Injection
7. **T1_F2_01: Mock Embedding Generation**
   - *Objective*: Verify that the mock embedding provider returns consistent float lists of dimension 768.
   - *Inputs*: String "Flash Crash".
   - *Expected Result*: A 768-dimensional float list.
   - *Verification*: Assert type is `list`, length is 768, values are floats.

8. **T1_F2_02: Semantic Rank and Retrieve Top N**
   - *Objective*: Verify that search returns records sorted by similarity descending, limited by `n`.
   - *Inputs*: Insert 5 records. Search with limit `n=3`.
   - *Expected Result*: Returns exactly 3 records, highest score first.
   - *Verification*: Assert `len(results) == 3` and `results[0]["score"] >= results[1]["score"]`.

9. **T1_F2_03: Meeting Indexing Flow**
   - *Objective*: Verify that `SemanticMeetingMemory.index_meeting` saves raw JSON and embeds/indexes it.
   - *Inputs*: Meeting record dictionary.
   - *Expected Result*: Record exists in `meeting_log.json` and in `meeting_vectors` SQLite table.
   - *Verification*: Read JSON log file and query SQLite table to confirm presence.

10: **T1_F2_04: Semantic Context Formatting**
    - *Objective*: Verify that `get_semantic_context` returns a properly structured string containing required fields.
    - *Inputs*: Database containing 2 records.
    - *Expected Result*: Formatted string contains date, meeting type, similarity score, summary, decisions, and actions.
    - *Verification*: Parse result string for key sections.

11. **T1_F2_05: Agent Context Prompt Injection**
    - *Objective*: Verify that `_build_agent_context` injects semantic context under the correct header.
    - *Inputs*: Mock `memory_context` string.
    - *Expected Result*: Output prompt contains `### Relevant Historical Context (Semantic Search)` followed by the mock text.
    - *Verification*: Perform substring assertion on the prompt dictionary.

12. **T1_F2_06: Facilitator Closing Prompt Injection**
    - *Objective*: Verify that `_build_closing` injects semantic context under `### Relevant Prior Decisions & Actions`.
    - *Inputs*: Mock `memory_context` string.
    - *Expected Result*: Output closing prompt contains the header and mock text.
    - *Verification*: Perform substring assertion on the closing prompt string.

---

### Tier 2: Boundary/Edge Cases (12 Test Cases)

#### Feature 1: Vector Database Edge Cases
13. **T2_F1_01: Zero Vector Input**
    - *Objective*: Verify that similarity calculation does not raise a division by zero error when a vector has zero magnitude.
    - *Inputs*: Vector `[0.0]*768`.
    - *Expected Result*: Cosine similarity returns `0.0` gracefully.
    - *Verification*: Pass zero vector to similarity function and assert return value is `0.0`.

14. **T2_F1_02: Dimension Mismatch Handling**
    - *Objective*: Verify that querying/inserting vectors of wrong size is rejected.
    - *Inputs*: Vector of size 1536.
    - *Expected Result*: Raises `ValueError` indicating dimension mismatch.
    - *Verification*: Wrap call in `assertRaises(ValueError)`.

15. **T2_F1_03: Empty Metadata and Raw Text**
    - *Objective*: Verify that inserting records with empty fields is handled gracefully.
    - *Inputs*: Metadata `{}`, raw text `""`.
    - *Expected Result*: Record is stored and retrieved successfully.
    - *Verification*: Assert retrieved metadata is empty dict.

16. **T2_F1_04: Database Connection Locking**
    - *Objective*: Verify that SQLite handles concurrent reads and writes using timeouts without deadlocking.
    - *Inputs*: Lock DB in one connection, attempt write in another.
    - *Expected Result*: SQLite waits/retries and does not fail immediately.
    - *Verification*: Set database timeout and verify successful completion of operations.

17. **T2_F1_05: Special Characters and SQL Injection in Metadata**
    - *Objective*: Verify that characters like quotes or semicolons do not corrupt the DB or cause injection.
    - *Inputs*: Metadata containing `' OR '1'='1; --` and emojis.
    - *Expected Result*: Record is safely inserted and read back.
    - *Verification*: Assert retrieved metadata matches inputs exactly.

18. **T2_F1_06: Database Path Permission Denied**
    - *Objective*: Verify that DB failures due to permissions throw a descriptive exception.
    - *Inputs*: Read-only path (e.g. `C:/Windows/System32/test.db`).
    - *Expected Result*: Raises `sqlite3.OperationalError` or `PermissionError`.
    - *Verification*: Wrap initialization in `assertRaises`.

#### Feature 2: Semantic Context Injection Edge Cases
19. **T2_F2_01: Empty Query Text**
    - *Objective*: Verify that querying with an empty string returns a default empty message.
    - *Inputs*: Query string `""`.
    - *Expected Result*: Returns `"No prior relevant meetings found."`.
    - *Verification*: Assert output matches the default empty text.

20. **T2_F2_02: Very Long Meeting Summaries (Truncation)**
    - *Objective*: Verify that extremely long summaries are truncated or budgeted to fit token limits.
    - *Inputs*: Summary containing 50,000 characters.
    - *Expected Result*: Retrieves without crashing; text is truncated or split.
    - *Verification*: Verify length of prompt context is within 500 token budget.

21. **T2_F2_03: Completely Unrelated Query Concept (Low Scoring)**
    - *Objective*: Verify that unrelated queries score very low.
    - *Inputs*: DB contains crypto strategy logs. Query: "best chocolate chip cookie recipe".
    - *Expected Result*: Cosine similarity score is near `0.0`.
    - *Verification*: Assert returned score is below `0.2`.

22. **T2_F2_04: Extreme Relevance Scores**
    - *Objective*: Verify similarity scores remain strictly in range `[-1.0, 1.0]`.
    - *Inputs*: Vectors with elements like `1e6` or `-1e6`.
    - *Expected Result*: Cosine similarity is bound between -1.0 and 1.0.
    - *Verification*: Assert `score >= -1.0` and `score <= 1.0`.

23. **T2_F2_05: Large Limit Parameter (n > DB Size)**
    - *Objective*: Verify that asking for more results than records in DB does not crash.
    - *Inputs*: DB has 2 records, query with limit `n=10`.
    - *Expected Result*: Returns exactly 2 records.
    - *Verification*: Assert `len(results) == 2`.

24. **T2_F2_06: Handling Embedding Service Failures**
    - *Objective*: Verify that if the embedding service raises an HTTP error, the system falls back gracefully.
    - *Inputs*: Inject failure in `EmbeddingService.get_embedding`.
    - *Expected Result*: Meeting continues; falls back to chronological list.
    - *Verification*: Assert meeting engine runs and outputs fallback warning.

---

### Tier 3: Cross-Feature Combinations (4 Test Cases)

25. **T3_01: Immediate Write-then-Query Consistency**
    - *Objective*: Verify that an indexed meeting is immediately available for semantic search queries.
    - *Inputs*: Index a new meeting with summary "De-risking portfolio due to regulatory concerns". Immediately search for "regulatory de-risk".
    - *Expected Result*: The newly indexed meeting is returned as the top result.
    - *Verification*: Assert returned result matches the indexed meeting's ID.

26. **T3_02: Multiple Sequential Meetings Indexing**
    - *Objective*: Verify that indexing 10 meetings sequentially correctly updates database states and maintains ranking integrity.
    - *Inputs*: Index 10 distinct meetings (Strategy, Briefing, Retro, etc.). Query for topic specific to meeting #5.
    - *Expected Result*: Meeting #5 is returned as the top match.
    - *Verification*: Assert correct ID is returned.

27. **T3_03: Meeting Trimming & Rolling Summary Sync**
    - *Objective*: Verify that when a meeting is popped from the JSON chronological sliding window (exceeding 5 full meetings), its vector representation remains queryable in the SQLite DB.
    - *Inputs*: Index 6 meetings. The first meeting is removed from the full active list in `meeting_log.json`. Query with a text similar to meeting #1.
    - *Expected Result*: Semantic search successfully retrieves the details of meeting #1 from the vector database.
    - *Verification*: Check if search results include meeting #1's ID.

28. **T3_04: Concurrent Vector Querying and Meeting Running**
    - *Objective*: Verify that a query can be executed from a running meeting while another meeting is saving its finalized state.
    - *Inputs*: Run search and insert threads simultaneously.
    - *Expected Result*: Both threads complete without deadlock or database locked exceptions.
    - *Verification*: Verify success exit code of both threads.

---

### Tier 4: Real-World Workloads & Scenarios (5 Test Cases)

29. **T4_01: Flash Crash Scenario**
    - *Objective*: Validate semantic memory behavior during a sudden market crash.
    - *Inputs*:
      - DB contains historical crash meeting: `"Flash Crash. Decisions: Sold 50% risk assets, set tight stop losses."`
      - Current price feed shows BTC down 15% in 30 minutes, triggering an emergency meeting.
    - *Expected Result*:
      - Semantic search returns the historical flash crash meeting as the top match.
      - Agent prompt includes: `• [Date: ...] Meeting: emergency_alert (Relevance: 0.88)\n Summary: Flash Crash. Decisions: Sold 50% risk assets...`
      - Facilitator summary references the historical decision.
    - *Verification*: Assert query results contain the historical crash ID and verify prompt injection strings.

30. **T4_02: Bull Run Scenario**
    - *Objective*: Validate semantic memory behavior during a strong upward breakout.
    - *Inputs*:
      - DB contains a strategy session: `"Bull Market Breakout. Decisions: DCA out of altcoins into stablecoins at target levels."`
      - Current price feed shows BTC up 12% in 24 hours.
    - *Expected Result*:
      - Search returns the bull market strategy session.
      - Prompts inject the profit-taking decisions.
      - Facilitator directs the team to check exit levels.
    - *Verification*: Verify matching record is retrieved and injected in agent contexts.

31. **T4_03: Sideways Chop Scenario**
    - *Objective*: Validate semantic memory behavior during flat, low-volatility conditions.
    - *Inputs*:
      - DB contains an altcoin scouting session discussing farming stables.
      - Current price feed shows volatility is extremely low.
    - *Expected Result*:
      - Search returns the stablecoin yield farming meeting.
      - Prompts inject yield farming/range-bound decisions.
      - Avoids executing panic sells or FOMO buys.
    - *Verification*: Verify low-volatility records are returned and no aggressive trade directives are executed.

32. **T4_04: Volatility Alert & Emergency Meeting Loop**
    - *Objective*: Validate that repeated high-volatility events do not duplicate historical memory context injection or overflow the agent context.
    - *Inputs*: Trigger 3 emergency meetings sequentially.
    - *Expected Result*:
      - Retrieval logic filters or ranks matching meetings to avoid injecting redundant, duplicate context.
      - Token limit budget of 500 is strictly honored.
    - *Verification*: Check prompt outputs for duplicated bullet points.

33. **T4_05: CEO Directives Override & Sentiment Shift**
    - *Objective*: Validate that a manual directive to de-risk queries past risk reviews and shapes execution.
    - *Inputs*:
      - CEO directive: `"De-risk portfolio immediately due to macro headwinds."`
      - DB has risk reviews with liquidation thresholds.
    - *Expected Result*:
      - Semantic search queries with "de-risk" and returns the specific drawdown steps.
      - Prompts guide the Trader to generate trade tags matching these steps.
    - *Verification*: Assert risk-reduction steps are injected and matched in final execution tags.
