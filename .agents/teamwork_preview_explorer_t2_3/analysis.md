# Analysis and Design Report: Semantic Memory Test Refactoring

## 1. Findings and Context

We analyzed the codebase and identified a major gap between the test suite and the actual production components for the semantic meeting memory.

### A. Production Code Gaps
1. **Missing Method**: `SemanticMeetingMemory` does not implement the `query_similar_meetings(self, query_text: str, n: int = 3) -> List[dict]` method, which is a required contract interface.
2. **Current Workaround**: The test suite bypasses this by dynamically monkeypatching a mock method onto the parent class `MeetingMemory` using a stateful python dictionary (`MockVectorDB`).
3. **Event Loop Impact**: In production, `index_meeting` and `get_semantic_context` perform synchronous network calls to the OpenAI embeddings API (`openai_client.embeddings.create`), which blocks the event loop.

### B. Test Suite Facade
1. **Mock Vector DB**: The test suite defines a mock emulator class `MockVectorDB` and patches `save_meeting` and `load` to interact solely with an in-memory dictionary.
2. **Untested Production DB**: The production `SQLiteVectorStore` is never initialized, written to, or searched during the 28 tests. If there were syntax errors, database connection leaks, or schema bugs, the tests would still pass.
3. **Verification Gap**: Cosine similarity is computed in the mock using python-based text overlaps instead of testing the actual math and database persistence.

---

## 2. Design for `query_similar_meetings`

To bridge the contract gap, we will implement `query_similar_meetings` on `SemanticMeetingMemory` in `discord-bridge/bot/memory.py`:

```python
    def query_similar_meetings(self, query_text: str, n: int = 3) -> List[dict]:
        """
        Computes the query embedding and returns the top n similar meeting records.
        Raises ValueError if there is a dimension mismatch between the query vector
        and stored vectors in the database.
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
            logger.error("Failed to generate embedding for similar meetings query: %s", exc)
            return []

        # Enforce dimension check against stored vectors to match contract requirements
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT vector FROM meeting_vectors LIMIT 1")
            row = cursor.fetchone()
        finally:
            conn.close()

        if row:
            try:
                stored_vector = json.loads(row[0])
                if stored_vector and len(stored_vector) != len(query_vector):
                    raise ValueError(
                        f"Dimension mismatch: Stored vector dimension is {len(stored_vector)} "
                        f"but query vector dimension is {len(query_vector)}."
                    )
            except (json.JSONDecodeError, TypeError):
                pass

        results = self.vector_store.search(query_vector, limit=n)
        
        meetings = []
        for res in results:
            metadata = res.get("metadata", {})
            meetings.append({
                "id": res["doc_id"],
                "type": metadata.get("type", "Unknown"),
                "timestamp": metadata.get("timestamp"),
                "summary": metadata.get("summary", ""),
                "decisions": metadata.get("decisions", []),
                "actions": metadata.get("actions", []),
                "similarity_score": res["score"]
            })
        return meetings
```

---

## 3. Refactoring Plan for `discord-bridge/test_semantic_memory.py`

### A. Remove Mock Facades
- Remove the class `MockVectorDB` and the singleton `mock_vector_db`.
- Remove the functions `save_vector_db_to_disk` and `load_vector_db_from_disk`.
- Remove the monkeypatched methods `mock_save_meeting` and `mock_load`.
- Let `MeetingMemory` use its real production implementation for database writes, loading, and persistence.

### B. Mock Only Network Components
We will intercept only the OpenAI Embeddings network client calls.

1. **Deterministic Mock Embeddings Generator**:
   Implement a mock embedding generator in the test that simulates cosine similarity using:
   - **Category Routing**: Assigns dominant indices for scenario-specific keywords.
   - **Bag-of-Words Model**: Translates texts into normalized sparse vectors based on word hashes to support phrase/unrelated similarities.
   - **Explicit Overrides**: Supports hardcoded scores for ordering tests.

```python
KEYWORDS_TO_VECTORS = [
    (["drops 15%", "may 2021", "liquidations", "crash"], 0),
    (["breakout", "bull run", "all-time high"], 1),
    (["sideways", "low volatility", "range-trading"], 2),
    (["emergency", "volatility index", "extreme volatility alert"], 3),
    (["funding rate", "squeeze"], 4)
]

embedding_overrides = {}

def get_mock_embedding(text: str, dimension: int = 128) -> List[float]:
    if text in embedding_overrides:
        return embedding_overrides[text]

    text_lower = text.lower()
    for i, (keywords, idx) in enumerate(KEYWORDS_TO_VECTORS):
        if any(k in text_lower for k in keywords):
            v = [0.0] * dimension
            v[idx] = 1.0
            return v

    # Fallback: Bag-of-words vector
    import re
    words = re.findall(r"\w+", text_lower)
    v = [0.0] * dimension
    if not words:
        v[0] = 1.0
        return v
    for word in words:
        h_idx = hash(word) % dimension
        v[h_idx] += 1.0
    magnitude = sum(x*x for x in v) ** 0.5
    if magnitude > 0:
        v = [x / magnitude for x in v]
    return v
```

