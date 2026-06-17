# Semantic Memory Exploration & Design Analysis

This document provides a detailed investigation of the production semantic memory database system in the `discord-bridge` service, designs the missing contract interface methods, and details a complete refactoring plan for the test suite `discord-bridge/test_semantic_memory.py`.

---

## 1. Analysis of Production Classes (`discord-bridge/bot/memory.py`)

The file `discord-bridge/bot/memory.py` defines two primary classes starting at line 204:

### A. `SQLiteVectorStore` (Lines 204-300)
- **Database Schema**: Creates a single SQLite table named `meeting_vectors` with three columns:
  - `doc_id` (TEXT PRIMARY KEY)
  - `vector` (TEXT): A JSON-serialized array of floats representing the embedding vector.
  - `metadata` (TEXT): A JSON-serialized dictionary containing meeting summary, type, decisions, actions, and timestamp.
- **Storage Strategy**: Uses `INSERT OR REPLACE` to atomically add or update document records.
- **Search Strategy**: 
  - Retrieves all vectors and metadata via `SELECT doc_id, vector, metadata FROM meeting_vectors`.
  - Performs **in-memory cosine similarity** calculation in pure Python using `math.sqrt` and `zip`:
    `similarity = dot_product / (q_norm * v_norm)`.
  - Sorts results by similarity score in descending order and returns the top `limit` items.

### B. `SemanticMeetingMemory` (Lines 302-418)
- **Inheritance**: Extends `MeetingMemory`, combining chronological JSON-based logging with vector database storage.
- **Initialization**: Instantiates the SQLite store at `DATA_DIR / "meeting_vectors.db"` and configures the `OpenAI` client (pointing to the LM Studio base URL by default).
- **Indexing (`index_meeting`)**: 
  - Formats meeting records into a single structured string representation:
    ```
    Meeting Type: {mtype}
    Summary: {summary}
    Decisions:
    - {decision_1}
    Action Items:
    - {action_1}
    ```
  - Sends this string to the OpenAI embeddings endpoint using the configured model (default: `text-embedding-ada-002`).
  - Stashes the generated vector along with metadata (structured representation text, summary, type, decisions, actions, timestamp) into the SQLite store.
- **Context Retrieval (`get_semantic_context`)**: Generates an embedding vector for the query text, queries the vector store, and returns a formatted multiline string suitable for injection into LLM prompts.

---

## 2. Design: Missing Interface Contract Method (`query_similar_meetings`)

To bridge the interface gap specified in `PROJECT.md`, `SemanticMeetingMemory` must implement the `query_similar_meetings` method.

### Proposed Code for `bot/memory.py`
We will inject this method inside the `SemanticMeetingMemory` class:

```python
    def query_similar_meetings(self, query_text: str, n: int = 3) -> List[dict]:
        """
        Computes the query embedding via OpenAI, queries the vector store, 
        and returns a list of meeting records with similarity scores.
        """
        if not query_text:
            return []

        from bot import settings
        model_id = settings.get("embedding_model_id", "text-embedding-ada-002")

        # 1. Compute the query embedding
        try:
            response = self.openai_client.embeddings.create(
                input=query_text,
                model=model_id,
            )
            query_vector = response.data[0].embedding
        except Exception as exc:
            logger.error("Failed to generate embedding for query_similar_meetings: %s", exc)
            return []

        # 2. Check for dimension mismatch to satisfy test expectations and prevent corrupt calculations
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT vector FROM meeting_vectors LIMIT 1")
            row = cursor.fetchone()
            if row:
                stored_vector = json.loads(row[0])
                if stored_vector and len(stored_vector) != len(query_vector):
                    raise ValueError(
                        f"Dimension mismatch: DB is {len(stored_vector)} but query is {len(query_vector)}"
                    )
        finally:
            conn.close()

        # 3. Search the vector database
        search_results = self.vector_store.search(query_vector, limit=n)

        # 4. Format search results to match standard meeting records with similarity_score injected
        records = []
        for res in search_results:
            meta = res.get("metadata", {})
            record = {
                "id": res.get("doc_id"),
                "type": meta.get("type"),
                "timestamp": meta.get("timestamp"),
                "summary": meta.get("summary"),
                "decisions": meta.get("decisions"),
                "actions": meta.get("actions"),
                "agent_contributions": meta.get("agent_contributions", {}),
                "similarity_score": res.get("score", 0.0)
            }
            records.append(record)
        return records
```

### Key Design Decisions
1. **Conforms to downstream meeting engine expectations**: Translates `SQLiteVectorStore` results into meeting records with a `similarity_score` key, matching what the existing test suite and `MeetingEngine` expect.
2. **Dimension mismatch guard**: Queries the SQLite DB for an existing vector, compares dimensions, and raises a `ValueError` on mismatch. This ensures robust database operations and lets the `test_vector_db_dimension_mismatch` test verify this condition using the real DB.

---

## 3. Integration Plan: `bot/meetings.py`

