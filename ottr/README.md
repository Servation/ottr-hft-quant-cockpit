# OTTR v2

A paper-trading engine where three models analyze and code acts. Design and rationale:
[`../V2_PLAN.md`](../V2_PLAN.md).

- **Council:** Claude, GPT (Codex CLI) and Gemini (Antigravity CLI) each give a stance per
  asset as validated JSON. A Claude chair decides on anonymized reports. The chair never
  gives dollar sizes.
- **Code:** sizes BUYs (vol target x conviction x regime, topping up only to target), runs
  the pre-trade gate and the protective monitor (trailing stop, concentration trim,
  drawdown breaker), and books paper fills at bid/ask with fees in an exact Decimal
  SQLite ledger.

## Setup (Windows host, where the model CLIs are signed in)

```bash
cd ottr
python -m venv .venv          # Python 3.11+
.venv/Scripts/pip install -e ".[dev]"
.venv/Scripts/ottr init --import-v1 ../discord-bridge/data/portfolio_state.json   # or: init --fresh
```

## Commands

| Command | Writes? | What it does |
|---|---|---|
| `ottr run` | yes (holds the lock) | the engine: risk 60s, equity 15m, council every 4h |
| `ottr meeting` | no | one council meeting on a scratch copy; prints dry-run verdicts |
| `ottr meeting --execute` | yes | one meeting that really paper-trades (engine must be stopped) |
| `ottr status` | no | book, risk state, benchmarks, task heartbeats |
| `ottr check` | no | replays the fill log; exits 2 if the books don't balance |
| `ottr resume` | yes | clears a drawdown halt and re-baselines the peak |

Kill switch: `OTTR_DRY_RUN=1` (or `trading.dry_run = true`) records "would fill"
verdicts and never fills. Optional: `DISCORD_WEBHOOK_URL` for notifications.

## Tests

```bash
.venv/Scripts/python -m pytest -q
```
