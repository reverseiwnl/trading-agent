# Backtest results — risk rules only (2026-06-10)

**What was tested:** the deterministic risk rules from `config.yaml`, replicated
faithfully from `src/decision_engine.py` in `backtest/backtest_rules.py` and run
with vectorbt over **2023-07-12 → 2026-06-10 (732 trading days, ~2.9 years)** of
daily adjusted prices for the watchlist (AAPL, MSFT, NVDA, GOOGL, JNJ, UNH),
starting from $100,000.

**What was NOT tested:** the LLM's research judgment. Per DESIGN.md, it cannot be
backtested honestly — any historical "LLM signal" is contaminated by training-data
hindsight. A deliberately naive momentum generator (SMA20/SMA100 trend, conviction
scaled from trailing 126d/63d returns, one signal per ticker per day) stands in for
it. **The P&L numbers below are therefore meaningless as evidence of edge.** They
exist only to show the rules' behavior under a realistic multi-year order flow.
The only honest performance validation remains months of paper trading vs VOO.

Cadence model: signals use data through close t−1; all decisions and fills happen
at the open of day t (mirrors a pre-market run placing market orders). The
circuit-breaker check uses the overnight gap (equity at open vs prior close) as
the stand-in for "intraday vs prior close."

Reproduce: `python backtest/backtest_rules.py` (add `--stress` for the
tightened-threshold code-path check). Machine-readable output in
`backtest/last_run_summary.json`.

## Headline numbers

| Metric | Rules portfolio | VOO buy-and-hold |
|---|---|---|
| Total return | +6.6% | +68.8% |
| CAGR | +2.2% | — |
| **Max drawdown** | **−4.7%** | **−18.7%** |
| Fills | 47 (25 buys, 14 signal sells, 8 stop sells) | 1 |

The return gap is expected and not the point: the naive signal is bad, and the
caps keep the portfolio ≤30% deployed (6 tickers × 5%), so ~70%+ sits in cash.
The relevant result is the drawdown line: **the rules contained max drawdown to
−4.7% through a window that included an −18.7% benchmark drawdown.** Containment
comes mostly from the position caps limiting deployment, secondarily from stops.

## Rule-by-rule verdicts

Every run hard-asserts these; the loop's accounting was independently replayed by
`vectorbt.Portfolio.from_orders` (shared cash) with **$0.00 max divergence** and an
identical fill count, so the numbers below aren't an artifact of one buggy simulator.

| Rule | Verdict | Evidence |
|---|---|---|
| Position cap (5% at buy) | **Holds** | Max weight at any fill: exactly 5.0000%; 1,396 buy attempts rejected by the cap |
| Sector cap (20% at buy) | Holds, but see surprise #4 | Max sector weight at fill 15.7%; **zero** rejections — unreachable with this watchlist |
| Stop-loss (8% below basis) | **Fires correctly**, but see surprise #1 | 8 stops, all ≥8.19% below basis at exit; none missed |
| Trade budget (5/day) | Holds; never bound | Max 4 signal trades in a day; stress mode (budget=2) proves the rejection path works |
| Circuit breaker (−3% intraday) | Path works; never fired naturally — surprise #3 | 0 trips in 2.9y; stress mode (−0.5%) trips it 5 days, rejecting 23 buys |
| Conviction floors (0.6 buy / 0.5 sell) | Hold | 1,463 buy + 82 sell rejections |
| Same-day stop freeze | Holds | All 8 stop days: duplicate sell / re-entry buy rejected |
| Min order floor ($50) | Never binds | At $100k equity the smallest passable buy is $3,000 |

The 1,404 "cannot sell: no position" rejections are the generator emitting sells
for unheld downtrending names; the engine handles them gracefully, as it must for
a sloppy LLM.

## Surprises (the actual findings)

1. **The 8% stop is a trigger, not an exit price.** Mean drawdown at stop exit was
   10.1%, worst **17.0%** (UNH gapping down through the level — at daily cadence
   the engine can't act until the next morning's open). Total realized stop losses
   were modest ($3,285 on $100k), but anyone reading "8% stop-loss" as "max 8%
   loss per position" is wrong. With single-position exposure capped at 5%, the
   worst observed stop cost ~0.9% of equity — the position cap, not the stop, is
   what bounds per-name damage.

2. **Stop → next-day re-entry whipsaw is real.** The engine freezes a stopped
   ticker only for the same run. Twice the naive signal re-bought within days —
   NVDA the **very next day** (Dec 2023): stopped on an 8%+ dip while the trend
   was still up, re-entered at roughly the same price, 8% loss realized for
   nothing. Two instances in 2.9 years is tolerable, but a momentum-flavored LLM
   would hit the same pattern. Worth considering a multi-day cooldown in
   config.yaml (human decision; engine currently encodes one day by design).

3. **The circuit breaker is effectively unreachable at current caps.** With
   deployment capped near 30% of equity, a −3% overnight equity gap needs a ~10%
   simultaneous adverse move across holdings. It never came close in 2.9 years
   (which included Aug 2024 and the 2025 drawdowns). It's a tail-event guard
   only — fine, but don't expect it to be doing day-to-day work. If the watchlist
   ever grows enough for deployment to approach 100%, −3% becomes an ordinary bad
   day and the breaker will start binding; revisit the threshold then.

4. **The sector cap cannot bind at decision time with this watchlist.** Max
   sector exposure at buy is 3 tech names × 5% = 15% < 20%. Zero sector-cap
   rejections all run. It's dormant insurance that only matters if the watchlist
   grows; don't mistake "never rejected anything" for "tested by live data."
   (Stress paths aside, the arithmetic makes it unreachable.)

5. **Winners drift above the 5% cap and nothing trims them.** The cap applies at
   buy time only; NVDA appreciated to **9.0%** of equity (Technology sector to
   16.2%). The stop is relative to cost basis, so a doubled position must give
   back its entire gain plus 8% before any rule forces an exit. Drift is
   unbounded in principle. Not necessarily wrong — letting winners run is a
   defensible policy — but it's an undocumented one; the system's true max
   single-name exposure is "5% + however far it runs," not 5%.

6. **The position cap doubles as a top-up blocker.** Once a high-conviction
   position is on, the daily re-emitted buy signal is rejected by the cap (the
   bulk of those 1,396 rejections). Averaging up is only possible after the
   position has lost value or conviction was low at entry. Emergent, conservative,
   and worth knowing about.

## Bottom line

The rules engine does what the compliance desk should: caps held exactly at
decision time, stops fired on every breach, budget/breaker/freeze paths all
provably work, and max drawdown stayed contained at −4.7%. The caveats are about
*coverage*, not correctness — gap risk through stops (#1), post-stop re-entry
(#2), and unbounded winner drift (#5) are policy gaps a human should explicitly
accept or close in config.yaml before promotion. None of this validates the
LLM layer; that remains paper trading's job.