To eliminate the need for the mock wrapper `mock_run_meeting` in tests, we integrate the semantic memory querying directly into `MeetingEngine.run_meeting()` in `discord-bridge/bot/meetings.py`.

### Proposed Changes to `bot/meetings.py`

Modify `MeetingEngine.run_meeting` to query the vector database and populate `memory_context` if it's not provided:

```python
    async def run_meeting(
        self,
        meeting_type_id: str,
        post_message_fn: PostMessageFn,
        price_data: str = "",
        portfolio_summary: str = "",
        ceo_directives: str = "",
        memory_context: str = "",
    ) -> dict:
        """
        Run a complete meeting with debate and return the meeting record.
        """
        mt = MEETING_TYPES.get(meeting_type_id)
        if mt is None:
            raise ValueError(f"Unknown meeting type: {meeting_type_id}")

        logger.info("Starting meeting: %s (%s)", mt.name, mt.id)

        # ---- Context retrieval: retrieve semantic historical context if not provided ----
        if not memory_context:
            query_text = price_data or "general market state"
            try:
                from bot import settings
                budget = settings.get("token_budgets", {}).get("meeting_history", 500)
                
                similar = meeting_memory.query_similar_meetings(query_text, n=3)
                if similar:
                    lines = []
                    current_words = 0
                    for m in similar:
                        ts = m.get("timestamp", "?")
                        mtype = m.get("type", "?")
                        summary = m.get("summary", "—")
                        formatted = f"• [{ts}] {mtype} — {summary}"
                        
                        # Approximate token counts via word count
                        words = formatted.split()
                        if current_words + len(words) > budget:
                            if not lines:
                                # Truncate the first meeting to fit the budget
                                allowed_words = max(1, budget - current_words)
                                truncated_formatted = " ".join(words[:allowed_words])
                                lines.append(truncated_formatted)
                            break
                        lines.append(formatted)
                        current_words += len(words)
                    memory_context = "\n".join(lines) or "No prior meetings on record."
                else:
                    memory_context = "No prior meetings on record."
            except Exception:
                logger.exception("Failed to retrieve semantic memory context")
                memory_context = "No prior meetings on record."
```

---

## 4. Refactoring Plan: `discord-bridge/test_semantic_memory.py`

We will completely rewrite `test_semantic_memory.py` to target the actual database classes (`SQLiteVectorStore` and `SemanticMeetingMemory`) and mock only the network client.

### Step 1: Remove Mocks and Monkeypatches
- Delete the `MockVectorDB` class and its instance `mock_vector_db`.
- Remove mock helper functions: `save_vector_db_to_disk`, `load_vector_db_from_disk`, `mock_save_meeting`, `mock_load`, `mock_query_similar_meetings`, and `mock_run_meeting`.
- Clean up `setup_memory_mocking` to remove database monkeypatching.

### Step 2: Update `patch_db_paths` to Re-initialize the Singleton
Since `meeting_memory` is imported globally, its paths and DB connections are established at import time. We must redirect these properties to the test fixture's `tmp_path` dynamically:

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

    # Re-initialize the singleton's vector store path
    meeting_memory.db_path = tmp_path / "meeting_vectors.db"
    meeting_memory.vector_store = SQLiteVectorStore(meeting_memory.db_path)

    yield

    bot.memory.DATA_DIR = original_data_dir
    bot.memory.LOG_PATH = original_log_path
    bot.portfolio._PORTFOLIO_FILE = original_portfolio_file
    bot.meetings.ROTATION_STATE_PATH = original_rotation_state

    # Restore default singleton paths
    meeting_memory.db_path = original_data_dir / "meeting_vectors.db"
    meeting_memory.vector_store = SQLiteVectorStore(meeting_memory.db_path)
```

### Step 3: Mock the OpenAI Embeddings Client
We mock the OpenAI API client calls hermetically. In `setup_memory_mocking`:

```python
import openai
from unittest.mock import MagicMock

# Define deterministic unit vector generator based on text hash
def get_mock_embedding(text: str, dimension: int = 128) -> List[float]:
    import hashlib
    if dimension <= 0:
        raise ValueError("Invalid dimension")
    sha256 = hashlib.sha256(text.encode("utf-8")).digest()
    vector = []
    for i in range(dimension):
        byte_idx = (i * 3) % len(sha256)
        val = (sha256[byte_idx] / 127.5) - 1.0
        vector.append(val)
    magnitude = sum(x*x for x in vector) ** 0.5
    if magnitude > 0:
        vector = [x / magnitude for x in vector]
    return vector

class MockOpenAI:
    def __init__(self, *args, **kwargs):
        self.embeddings = MockEmbeddings()

class MockEmbeddings:
    def __init__(self):
        self.captured_inputs = []
        self.custom_embeddings = {}

    def create(self, input, model):
        # Allow register of custom embeddings for similarity tests
        text = str(input)
        self.captured_inputs.append(text)
        
        # Default or custom vector
        if text in self.custom_embeddings:
            vector = self.custom_embeddings[text]
        else:
            dimension = getattr(meeting_memory, "_embedding_dimension", 128)
            vector = get_mock_embedding(text, dimension=dimension)
            
        mock_data = MagicMock()
        mock_data.embedding = vector
        mock_response = MagicMock()
        mock_response.data = [mock_data]
        return mock_response

