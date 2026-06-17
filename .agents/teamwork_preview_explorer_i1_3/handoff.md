# Handoff Report — Meeting Memory and SQLite Vector Database Integration

This report details the findings and recommendations regarding how meeting logs are recorded and stored, how embedding input texts should be formatted, and how the system should integrate an SQLite vector database while fully preserving the existing JSON storage.

---

## 1. Observation

Our code investigation within `discord-bridge` revealed the following exact behaviors:

### 1.1 Meeting Recording and JSON Storage
* **File Path**: `d:\crypto-trading-bot\discord-bridge\bot\memory.py`
* **Instantiation**: `MeetingMemory` is instantiated as a module-level singleton on **line 200**:
  ```python
  meeting_memory = MeetingMemory()
  ```
* **Storage and Persistence**:
  * The class holds memory state in `self._meetings: List[dict]`, `self._decisions: List[dict]`, and `self._rolling_summary: str` (Lines 41-43).
  * It loads state from `data/meeting_log.json` on initialization (Lines 48-67).
  * **Line 96 (`save_meeting`)**: Records a meeting dict and trims older meetings:
    ```python
    def save_meeting(self, meeting_record: dict) -> None:
        self._meetings.append(meeting_record)

        # Trim: condense oldest meetings into rolling_summary
        while len(self._meetings) > MAX_FULL_MEETINGS:
            oldest = self._meetings.pop(0)
            condensed = self._condense_meeting(oldest)
            if self._rolling_summary:
                self._rolling_summary += "\n---\n" + condensed
            else:
                self._rolling_summary = condensed

        self.save()
    ```
  * **Lines 69-92 (`save`)**: Uses an atomic write pattern by writing the JSON payload to a temporary file (`prefix="meeting_log_"`, `suffix=".tmp"`) in the same directory (`data/`) and then replacing `meeting_log.json` using `os.replace`.

### 1.2 Meeting Record Creation
* **File Path**: `d:\crypto-trading-bot\discord-bridge\bot\meetings.py`
* **Lines 271-282 (`run_meeting` - persistence step)**:
  ```python
  # ---- 5. Persist meeting record -------------------------------------
  meeting_record = MeetingMemory.make_meeting_record(
      meeting_type=mt.id,
      summary=closing_msg[:300],
      agent_contributions=agent_contributions,
      decisions=self._extract_decisions(closing_msg),
      actions=self._extract_actions(closing_msg),
  )
  meeting_memory.save_meeting(meeting_record)
  ```
* **Summary Limitation**: Note that `summary` is populated with only the first 300 characters of the facilitator's closing message (`closing_msg[:300]`).
* **Extraction Fallbacks**:
  * **Lines 561-578**: If `_extract_decisions` or `_extract_actions` fails to parse bullet points starting with `decision:` or `action:`, they default to `["See facilitator summary for details."]`.

---

## 2. Logic Chain

1. **Truncation of Summary**: Because `meetings.py` records `summary` as `closing_msg[:300]`, the `summary` field is too short to capture the full semantic breadth of a complex meeting. Therefore, formatting embedding input text *cannot* rely solely on the `summary` field; it must extract the full facilitator's closing message from the `agent_contributions` dictionary.
2. **Facilitator Message Extraction**: The facilitator's contribution matches the full `closing_msg`. Since `summary` is exactly the first 300 characters of `closing_msg`, we can search `agent_contributions` for the value that starts with the `summary` prefix to reliably extract the full closing message.
3. **Decisions and Actions Cleanup**: If `decisions` or `actions` contains the fallback string `["See facilitator summary for details."]`, using it verbatim in the embedding input creates redundant noise. The formatter should identify this fallback and replace it with a cleaner note, ensuring the embedding model focuses on the actual details in the facilitator's closing message.
4. **Dual-Write Architecture**:
   * **JSON preservation**: The JSON file is the human-readable source of truth and maintains compatibility for current sliding-window mechanisms (`_meetings[-3:]`).
   * **SQLite augmentation**: The SQLite database functions as an append-only long-term semantic store.
   * **Decoupled execution**: When `save_meeting` is called, it should first write to JSON (synchronously, as before). Then, it should trigger vector database indexing asynchronously. This ensures that a failure in the embedding service (e.g., local LM-Studio offline) does not block saving the meeting or disrupt bot operations.
5. **Startup Self-Healing**: To ensure the SQLite store is synchronized with the JSON file, the initialization flow should reconcile the two stores. If any meeting ID in `meeting_log.json` is missing from the SQLite database, it should be automatically indexed.

---

## 3. Caveats

