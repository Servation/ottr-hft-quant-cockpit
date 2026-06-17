# Scope: OTTR HFT Semantic Memory Implementation Track

## Architecture
- **Vector Storage**: pure-Python + SQLite-backed vector store using standard library `sqlite3` in `bot/memory.py`.
- **Embeddings**: generation of 768-dimensional embeddings by calling `client.embeddings.create` on `AsyncOpenAI` client (which routes to LM-Studio's loaded model).
- **Orchestration integration**: query vector DB using current market state, format retrieved top 3 historical meetings, and pass/inject this context into agent contexts in `bot/meetings.py` and `bot/scheduler.py`.

## Code Layout
- `discord-bridge/bot/memory.py` (implementation of database and retrieval logic)
- `discord-bridge/bot/meetings.py` (injection of semantic context)
- `discord-bridge/bot/scheduler.py` (passing query and context to meetings)

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|---|---|---|---|
| I1 | Database Integration | Implement SQLite-backed vector database and embedding generator in `bot/memory.py` | None | DONE |
| I2 | Prompt Injection | Integrate semantic search query, retrieval, and prompt injection in `bot/meetings.py` / `bot/scheduler.py` | I1 | DONE |
| I3 | E2E Testing Integration | Execute full tests from the E2E Testing Track and verify compliance | I2 | DONE |

## Interface Contracts
### SQLiteVectorStore
- `add_document(doc_id: str, vector: List[float], metadata: dict) -> None`
- `search(query_vector: List[float], limit: int = 3) -> List[dict]`

### SemanticMeetingMemory
- `index_meeting(meeting_record: dict) -> None`
- `get_semantic_context(query_text: str, limit: int = 3) -> str`
