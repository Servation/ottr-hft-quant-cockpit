# Analysis Report: 4-Tier E2E Test Suite Design for HFT Semantic Memory

**Date**: 2026-06-15T23:08:00Z  
**Author**: `teamwork_preview_explorer_t1_1` (Exploration Subagent)  
**Status**: Completed  
**Working Directory**: `d:\crypto-trading-bot\.agents\teamwork_preview_explorer_t1_1`

---

## 1. Codebase Exploration & Contract Analysis

We investigated the following files in the workspace:
- `discord-bridge/bot/memory.py` (MeetingMemory class and persistence logic)
- `discord-bridge/bot/meetings.py` (MeetingEngine, turn-taking, and debate flow)
- `PROJECT.md` (Architecture, milestones, and interface contracts)
- `.agents/teamwork_preview_orchestrator_test/SCOPE.md` (Test philosophy and coverage plan)

### 1.1 `discord-bridge/bot/memory.py` Findings
- Currently manages a chronological JSON-based meeting memory (`data/meeting_log.json`) storing the last 5 full meetings and condensing older ones into a `rolling_summary` string.
- Under the proposed vector DB expansion:
  - `MeetingMemory.save_meeting(meeting_record: dict)` will be modified to generate embeddings for meeting details/summaries and save them to a local database.
  - `MeetingMemory.query_similar_meetings(query_text: str, n: int = 3) -> List[dict]` will be added. It will compute the vector embedding of `query_text` and search for the top `n` most similar historical meetings.
- Since `chromadb` is not installed and the system runs in an offline `CODE_ONLY` network mode, we will design the test suite to support a **pure-Python SQLite-backed vector store** as suggested by the explorer team.

### 1.2 `discord-bridge/bot/meetings.py` Findings
- Orchestrates multi-agent meetings (`run_meeting`).
- We must verify that `run_meeting` embeds current market state (`price_data`, etc.), calls `query_similar_meetings`, and formats the retrieved meetings into the `memory_context` before passing it to `_build_agent_context`.
- We must verify that this context is injected into agent prompts and is also available to the facilitator when closing the meeting.

---

## 2. Vector Memory SQLite & Mocking Strategy

To test the semantic memory without relying on external network requests or heavy C-compiled vector DB packages, the test infrastructure must leverage:
1. **SQLite (`sqlite3`) in-memory databases (`:memory:`)** for unit and integration testing.
2. **Deterministic Vector Mocking**: Mocks the embedding model (`text-embedding-ada-002`/`nomic-embed-text-v1.5`) via `unittest.mock`.
3. **Cos-Sim Evaluation**: Hand-coded cosine similarity using standard library `math` and `zip` to enable exact-match or proximity-based retrieval in unit tests.

---

## 3. Reconciled `TEST_INFRA.md` Design Blueprint

Below is the proposed test infrastructure layout and configuration, to be reconciled as `TEST_INFRA.md` in the project.

### 3.1 Test Framework Architecture
The test suite will be located in `discord-bridge/tests/` or co-located in `discord-bridge/test_semantic_memory.py` according to layout rules. We recommend using `pytest` and `pytest-asyncio` as the runner, with fallback support for standard `unittest` to ensure zero-dependency compatibility.

### 3.2 Mock Specifications
- **Embedding Mock**: A helper `MockEmbeddingService` that maps specific keywords (e.g., "crash", "bull", "range") to predefined high-dimensional vectors, or generates standard unit vectors with set coordinates to guarantee deterministic similarity test runs.
- **LLM API Mock**: Intercepts `AsyncOpenAI` completions to return structured agent statements and closing summaries containing execution tags (`[TRADE: ...]`, `[PARAM: ...]`).

---

## 4. 4-Tier Test Suite: 29 Detailed Test Cases

We have designed **29 test cases** (well exceeding the 27 required) mapped across the 4 tiers:

### Tier 1: Feature Coverage (10 Test Cases)
Validates individual system components under normal operational paths.

