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


# --------------------------------------------------------------------# ---------------------------------------------------------------------------
# Vesper Text TF-IDF Semantic Memory Extension
# ---------------------------------------------------------------------------
import sys

# Add Vesper to path so we can import its engine
VESPER_PATH = r"d:\vesper-text"
if VESPER_PATH not in sys.path:
    sys.path.append(VESPER_PATH)

import vesper_engine

VAULT_DIR = DATA_DIR / "vesper_vault"

class SemanticMeetingMemory(MeetingMemory):
    """
    Extends MeetingMemory to store meeting records as Markdown files in a local vault
    and query them using Vesper Text (TF-IDF Intent Isolator).
    """

    def __init__(self) -> None:
        super().__init__()
        self._lock: Optional[asyncio.Lock] = None
        
        # Ensure vault exists
        VAULT_DIR.mkdir(parents=True, exist_ok=True)
        
        # Migrate existing meetings to vault if they don't exist
        for m in self._meetings:
            self._write_meeting_to_vault(m)

    @property
    def lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock
        
    def _write_meeting_to_vault(self, meeting_record: dict) -> None:
        """Writes a meeting record to a Markdown file in the Vesper vault."""
        meeting_id = meeting_record.get("id", "unknown")
        file_path = VAULT_DIR / f"{meeting_id}.md"
        
        # Don't overwrite if it already exists
        if file_path.exists():
            return
            
        mtype = meeting_record.get("type", "Unknown")
        ts = meeting_record.get("timestamp", "?")
        summary = meeting_record.get("summary", "")
        decisions = meeting_record.get("decisions", [])
        actions = meeting_record.get("actions", [])
        contribs = meeting_record.get("agent_contributions", {})
        
        decision_str = "\n".join(f"- {d}" for d in decisions) if decisions else "None"
        action_str = "\n".join(f"- {a}" for a in actions) if actions else "None"
        
        content = f"# Meeting {meeting_id}\n\n"
        content += f"**Type:** {mtype}\n**Timestamp:** {ts}\n\n"
        content += f"## Summary\n{summary}\n\n"
        content += f"## Decisions\n{decision_str}\n\n"
        content += f"## Action Items\n{action_str}\n\n"
        
        for agent, text in contribs.items():
            content += f"## {agent.capitalize()}\n{text}\n\n"
            
        try:
            file_path.write_text(content, encoding="utf-8")
        except Exception as exc:
            logger.error("Failed to write meeting %s to Vesper vault: %s", meeting_id, exc)

    async def get_semantic_context(self, query_text: str, limit: int = 3) -> str:
        """
        Uses Vesper TF-IDF engine to extract relevant context packets from the vault.
        First expands the query using an LLM to include synonyms.
        """
        if not query_text:
            return "No query text provided."

        # Expand query using LLM
        from bot.agents import agent_llm
        expanded_query = query_text
        try:
            prompt = (
                f"Given the query: '{query_text}', expand it with a few key synonyms "
                f"(e.g., SOL -> Solana, ETH -> Ethereum, position -> allocation) to improve exact keyword matching. "
            )
            expanded_query, _ = await agent_llm.generate_response(
                agent_id="meeting_chair",  # Reuse meeting_chair's persona lightly
                context_messages=[
                    {"role": "system", "content": "You are a query expansion tool for a crypto TF-IDF engine. Respond ONLY with the expanded query string, no other text."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=50
            )
            # if it errored out, it will start with [error], so we fallback
            if expanded_query.startswith("[error]"):
                expanded_query = query_text
        except Exception as e:
            logger.error("Failed to expand query: %s", e)
            pass

        logger.info("Vesper query expanded to: %s", expanded_query)
        packet, metadata, err = vesper_engine.generate_context_packet(str(VAULT_DIR), expanded_query, top_k=limit)
        
        if err:
            logger.warning("Vesper search error or no matches: %s", err)
            return "No matching meeting context found."
            
        return packet

    async def save_meeting(self, meeting_record: dict) -> None:
        """
        Saves the meeting to JSON via parent class, and indexes it via Vesper vault.
        """
        async with self.lock:
            super().save_meeting(meeting_record)
            self._write_meeting_to_vault(meeting_record)

# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
meeting_memory = SemanticMeetingMemory()