* **Event Loop Availability**: `save_meeting()` is synchronous, but the bot runs inside an `asyncio` event loop. When launching the async embedding task from `save_meeting()`, we must check if an event loop is running (using `asyncio.get_event_loop()`). If it is running, we can schedule the task using `loop.create_task()`. If not (e.g., in a synchronous test suite or setup script), it should fall back to a synchronous run or a thread executor.
* **Vector Dimension Volatility**: Local models loaded in LM-Studio can change (e.g., switching between Nomic's 768-dimensional model and OpenAI's 1536-dimensional Ada model). The SQLite database should store the model ID alongside the vectors, and similarity calculations must handle dimension mismatches gracefully.
* **Rolling Summary Exclusions**: Once a meeting is popped from `self._meetings` and appended to the text-only `self._rolling_summary` in JSON, the raw structured record is lost from the JSON file. However, it will remain permanently indexed in the SQLite vector database. Re-indexing on startup is therefore limited to the meetings still present in the JSON `meetings` array.

---

## 4. Conclusion & Recommendations

### 4.1 Embedding Input Text Formatting
We recommend the following function to format a meeting record into the embedding input text:

```python
def format_meeting_for_embedding(meeting_record: dict) -> str:
    """
    Concatenates and formats meeting fields into a standardized, rich representation
    specifically structured for high-quality semantic vector search.
    """
    meeting_id = meeting_record.get("id", "unknown")
    meeting_type = meeting_record.get("type", "unknown")
    timestamp = meeting_record.get("timestamp", "unknown")
    
    # Format decisions list
    decisions = meeting_record.get("decisions", [])
    if not decisions or decisions == ["See facilitator summary for details."]:
        decisions_str = "None explicitly extracted (refer to closing message below)"
    else:
        decisions_str = "\n".join(f"- {d}" for d in decisions)
        
    # Format action items list
    actions = meeting_record.get("actions", [])
    if not actions or actions == ["See facilitator summary for details."]:
        actions_str = "None explicitly extracted (refer to closing message below)"
    else:
        actions_str = "\n".join(f"- {a}" for a in actions)
        
    # Extract facilitator full closing message
    summary_prefix = meeting_record.get("summary", "")
    agent_contributions = meeting_record.get("agent_contributions", {})
    facilitator_msg = ""
    other_contributions = []
    
    for agent_id, text in agent_contributions.items():
        if summary_prefix and text.startswith(summary_prefix):
            facilitator_msg = text
        else:
            # Keep other agent contributions truncated to avoid diluting semantic focus
            truncated_text = text if len(text) <= 500 else text[:500] + "..."
            other_contributions.append(f"  * {agent_id}: {truncated_text}")
            
    # Fallback if facilitator message is not matched by prefix
    if not facilitator_msg and agent_contributions:
        sorted_contribs = sorted(agent_contributions.items(), key=lambda x: len(x[1]), reverse=True)
        facilitator_msg = sorted_contribs[0][1]
        other_contributions = [
            f"  * {k}: {v if len(v) <= 500 else v[:500] + '...'}" 
            for k, v in sorted_contribs[1:]
        ]
        
    other_contribs_str = "\n".join(other_contributions) if other_contributions else "None"
    
    # Formulate structured text block
    formatted_text = (
        f"Meeting Type: {meeting_type}\n"
        f"Timestamp: {timestamp}\n"
        f"Meeting ID: {meeting_id}\n\n"
        f"Facilitator Closing Summary:\n{facilitator_msg.strip()}\n\n"
        f"Decisions:\n{decisions_str}\n\n"
        f"Action Items:\n{actions_str}\n\n"
        f"Agent Contributions:\n{other_contribs_str}"
    )
    return formatted_text
```

### 4.2 SQLite Database Schema
The database should reside at `data/meeting_vectors.db` and use the following schema:

```sql
CREATE TABLE IF NOT EXISTS meeting_vectors (
    id TEXT PRIMARY KEY,          -- Matches the meeting UUID from JSON
    vector TEXT NOT NULL,         -- JSON-serialized float list of the embedding
    metadata TEXT NOT NULL,       -- JSON-serialized metadata (type, timestamp, summary, decisions, actions)
    timestamp TEXT NOT NULL,      -- UTC ISO-8601 string for sorting/filtering
    model_id TEXT NOT NULL        -- Embedding model name used (e.g. nomic-embed-text-v1.5)
);
```

### 4.3 Augmented Memory Singleton Class Design
We recommend subclassing `MeetingMemory` as `SemanticMeetingMemory` to inherit JSON persistence capabilities and extend them with SQLite database interactions:

