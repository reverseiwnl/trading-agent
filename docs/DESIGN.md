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

## 2026-06-10 — backtest of the rules engine (backtest/backtest_rules.py)

- **Scope honesty preserved**: only the deterministic rules were tested, with a
  naive SMA20/SMA100 momentum generator (conviction scaled from trailing
  126d/63d returns) standing in for the LLM. P&L of the backtest is explicitly
  meaningless as evidence of edge; the test target is rule behavior. Full
  results and surprises in docs/backtest_results.md.
- **Simulator mirrors decision_engine.py exactly** (check ordering, CAP_EPSILON,
  budget-exempt stop sweep, same-day stop freeze, pending-notional cap
  accounting) and reads live config.yaml values, so a config change re-tests
  the new numbers. Cadence model: signals from close t-1, decisions + fills at
  open t (pre-market run), breaker on the overnight gap.
- **vectorbt is the independent auditor, not the rule engine.** The daily loop
  emits share-level orders; vbt.Portfolio.from_orders (cash_sharing,
  call_seq='auto') replays them from scratch. Run asserts $0 equity divergence
  and identical fill counts — two implementations agreeing on the accounting.
  Adjusted prices throughout (raw prices would fire fake stops on NVDA's 2024
  10:1 split).
- **--stress mode exists because two rules never trigger naturally**: at 30%
  max deployment a -3% equity gap is a tail event (breaker: 0 trips in 2.9y)
  and 6 tickers rarely produce >5 approvals (budget never bound). Stress run
  tightens thresholds in-process (config.yaml untouched) and asserts both
  paths fire.
- **Verdict (2023-07 -> 2026-06)**: all caps held exactly at decision time, all
  8 stops fired at >=8.19% below basis, max drawdown -4.7% vs VOO's -18.7%.
  Open policy gaps for a human to accept or close before promotion: stops gap
  through the 8% level at daily cadence (worst exit -17.0%); a stopped name can
  re-enter the next day (NVDA did, realizing the loss for nothing — consider a
  multi-day cooldown); winners drift above the 5% cap with no trimming rule
  (NVDA reached 9.0% of equity); the sector cap is arithmetically unreachable
  until the watchlist grows (3 tech names x 5% = 15% < 20%).

## 2026-06-10 — routine dry run, UTF-8 hardening, local scheduled task

- **Full manual dry run of ROUTINE_PROMPT.md passed end-to-end** (fetch → research
  → signals → decision engine → digest). First real signals file produced one
  approved order (JNJ buy, $3,250 notional at 0.65 conviction) and a clean digest.
- **All file I/O and console output is now explicit UTF-8.** `Path.read_text`/
  `write_text` on Windows default to cp1252: fetch_data.py was writing snapshots
  cp1252-encoded (a Benzinga em-dash made GOOGL.json invalid UTF-8), and any
  headline outside cp1252 would have crashed the fetch with UnicodeEncodeError.
  Every read/write in src/ now passes `encoding="utf-8"`, and each runnable
  script reconfigures stdout/stderr to UTF-8 with `errors="replace"` so a print
  can never kill a scheduled run whose output is redirected to a log file.
- **Local scheduling = Windows Task Scheduler, not an in-session mechanism.**
  Task "TradingAgent Daily Routine" runs weekdays 7:00 AM local (machine is
  America/Chicago, DST-aware) and calls scripts/run_routine.ps1, which extracts
  the prompt verbatim from between ROUTINE_PROMPT.md's `---` markers, pipes it
  via stdin to headless `claude -p --permission-mode acceptEdits` (stdin avoids
  argument-quoting mangling), and logs to logs/routine_<date>.log (gitignored).
  Headless permissions are a tight allowlist in .claude/settings.local.json
  (the three routine scripts + WebSearch) — no blanket permission bypass.
  Task settings: StartWhenAvailable (missed 7:00 runs fire on wake), WakeToRun,
  2h execution limit, interactive logon token (runs only while ivy10 is logged
  in — acceptable for the supervised phase; revisit if promoting).
