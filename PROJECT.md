# Project: OTTR HFT Semantic Memory

## Architecture
- **Vector Database**: Integration of a local vector database (e.g. ChromaDB) or custom vector/embedding storage solution in `bot/memory.py`.
- **Embeddings**: Generation of vector embeddings for meetings using an embedding provider (e.g., local model or OpenAI embeddings via existing connector).
- **Semantic Injection**: Integration inside `bot/meetings.py` during `run_meeting`, querying the vector database using current market conditions and injecting the resulting context.
- **Verification**: Programmatic test script `test_semantic_memory.py` validating that insertion and query retrieval operate correctly.

## Code Layout
- `discord-bridge/bot/memory.py`: Vector database logic, embedding generation, meeting persistence.
- `discord-bridge/bot/meetings.py`: Orchestration of meetings, embedding current market conditions, semantic context injection.
- `discord-bridge/test_semantic_memory.py`: Vector memory validation script.

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|---|---|---|---|
| M1 | Exploration | Analyze codebase, libraries, and design embedding/vector DB strategy | None | DONE |
| M2 | Vector DB Implementation | Implement ChromaDB/vector storage integration in `bot/memory.py` | M1 | DONE |
| M3 | Semantic Context Injection | Integrate vector DB querying and context injection in `bot/meetings.py` | M2 | DONE |
| M4 | Vector Memory Verification | Implement `test_semantic_memory.py` and verify all requirements | M3 | DONE |
| M5 | Adversarial & Integrity Audit | Final verification with Challenger and Forensic Auditor | M4 | DONE |

## Interface Contracts
### `bot/memory.py`
- `MeetingMemory.save_meeting(meeting_record: dict) -> None`: Expanded to also generate embeddings for the meeting summary/details and insert them into the vector database.
- `MeetingMemory.query_similar_meetings(query_text: str, n: int = 3) -> List[dict]`: Computes embedding for `query_text` and searches the vector DB for the top `n` most similar historical meetings.

### `bot/meetings.py`
- In `MeetingEngine.run_meeting()`: Before running a meeting, embed current market state (`price_data`, etc.) and call `query_similar_meetings` to get the top 3 historical meetings. Format and pass them via `memory_context` to `_build_agent_context`.