2. **OpenAI Mocking Fixture**:
   Create a fixture `setup_openai_mocking` to mock the OpenAI embeddings client:
```python
@pytest.fixture(autouse=True)
def setup_openai_mocking(monkeypatch):
    """Mocks the OpenAI embeddings client to run offline."""
    captured_inputs = []

    def mock_create(input, model):
        captured_inputs.append(input)
        dim = getattr(meeting_memory, "_embedding_dimension", 128)
        
        if isinstance(input, list):
            mock_data_list = []
            for item in input:
                m_data = MagicMock()
                m_data.embedding = get_mock_embedding(item, dimension=dim)
                mock_data_list.append(m_data)
        else:
            m_data = MagicMock()
            m_data.embedding = get_mock_embedding(input, dimension=dim)
            mock_data_list = [m_data]

        mock_response = MagicMock()
        mock_response.data = mock_data_list
        return mock_response

    # Patch the singleton instance
    monkeypatch.setattr(meeting_memory.openai_client.embeddings, "create", mock_create)
    
    # Patch the OpenAI class itself for any new instances
    import openai
    mock_client = MagicMock()
    mock_client.embeddings.create.side_effect = mock_create
    monkeypatch.setattr(openai, "OpenAI", lambda *args, **kwargs: mock_client)

    return captured_inputs
```

### C. Isolated Database Redirects
Refactor `patch_db_paths` to redirect the production classes to `tmp_path`:
```python
@pytest.fixture(autouse=True)
def patch_db_paths(tmp_path):
    """Redirects all database and JSON file logs to tmp_path."""
    original_data_dir = bot.memory.DATA_DIR
    original_log_path = bot.memory.LOG_PATH
    original_portfolio_file = bot.portfolio._PORTFOLIO_FILE
    original_rotation_state = bot.meetings.ROTATION_STATE_PATH

    bot.memory.DATA_DIR = tmp_path
    bot.memory.LOG_PATH = tmp_path / "meeting_log.json"
    bot.portfolio._DATA_DIR = tmp_path
    bot.portfolio._PORTFOLIO_FILE = tmp_path / "portfolio_state.json"
    bot.meetings.ROTATION_STATE_PATH = tmp_path / "rotation_state.json"

    # Direct the singleton meeting_memory to the temp SQLite database
    meeting_memory.db_path = tmp_path / "meeting_vectors.db"
    meeting_memory.vector_store = SQLiteVectorStore(meeting_memory.db_path)
    meeting_memory.load()  # Loads empty state since JSON doesn't exist yet

    yield

    bot.memory.DATA_DIR = original_data_dir
    bot.memory.LOG_PATH = original_log_path
    bot.portfolio._PORTFOLIO_FILE = original_portfolio_file
    bot.meetings.ROTATION_STATE_PATH = original_rotation_state
```

### D. Refactoring Individual Test Cases
1. **test_vector_db_save_meeting_happy_path**:
   - Query SQLite directly using `sqlite3.connect` to assert that the record is in `meeting_vectors` table.
2. **test_vector_db_embedding_generation**:
   - Save a record, retrieve the input string captured by the OpenAI mock, and assert that it matches the production formatted text representation exactly.
3. **test_vector_db_query_similar_returns_ordered_results**:
   - Populate `embedding_overrides` with specific mock vectors to achieve exact similarity scores (e.g. 0.9, 0.7, 0.4) and assert descending order.
4. **test_vector_db_persistence_across_instances**:
   - Instantiate a new `SemanticMeetingMemory` instead of `MeetingMemory`. Point its database path to the same `tmp_path` and assert that it can query the same data successfully.
5. **test_vector_db_file_lock_concurrent_writes**:
   - Run saves concurrently, then query SQLite directly to confirm that all 10 records are successfully stored.
6. **Scenario Tests (Tier 4)**:
   - Rely on category routing inside `get_mock_embedding` to automatically return highly similar embeddings for the query and seeded records, allowing the rest of the engine logic to run unmodified.
