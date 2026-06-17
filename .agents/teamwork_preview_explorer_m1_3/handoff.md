# Meeting Investigation and Vector Search Integration Handoff

## 1. Observation
We conducted a detailed code investigation within the `discord-bridge` service. Here are our exact observations:

### Current Meeting Flow
* **File Path**: `discord-bridge/bot/meetings.py`
  * **Line 187 (`run_meeting`)**: The engine accepts `memory_context: str = ""`.
  * **Lines 214-218 (Round 1 Participants)**: Passes `memory_context` when building the agent context:
    ```python
    context = self._build_agent_context(
        agent_id, mt, conversation_log,
        price_data, portfolio_summary, ceo_directives, memory_context,
        is_debate_round=False,
    )
    ```
  * **Lines 244-248 (Round 2 Debate)**: Also passes `memory_context` to the debate round.
  * **Lines 493-494 (`_build_agent_context`)**: Injects the memory context into the prompt:
    ```python
    if memory_context:
        user_content_parts.append(f"### Recent Meeting History\n{memory_context}")
    ```
  * **Lines 523-556 (`_build_closing`)**: The facilitator's closing prompt builder does **not** accept or inject `memory_context` at all.

### Memory & Persistence
* **File Path**: `discord-bridge/bot/memory.py`
  * **Line 124 (`get_recent_context`)**: Retrieves a chronological list of the last `n` meetings (default 3) from local storage:
    ```python
    def get_recent_context(self, n: int = 3) -> str:
        recent = self._meetings[-n:]
        if not recent:
            return "No prior meetings on record."
        lines: list[str] = []
        for m in recent:
            ts = m.get("timestamp", "?")
            mtype = m.get("type", "?")
            summary = m.get("summary", "—")
            lines.append(f"• [{ts}] {mtype} — {summary}")
        return "\n".join(lines)
    ```
  * **Lines 104-110 (`save_meeting`)**: Trim logic moves old meetings beyond 5 into a single string `self._rolling_summary`, which is not queried by `get_recent_context()`.
* **File Path**: `discord-bridge/bot/scheduler.py`
  * **Lines 155-160 (`_execute_meeting`)**: The scheduler retrieves the context chronologically:
    ```python
    memory_context = ""
    try:
        memory_context = meeting_memory.get_recent_context()
    except Exception:
        logger.exception("Failed to load memory context")
    ```

### Meeting Logs Shape
* **File Path**: `discord-bridge/data/meeting_log.json`
  * Meetings are saved in a JSON array under the key `"meetings"`. A typical record matches the following:
    ```json
    {
      "id": "4853475d-f3a4-40b1-b6b9-61a1cddf0322",
      "type": "risk_review",
      "timestamp": "2026-06-15T07:00:57.913282+00:00",
      "summary": "**Risk Review Summary**\n\n**Key Perspectives:**...",
      "agent_contributions": { ... },
      "decisions": [ "See facilitator summary for details." ],
      "actions": [ "See facilitator summary for details." ]
    }
    ```
  * Due to loose formatting output by LLMs, the `decisions` and `actions` fields often fallback to `"See facilitator summary for details."`, indicating the primary source of semantic information lies in the `summary` block.

---

## 2. Logic Chain
1. **Limitation of chronological sliding window**: The current implementation slice `self._meetings[-3:]` only maintains immediate context. If a crucial decision or strategy was finalized 4 meetings ago, it disappears from the context.
2. **Facilitator blindness**: Because `_build_closing` does not receive `memory_context`, the facilitator has zero historical memory. This can lead to duplicate decisions or redundant action items (e.g. re-booking a meeting or repeatedly selling an asset).
3. **Data richness**: The meeting summary is highly detailed. To enable accurate vector matching, the document representation embedded should combine the meeting type, summary, decisions, and actions.
4. **Integration**: We must introduce clean abstractions to perform embeddings and vector database search, maintaining separation of concerns while staying compatible with the existing `meeting_log.json` file.

---

## 3. Caveats
* **Network Restrictions**: Since the bot runs under local or air-gapped environments, the Embedding Service must fallback gracefully (e.g., to a mock or local library) if the OpenAI API endpoint is unavailable.
* **Token Budget limits**: `config/settings.yaml` defines a `meeting_history` budget of 500 tokens. Detailed summaries of the top 3 semantically relevant meetings might exceed this limit. We must truncate summaries or increase the token limit.

---

## 4. Conclusion & Interface Contracts

### 4.1 Prompt Injection Strategy

#### Search Query Construction
Formulate the search query using the current meeting's parameters to retrieve relevant historical records:
```python
query_text = f"Meeting Type: {mt.name}. Focus: {mt.focus}. CEO Directives: {ceo_directives}"
```

