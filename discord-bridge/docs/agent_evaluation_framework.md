# Agent Evaluation Framework

This document outlines the step-by-step process we use to evaluate and fine-tune each of the 8 agents on the OTTR trading floor. The goal is to ensure each agent has a distinct, non-overlapping function, adheres strictly to modern security standards (especially regarding tool access), and produces highly auditable outputs.

## Step 1: Role Alignment & Swimlanes
Before we touch the prompts, we must verify the agent's reason to exist.
* **Distinct Signal:** What unique perspective or data does this agent bring that no other agent provides?
* **Overlap Check:** Does this agent's function blur into another? (e.g., *Trader* vs *Portfolio Manager*, or *Technical Analyst* vs *Altcoin Screener*).
* **Action:** If overlap exists, we will either rewrite the persona to narrow their focus, or recommend merging the agents to save on LLM latency and context bloat.

## Step 2: Security & Access Control (RBAC)
Agents with access to `ACTION_TOOLS` (e.g., `execute_trade`, `update_parameter`) represent the largest security risk.
* **Tool Scoping:** Does this agent *need* native execution tools? Advisory roles (analysts, auditors) should **only** have `READ_TOOLS`.
* **Prompt Injection Defense:** If the agent ingests external data (like the *Sentiment Analyst* reading tweets/news), we must ensure their prompt contains explicit guardrails against acting on injected commands (e.g., ignoring hidden "BUY NOW" instructions).
* **Action:** We will strip `ACTION_TOOLS` from advisory agents in the codebase and explicitly command them in their persona files to NEVER attempt execution.

## Step 3: Prompt Guardrails & Token Efficiency
With up to 8 agents in a single meeting, context bloat is a massive issue.
* **Conciseness:** Is the prompt forcing the agent to be brief and action-oriented?
* **Formatting:** Does the prompt enforce a strict structure (e.g., bullet points, R/R ratios, clear BUY/SELL/HOLD stances)?
* **Action:** Fine-tune the persona `.txt` files to enforce hard token limits and structured outputs (e.g., removing conversational fluff).

## Step 4: Auditability & Chain of Thought
If a trade loses money, we must be able to audit the decision chain.
* **Data Referencing:** Are they instructed to cite specific data points (e.g., funding rates, technical levels) rather than vague "market sentiment"?
* **Accountability:** Does the prompt force them to state their assumptions clearly so the *Risk Auditor* and *Meeting Chair* can challenge them?
* **Action:** Update prompts to require data-backed rationale for every proposal.

## Step 5: System Breaker Alignment
LLMs hallucinate. We must evaluate how an agent's failure state interacts with the system's hardcoded safety nets.
* **Sanity Checks:** If this agent proposes an absurd trade (e.g., "100x leverage on Dogecoin"), is the *Risk Auditor* prompted to catch it? Are there programmatic limits (e.g., `min_trade_usd`, cash limits) that block it?
* **Action:** Ensure the *Risk Auditor* and *Meeting Chair* personas explicitly check proposals against system constraints.
