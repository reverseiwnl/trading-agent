# Agentic Trading System — setup

Daily-cadence trading agent: Claude researches, deterministic code trades (paper).
Read CLAUDE.md first — it is the operating contract for any Claude Code session.

## Getting started in Claude Code

1. Put this folder somewhere permanent and `git init` it (history of CLAUDE.md and
   docs/DESIGN.md *is* the project memory).
2. `python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`
3. `cp .env.example .env` and add your Alpaca **paper** keys (free at alpaca.markets).
4. Open the folder in Claude Code. It reads CLAUDE.md automatically. Good first
   prompt: "Read CLAUDE.md and docs/DESIGN.md, then implement fetch_data.py per the
   TODO list."

## Build order (mirrors CLAUDE.md TODOs)

fetch_data -> decision_engine (+ pytest coverage of every risk rule) -> execute
(paper) -> report + VOO benchmark -> backtest rules with vectorbt -> local
scheduled task (ROUTINE_PROMPT.md) -> remote routine -> PROMOTION_CHECKLIST.md.

## Keeping Claude Code up to date

- CLAUDE.md = standing instructions + current TODO state. Claude updates the TODOs
  as it works; you review diffs like any code change.
- docs/DESIGN.md = append-only decision log. New architectural choices get a dated
  entry so future sessions inherit the reasoning, not just the code.
- After a significant session, ask Claude: "Update CLAUDE.md's TODO list and append
  today's decisions to docs/DESIGN.md." That one habit keeps every future session
  (and the daily routine) in sync with where the project actually is.

## Safety posture

Paper only. The decision engine hard-exits if config mode != paper. Risk limits
live in config.yaml and are changed by you, not by the agent. See
PROMOTION_CHECKLIST.md for what must be true before live trading is discussed.

Not financial advice; this system has no guaranteed edge. The benchmark report
exists precisely so the project can tell you honestly if VOO is beating it.
