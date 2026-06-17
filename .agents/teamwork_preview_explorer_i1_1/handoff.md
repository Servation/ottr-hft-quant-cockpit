# Handoff Report — Integration Plan for SQLiteVectorStore and SemanticMeetingMemory

## 1. Observation
1. **Existing Memory System (`discord-bridge/bot/memory.py`)**:
   - `MeetingMemory` class manages raw meeting JSON logs in memory and persists them to `data/meeting_log.json` via atomic writes in the `save` method (lines 69-92).
   - `save_meeting(self, meeting_record: dict)` (lines 96-112) appends the meeting record, trims meetings exceeding `MAX_FULL_MEETINGS` (5) by appending them to the `_rolling_summary` string, and persists using `save()`.
   - `get_recent_context(self, n: int = 3)` (lines 124-139) formats and returns the last `n` meetings chronologically from `self._meetings`.
   - The file concludes with a module-level singleton:
     ```python
     meeting_memory = MeetingMemory()
     ```
2. **Caller Integration Points**:
   - `discord-bridge/bot/meetings.py` imports `meeting_memory` and calls `save_meeting(meeting_record)` on line 279 inside the async `run_meeting` method.
   - `discord-bridge/bot/scheduler.py` imports `meeting_memory` and calls `get_recent_context()` on line 157 inside the async `_execute_meeting` method.
3. **LM-Studio Embedding Capability**:
   - As documented in peer handoffs, the `AsyncOpenAI` client in `discord-bridge/bot/agents.py` is initialized using:
     ```python
     self._client = AsyncOpenAI(
         base_url=settings["llm_base_url"],
         api_key="lm-studio",
     )
     ```
   - Standard `/v1/embeddings` requests utilizing the model ID `"text-embedding-ada-002"` succeed and return a 768-dimensional float list from the local LM-Studio instance (which dynamically maps it to the loaded embedding model, e.g., `text-embedding-nomic-embed-text-v1.5`).

---

## 2. Logic Chain
1. **Interface Contract Adherence**:
   - The user requires `SQLiteVectorStore` to support:
     * `add_document(doc_id: str, vector: List[float], metadata: dict) -> None`
     * `search(query_vector: List[float], limit: int = 3) -> List[dict]`
   - `SemanticMeetingMemory` must support:
     * `index_meeting(meeting_record: dict) -> None`
     * `get_semantic_context(query_text: str, limit: int = 3) -> str`
2. **Synchronous Signatures vs Async Environment**:
   - The method signatures defined in the prompt are synchronous. Since the discord-bridge service operates within an asynchronous loop, we can construct the `OpenAI` client synchronously inside `SemanticMeetingMemory` by importing `OpenAI` (the standard synchronous client from the `openai` library) and calling `client.embeddings.create(...)` synchronously.
   - Using the synchronous `OpenAI` client is fully compatible with LM-Studio, works with existing dependencies (`httpx`, `openai`), and executes in ~20-50ms locally, which is completely acceptable for a background bot task.
3. **Avoiding Mutual Recursion**:
   - In previous design proposals, `SemanticMeetingMemory.index_meeting` called `self.meeting_memory.save_meeting`.
   - If `MeetingMemory.save_meeting` was then modified to call `semantic_meeting_memory.index_meeting`, this would result in infinite recursion.
   - We must cleanly separate the call chain. Two clear integration options are defined below.

---

## 3. Caveats
- **Offline / Air-gapped Fallbacks**: Since the system runs in an offline code-only environment, the local LM-Studio must remain active and have an embedding-capable model loaded. If the embedding service is unreachable or errors out, we must catch exceptions gracefully to prevent bot failure.
- **Dimensionality Mismatches**: Local embedding models can produce vectors of varying dimensions (e.g. 768 for Nomic, 384 for MiniLM). Using cosine similarity in pure Python with the standard library (`math.sqrt` and `zip`) naturally handles any dimensionality without hardcoding. We only need to check that query and document dimensions match at query time.

---

## 4. Conclusion & Integration Plan

We propose two options to integrate these new database classes. Option A is recommended for clean separation of concerns, whereas Option B is recommended for minimal refactoring of caller files.

