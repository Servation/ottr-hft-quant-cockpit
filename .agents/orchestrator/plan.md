# Project Plan: OTTR HFT Semantic Memory

## Objectives
1. Implement local vector database (e.g. ChromaDB) in `bot/memory.py` to store meeting records.
2. Embed and query meetings semantically.
3. Modify meeting flow in `bot/meetings.py` to embed current market state and inject top 3 historical meetings.
4. Verify using `test_semantic_memory.py` asserting flash crash scenario.

## Milestones & Status
- **Milestone 1: Exploration**
  - [x] Analyze environment to see if ChromaDB/other packages are available.
  - [x] Inspect existing LLM connector and config settings.
  - [x] Formulate design strategy for vector database storage.
- **Milestone 2: Vector DB Implementation**
  - [x] Implement local vector DB storage in `bot/memory.py`.
  - [x] Handle embedding creation for meeting summaries.
- **Milestone 3: Semantic Context Injection**
  - [x] Embed current market conditions before a meeting begins.
  - [x] Query top 3 relevant meetings.
  - [x] Inject retrieved context into prompts.
- **Milestone 4: Verification**
  - [x] Create `test_semantic_memory.py`.
  - [x] Run the test script and verify assertions.
- **Milestone 5: Final Hardening and Integrity Audit**
  - [x] Run Challenger to assert behavior.
  - [x] Run Forensic Auditor to verify no integrity violations.
