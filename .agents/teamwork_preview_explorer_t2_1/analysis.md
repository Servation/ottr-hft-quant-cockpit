# Analysis and Refactoring Proposal: OTTR HFT Semantic Memory

## 1. Executive Summary
This report presents an investigation into the integration of semantic meeting memory in the `discord-bridge` service. 
The current test suite `test_semantic_memory.py` functions as a facade by utilizing an in-memory database emulator (`MockVectorDB`) and bypassing the actual production classes (`SQLiteVectorStore` and `SemanticMeetingMemory`). Additionally, the production database implementation is missing the interface contract method `query_similar_meetings`.

To address these gaps, we propose:
1. Implementing `query_similar_meetings` inside `SemanticMeetingMemory` in `discord-bridge/bot/memory.py`.
2. Refactoring `discord-bridge/test_semantic_memory.py` to target the actual production database classes, mocking only the external network components (OpenAI embedding endpoint client, price feed, and agent LLM response generation).

---

## 2. Reviewers' Feedback Synthesis
Both reviewers (`teamwork_preview_reviewer_t2_1` and `teamwork_preview_reviewer_t2_2`) confirmed that:
- All 28 tests pass successfully but only test an in-memory python dictionary (`MockVectorDB`) and persist to a dummy JSON file (`mock_vector_db.json`).
- The production database classes (`SQLiteVectorStore` and `SemanticMeetingMemory`) are completely untested, and their schema, connection management, and calculations are unverified.
- `SemanticMeetingMemory` lacks the required `query_similar_meetings` method specified in `PROJECT.md`.
- Synchronous calls to the OpenAI embeddings API inside `index_meeting` block the single-threaded asyncio event loop in production, risking Discord heartbeat timeouts.

---

## 3. Production Code Analysis (`discord-bridge/bot/memory.py`)
- **`SQLiteVectorStore`**: A SQLite-backed vector store using standard sqlite3 parameters.
  - Generates a `meeting_vectors` table containing `doc_id`, JSON-stringified `vector`, and JSON-stringified `metadata`.
  - cosine similarity is calculated in pure Python using `math.sqrt` and `sum(q * v for q, v in zip(query_vector, vector))`.
  - **Issue**: Mismatched vector dimensions are silently skipped in `search()` instead of raising an error, which conflicts with testing for dimension mismatch handling.
- **`SemanticMeetingMemory`**: Inherits from `MeetingMemory`.
  - Points to `DATA_DIR / "meeting_vectors.db"`.
  - Uses `openai.OpenAI` client initialized with settings configurations.
  - **Issue**: Singleton `meeting_memory` is initialized at import time. This makes test isolation difficult if path fixtures do not re-bind the instance's database path/vector store.
  - **Issue**: Lacks `query_similar_meetings`.

---

## 4. Proposed Implementation of `query_similar_meetings`
To bridge the contract gap, we will implement `query_similar_meetings` in `SemanticMeetingMemory` in `discord-bridge/bot/memory.py`:

```python
    def query_similar_meetings(self, query_text: str, n: int = 3) -> List[dict]:
        """
        Query similar meetings based on the semantic query embedding.
        Returns a list of meeting dictionary records from the database,
        each decorated with a 'similarity_score'.
        """
        if not query_text:
            return []

        from bot import settings
        model_id = settings.get("embedding_model_id", "text-embedding-ada-002")

        try:
            response = self.openai_client.embeddings.create(
                input=query_text,
                model=model_id,
            )
            query_vector = response.data[0].embedding
        except Exception as exc:
            logger.error("Failed to generate embedding for query_similar_meetings: %s", exc)
            return []

        # Enforce dimension mismatch detection
        if self.db_path.exists():
            import sqlite3
            conn = sqlite3.connect(self.db_path)
            try:
                cursor = conn.cursor()
                cursor.execute("SELECT vector FROM meeting_vectors LIMIT 1")
                row = cursor.fetchone()
                if row:
                    try:
                        stored_vector = json.loads(row[0])
                        if stored_vector and len(stored_vector) != len(query_vector):
                            raise ValueError(
                                f"Dimension mismatch: stored vectors are of size {len(stored_vector)}, "
                                f"but query vector is of size {len(query_vector)}"
                            )
                    except json.JSONDecodeError:
                        pass
            finally:
                conn.close()

        results = self.vector_store.search(query_vector, limit=n)
        
        meetings_dict = {m["id"]: m for m in self._meetings}
        returned_meetings = []
        for res in results:
            m_id = res["doc_id"]
            if m_id in meetings_dict:
                # Return the full meeting record from memory (shallow copied to prevent side effects)
                m_copy = dict(meetings_dict[m_id])
            else:
                # Reconstruct from SQLite metadata
                metadata = res.get("metadata", {})
                m_copy = {
                    "id": m_id,
                    "type": metadata.get("type", "Unknown"),
                    "summary": metadata.get("summary", ""),
                    "decisions": metadata.get("decisions", []),
                    "actions": metadata.get("actions", []),
                    "timestamp": metadata.get("timestamp", "?"),
                    "agent_contributions": {},
                }
            m_copy["similarity_score"] = res["score"]
            returned_meetings.append(m_copy)

        return returned_meetings
```

