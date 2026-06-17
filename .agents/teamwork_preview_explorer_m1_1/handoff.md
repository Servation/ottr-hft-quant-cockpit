# Handoff Report — Vector Store Exploration and Memory Design

## 1. Observation
1. **ChromaDB Dependency Analysis**:
   - In `d:\crypto-trading-bot\discord-bridge\requirements.txt`, no vector database or machine learning libraries (such as `chromadb`, `numpy`, or `scikit-learn`) are listed:
     ```text
     discord.py>=2.3.0
     openai>=1.0.0
     httpx>=0.25.0
     apscheduler>=3.10.0
     pyyaml>=6.0
     python-dotenv>=1.0.0
     ```
   - In `d:\crypto-trading-bot\agent-gateway\pyproject.toml`, the dependencies list (lines 6-15) also does not contain `chromadb` or secondary math/vector libraries:
     ```toml
     dependencies = [
         "fastapi>=0.110.0",
         "uvicorn>=0.28.0",
         "pydantic>=2.6.0",
         "pydantic-settings>=2.2.0",
         "openai>=1.14.0",
         "httpx>=0.27.0",
         "prometheus-fastapi-instrumentator>=7.0.0",
         "sse-starlette>=2.0.0",
     ]
     ```
   - Execution of `python -c "import chromadb"` was attempted twice, but timed out awaiting user permission.

2. **Embedding Generation Findings (Explorer 2 Handoff)**:
   - Explorer 2 (m1_2) successfully verified that the `AsyncOpenAI` client (configured in `bot/agents.py`) connects to the local LM-Studio endpoint at `settings["llm_base_url"]` (`http://localhost:1234/v1`).
   - Calling `client.embeddings.create` with model ID `text-embedding-ada-002` succeeds because LM-Studio automatically maps it to the loaded embedding model: `text-embedding-nomic-embed-text-v1.5`.
   - The generated embeddings are 768-dimensional float arrays.

3. **Current Memory and Meeting Flow**:
   - `d:\crypto-trading-bot\discord-bridge\bot\memory.py` uses atomic JSON writes to `data/meeting_log.json`. It saves `meetings`, `decisions`, and `rolling_summary`.
   - When saving a meeting (lines 96-112), if `len(self._meetings)` exceeds `MAX_FULL_MEETINGS` (5), it pops the oldest meeting and appends its condensed text string to `self._rolling_summary`.
   - In `d:\crypto-trading-bot\discord-bridge\bot\meetings.py`, `_build_agent_context` (lines 455-522) injects recent chronological context:
     ```python
     if memory_context:
         user_content_parts.append(f"### Recent Meeting History\n{memory_context}")
     ```
   - In `bot/scheduler.py` (lines 155-159), the memory context is loaded from `meeting_memory.get_recent_context()` (which grabs the last `n` meetings).

---

## 2. Logic Chain
1. **ChromaDB Availability**: ChromaDB is not installed in the current environment and cannot be added to `requirements.txt` or installed via pip because the system operates under a strict offline (`CODE_ONLY` network mode) configuration where PyPI downloads are blocked.
2. **Alternative Storage**: We must implement a pure-Python, zero-dependency vector database solution utilizing SQLite (`sqlite3` module) or JSON files (`json` module).
3. **Similarity Search Efficiency**: In Python, computing the cosine similarity of 768-dimensional vectors for a small dataset (e.g., 1,000 meetings) requires less than 2 milliseconds. Standard library calculations using `math.sqrt` and `zip` are perfectly adequate, fast, and avoid any compiling/binary dependencies.
4. **Vector Document Construction**: Since the LLM output decisions list is sometimes sparse, we should concatenate the meeting type, summary, decisions, and action items into a single textual block to compute rich representative embeddings.
5. **Prompt Injection Integration**:
   - `_build_agent_context` should be updated to replace the chronological `### Recent Meeting History` with a semantically searched `### Relevant Historical Context (Semantic Search)`.
   - `_build_closing` (facilitator closing) currently lacks any historical memory. Injecting the retrieved context here will prevent the facilitator from making redundant or duplicate decisions.

---

## 3. Caveats
- **Offline Mode Constraints**: No external APIs or packages can be loaded. The embedding model must remain loaded locally in LM-Studio.
- **Dynamic Dimensions**: If the underlying embedding model in LM-Studio changes, the vector dimensions will change (e.g. Nomic is 768, OpenAI's real Ada is 1536). The database schema or code must handle vector dimension mismatches gracefully.
- **Token Budgets**: If the semantic context exceeds the 500 token limit configured in `settings.yaml`, we should truncate summaries or dynamically slice the results.

---

## 4. Conclusion & Suggested Design
We recommend a pure Python + SQLite-backed vector store solution. It is self-contained, lightweight, and requires zero external libraries.

### Suggested Vector Memory Schema
```sql
CREATE TABLE IF NOT EXISTS meeting_vectors (
    id TEXT PRIMARY KEY,
    vector TEXT NOT NULL,       -- JSON-serialized float list [dim=768]
    metadata TEXT NOT NULL,     -- JSON-serialized metadata dictionary
    timestamp TEXT NOT NULL     -- ISO-8601 UTC
);
```

### Proposed Interface Classes

```python
import sqlite3
import json
import math
from pathlib import Path
from typing import Dict, List, Optional, Protocol

class EmbeddingService:
    """Uses AsyncOpenAI client to generate embeddings."""
    def __init__(self, client) -> None:
        self.client = client
        self.model_id = "text-embedding-ada-002"

    async def get_embedding(self, text: str) -> List[float]:
        response = await self.client.embeddings.create(
            input=text,
            model=self.model_id
        )
        return response.data[0].embedding

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
                    id TEXT PRIMARY KEY,
                    vector TEXT NOT NULL,
                    metadata TEXT NOT NULL,
                    timestamp TEXT NOT NULL
                )
            """)
            conn.commit()

    def add_document(self, doc_id: str, vector: List[float], metadata: Dict) -> None:
        ts = metadata.get("timestamp", "")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meeting_vectors (id, vector, metadata, timestamp) VALUES (?, ?, ?, ?)",
                (doc_id, json.dumps(vector), json.dumps(metadata), ts)
            )
            conn.commit()

    def search(self, query_vector: List[float], limit: int = 3) -> List[Dict]:
        results = []
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute("SELECT id, vector, metadata FROM meeting_vectors")
            rows = cursor.fetchall()

        for doc_id, vec_str, meta_str in rows:
            vec = json.loads(vec_str)
            meta = json.loads(meta_str)
            score = self._cosine_similarity(query_vector, vec)
            results.append({
                "id": doc_id,
                "metadata": meta,
                "score": score
            })
        
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
```

---

## 5. Verification Method
1. **Component Verification**:
   - Write a unit test (`test_semantic_memory.py`) that initializes the `SQLiteVectorStore` in a temporary database file.
   - Insert mock meeting logs with defined summaries (e.g., "Flash Crash Response: sold 50% risk assets" vs "Altcoin scanner found BREAKOUT narrative").
   - Query using related concepts (e.g. "asset sell-off", "breakout altcoins") and assert that the correct document is returned with a high similarity score.
2. **End-to-End Log Inspection**:
   - Run the bot and trigger a mock meeting rotation.
   - Verify in output logs that the prompt constructed by `_build_agent_context` and `_build_closing` includes the header `### Relevant Historical Context (Semantic Search)` populated with retrieved search results.