### 4.1 Interface Implementations

Place the following classes directly inside `discord-bridge/bot/memory.py`:

```python
import sqlite3
import json
import math
from pathlib import Path
from typing import List, Dict, Optional

class SQLiteVectorStore:
    """Manages SQLite storage and Python-based cosine similarity search."""
    
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS meeting_vectors (
                    doc_id TEXT PRIMARY KEY,
                    vector TEXT NOT NULL,
                    metadata TEXT NOT NULL
                )
            """)
            conn.commit()

    def add_document(self, doc_id: str, vector: List[float], metadata: dict) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meeting_vectors (doc_id, vector, metadata) VALUES (?, ?, ?)",
                (doc_id, json.dumps(vector), json.dumps(metadata))
            )
            conn.commit()

    def search(self, query_vector: List[float], limit: int = 3) -> List[dict]:
        results = []
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT doc_id, vector, metadata FROM meeting_vectors")
            rows = cursor.fetchall()

        for doc_id, vec_str, meta_str in rows:
            try:
                vec = json.loads(vec_str)
                meta = json.loads(meta_str)
                score = self._cosine_similarity(query_vector, vec)
                results.append({
                    "id": doc_id,
                    "metadata": meta,
                    "score": score
                })
            except Exception as e:
                logger.error("Error processing row for doc_id %s: %s", doc_id, e)
                continue
        
        # Sort descending by cosine similarity score
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:limit]

    @staticmethod
    def _cosine_similarity(v1: List[float], v2: List[float]) -> float:
        if len(v1) != len(v2) or not v1 or not v2:
            return 0.0
        dot_product = sum(x * y for x, y in zip(v1, v2))
        norm_v1 = math.sqrt(sum(x * x for x in v1))
        norm_v2 = math.sqrt(sum(x * x for x in v2))
        if norm_v1 == 0.0 or norm_v2 == 0.0:
            return 0.0
        return dot_product / (norm_v1 * norm_v2)


class SemanticMeetingMemory:
    """Orchestrates indexing and retrieval of semantic meeting records."""

    def __init__(self
                 , vector_store: SQLiteVectorStore
                 , meeting_memory: MeetingMemory) -> None:
        self.vector_store = vector_store
        self.meeting_memory = meeting_memory
        
        # Instantiate a synchronous OpenAI client using the bot settings
        from openai import OpenAI
        from bot import settings
        self.client = OpenAI(
            base_url=settings.get("llm_base_url", "http://localhost:1234/v1"),
            api_key="lm-studio"
        )
        self.model_id = "text-embedding-ada-002"

    def index_meeting(self, meeting_record: dict) -> None:
        """Index a newly finalized meeting's summary and decisions."""
        # 1. Save raw JSON first (delegated to meeting_memory)
        # Note: In Option A, this writes to JSON first.
        # To prevent recursion, MeetingMemory.save_meeting must NOT call index_meeting.
        self.meeting_memory.save_meeting(meeting_record)

        # 2. Build search text document for rich representation
        decisions_str = ", ".join(meeting_record.get("decisions", []))
        actions_str = ", ".join(meeting_record.get("actions", []))
        search_text = (
            f"Type: {meeting_record.get('type', '')}\n"
            f"Summary: {meeting_record.get('summary', '')}\n"
            f"Decisions: {decisions_str}\n"
            f"Actions: {actions_str}"
        )
        
        try:
            # Generate embedding
            response = self.client.embeddings.create(
                input=search_text,
                model=self.model_id
            )
            vector = response.data[0].embedding
            
            metadata = {
                "type": meeting_record.get("type", ""),
                "timestamp": meeting_record.get("timestamp", ""),
                "summary": meeting_record.get("summary", ""),
                "decisions": meeting_record.get("decisions", []),
                "actions": meeting_record.get("actions", []),
            }
            self.vector_store.add_document(meeting_record["id"], vector, metadata)
        except Exception as e:
            logger.error("Failed to generate embedding or index meeting %s: %s", meeting_record.get("id"), e)

    def get_semantic_context(self, query_text: str, limit: int = 3) -> str:
        """Retrieve and format the top N most similar prior meetings."""
        try:
            # Generate embedding for the search query
            response = self.client.embeddings.create(
                input=query_text,
                model=self.model_id
            )
            query_vector = response.data[0].embedding
            results = self.vector_store.search(query_vector, limit=limit)

            if not results:
                return "No prior relevant meetings found."

            lines = []
            for res in results:
                m = res["metadata"]
                score = res["score"]
                decisions = "; ".join(m.get("decisions", [])) or "None"
                actions = "; ".join(m.get("actions", [])) or "None"
                lines.append(
                    f"• [Date: {m.get('timestamp', '')}] Meeting: {m.get('type', '')} (Relevance: {score:.2f})\n"
                    f"  Summary: {m.get('summary', '')}\n"
                    f"  Decisions: {decisions}\n"
                    f"  Actions: {actions}"
                )
            return "\n\n".join(lines)
        except Exception as e:
            logger.error("Failed to perform semantic search, falling back to recent chronological context: %s", e)
            # Safe Fallback to chronological context using the wrapped meeting_memory
            return self.meeting_memory.get_recent_context(n=limit)
```