- **Open question flagged, deliberately not changed:** ROUTINE_PROMPT.md never
  runs execute.py, so approved orders accumulate in trades.db unsubmitted and
  expire at day rollover (execute.py only loads today's run_date). Coherent as
  a human-in-the-loop supervised phase (review digest, run execute.py manually
  before close), but it means the routine alone never trades. A human should
  decide whether to add an execute step to the routine prompt before (or at)
  promotion to remote.

## 2026-06-10 — promoted to cloud routine; state committed to the repo

- **Skipped the planned 1-2 week local phase** by explicit user decision (one
  clean manual dry run stood in for it). Still paper-only; PROMOTION_CHECKLIST
  gates anything live, unchanged.
- **The repo is now the system of record.** Cloud routine runs start from a
  fresh clone, so anything not in git does not exist for them. data/ (daily
  snapshots + trades.db), signals/, and reports/ are now tracked; the routine's
  new step 6 commits exactly those paths and pushes to main each run. Rationale
  over alternatives: zero new infrastructure, every decision/digest lands in
  git history (audit trail for free), and the local machine can always
  `git pull` to inspect. Cost: trades.db is an opaque binary in diffs and the
  repo grows ~70 KB/day — trivial at this scale, revisit if the watchlist grows
  10x.
- **Single-writer rule.** Only the cloud routine writes state unprompted. The
  local Task Scheduler task is disabled (kept as fallback). Any local
  state-writing run (execute.py after digest review, flatten.py) must be:
  pull, run, commit, push.
- **Failure semantics preserved end-to-end**: a failed fetch still produces and
  COMMITS an error digest (step 1 now routes to steps 5-6); a rejected push
  retries once after rebase, then fails loudly — "a run whose state was not
  pushed did not happen."
- **Schedule is UTC-fixed**: 12:00 UTC weekdays = 7:00 AM CDT now, 6:00 AM CST
  after DST ends — both pre-market, so left as-is deliberately.
- **execute.py remains outside the routine** (human-in-the-loop unchanged from
  the local design): the routine proposes and reports; submitting approved
  orders stays a manual, reviewed act.

## 2026-06-10 — routine model bumped to Opus 4.8; headline-citation rule added

First cloud run (Sonnet 4.6) claimed "no news items in today's data pull"
while the snapshots it had just committed held 6-10 headlines per ticker — a
diligence failure, not a data failure. Two-part fix: (1) step 2 of the routine
prompt now requires citing at least one specific headline per ticker, or an
explicit statement that the ticker's headline list was empty (makes skipping
the news detectable in every digest); (2) routine model raised to
claude-opus-4-8 — the research step is the system's only source of edge and
runs once daily, so paying for quality there is the right asymmetry while the
deterministic engine caps the downside. Revisit after a week of digests;
Fable 5 is the next step up if Opus still under-engages with sources.

## 2026-06-11 — $5,000 trading budget (bankroll mode); routine model -> Fable 5

- **User decision: cap the system's deployable money at $5,000 regardless of
  the $100k paper balance** — and, chosen explicitly between the two readings,
  treat the budget as the bankroll rather than a mere spend ceiling: sizing,
  position cap, and sector cap all compute from min(equity, budget), so the
  system behaves as a faithful miniature (typical buy ~$162 at 0.65 conviction,
  $250/ticker, $1k/sector) instead of letting one full-size $3,250 order eat
  65% of the budget.
- **Budget accounting is in spend terms** (cost basis of open positions plus
  buys pending execution), not market value: "$5k able to be spent" means
  dollars out the door; appreciation neither frees nor consumes budget.
- **Pending-buy awareness**: the engine now reads today's approved-but-unfilled
  buys from trades.db; they consume budget and block same-day re-approval
  (execute.py's client_order_id dedup means a re-approval could never execute
  anyway — rejecting it keeps the decision log honest). Conservative by
  construction: over-counting can only under-spend.
- **Circuit breaker re-based to dollars vs the bankroll** (3% of $5k = $150
  intraday loss freezes buys). Kept as a percentage of full account equity it
  would be ~20x too insensitive: realistic bad days for a $5k book move equity
  by ~0.1-0.2%, far below the 3% account-level threshold, so the rule would
  effectively never fire. The dollar basis restores its intent at the new
  scale. (Configs without a budget keep the original percentage behavior.)
- **Stop-loss unchanged** (8% below per-position cost basis — already
  budget-independent).
- Tests updated to the new rules-in-force and extended (56 pass): bankroll
  sizing, total-spend cap within and across runs, cost-basis accounting,
  pending-buy blocking, dollar-based breaker, pending_buy_notional DB reads.
- **Routine model bumped Opus 4.8 -> Fable 5** (claude-fable-5) per user
  preference, same rationale as the Opus bump: research quality is the
  system's only source of edge and the engine caps the downside.
- **Caveat flagged to the user**: two stale full-size JNJ $3,250 approvals
  (2026-06-11, from validation runs under pre-budget sizing) sit unexecuted in
  trades.db. Running execute.py on 2026-06-11 would submit one of them and
  blow the new budget's sizing intent. Do not execute today; the rows are
  inert from tomorrow's run_date onward.