| ID | Feature | Test Case Name | Objective / Scenario | Setup / Inputs | Expected Output / Assertion |
|---|---|---|---|---|---|
| **T1.F1.1** | Vector DB | SQLite DB Initialization | Verify SQLite database schema initializes correctly. | Path to temporary SQLite db. | Table `meeting_vectors` exists with fields `id`, `vector`, `metadata`, `timestamp`. |
| **T1.F1.2** | Vector DB | Insert Document | Verify happy-path saving of a meeting vector. | Mock meeting ID, 768-dim vector, metadata dict. | DB stores exactly 1 row; values retrieved match inputs. |
| **T1.F1.3** | Vector DB | Cosine Similarity Math | Validate cosine similarity calculation with known inputs. | Vector A `[1, 0]`, B `[1, 0]`, C `[0, 1]`, D `[0.707, 0.707]`. | A-B sim = `1.0`, A-C = `0.0`, A-D = `0.707`. |
| **T1.F1.4** | Vector DB | Top-N Retrieval Ranking | Verify that querying returns top `n` sorted matches. | Populate 5 docs with different vectors. Query with target vector. | Returns exactly `n` results sorted in descending order of similarity score. |
| **T1.F1.5** | Vector DB | Upsert / Overwrite Row | Verify inserting existing ID updates vector and metadata. | Insert Doc A twice with different vectors. | Total count remains 1; vector matches second insert. |
| **T1.F2.1** | Semantic Injection | Embedding Generator Call | Verify embedding service correctly calls client. | Text "HFT volatility increase". | Client `embeddings.create` called with correct model and inputs. |
| **T1.F2.2** | Semantic Injection | Context Formatting | Verify formatting of retrieved context into LLM prompt text. | List of 2 retrieved meeting dicts. | Context contains formatted string list starting with `• [timestamp] type — summary`. |
| **T1.F2.3** | Semantic Injection | Prompt Context Injection | Verify context is passed to agent prompts. | Mock `query_similar_meetings` to return static results. Run `run_meeting`. | Context sent to LLM contains the `### Relevant Historical Context` section. |
| **T1.F2.4** | Semantic Injection | Facilitator Context Injection | Verify context is passed to the facilitator's closing prompt. | Run `run_meeting` to closing stage. | Facilitator prompt contains `memory_context`. |
| **T1.F2.5** | Semantic Injection | Database Clear / Truncate | Verify ability to clear all vectors. | Insert 3 documents, call `clear_database()`. | Database size is 0. |

---

### Tier 2: Boundary & Edge Cases (10 Test Cases)
Verifies resilience to bad inputs, system stress, or external library failures.

| ID | Feature | Test Case Name | Objective / Scenario | Setup / Inputs | Expected Output / Assertion |
|---|---|---|---|---|---|
| **T2.F1.1** | Vector DB | Empty Input Handling | Test saving empty summary or empty description. | `save_meeting` with empty summary `""`. | Generates embedding of empty string or default vector without raising exceptions. |
| **T2.F1.2** | Vector DB | Very Long Text Summary | Test saving a massive meeting log (100k+ characters). | Summary string of length 100,000. | String is stored correctly; embedding generated via truncation/chunking without crash. |
| **T2.F1.3** | Vector DB | DB Lock / Write Conflict | Simulate database write failure due to SQLite locks. | Mock `sqlite3.connect` to raise `OperationalError("locked")`. | Memory service logs warning and returns gracefully (non-blocking). |
| **T2.F1.4** | Vector DB | Dimension Mismatch | Query database with vector of different length. | DB has 768-dim vectors. Query with 1536-dim vector. | Raises `ValueError` or returns similarity `0.0` rather than silent crash. |
| **T2.F1.5** | Vector DB | Concurrent DB Write | Test concurrent async inserts to SQLite. | 10 concurrent async tasks inserting meetings. | No database corruptions; count of rows is exactly 10. |
| **T2.F2.1** | Semantic Injection | Unrelated Concept Distance | Verify that unrelated queries score low. | Saved: "risk reduction". Query: "how to clean coffee maker". | Score is below similarity threshold (e.g. < 0.2). |
| **T2.F2.2** | Semantic Injection | Null Query Handle | Pass empty query `""` or `None` to similarity search. | Call `query_similar_meetings(None)`. | Returns empty list gracefully; does not throw exception. |
| **T2.F2.3** | Semantic Injection | Exact Phrase vs Synonym | Verify that exact matches score higher than synonyms. | Saved: "buy BTC". Query A: "buy BTC", Query B: "acquire bitcoin". | Similarity Score A > Score B. |
| **T2.F2.4** | Semantic Injection | Out of Bounds Limit `n` | Request `n=0`, `n=-1`, or `n=1000` items. | Call search with `n=0` or `n=1000` on a DB with 5 items. | `n=0` returns `[]`; `n=1000` returns all 5 items without error. |
| **T2.F2.5** | Semantic Injection | Embedding Service Offline | Simulate connection failure to LM-Studio. | Mock embedding API to throw `openai.APIConnectionError`. | Engine catches error, logs it, falls back to chronological history. |

