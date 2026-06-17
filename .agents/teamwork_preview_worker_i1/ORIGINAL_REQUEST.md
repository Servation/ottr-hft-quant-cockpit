## 2026-06-15T23:08:24Z
You are teamwork_preview_worker_i1.
Your working directory is d:\crypto-trading-bot\.agents\teamwork_preview_worker_i1.
Your task is to implement the SQLite-backed vector database and embedding generation in `discord-bridge/bot/memory.py` and `discord-bridge/bot/agents.py`.

Specifically, you need to make the following changes:
1. In `discord-bridge/bot/agents.py`:
   - Add a method `async def generate_embedding(self, text: str | list[str], model: Optional[str] = None) -> list[float] | list[list[float]]` to the `AgentLLM` class.
   - This method MUST NOT acquire `self._lock` (which chat completions use).
   - It should call `self._client.embeddings.create` with `model=model or settings.get("embedding_model_id", "text-embedding-ada-002")` (and handle standard errors gracefully, e.g. returning empty lists/vectors on failure).
   
2. In `discord-bridge/bot/memory.py`:
   - Implement `SQLiteVectorStore` class with these methods:
     * `__init__(self, db_path: Path) -> None`
     * `_init_db(self) -> None` (creates the SQLite DB and table `meeting_vectors` if not exists, containing fields: `doc_id` PRIMARY KEY, `vector` TEXT, `metadata` TEXT)
     * `add_document(self, doc_id: str, vector: List[float], metadata: dict) -> None` (atomic insert or replace)
     * `search(self, query_vector: List[float], limit: int = 3) -> List[dict]` (reads all documents, computes cosine similarity using pure Python math/zip, sorts descending, and returns top `limit` results)
   
   - Implement `SemanticMeetingMemory` class which inherits from `MeetingMemory`. It should support:
     * `__init__(self) -> None` (calls `super().__init__()`, initializes database path at `data/meeting_vectors.db`, instantiates `SQLiteVectorStore`, and instantiates a synchronous `OpenAI` client pointing to LM-Studio base URL)
     * `index_meeting(self, meeting_record: dict) -> None` (concatenates meeting type, summary, decisions, and action items into a clean textual representation; calls the synchronous `OpenAI` client to get the embedding; and inserts the document into the vector store)
     * `get_semantic_context(self, query_text: str, limit: int = 3) -> str` (gets query embedding via the synchronous client, searches the vector store, and returns a formatted string of the top results)
     * `save_meeting(self, meeting_record: dict) -> None` (overrides/extends parent: calls `super().save_meeting(meeting_record)` to preserve the JSON file operations, and then calls `self.index_meeting(meeting_record)`)
     
   - Ensure the module-level singleton `meeting_memory` is instantiated as `SemanticMeetingMemory()`.

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A Forensic Auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

Please implement these changes cleanly. When done, write your completion report to `d:\crypto-trading-bot\.agents\teamwork_preview_worker_i1\handoff.md` and send a message to me (parent conversation ID: 0a025cb7-2b5e-4468-99e2-279a0645c18d) with the path.
