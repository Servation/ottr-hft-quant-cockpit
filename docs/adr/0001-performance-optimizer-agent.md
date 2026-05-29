# ADR 0001: Introduction of Performance Optimizer Agent for System Tuning

## Status
Proposed (Approved in review)

## Context
The OTTR trading system uses static parameters for risk management and trade execution (e.g., `stop_loss_limit`, `portfolio_cap`, `slippage_limit`, and execution path thresholds). In volatile crypto markets, static parameters can lead to excessive stop-losses or missed opportunities. We want the system to dynamically optimize its own parameters over time based on trade history.

## Decision
We will introduce a **Performance Optimizer Agent** that:
1. Periodically analyzes execution history from `trade_journal.json`.
2. Employs an LLM to evaluate system performance (win/loss ratio, average drawdown, slippage cost).
3. Recommends or directly applies modifications to active configuration parameters via a secure control API.

To protect against anomalous LLM actions, the parameter adjustments will be bounded by hard limits enforced at the Python gateway level (e.g., `portfolio_cap` can never exceed 15%, `stop_loss_limit` can never exceed 20%).

## Consequences
- **Pros**:
  - The system dynamically adapts to changing volatility without manual configuration.
  - The agent can learn from past mistakes (e.g., if slippage is too high, it increases VWAP/TWAP utilization).
- **Cons**:
  - LLM execution time/cost adds to system overhead.
  - Potential risk of sub-optimal parameter drift if the agent misinterprets performance logs. This is mitigated by hard-coded boundary clamps.