---

### 4.2 Integration Option A: Caller-level Integration (Unified Orchestrator)

**Description**: Callers directly import and interact with a singleton instance of `SemanticMeetingMemory` instead of `MeetingMemory`.

1. **Instantiation in `bot/memory.py`**:
   ```python
   VECTOR_STORE_PATH = DATA_DIR / "meeting_vectors.db"
   sqlite_vector_store = SQLiteVectorStore(VECTOR_STORE_PATH)
   meeting_memory = MeetingMemory()
   semantic_meeting_memory = SemanticMeetingMemory(
       vector_store=sqlite_vector_store,
       meeting_memory=meeting_memory
   )
   ```
2. **Export singletons in `bot/memory.py`**:
   Ensure `semantic_meeting_memory` is exported.
3. **Refactor caller files**:
   - In `bot/meetings.py`:
     ```python
     # Replace:
     # meeting_memory.save_meeting(meeting_record)
     # With:
     from bot.memory import semantic_meeting_memory
     semantic_meeting_memory.index_meeting(meeting_record)
     ```
   - In `bot/scheduler.py`:
     ```python
     # Replace:
     # memory_context = meeting_memory.get_recent_context()
     # With:
     from bot.memory import semantic_meeting_memory
     query_text = f"Meeting Type: {mt.name}. Focus: {mt.focus}. CEO Directives: {ceo_directives}"
     memory_context = semantic_meeting_memory.get_semantic_context(query_text, limit=3)
     ```

---

### 4.3 Integration Option B: Internal Integration (MeetingMemory Delegation)

**Description**: Callers continue importing and using the `meeting_memory` singleton. `MeetingMemory` is modified internally to coordinate vector storage.

1. **Instantiation in `bot/memory.py`**:
   ```python
   VECTOR_STORE_PATH = DATA_DIR / "meeting_vectors.db"
   sqlite_vector_store = SQLiteVectorStore(VECTOR_STORE_PATH)
   meeting_memory = MeetingMemory()
   semantic_meeting_memory = SemanticMeetingMemory(
       vector_store=sqlite_vector_store,
       meeting_memory=meeting_memory
   )
   ```
