# Scope: OTTR HFT Semantic Memory Testing Track

## Test Philosophy
- Opaque-box, requirement-driven. No dependency on internal implementation details.
- Validate: vector storage correctness, semantic retrieval precision, context injection correctness, and end-to-end integration.

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|---|---|---|---|
| T1 | Test Design | Design test framework and write `TEST_INFRA.md` | None | DONE |
| T2 | Implement Test Cases | Create comprehensive 4-Tier test suite including `test_semantic_memory.py` | T1 | DONE |
| T3 | Publish Test Suite | Publish `TEST_READY.md` summarizing the tests and run instructions | T2 | DONE |

## Coverage Plan
- **Feature 1: Vector Database Integration**
  - Tier 1: Happy-path insertion, updates, persistence.
  - Tier 2: Extreme values, empty fields, very long texts, dimensions mismatch, database file permission issues.
- **Feature 2: Semantic Context Injection**
  - Tier 1: Querying, similarity scoring, correct ranking, retrieval of top 3 matches.
  - Tier 2: Querying with completely unrelated concepts (scoring low), empty/null query handles, exact phrase matching.
- **Feature 3: E2E Integration & Flows**
  - Tier 3: Pairwise combination of database insertion and meeting scheduling flows.
  - Tier 4: Real-world scenarios (e.g. Flash Crash scenario, Bull Run scenario, Sideways Chop scenario, asserting correct context injection in prompts).