---

## 5. Refactoring Plan for `discord-bridge/test_semantic_memory.py`

### Step 1: Remove Mocks and Emulators
- Remove the `MockVectorDB` class and its singleton instance `mock_vector_db`.
- Remove helper functions `save_vector_db_to_disk` and `load_vector_db_from_disk`.
- Remove `mock_save_meeting` and `mock_load`.
- Do NOT monkeypatch `MeetingMemory.save_meeting` or `MeetingMemory.load`.

### Step 2: Update database Redirects in Fixtures
The import-time singleton initialization of `meeting_memory` caches the production DB paths. Update the `patch_db_paths` fixture to explicitly patch `meeting_memory` paths and re-instantiate its vector store under pytest's `tmp_path`:

```python
@pytest.fixture(autouse=True)
def patch_db_paths(tmp_path):
    """Redirects all database and JSON file logs to tmp_path."""
    original_data_dir = bot.memory.DATA_DIR
    original_log_path = bot.memory.LOG_PATH
    original_portfolio_file = bot.portfolio._PORTFOLIO_FILE
    original_rotation_state = bot.meetings.ROTATION_STATE_PATH
    
    # Singleton cached paths
    original_singleton_db_path = meeting_memory.db_path
    original_singleton_vector_store = meeting_memory.vector_store

    # Redirect module-level variables
    bot.memory.DATA_DIR = tmp_path
    bot.memory.LOG_PATH = tmp_path / "meeting_log.json"
    bot.portfolio._DATA_DIR = tmp_path
    bot.portfolio._PORTFOLIO_FILE = tmp_path / "portfolio_state.json"
    bot.meetings.ROTATION_STATE_PATH = tmp_path / "rotation_state.json"

    # Redirect singleton attributes
    meeting_memory.db_path = tmp_path / "meeting_vectors.db"
    meeting_memory.vector_store = bot.memory.SQLiteVectorStore(meeting_memory.db_path)

    yield

    bot.memory.DATA_DIR = original_data_dir
    bot.memory.LOG_PATH = original_log_path
    bot.portfolio._PORTFOLIO_FILE = original_portfolio_file
    bot.meetings.ROTATION_STATE_PATH = original_rotation_state
    
    meeting_memory.db_path = original_singleton_db_path
    meeting_memory.vector_store = original_singleton_vector_store
```

### Step 3: Mock the OpenAI Embeddings Client
Create a new autouse fixture `setup_openai_mocking` to mock the OpenAI embeddings creator globally:

