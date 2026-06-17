# Original User Request

## Initial Request — 2026-06-15T23:02:00Z

# Teamwork Project Prompt — Draft

The OTTR Crypto Trading Bot needs a Long-Term Vector Database Memory system so agents can semantically search through past meeting transcripts to find historical precedent for current market conditions.

Working directory: d:\crypto-trading-bot\discord-bridge
Integrity mode: development

## Requirements

### R1. Vector Database Integration
Implement a local embedding/vector database solution (e.g., ChromaDB) inside `bot/memory.py`. It should store the `MeetingRecord` objects (including summaries, decisions, and outcomes) as vector embeddings.

### R2. Semantic Context Injection
Modify the meeting orchestration flow so that before a meeting begins, the bot embeds the *current* market conditions (price action, funding rates, etc.) and uses semantic search to fetch the top 3 most relevant historical meetings. This historical context must be injected into the prompt for the Meeting Chair and all participants.

## Acceptance Criteria

### Vector Memory Verification
- [ ] Create a `test_semantic_memory.py` script that inserts 3 mock meetings (e.g., "Bull run", "Sideways chop", "Flash crash") into the local vector DB.
- [ ] The script must query the DB using a new "Flash crash" market state and programmatically assert that the "Flash crash" historical meeting is returned as the #1 result.

## Follow-up — 2026-06-16T22:33:01Z

Hey team, how is the vector database integration and memory semantic search coming along? Do you have the ChromaDB setup mostly done and the tests passing?