---

### Tier 3: Cross-Feature Combinations (4 Test Cases)
Verifies the interactions and consistency between vector DB and meeting engine.

| ID | Test Case Name | Objective / Scenario | Setup / Inputs | Expected Output / Assertion |
|---|---|---|---|---|
| **T3.C3.1** | Read-After-Write Consistency | Verify meeting is queryable immediately after execution. | Run a strategy meeting. Save meeting record. Query database for meeting keywords. | Newly saved meeting is returned as top result; score matches query embedding. |
| **T3.C3.2** | Sequential Meeting Updates | Verify that multiple sequential meetings persist correctly in DB and JSON log. | Run 10 meetings in sequence. | JSON log contains last 5 full meetings + rolling summary. Vector DB contains all 10 meetings. |
| **T3.C3.3** | Directive Loopback | Verify decision in Meeting A modifies parameter, changing context in Meeting B. | Meeting A outputs `[PARAM: min_trade_usd=250]`. Execute Meeting B. | Portfolio is updated to $250. Meeting B retrieves Meeting A's decision. |
| **T3.C3.4** | Debate, Trade, and Persist | Verify multi-agent debate and trade execution loop persists to vector store. | Mock agents debating "BUY BTC". Trader executes trade. Save meeting. | Trade executes; meeting record with "BUY BTC" summary is indexed and retrievable. |

---

### Tier 4: Real-World Scenarios (5 Test Cases)
Simulates end-to-end HFT market states and validates overall system behavior.

| ID | Scenario Name | Market State Inputs | Expected Agent Actions | Expected E2E Assertions |
|---|---|---|---|---|
| **T4.S4.1** | Flash Crash | BTC price drops 15% in 1 hr. Volatility is extreme. | Emergency meeting. Risk Auditor calls to sell assets. Trader executes SELL. | 1. Semantic query retrieves previous panic/selloff logs.<br>2. Trade `[TRADE: SELL BTC 0.5]` is parsed and executed.<br>3. Meeting logged in DB. |
| **T4.S4.2** | Bull Run | BTC price spikes 10%. Positive funding rate. | Strategy session. Agents call for 100% long leverage. | 1. Query returns historical bullish meetings.<br>2. Trader executes `[TRADE: BUY BTC 1000]`. Cash is deployed.<br>3. Run totals verify update. |
| **T4.S4.3** | Sideways Chop | BTC price flat. Volatility low. | Risk Review session. Technical Analyst calls to hold positions. | 1. Query returns range-bound historical profiles.<br>2. No trades executed (HOLD). Limit orders placed.<br>3. Watchlist updated. |
| **T4.S4.4** | Volatility Cascade | Consecutive price shocks triggering back-to-back alerts. | Two consecutive emergency alerts within 10 minutes. | 1. Cooldown limits prevent second meeting run (throttled).<br>2. First alert retrieves prior crash context, avoiding duplicate action. |
| **T4.S4.5** | Altcoin Breakthrough | Screener flags SOL narrative breakout. | Altcoin scouting session. Screener pushes SOL narrative. | 1. Query retrieves SOL/watchlist records.<br>2. SOL is added to watchlist; Trader buys SOL.<br>3. DB updates SOL vector. |

---

## 5. Proposed Test Suite File: `test_semantic_memory.py`

Below is the structured layout of the python script `test_semantic_memory.py` that will implement these test tiers.

```python
import pytest
import sqlite3
import json
import math
from unittest.mock import AsyncMock, MagicMock, patch
from bot.memory import MeetingMemory
from bot.meetings import MeetingEngine, MEETING_TYPES

# Mock data and vectors
MOCK_768_DIM_VEC = [0.1] * 768

@pytest.fixture
def temp_db(tmp_path):
    """Fixture to create a temporary SQLite DB for vector testing."""
    db_file = tmp_path / "test_vector_memory.db"
    return db_file

@pytest.mark.asyncio
async def test_t1_f1_1_db_init(temp_db):
    """T1.F1.1: Verify SQLite schema initializes correctly."""
    # Test DB setup and table assertions
    pass

# ... implementation of all 29 test cases ...
```

---

## 6. Verification Plan & Test Commands

To verify the test suite:
1. Ensure the dependencies are installed:
   ```powershell
   pip install pytest pytest-asyncio
   ```
2. Execute the test command:
   ```powershell
   pytest discord-bridge/test_semantic_memory.py -v
   ```
3. Run with coverage:
   ```powershell
   pytest --cov=bot discord-bridge/test_semantic_memory.py
   ```
