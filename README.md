# Agentic Trading System

A daily-cadence, research-driven **paper** trading agent. An LLM (Claude, via a
scheduled cloud routine) reads fresh market data and news each morning and writes
structured buy/sell/hold *signals*; a deterministic decision engine validates
those signals, applies hard risk rules, and only then are orders submitted — to
Alpaca's **paper** endpoint, never live.

Read `CLAUDE.md` first — it is the operating contract for any Claude Code
session. `docs/DESIGN.md` is the append-only decision log.

## What it can and cannot do

**This repo cannot execute real-money trades.** There is no live-trading code
path at all:

- Every Alpaca client is constructed `paper=True`, and `execute.py` /
  `flatten.py` additionally verify at runtime that the client's base URL is
  `https://paper-api.alpaca.markets` before anything is submitted.
- Every entry point that could place an order hard-exits unless
  `config.yaml` says `mode: paper`. Setting `mode: live` does **not** enable
  live trading — it disables everything (loud refusal, exit code 2).
- Going live would require deliberate code changes, gated by every box in
  `PROMOTION_CHECKLIST.md` being checked by a human.

Not financial advice; this system has no guaranteed edge. The VOO benchmark in
the daily digest exists precisely so the project can tell you honestly if
buy-and-hold is beating it.

## Architecture (daily flow)

```
fetch_data.py -> [Claude research step] -> decision_engine.py -> execute.py -> report.py
   data/            signals/                approved orders       fills         reports/
 snapshots       signals_<date>.json         in trades.db      in trades.db    digest_<date>.md
```

1. **`src/fetch_data.py`** — pulls per-ticker snapshots (price, 30 daily bars,
   fundamentals incl. sector, recent headlines) into `data/<date>/` for the
   watchlist + current holdings + benchmark. Every failure is recorded in the
   snapshot's `errors` list and the run manifest; a partial pull exits nonzero.
2. **Research step (Claude, via `ROUTINE_PROMPT.md`)** — reads the snapshots,
   supplements with web search, writes `signals/signals_<date>.json` conforming
   to `signals/schema.json`. This is the only "scoring" in the system, and it is
   deliberately *not* code: the LLM proposes; deterministic code disposes.
3. **`src/decision_engine.py`** — treats the signals file as untrusted input:
   strict pydantic validation (schema, dates, tickers, conviction bounds), then
   the risk rules below. Every verdict — approved or rejected, with its reason —
   is logged to `data/trades.db`.
4. **`src/execute.py`** — submits approved orders to the Alpaca paper endpoint
   with idempotent client order ids, re-validates sizing against *current*
   config/account state, polls every order to confirmation, and never assumes
   success. (The cloud routine does not run this step: submitting approved
   orders is a manual, reviewed act.)
5. **`src/report.py`** — writes `reports/digest_<date>.md`: positions, P&L vs a
   VOO buy-and-hold counterfactual, all verdicts with reasoning, execution
   results, and any data errors.
6. **`src/flatten.py`** — kill switch: cancel all open orders, close every
   position at market, full audit trail.

Shared plumbing lives in `src/common.py` (paths, config, credentials, logging)
and `src/trading_day.py` (one definition of "today", America/Chicago).

## How signal handling works (high level)

The LLM's signal is `{ticker, action, conviction 0-1, thesis, sources,
timestamp}`. The decision engine — all rules live in `config.yaml`, never in
the LLM's hands — then:

- sizes buys as `bankroll x max_position_pct x conviction`, where the bankroll
  is `min(account equity, trading_budget_dollars)` ($5,000 hard budget);
- enforces per-ticker (5%) and per-sector (20%) exposure caps, counting
  pending/unfilled buys as exposure (a fill is never assumed);
- rejects buys below the conviction floor (0.6), below the minimum order size,
  past the 5-trades/day cap, on unknown sectors, or over the total budget;
- runs a stop-loss sweep (8% below cost basis) on every run, independent of
  signals and exempt from the trade cap;
- freezes all new buys if the portfolio is down >3% of the bankroll intraday
  (circuit breaker);
- on ANY validation failure: no trade + a logged rejection. "No valid signal"
  never means "guess".

## Data sources

- **Alpaca** (paper account): market data (IEX feed), news API, account state,
  order submission. Free tier.
- **yfinance**: fallback for bars/news; the only source of fundamentals
  (incl. the sector used by the sector cap).
- **Web search** (research step only): breaking news on current holdings.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate   # Python 3.11+
pip install -r requirements.txt
cp .env.example .env                                 # add Alpaca PAPER keys
```

Required env vars (see `.env.example`; `.env` is gitignored — never commit it):

| Variable | Purpose |
|---|---|
| `ALPACA_API_KEY` | Alpaca **paper** account API key |
| `ALPACA_SECRET_KEY` | Alpaca **paper** account secret |

Each script runs standalone with no args for "today's run":

```bash
python src/fetch_data.py
python src/decision_engine.py signals/signals_<today>.json
python src/report.py
python src/execute.py     # manual step: submit today's approved orders
python src/flatten.py     # kill switch: close everything
```

Runs are debuggable after the fact: each script tees a timestamped DEBUG log to
`logs/<script>_<date>.log` (gitignored), decisions/executions live in
`data/trades.db`, and the digest summarizes everything human-readably.

## Tests

```bash
pytest            # no network, no keys needed; uses tmp DBs and fake clients
```

The suite covers every risk rule at its boundaries (against the *live*
`config.yaml` values, so loosening a rule fails tests), the paper guard, order
idempotency and revalidation, the kill switch, the benchmark math, and
fetch_data's fallback/error contract. All external APIs are mocked.

To sanity-check a clone end-to-end without any keys or network:

```bash
python scripts/replay_day.py   # replays the latest committed signals file
                               # through the real validation + risk rules, read-only
```

The vectorbt rules backtest is optional and heavy:
`pip install -r backtest/requirements.txt`, then
`python backtest/backtest_rules.py` (findings: `docs/backtest_results.md`).

## Operations

- The cloud routine (weekdays 12:00 UTC, prompt in `ROUTINE_PROMPT.md`) runs
  fetch → research → decide → digest, then commits the day's state (`data/`,
  `signals/`, `reports/`, `trades.db`) back to `main` — the repo is the system
  of record and the routine is its single writer. Pull before running anything
  locally that writes state.
- Safety posture: paper only. Risk limits live in `config.yaml` and are changed
  by you, not by the agent. See `PROMOTION_CHECKLIST.md` for what must be true
  before live trading is even discussed.