@pytest.fixture(autouse=True)
def setup_memory_mocking(monkeypatch):
    """Monkeypatches only OpenAI client to intercept embeddings generation."""
    mock_openai_instance = MockOpenAI()
    
    # Reset embedding dimension to default
    meeting_memory._embedding_dimension = 128
    
    # Inject MockOpenAI
    monkeypatch.setattr(openai, "OpenAI", lambda *args, **kwargs: mock_openai_instance)
    
    # Update singleton's openai_client instance
    meeting_memory.openai_client = mock_openai_instance
    
    return mock_openai_instance
```

### Step 4: Refactor Test Cases & Assertions

Here is how each test is refactored to verify the production classes:

#### 1. `test_vector_db_save_meeting_happy_path`
- **Before**: Asserted against `mock_vector_db.meetings`.
- **After**: Checks that the meeting record resides in the real database:
  ```python
  def test_vector_db_save_meeting_happy_path(setup_memory_mocking):
      record = MeetingMemory.make_meeting_record(
          "morning_briefing", "Happy path test", {"trader": "A"}, ["Decision 1"], ["Action 1"]
      )
      meeting_memory.save_meeting(record)
      
      # Query from production memory DB
      results = meeting_memory.query_similar_meetings("Happy path test", n=1)
      assert len(results) == 1
      assert results[0]["id"] == record["id"]
      assert results[0]["summary"] == "Happy path test"
  ```

#### 2. `test_vector_db_embedding_generation`
- **Before**: Checked `mock_vector_db.last_embedded_text`.
- **After**: Validates the actual structured string output by production `index_meeting` and sent to the client:
  ```python
  def test_vector_db_embedding_generation(setup_memory_mocking):
      record = MeetingMemory.make_meeting_record(
          "morning_briefing", "Summary text", {}, ["Decision A", "Decision B"], []
      )
      meeting_memory.save_meeting(record)
      
      expected_text = (
          "Meeting Type: morning_briefing\n"
          "Summary: Summary text\n"
          "Decisions:\n"
          "- Decision A\n"
          "- Decision B\n"
          "Action Items:\n"
          "None"
      )
      assert setup_memory_mocking.embeddings.captured_inputs[-1] == expected_text
  ```

#### 3. `test_vector_db_query_similar_returns_ordered_results`
- **Before**: Wrote scores to `mock_vector_db.override_similarities`.
- **After**: We register custom unit vectors (norm = 1.0) on the mock client to guarantee specific dot products (similarities) during search calculation:
  ```python
  def test_vector_db_query_similar_returns_ordered_results(setup_memory_mocking):
      m1 = MeetingMemory.make_meeting_record("morning_briefing", "Meeting 1", {}, [], [])
      m2 = MeetingMemory.make_meeting_record("morning_briefing", "Meeting 2", {}, [], [])
      m3 = MeetingMemory.make_meeting_record("morning_briefing", "Meeting 3", {}, [], [])
      
      meeting_memory.save_meeting(m1)
      meeting_memory.save_meeting(m2)
      meeting_memory.save_meeting(m3)
      
      # We query using text "query"
      # We setup the mock client to return custom unit vectors for indexing & query:
      # Query vector: [1.0, 0.0, 0.0]
      # m1 vector: [0.9, 0.435, 0.0] -> dot product with query = 0.9
      # m2 vector: [0.4, 0.916, 0.0] -> dot product with query = 0.4
      # m3 vector: [0.7, 0.714, 0.0] -> dot product with query = 0.7
      
      setup_memory_mocking.embeddings.custom_embeddings["query"] = [1.0, 0.0, 0.0]
      
      # Re-index documents with the custom vectors directly in SQLite to test cosine similarity search
      # (Alternatively, we register custom embeddings in the mock client before saving)
      ...
  ```

#### 4. `test_vector_db_persistence_across_instances`
- **Before**: Read `mock_vector_db.json`.
- **After**: Verifies database persistence by instantiating a fresh `SemanticMeetingMemory` and checking that it can read the SQLite file:
  ```python
  def test_vector_db_persistence_across_instances(tmp_path, setup_memory_mocking):
      record = MeetingMemory.make_meeting_record("morning_briefing", "Persistent meeting", {}, ["Decision X"], [])
      meeting_memory.save_meeting(record)
      
      # Instantiate a new memory class pointing to the same database path
      new_memory = SemanticMeetingMemory()
      new_memory.db_path = tmp_path / "meeting_vectors.db"
      new_memory.vector_store = SQLiteVectorStore(new_memory.db_path)
      new_memory.openai_client = setup_memory_mocking
      
      results = new_memory.query_similar_meetings("Persistent meeting", n=1)
      assert len(results) == 1
      assert results[0]["id"] == record["id"]
  ```
