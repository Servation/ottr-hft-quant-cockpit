"""
Meeting memory and decision log for the discord-bridge service.

Persists meeting records and decisions to data/meeting_log.json with
atomic writes (temp file + os.replace).  Keeps the last 5 full meetings
and condenses older ones into a rolling summary string.
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
LOG_PATH = DATA_DIR / "meeting_log.json"

MAX_FULL_MEETINGS = 5


class MeetingMemory:
    """
    Manages the persistent meeting log, decision log, and rolling summary.

    Data shape on disk::

        {
            "meetings": [ ... ],
            "decisions": [ ... ],
            "rolling_summary": "..."
        }
    """

    def __init__(self) -> None:
        self._meetings: List[dict] = []
        self._decisions: List[dict] = []
        self._rolling_summary: str = ""
        self.load()

    # -- persistence --------------------------------------------------------

    def load(self) -> None:
        """Load state from disk (or initialize empty)."""
        if LOG_PATH.exists():
            try:
                data = json.loads(LOG_PATH.read_text(encoding="utf-8"))
                self._meetings = data.get("meetings", [])
                self._decisions = data.get("decisions", [])
                self._rolling_summary = data.get("rolling_summary", "")
                logger.info(
                    "Loaded meeting memory: %d meetings, %d decisions",
                    len(self._meetings),
                    len(self._decisions),
                )
            except (json.JSONDecodeError, KeyError) as exc:
                logger.warning("Corrupt meeting log, resetting: %s", exc)
                self._meetings = []
                self._decisions = []
                self._rolling_summary = ""
        else:
            logger.info("No existing meeting log — starting fresh.")

    def save(self) -> None:
        """Atomic write: serialize to temp file then os.replace."""
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        payload = {
            "meetings": self._meetings,
            "decisions": self._decisions,
            "rolling_summary": self._rolling_summary,
        }

        # Write to temp in the same directory so os.replace is atomic
        fd, tmp_path = tempfile.mkstemp(
            dir=str(DATA_DIR), suffix=".tmp", prefix="meeting_log_"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, str(LOG_PATH))
            logger.debug("Meeting log saved (%d meetings).", len(self._meetings))
        except Exception:
            # Clean up orphaned temp file on failure
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    # -- meetings -----------------------------------------------------------

    def save_meeting(self, meeting_record: dict) -> None:
        """
        Append a meeting record, trim older meetings beyond
        MAX_FULL_MEETINGS into the rolling summary, and persist.
        """
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

    @staticmethod
    def _condense_meeting(meeting: dict) -> str:
        """Reduce a full meeting record to a compact summary line."""
        ts = meeting.get("timestamp", "?")
        mtype = meeting.get("type", "?")
        summary = meeting.get("summary", "No summary.")
        decisions = meeting.get("decisions", [])
        decision_str = "; ".join(decisions) if decisions else "None"
        return f"[{ts}] {mtype}: {summary} | Decisions: {decision_str}"

    def get_recent_context(self, n: int = 3) -> str:
        """
        Return the last *n* meeting summaries formatted as text,
        suitable for injecting into an LLM context window.
        """
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

    # -- decisions ----------------------------------------------------------

    def save_decision(self, decision_record: dict) -> None:
        """Append a decision record and persist."""
        self._decisions.append(decision_record)
        self.save()

    def get_decision_log(self, n: int = 10) -> List[dict]:
        """Return the last *n* decision records."""
        return self._decisions[-n:]

    # -- rolling summary ----------------------------------------------------

    def get_rolling_summary(self) -> str:
        """Return the condensed rolling summary of older meetings."""
        return self._rolling_summary or "No older meeting history yet."

    # -- factory helpers ----------------------------------------------------

    @staticmethod
    def make_meeting_record(
        meeting_type: str,
        summary: str,
        agent_contributions: Dict[str, str],
        decisions: List[str],
        actions: List[str],
    ) -> dict:
        """Build a meeting record dict with a fresh UUID and timestamp."""
        return {
            "id": str(uuid4()),
            "type": meeting_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "summary": summary,
            "agent_contributions": agent_contributions,
            "decisions": decisions,
            "actions": actions,
        }

    @staticmethod
    def make_decision_record(
        decision: str,
        meeting_id: str,
        supporters: List[str],
        dissenters: Optional[List[str]] = None,
    ) -> dict:
        """Build a decision record dict with a fresh UUID and timestamp."""
        return {
            "id": str(uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "decision": decision,
            "meeting_id": meeting_id,
            "supporters": supporters,
            "dissenters": dissenters or [],
        }


# ---------------------------------------------------------------------------
# SQLite Vector Database and Semantic Memory Extensions
# ---------------------------------------------------------------------------
import sqlite3
import math

class SQLiteVectorStore:
    """
    A simple SQLite-backed vector store that persists document IDs, vectors, and metadata,
    and supports cosine similarity search.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        """Creates the SQLite DB and table meeting_vectors if it doesn't exist."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS meeting_vectors (
                    doc_id TEXT PRIMARY KEY,
                    vector TEXT,
                    metadata TEXT
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

    def add_document(self, doc_id: str, vector: List[float], metadata: dict) -> None:
        """Insert or replace a document vector and metadata atomically."""
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            vector_str = json.dumps(vector)
            metadata_str = json.dumps(metadata)
            cursor.execute(
                """
                INSERT OR REPLACE INTO meeting_vectors (doc_id, vector, metadata)
                VALUES (?, ?, ?)
                """,
                (doc_id, vector_str, metadata_str),
            )
            conn.commit()
        finally:
            conn.close()

    def search(self, query_vector: List[float], limit: int = 3) -> List[dict]:
        """
        Reads all documents, computes cosine similarity using pure Python math/zip,
        sorts descending, and returns top limit results.
        """
        if not query_vector:
            return []

        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT doc_id, vector, metadata FROM meeting_vectors")
            rows = cursor.fetchall()
        finally:
            conn.close()

        results = []
        q_norm = math.sqrt(sum(q * q for q in query_vector))
        if q_norm == 0:
            return []

        for doc_id, vector_str, metadata_str in rows:
            try:
                vector = json.loads(vector_str)
                metadata = json.loads(metadata_str)
            except Exception as exc:
                logger.error("Failed to parse vector or metadata for document %s: %s", doc_id, exc)
                continue

            if not vector:
                continue
            if len(vector) != len(query_vector):
                raise ValueError(
                    f"Query vector dimension {len(query_vector)} does not match "
                    f"database vector dimension {len(vector)}."
                )

            dot_product = sum(q * v for q, v in zip(query_vector, vector))
            v_norm = math.sqrt(sum(v * v for v in vector))

            if v_norm == 0:
                similarity = 0.0
            else:
                similarity = dot_product / (q_norm * v_norm)

            results.append({
                "doc_id": doc_id,
                "vector": vector,
                "metadata": metadata,
                "score": similarity
            })

        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:limit]


class SemanticMeetingMemory(MeetingMemory):
    """
    Extends MeetingMemory to store meeting vectors in SQLite and query them semantically.
    """

    def __init__(self) -> None:
        super().__init__()
        self.db_path = DATA_DIR / "meeting_vectors.db"
        self.vector_store = SQLiteVectorStore(self.db_path)
        self._lock: Optional[asyncio.Lock] = None
        from openai import OpenAI
        from bot import settings
        self.openai_client = OpenAI(
            base_url=settings.get("llm_base_url", "http://localhost:1234/v1"),
            api_key="lm-studio",
        )

    @property
    def lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    async def index_meeting(self, meeting_record: dict) -> None:
        """
        Concatenates meeting type, summary, decisions, and action items,
        calls agent_llm to embed, and adds it to SQLiteVectorStore.
        """
        doc_id = meeting_record.get("id")
        if not doc_id:
            logger.warning("Meeting record lacks ID, cannot index.")
            return

        mtype = meeting_record.get("type", "Unknown")
        summary = meeting_record.get("summary", "")
        decisions = meeting_record.get("decisions", [])
        actions = meeting_record.get("actions", [])

        decision_str = "\n".join(f"- {d}" for d in decisions) if decisions else "None"
        action_str = "\n".join(f"- {a}" for a in actions) if actions else "None"

        text_rep = (
            f"Meeting Type: {mtype}\n"
            f"Summary: {summary}\n"
            f"Decisions:\n{decision_str}\n"
            f"Action Items:\n{action_str}"
        )

        try:
            from bot.agents import agent_llm
            vector = await agent_llm.generate_embedding(text_rep)
        except Exception as exc:
            logger.error("Failed to generate embedding for meeting %s: %s", doc_id, exc)
            return

        if not vector or not isinstance(vector, list):
            logger.error("Failed to generate valid embedding for meeting %s", doc_id)
            return

        metadata = {
            "text": text_rep,
            "type": mtype,
            "summary": summary,
            "decisions": decisions,
            "actions": actions,
            "timestamp": meeting_record.get("timestamp"),
            "agent_contributions": meeting_record.get("agent_contributions", {}),
        }

        async with self.lock:
            self.vector_store.add_document(doc_id, vector, metadata)
        logger.info("Successfully indexed meeting %s.", doc_id)

    async def get_semantic_context(self, query_text: str, limit: int = 3) -> str:
        """
        Gets query embedding via agent_llm, searches the vector store,
        and returns a formatted string of the top results.
        """
        if not query_text:
            return "No query text provided."

        try:
            from bot.agents import agent_llm
            query_vector = await agent_llm.generate_embedding(query_text)
        except Exception as exc:
            logger.error("Failed to generate embedding for semantic query: %s", exc)
            return "Failed to retrieve semantic context due to embedding error."

        if not query_vector or not isinstance(query_vector, list):
            return "Failed to retrieve semantic context due to empty/invalid embedding."

        results = self.vector_store.search(query_vector, limit=limit)
        if not results:
            return "No matching meeting context found."

        formatted_results = []
        for i, res in enumerate(results, 1):
            metadata = res.get("metadata", {})
            mtype = metadata.get("type", "Unknown")
            ts = metadata.get("timestamp", "?")
            summary = metadata.get("summary", "")
            score = res.get("score", 0.0)

            formatted_results.append(
                f"[{i}] {mtype} ({ts}) [Score: {score:.3f}]\n"
                f"Summary: {summary}\n"
                f"Decisions: {', '.join(metadata.get('decisions', [])) or 'None'}\n"
                f"Action Items: {', '.join(metadata.get('actions', [])) or 'None'}"
            )

        return "\n\n".join(formatted_results)

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

    async def save_meeting(self, meeting_record: dict) -> None:
        """
        Saves the meeting to JSON via parent class, and indexes it via SQLite vector store.
        """
        async with self.lock:
            super().save_meeting(meeting_record)
        await self.index_meeting(meeting_record)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
meeting_memory = SemanticMeetingMemory()

