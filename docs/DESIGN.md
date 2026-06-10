# DESIGN.md — Decisions and rationale

This file is the durable memory of design discussions. Append new decisions with
dates; do not rewrite history.

## 2026-06-10 — Initial architecture (from claude.ai planning session)

### Five-layer architecture
1. **Data ingestion** — Alpaca/Polygon for market data, news API + SEC EDGAR for
   unstructured research material. Daily pre-market cadence.
2. **Research layer (LLM)** — reads new material on watchlist + holdings, emits
   structured JSON signals (ticker, thesis, conviction 0-1, action). Structured
   output is mandatory: free-form prose is unusable and unauditable.
3. **Decision engine (deterministic)** — the compliance desk. Applies position
   sizing, per-ticker/sector caps, stop-losses, daily trade budget. The single most
   important architectural choice: LLM proposes, code disposes. LLMs hallucinate
   and have no native risk concept; rules cap the blast radius.
4. **Execution** — Alpaca (free API, paper environment identical to live,
   fractional shares). Confirm fills; never assume an order succeeded.
5. **State/memory/monitoring** — SQLite (upgrade to Postgres if needed) tracking
   positions, every decision WITH its reasoning, P&L. Alerting on every trade.
   Kill switch that flattens all positions.

### Scheduling: Claude Code routines
Routines replace the VPS/Lambda scheduler layer. Plan:
- Develop interactively in normal sessions.
- Stage 1: LOCAL scheduled task (runs only while app is open) to observe behavior.
- Stage 2: REMOTE routine on Anthropic-managed infra, paper trading only.
- Routine acts as orchestrator + analyst; it runs the deterministic scripts for
  anything touching money. It does not improvise trades.

### Key risks acknowledged
- Can backtest the RULES, cannot honestly backtest the LLM's research judgment
  (training-data hindsight contamination). Only honest validation: months of
  paper trading vs simply holding VOO.
- No informational edge from reading public news; value, if any, comes from
  discipline and breadth (processes more filings, never panic-sells).
- Agent nondeterminism: routine runs may vary. Decision engine treats agent
  output as untrusted input — strict pydantic validation, reject-and-alert.
- Secrets: Alpaca keys in routine env only, scoped (no withdrawal perms for any
  future live key), never in prompt/repo/logs.
- Frictions: PDT rule under $25k equity (daily cadence mostly avoids), short-term
  capital gains taxes, LLM API cost per research run.

### Build order
Backtested rules engine -> add LLM research layer -> paper trade combined system
2-3 months -> promotion review. Live money only after PROMOTION_CHECKLIST.md.

## 2026-06-10 — fetch_data.py implementation decisions

- **Alpaca IEX feed, not SIP** — free tier has no SIP entitlement; explicit
  `feed=IEX` avoids permission errors. Good enough for daily-cadence decisions.
- **yfinance is the only fundamentals source** (Alpaca has none). `sector` is
  captured per ticker because the decision engine's 20% sector cap needs it.
- **Benchmark (VOO) is fetched daily** alongside watchlist + holdings so
  report.py can compute P&L vs benchmark without a separate pull.
- **Fail-loudly contract**: per-field errors land in each snapshot's `errors`
  list; `data/<date>/_manifest.json` records per-ticker status (ok/partial/
  failed) and a `clean` flag. Exit codes: 0 clean, 1 partial, 2 fatal. A ticker
  with no price at all is "failed"; downstream should treat its snapshot as
  unusable. Orchestrator must surface nonzero exits, not retry silently.
- **News**: Alpaca news API primary (last 7 days, max 10), yfinance `.news`
  fallback (handles both old flat and new `content`-nested item formats).
  Finnhub key in .env remains unused for now.

## 2026-06-10 — decision_engine.py risk-logic decisions

- **Caps count existing exposure plus same-run approvals.** A buy is checked
  against (current market value of the position) + (notional already approved
  for that ticker/sector earlier in the same run) + (the new order). Pending
  sells do NOT free up room — a fill is never assumed, so exposure only shrinks
  once execute.py confirms it.
- **Unknown sector = the ticker itself.** If a data/ snapshot has no sector,
  the ticker becomes its own sector bucket. Conservative: an unclassified
  position can never hide inside (or dilute) a real sector's 20% cap.
- **Stop-loss sweep runs first, every run, signal-independent.** Any position
  >= 8% below per-share cost basis gets a sell order even if the signals file
  says hold (or says nothing). Stop exits are exempt from max_trades_per_day —
  risk exits are never rationed. They fire under the circuit breaker too (it
  only freezes buys).
- **A stop-lossed ticker is frozen for signals that run**: a signal sell is
  skipped as a duplicate (one exit order, not two), and a signal buy is
  rejected — re-entering a name on the day it blew through its stop is a
  human decision, not an agent one.
- **Trade-budget rejections are explicit.** Signals past the 5-trade cap are
  rejected with "max trades per day reached", not silently dropped (the
  skeleton's `break` hid them).
- **Decision log**: every verdict goes to `data/trades.db` table `decisions`
  (ts, run_date, signal_json, verdict, reason, order_json). Stop-loss exits log
  with NULL signal_json; whole-file rejections (malformed/stale signals) log
  one row carrying the raw payload. Nothing the engine decides is unlogged.
- **Intraday P&L for the circuit breaker** = (equity - last_equity) /
  last_equity from the Alpaca account, i.e. vs prior close; trips at <= -3%.
- **Tests are network-free** (hand-built portfolio dicts, monkeypatched
  get_portfolio_state/DB_PATH) and assert against the real config.yaml values,
  so a config change that loosens a rule will fail the suite and force a look.