```python
import sqlite3
import json
import logging
import asyncio
from pathlib import Path
from bot.memory import MeetingMemory

logger = logging.getLogger(__name__)

class SemanticMeetingMemory(MeetingMemory):
    """
    Subclass of MeetingMemory that extends standard JSON storage with 
    SQLite vector storage, providing dual-write persistence and semantic search.
    """
    
    def __init__(self, db_path: Path, embedding_service) -> None:
        super().__init__()
        self.db_path = db_path
        self.embedding_service = embedding_service
        self._init_db()
        
        # Trigger async self-healing startup task
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self.reconcile_vector_store())
            else:
                asyncio.run(self.reconcile_vector_store())
        except Exception:
            logger.exception("Failed to schedule startup vector store reconciliation")

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS meeting_vectors (
                    id TEXT PRIMARY KEY,
                    vector TEXT NOT NULL,
                    metadata TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    model_id TEXT NOT NULL
                )
            """)
            conn.commit()

    def save_meeting(self, meeting_record: dict) -> None:
        """
        Dual-write pattern: Save to JSON synchronously, then schedule
        SQLite vector store indexing in the background.
        """
        # 1. Preserves existing JSON file write
        super().save_meeting(meeting_record)
        
        # 2. Asynchronously index the meeting record in SQLite
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self.index_meeting_async(meeting_record))
            else:
                # Synchronous fallback for scripts / tests
                asyncio.run(self.index_meeting_async(meeting_record))
        except Exception as exc:
            logger.error("Failed to trigger background vector indexing: %s", exc)

    async def index_meeting_async(self, meeting_record: dict) -> None:
        """Helper to generate embeddings and write to SQLite."""
        try:
            doc_id = meeting_record["id"]
            text_to_embed = format_meeting_for_embedding(meeting_record)
            
            # Generate embedding via LM-Studio or OpenAI client
            vector = await self.embedding_service.get_embedding(text_to_embed)
            model_id = getattr(self.embedding_service, "model_id", "unknown")
            
            metadata = {
                "type": meeting_record.get("type"),
                "timestamp": meeting_record.get("timestamp"),
                "summary": meeting_record.get("summary"),
                "decisions": meeting_record.get("decisions", []),
                "actions": meeting_record.get("actions", []),
            }
            
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO meeting_vectors (id, vector, metadata, timestamp, model_id) VALUES (?, ?, ?, ?, ?)",
                    (doc_id, json.dumps(vector), json.dumps(metadata), meeting_record["timestamp"], model_id)
                )
                conn.commit()
            logger.info("Successfully indexed meeting %s in SQLite vector DB.", doc_id)
        except Exception as exc:
            logger.error("Error indexing meeting %s in SQLite vector DB: %s", meeting_record.get("id"), exc)

    async def reconcile_vector_store(self) -> None:
        """
        Startup Self-Healing: Query database for existing keys and
        index any JSON-stored meetings that are missing in SQLite.
        """
        logger.info("Starting vector store reconciliation...")
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.execute("SELECT id FROM meeting_vectors")
                indexed_ids = {row[0] for row in cursor.fetchall()}
        except Exception as exc:
            logger.error("Failed to query SQLite during reconciliation: %s", exc)
            return

        for meeting in self._meetings:
            meeting_id = meeting.get("id")
            if meeting_id and meeting_id not in indexed_ids:
                logger.info("Reconciliation: Found missing meeting %s. Indexing...", meeting_id)
                await self.index_meeting_async(meeting)
        logger.info("Vector store reconciliation complete.")
```

---

## 5. Verification Method

To verify this design and integration independently:

1. **Unit Test Verification**:
   * Create `discord-bridge/tests/test_semantic_memory.py` with mock implementations of the `EmbeddingService`.
   * Initialize `SemanticMeetingMemory` with a temporary SQLite file.
   * Call `save_meeting(meeting_record)` with a mock meeting and verify:
     1. The meeting is present in `meeting_log.json`.
     2. An entry exists in the SQLite table `meeting_vectors` with matching `id`, valid JSON vector string, and correct timestamp/metadata.
2. **Reconciliation Test**:
   * Add a meeting directly to the `meetings` array in `meeting_log.json` while the SQLite database is empty.
   * Reinitialize the `SemanticMeetingMemory` and verify that the reconciliation startup routine detects the missing record, triggers embedding generation, and adds it to SQLite.
3. **Database Inspection**:
   * Run standard SQLite CLI command on the target file:
     ```powershell
     sqlite3 data/meeting_vectors.db "SELECT id, timestamp, model_id FROM meeting_vectors;"
     ```