#### Formatting of Top 3 Historical Meetings
Retrieve the top 3 matches and format them with similarity scores and explicit headers:
```
### Relevant Historical Context (Semantic Search)
• [Date: 2026-06-15T07:00:57Z] Meeting: risk_review (Relevance: 0.89)
  - Summary: Executed a partial profit take (50%) to de-risk from overextension.
  - Decisions: Sell 50% BTC and ETH.
  - Actions: Trader to execute sell orders.

• [Date: ...] ...
```

#### Injection Points
1. **Participant Context (`_build_agent_context` in `bot/meetings.py`)**:
   Replace `### Recent Meeting History` with `### Relevant Historical Context (Semantic Search)` using the formatted results.
2. **Facilitator Context (`_build_closing` in `bot/meetings.py`)**:
   Add `semantic_memory_context` to the parameters and append it to `closing_prompt`:
   ```python
   f"### Relevant Prior Decisions & Actions\n"
   f"Refer to these prior meetings to ensure continuity and avoid duplicate decisions:\n"
   f"{semantic_memory_context}\n\n"
   ```

---

### 4.2 Interface Contracts (Design Plan)

We propose the following three decoupled interfaces for vector search integration:

#### A. Embedding Service Protocol
Defines the client for vector generation.
```python
from typing import List, Protocol

class EmbeddingService(Protocol):
    """Abstraction for generating text embeddings."""

    async def get_embedding(self, text: str) -> List[float]:
        """Generate a vector embedding list for the input text."""
        ...
```

#### B. Vector Store Protocol
Abstracts storage and similarity search.
```python
from typing import Dict, List, Optional, Protocol

class VectorStore(Protocol):
    """Abstraction for vector database operations."""

    async def add_document(self, doc_id: str, vector: List[float], metadata: Dict) -> None:
        """Insert or update a document embedding and its metadata."""
        ...

    async def search(self, query_vector: List[float], limit: int = 3) -> List[Dict]:
        """
        Perform cosine similarity search and return top matching documents.
        Returns:
            List of dicts: [{"id": str, "metadata": Dict, "score": float}]
        """
        ...

    async def save(self) -> None:
        """Persist vector index to disk (if file-backed)."""
        ...

    async def load(self) -> None:
        """Load vector index from disk."""
        ...
```

#### C. Semantic Meeting Memory Manager
Coordinates the interaction between raw JSON logs, the embedding service, and the vector store.
```python
class SemanticMeetingMemory:
    """Orchestrates indexing and retrieval of semantic meeting records."""

    def __init__(
        self,
        embedding_service: EmbeddingService,
        vector_store: VectorStore,
        meeting_memory: MeetingMemory
    ):
        self.embedding_service = embedding_service
        self.vector_store = vector_store
        self.meeting_memory = meeting_memory

    async def index_meeting(self, meeting_record: dict) -> None:
        """Index a newly finalized meeting's summary and decisions."""
        # Save raw JSON first
        self.meeting_memory.save_meeting(meeting_record)

        # Build search text document
        decisions_str = ", ".join(meeting_record.get("decisions", []))
        actions_str = ", ".join(meeting_record.get("actions", []))
        search_text = (
            f"Type: {meeting_record['type']}\n"
            f"Summary: {meeting_record['summary']}\n"
            f"Decisions: {decisions_str}\n"
            f"Actions: {actions_str}"
        )
        
        vector = await self.embedding_service.get_embedding(search_text)
        metadata = {
            "type": meeting_record["type"],
            "timestamp": meeting_record["timestamp"],
            "summary": meeting_record["summary"],
            "decisions": meeting_record.get("decisions", []),
            "actions": meeting_record.get("actions", []),
        }
        await self.vector_store.add_document(meeting_record["id"], vector, metadata)
        await self.vector_store.save()

    async def get_semantic_context(self, query_text: str, limit: int = 3) -> str:
        """Retrieve and format the top N most similar prior meetings."""
        query_vector = await self.embedding_service.get_embedding(query_text)
        results = await self.vector_store.search(query_vector, limit=limit)

        if not results:
            return "No prior relevant meetings found."

        lines = []
        for res in results:
            m = res["metadata"]
            score = res["score"]
            decisions = "; ".join(m.get("decisions", [])) or "None"
            actions = "; ".join(m.get("actions", [])) or "None"
            lines.append(
                f"• [Date: {m['timestamp']}] Meeting: {m['type']} (Relevance: {score:.2f})\n"
                f"  Summary: {m['summary']}\n"
                f"  Decisions: {decisions}\n"
                f"  Actions: {actions}"
            )
        return "\n\n".join(lines)
```

---

## 5. Verification Method
1. **Codebase Inspection**:
   * Confirm the paths and locations cited in this document by reading:
     * `discord-bridge/bot/meetings.py`
     * `discord-bridge/bot/memory.py`
     * `discord-bridge/bot/scheduler.py`
2. **Context Integrity Verification**:
   * Once implemented, execute `python -m bot.main` (or run a scheduled mock meeting flow) and inspect output prompts in logging to ensure `### Relevant Historical Context (Semantic Search)` is populated correctly.