```python
@pytest.fixture(autouse=True)
def setup_openai_mocking(monkeypatch):
    """Mocks OpenAI client globally to return deterministic mock embeddings."""
    mock_create = MagicMock()
    
    def side_effect(input, model):
        if isinstance(input, list):
            inputs = input
        else:
            inputs = [input]
        data = []
        for inp in inputs:
            # Control similarity outcomes by returning specific unit vectors for specific test cases
            if "query" in inp:
                emb = [1.0] + [0.0] * 127
            elif "Meeting 1" in inp:
                emb = [0.9, math.sqrt(1 - 0.9**2)] + [0.0] * 126
            elif "Meeting 2" in inp:
                emb = [0.4, math.sqrt(1 - 0.4**2)] + [0.0] * 126
            elif "Meeting 3" in inp:
                emb = [0.7, math.sqrt(1 - 0.7**2)] + [0.0] * 126
            else:
                # Default mock embedding generator
                emb = get_mock_embedding(inp, dimension=128)
            
            mock_data_item = MagicMock()
            mock_data_item.embedding = emb
            data.append(mock_data_item)
            
        mock_response = MagicMock()
        mock_response.data = data
        return mock_response
        
    mock_create.side_effect = side_effect
    
    class MockOpenAI:
        def __init__(self, *args, **kwargs):
            self.embeddings = MagicMock()
            self.embeddings.create = mock_create
            
    # Mock future instantiations
    monkeypatch.setattr("openai.OpenAI", MockOpenAI)
    # Mock current singleton
    meeting_memory.openai_client = MockOpenAI()
    
    return mock_create
```

### Step 4: Keep Scheduler-level Context Injection wrapper
Since production `MeetingEngine.run_meeting` does not query the vector database internally yet, keep the `mock_run_meeting` patch but clean it to call the real `query_similar_meetings`:

```python
# In setup_memory_mocking:
monkeypatch.setattr(MeetingEngine, "run_meeting", mock_run_meeting)
```

### Step 5: Refactor Test Assertions to target SQLite
Modify the test assertions to check SQLite database rows instead of the dummy in-memory mock database:

1. **`test_vector_db_save_meeting_happy_path`**:
   ```python
   # Assert using SQLite connection
   import sqlite3
   conn = sqlite3.connect(meeting_memory.db_path)
   cursor = conn.cursor()
   cursor.execute("SELECT doc_id, metadata FROM meeting_vectors WHERE doc_id = ?", (record["id"],))
   row = cursor.fetchone()
   conn.close()
   assert row is not None
   metadata = json.loads(row[1])
   assert metadata["summary"] == "Happy path test"
   ```

2. **`test_vector_db_embedding_generation`**:
   Capture input to `setup_openai_mocking` and check:
   ```python
   assert setup_openai_mocking.called
   args, kwargs = setup_openai_mocking.call_args
   captured_text = kwargs.get("input") or args[0]
   expected_text = (
       "Meeting Type: morning_briefing\n"
       "Summary: Summary text\n"
       "Decisions:\n"
       "- Decision A\n"
       "- Decision B\n"
       "Action Items:\n"
       "None"
   )
   assert captured_text == expected_text
   ```

3. **`test_vector_db_persistence_across_instances`**:
   Instead of reloading the mock vector DB, just instantiate a new `SemanticMeetingMemory` (which automatically opens the temp SQLite database file) and assert query results:
   ```python
   new_memory = SemanticMeetingMemory()
   # openai client on new_memory is automatically mocked by MockOpenAI
   results = new_memory.query_similar_meetings("Persistent meeting", n=1)
   assert len(results) == 1
   assert results[0]["id"] == record["id"]
   ```

4. **`test_flow_multiple_sequential_meetings`**:
   Verify count of documents in the SQLite vector database:
   ```python
   conn = sqlite3.connect(meeting_memory.db_path)
   cursor = conn.cursor()
   cursor.execute("SELECT COUNT(*) FROM meeting_vectors")
   count = cursor.fetchone()[0]
   conn.close()
   assert count == 6
   ```

---

## 6. Verification and Rollout Plan
To verify the implementation of this proposal:
1. Apply the proposed implementation of `query_similar_meetings` to `discord-bridge/bot/memory.py`.
2. Apply the refactoring plan to `discord-bridge/test_semantic_memory.py`.
3. Run `pytest discord-bridge/test_semantic_memory.py -v`.
4. Check that all 28 tests pass successfully.
5. Inspect the temporary directory after test execution to confirm that the SQLite database file (`meeting_vectors.db`) is successfully created, populated, and removed according to the pytest fixture teardown.