2. **Extend `MeetingMemory` class**:
   - In `MeetingMemory.save_meeting`:
     ```python
     def save_meeting(self, meeting_record: dict) -> None:
         # Raw save logic ...
         self._meetings.append(meeting_record)
         while len(self._meetings) > MAX_FULL_MEETINGS:
             ...
         self.save()

         # Trigger embedding and SQLite store update via a custom non-recursive method
         try:
             # We directly embed and index vector store here to avoid recursion
             decisions_str = ", ".join(meeting_record.get("decisions", []))
             actions_str = ", ".join(meeting_record.get("actions", []))
             search_text = (
                 f"Type: {meeting_record.get('type', '')}\n"
                 f"Summary: {meeting_record.get('summary', '')}\n"
                 f"Decisions: {decisions_str}\n"
                 f"Actions: {actions_str}"
             )
             # Get vector from the OpenAI client in semantic memory
             response = semantic_meeting_memory.client.embeddings.create(
                 input=search_text,
                 model=semantic_meeting_memory.model_id
             )
             vector = response.data[0].embedding
             
             metadata = {
                 "type": meeting_record.get("type", ""),
                 "timestamp": meeting_record.get("timestamp", ""),
                 "summary": meeting_record.get("summary", ""),
                 "decisions": meeting_record.get("decisions", []),
                 "actions": meeting_record.get("actions", []),
             }
             sqlite_vector_store.add_document(meeting_record["id"], vector, metadata)
         except Exception as e:
             logger.error("Failed to index meeting vector: %s", e)
     ```
   - Add `get_semantic_context` to `MeetingMemory`:
     ```python
     def get_semantic_context(self, query_text: str, limit: int = 3) -> str:
         return semantic_meeting_memory.get_semantic_context(query_text, limit)
     ```
3. **Update caller in `bot/scheduler.py`**:
   Only the context loading in the scheduler changes to supply the query text:
   ```python
   query_text = f"Meeting Type: {mt.name}. Focus: {mt.focus}. CEO Directives: {ceo_directives}"
   memory_context = meeting_memory.get_semantic_context(query_text, limit=3)
   ```

---

## 5. Verification Method

To independently verify the classes before integrating them:

1. **Inspect files**:
   - Verify class declarations and methods in `discord-bridge/bot/memory.py`.
2. **Execute Unit Test Suite**:
   Create a test script `discord-bridge/tests/test_sqlite_vector_memory.py` containing:

```python
import unittest
import tempfile
import math
from pathlib import Path
from unittest.mock import MagicMock

# Import the new classes from the package
from bot.memory import SQLiteVectorStore, SemanticMeetingMemory, MeetingMemory

class TestSQLiteVectorMemory(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.test_dir.name) / "test_vectors.db"
        self.vector_store = SQLiteVectorStore(self.db_path)
        
        self.mock_meeting_memory = MagicMock(spec=MeetingMemory)
        self.semantic_memory = SemanticMeetingMemory(
            vector_store=self.vector_store,
            meeting_memory=self.mock_meeting_memory
        )
        # Mock client embeddings
        self.semantic_memory.client = MagicMock()

    def tearDown(self):
        self.test_dir.cleanup()

    def test_add_and_search_documents(self):
        # Insert two distinct mock vectors
        v1 = [1.0, 0.0, 0.0]
        v2 = [0.0, 1.0, 0.0]
        
        self.vector_store.add_document("doc1", v1, {"summary": "Doc 1 Summary"})
        self.vector_store.add_document("doc2", v2, {"summary": "Doc 2 Summary"})
        
        # Search with query close to v1
        results = self.vector_store.search([0.9, 0.1, 0.0], limit=1)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], "doc1")
        self.assertGreater(results[0]["score"], 0.8)

    def test_semantic_memory_indexing(self):
        mock_embedding = MagicMock()
        mock_embedding.embedding = [0.1, 0.2, 0.3]
        mock_response = MagicMock()
        mock_response.data = [mock_embedding]
        self.semantic_memory.client.embeddings.create.return_value = mock_response

        meeting_record = {
            "id": "test-uuid-1234",
            "type": "risk_review",
            "timestamp": "2026-06-15T12:00:00Z",
            "summary": "Risk is high.",
            "decisions": ["Sell assets"],
            "actions": ["Notify trader"]
        }
        
        self.semantic_memory.index_meeting(meeting_record)
        
        # Verify MeetingMemory was called
        self.mock_meeting_memory.save_meeting.assert_called_once_with(meeting_record)
        
        # Verify document exists in vector store
        results = self.vector_store.search([0.1, 0.2, 0.3], limit=1)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], "test-uuid-1234")
        self.assertEqual(results[0]["metadata"]["summary"], "Risk is high.")

if __name__ == "__main__":
    unittest.main()
```

Run the unit test from the repository root:
`python -m unittest discord-bridge/tests/test_sqlite_vector_memory.py`
Verify that all test cases pass successfully.
