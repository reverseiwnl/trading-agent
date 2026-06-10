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

## 2026-06-10 — execute.py implementation decisions

- **Input is the decisions table, not a file handoff.** execute.py reads
  today's approved orders straight from `data/trades.db` (`decisions` rows,
  verdict='approved') — the durable record decision_engine already writes, and
  the only place the decision row id (which every execution must link to)
  exists. Re-parsing stdout or a JSON file would mean fragile re-matching of
  orders back to decision rows.
- **Double paper guard, independently checked.** Guard 1: config.yaml mode
  must be "paper" (checked before any env read or client construction).
  Guard 2: after building `TradingClient(..., paper=True)`, the client's
  actual base URL is verified to be `https://paper-api.alpaca.markets` —
  defense against an SDK default change or env override. Either failure =>
  exit 2, zero submissions.
- **Idempotency is two-layer.** `client_order_id = run_date-ticker-side`.
  Layer 1 (local): a prior executions row with that id blocks resubmission.
  Layer 2 (Alpaca): the API rejects duplicate client_order_ids — covers a
  lost/wiped local log; on that rejection we look the order up by client id
  and adopt it instead of failing. Side effect: two same-side approvals for
  one ticker in one day collapse to a single order — acceptable, conservative.
- **The execution row is written BEFORE the network call** (status
  'submitting'), then updated. A crash mid-submit leaves the attempt on
  record, which blocks a blind retry on the next run.
- **A skipped duplicate is only "green" if the prior attempt filled.** A rerun
  after an unconfirmed/failed attempt still skips (never double-submits) but
  returns 'skipped_unresolved' and exits 1: today's intent is unexecuted and
  clearing it is a manual decision, never automatic.
- **Polling never assumes success.** Poll until filled / rejected / canceled /
  expired or a 60s timeout. Partial fill still open at timeout is logged as
  'partially_filled' WITH the fill quantity (we keep polling through partials
  rather than stopping at first partial — market orders usually complete in
  seconds). Anything else still open at timeout logs 'unconfirmed'.
- **Sells size from the live position** (`get_open_position`) at submit time,
  qty='all' resolved to actual shares; no position => 'failed' row, no order.
- **Tests share one tmp trades.db with decision_engine** and seed via
  `de.log_decisions(...)` — the real production write path — so the
  decision-row linkage and the rerun/no-double-submit property are tested
  end to end against a scripted fake Alpaca client.

## 2026-06-10 — report.py benchmark + flatten.py kill switch decisions

- **Benchmark is a deposit-mirroring counterfactual, not a lump-sum compare.**
  `trades.db` table `benchmark_deposits`: every cash deposit into the paper
  account hypothetically buys VOO instead. Inception row = account equity at
  the first report run (seeded 2026-06-10 at $100,000, while the account held
  zero positions, so equity == deposits exactly). Later cash movements are
  mirrored from Alpaca CSD/CSW account activities (raw
  `GET /account/activities` — alpaca-py 0.43 has no typed wrapper), idempotent
  by activity id; withdrawals are negative deposits (negative shares).
- **Deposits price at the first ACTUAL VOO close on/after the deposit date**,
  read from the `data/<date>/VOO.json` snapshots fetch_data.py writes — never
  an interpolation. A deposit whose first close hasn't printed yet counts at
  face value until it does. A deposit older than the bar window we hold is
  NEVER priced at the wrong close — it stays cash and the digest warns, because
  a wrong benchmark is worse than a loud incomplete one. Once priced, a deposit
  is immutable (idempotent re-pricing changes nothing).
- **The digest's "System minus benchmark" line is the project's honesty
  number**: cumulative P&L of the account vs the counterfactual, both measured
  against the same net deposits, VOO valued at the latest actual snapshot
  price. Stale snapshot => explicit warning in the digest and exit 1.
- **report.py reuses execute's paper guard** even though it's read-only, and
  exits: 0 clean digest, 1 digest written but degraded (stale/unpriceable
  benchmark, missing manifest, ...), 2 fatal (no digest).
- **flatten.py cancels ALL open orders before selling** — a pending buy filling
  mid-flatten or a stale sell stacking onto ours would defeat the kill switch.
  Then it market-sells the freshly fetched qty of every position and polls each
  to confirmation via execute's machinery (same paper guard, same
  decisions+executions audit trail, so digests pick flattens up automatically).
- **Flatten decision rows keep order_json NULL** so a same-day re-run of
  execute.py can never re-submit a flatten as a pending order; the full request
  lives on the execution row. client_order_id is
  `flatten-<date>-<ticker>-<attempt>`: re-running after a failed close gets a
  fresh id instead of being blocked by the duplicate check — safe because open
  orders were just canceled and only the current remaining qty is sold.
  Exit codes: 0 flat, 1 NOT flat (needs attention), 2 paper guard refused.
