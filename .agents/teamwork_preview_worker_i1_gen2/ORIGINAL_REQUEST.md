## 2026-06-15T23:14:35Z
You are teamwork_preview_worker_i1_gen2.
Your working directory is d:\crypto-trading-bot\.agents\teamwork_preview_worker_i1_gen2.
Your task is to fix critical bugs, architectural gaps, and testing facades in the SQLite vector database implementation.

Please apply the following changes:

1. In `discord-bridge/bot/agents.py`:
   - Refactor `AgentLLM.__init__` to NOT initialize `self._lock = asyncio.Lock()` at import time (which binds to the wrong event loop).
   - Lazily initialize `self._lock` inside the `lock` property or at the beginning of `generate_response` (e.g., check `if self._lock is None: self._lock = asyncio.Lock()`). Ensure it is loop-safe.

2. In `discord-bridge/bot/memory.py`:
   - Change `SemanticMeetingMemory`'s signature and implementation to make all vector DB/embeddings methods ASYNCHRONOUS:
     * `async def index_meeting(self, meeting_record: dict) -> None`
     * `async def get_semantic_context(self, query_text: str, limit: int = 3) -> str`
     * `async def save_meeting(self, meeting_record: dict) -> None`
     * `async def query_similar_meetings(self, query_text: str, n: int = 3) -> List[dict]`
   - Inside these methods, call `await agent_llm.generate_embedding(text)` (import `agent_llm` from `bot.agents` to generate the 768-dimensional float list embedding). This avoids all synchronous/blocking HTTP client network calls and runs lock-free.
   - Implement `query_similar_meetings(self, query_text: str, n: int = 3) -> List[dict]` which gets the query embedding and returns the list of raw meeting dicts (retrieved from `vector_store.search`).
   - Add a lazy `self._lock = None` inside `SemanticMeetingMemory` and create `asyncio.Lock()` lazily inside an async lock property or the methods. Acquire this lock around both JSON-file write operations (e.g. `super().save_meeting` / `super().save`) and SQLite DB operations inside `save_meeting` and `index_meeting` to guarantee thread/concurrency safety.
   - In `SQLiteVectorStore.search`, raise `ValueError` (or a subclass) when the dimension of the queried vector does not match the database vector length, instead of silently continuing.

3. In `discord-bridge/bot/meetings.py`:
   - Change `meeting_memory.save_meeting(meeting_record)` to `await meeting_memory.save_meeting(meeting_record)`.
   - Update `MeetingEngine.run_meeting` to dynamically perform the semantic search and format the results if `memory_context` is empty/not provided:
     ```python
     if not memory_context:
         query_text = price_data or "general market state"
         try:
             similar = await meeting_memory.query_similar_meetings(query_text, n=3)
             if similar:
                 lines = []
                 for m in similar:
                     ts = m.get("timestamp", "?")
                     mtype = m.get("type", "?")
                     summary = m.get("summary", "—")
                     lines.append(f"• [{ts}] {mtype} — {summary}")
                 memory_context = "\n".join(lines)
             else:
                 memory_context = "No prior meetings on record."
         except Exception:
             logger.exception("Failed to query similar meetings")
             memory_context = "No prior meetings on record."
     ```

4. In `discord-bridge/bot/scheduler.py`:
   - Update `memory_context = meeting_memory.get_recent_context()` to query the semantic database asynchronously:
     ```python
     memory_context = ""
     try:
         query_text = f"Meeting Type: {meeting_type}. Market State: {price_str}. Directives: {ceo_directives}"
         memory_context = await meeting_memory.get_semantic_context(query_text, limit=3)
     except Exception:
         logger.exception("Failed to load semantic memory context")
         try:
             memory_context = meeting_memory.get_recent_context()
         except Exception:
             pass
     ```

5. In `discord-bridge/test_semantic_memory.py`:
   - Update the existing test mocks and calls to handle the now-asynchronous methods (`await save_meeting` and `await run_meeting`).
   - Add new unit tests `TestSQLiteVectorStore` and `TestSemanticMeetingMemory` at the end of the file. These tests must NOT monkeypatch the database classes. They should test `SQLiteVectorStore` and `SemanticMeetingMemory` directly using a real temporary database file (created in a temp directory) and verify:
     1. Storing and retrieving vectors in `SQLiteVectorStore`.
     2. Throwing exceptions on vector dimension mismatch.
     3. Running `save_meeting`, `index_meeting`, and `get_semantic_context` on `SemanticMeetingMemory` (mocking out only the `agent_llm.generate_embedding` call to return a mock vector).
     4. Concurrency safety when calling `save_meeting` in parallel.
