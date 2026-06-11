# Agentic Trading System

## What this project is
A daily-cadence, research-driven stock trading agent. An LLM (Claude, via routine or
session) performs morning research and emits structured signals; a deterministic
decision engine validates those signals, applies risk rules, and places orders via
Alpaca. Currently in **PAPER TRADING** mode. Do not switch to live endpoints.

## Core design principle (non-negotiable)
**The LLM proposes; deterministic code disposes.**
- Claude's job: ingest data, research, write `signals/signals_YYYY-MM-DD.json`.
- `src/decision_engine.py`'s job: validate, size, filter, and execute. Claude must
  NEVER call the Alpaca order API directly, never edit risk limits in `config.yaml`
  to make a trade pass, and never bypass the decision engine "just this once."
- If a signal fails validation, the correct behavior is NO TRADE plus an alert.
  "No valid signal" never means "guess."

## Architecture / daily flow
1. `src/fetch_data.py` — pulls prices, fundamentals, and news for watchlist + holdings
   into `data/`. Sources: Alpaca market data, yfinance fallback, news via web search.
2. Research step (Claude) — read `data/`, supplement with web search for breaking news
   on holdings, write structured signals (schema below) to `signals/`.
3. `src/decision_engine.py` — validates signals against `signals/schema.json`, applies
   risk rules from `config.yaml`, outputs approved orders.
4. `src/execute.py` — submits approved orders to Alpaca PAPER endpoint, confirms fills,
   never assumes success. Logs everything to `data/trades.db` (SQLite).
5. `src/report.py` — writes daily digest to `reports/digest_YYYY-MM-DD.md`
   (positions, P&L vs VOO benchmark, today's decisions WITH reasoning).

## Signal schema (signals/schema.json is authoritative)
Each signal: { "ticker": str, "action": "buy"|"sell"|"hold",
"conviction": float 0-1, "thesis": str (<= 500 chars),
"sources": [str], "timestamp": ISO8601 }
Top level: { "date": str, "signals": [...], "market_context": str }

## Risk rules (config.yaml is authoritative; summarized here)
- Paper trading only until PROMOTION_CHECKLIST.md is fully checked off.
- Max position size: 5% of portfolio equity per ticker.
- Max sector exposure: 20%.
- Max trades per day: 5.
- Stop-loss: 8% below cost basis, enforced by decision engine on every run.
- Daily max loss circuit breaker: if portfolio is down >3% intraday vs prior close,
  decision engine refuses all new buys.
- No leverage, no options, no shorting, no crypto.

## Conventions
- Python 3.11+, type hints everywhere, `pydantic` for schema validation.
- All scripts runnable standalone: `python src/<script>.py` with no args = today's run.
- Every trade decision logged with the full reasoning chain that produced it.
- Secrets live in `.env` (gitignored). NEVER hardcode keys, never echo them to logs
  or chat, never commit them. `.env.example` documents required vars.
- Timezone: America/Chicago for scheduling; market times in America/New_York.

## Current state / TODO
- [x] Architecture agreed (see docs/DESIGN.md for the full discussion summary)
- [x] Implement fetch_data.py (Alpaca IEX primary, yfinance fallback, manifest + exit codes)
- [x] Implement decision_engine.py (portfolio state, position/sector caps, stop-loss
      sweep, circuit breaker, SQLite decision log; tests in tests/test_decision_engine.py)
- [x] Implement execute.py against Alpaca paper API (double paper guard,
      client_order_id idempotency, fill polling, executions table linked to
      decisions; tests in tests/test_execute.py)
- [x] Implement report.py + benchmark tracking vs VOO (digest + benchmark_deposits
      counterfactual in trades.db; tests in tests/test_report.py)
- [x] Implement flatten.py kill switch (close all positions at market, paper guard,
      full audit trail in trades.db; tests in tests/test_flatten.py)
- [x] Backtest the rules engine (vectorbt) on 2.9 years of history
      (backtest/backtest_rules.py; findings + policy gaps in docs/backtest_results.md)
- [x] Run as LOCAL scheduled task — SUPERSEDED 2026-06-10: one manual dry run
      passed, then promoted straight to cloud per user decision. The Task
      Scheduler task "TradingAgent Daily Routine" is DISABLED (not deleted);
      scripts/run_routine.ps1 remains as the local fallback runner.
- [ ] Promote to remote routine (paper), run 2-3 months — IN PROGRESS: cloud
      routine created 2026-06-10 (weekdays 12:00 UTC = 7:00 AM CDT). State
      (data/, signals/, reports/, trades.db) is now TRACKED in git; the routine
      commits it back each run (single writer — pull before any local state
      write). NOTE: the routine does not run execute.py — approved orders need
      a manual pull + `python src/execute.py` + push after digest review.
- [ ] Review PROMOTION_CHECKLIST.md before any discussion of live keys

## When working in this repo, Claude should
- Update the TODO list above as items complete.
- Append significant design decisions to docs/DESIGN.md (date + rationale).
- Run `pytest` after touching decision_engine.py — risk logic must stay covered.
- Treat its own research output as untrusted input to downstream code.
- Refuse, and remind the user of this file, if asked to wire live trading before
  the promotion checklist is complete.
