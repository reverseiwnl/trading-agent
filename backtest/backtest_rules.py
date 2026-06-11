"""
backtest_rules.py — backtest of the RISK RULES, not the strategy.

Purpose (per CLAUDE.md / docs/DESIGN.md): the LLM's research judgment cannot be
backtested honestly (training-data hindsight). What CAN be tested is whether the
deterministic rules in config.yaml behave sanely under a multi-year price path:
do stop-losses fire when they should, do position/sector caps hold, is the daily
trade budget respected, does the circuit breaker only freeze buys, and is max
drawdown contained?

A deliberately naive momentum signal generator (SMA20 vs SMA100 trend, conviction
scaled from trailing momentum) stands in for the LLM. Its job is to produce a
realistic stream of buy/sell/hold signals that exercises every rule path — NOT to
make money. Any P&L shown here says nothing about the live system's edge.

Mechanics mirror src/decision_engine.py:
  - same check ordering (watchlist -> stop-frozen -> trade budget -> per-side checks)
  - stop-loss sweep first, exempt from the trade budget, fires under the breaker
  - caps count existing market value plus same-run pending notional, CAP_EPSILON
  - conviction-scaled sizing with the min-order floor
Cadence model: signals are computed from data through close t-1; all decisions
and fills happen at the OPEN of day t (a pre-market run placing market orders).
Circuit breaker compares equity at open t vs equity at close t-1 (overnight gap
stands in for "intraday vs prior close").

The daily loop produces share-level orders; vectorbt replays them independently
(Portfolio.from_orders, shared cash) as a cross-check on the loop's accounting
and supplies drawdown/return stats, plus a buy-and-hold VOO benchmark.

Usage: python backtest/backtest_rules.py            (no args; downloads/caches prices)
       python backtest/backtest_rules.py --stress   (tightened thresholds, proves the
                                                     breaker/budget/stop code paths fire)
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
import vectorbt as vbt
import yaml
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text())
RISK = CONFIG["risk"]
SIZING = CONFIG["sizing"]
WATCHLIST: list[str] = list(CONFIG["watchlist"])
BENCHMARK: str = CONFIG["benchmark"]

# Static sector map (offline backtest; live system reads sectors from data/
# snapshots). Unknown ticker -> itself, same conservative fallback as the engine.
SECTORS: dict[str, str] = {
    "AAPL": "Technology",
    "MSFT": "Technology",
    "NVDA": "Technology",
    "GOOGL": "Communication Services",
    "JNJ": "Healthcare",
    "UNH": "Healthcare",
}

START = "2023-01-01"          # ~6 months warmup + ~2.9 years traded window
WARMUP_DAYS = 130             # SMA100 + 126d momentum need history
INIT_CASH = 100_000.0
CAP_EPSILON = 1e-6            # same tolerance as decision_engine.py

CACHE = Path(__file__).resolve().parent / "price_cache.pkl"
SUMMARY_OUT = Path(__file__).resolve().parent / "last_run_summary.json"


# ---------- Data ----------

def load_prices() -> tuple[pd.DataFrame, pd.DataFrame]:
    """(open, close) DataFrames, dividend/split-adjusted, columns = watchlist +
    benchmark. Adjusted prices matter: NVDA's 2024 10:1 split would otherwise
    look like a -90% day and fire every stop. Cached to disk for offline reruns."""
    tickers = WATCHLIST + [BENCHMARK]
    if CACHE.exists():
        raw = pd.read_pickle(CACHE)
    else:
        raw = yf.download(tickers, start=START, auto_adjust=True, progress=False)
        raw.to_pickle(CACHE)

    if isinstance(raw.columns, pd.MultiIndex):
        # yfinance returns either (field, ticker) or (ticker, field)
        level0 = set(raw.columns.get_level_values(0))
        if "Open" in level0:
            open_df, close_df = raw["Open"], raw["Close"]
        else:
            open_df = raw.xs("Open", axis=1, level=1)
            close_df = raw.xs("Close", axis=1, level=1)
    else:
        raise RuntimeError("expected multi-ticker download with MultiIndex columns")

    open_df = open_df[tickers].dropna(how="any")
    close_df = close_df[tickers].loc[open_df.index]
    return open_df, close_df


# ---------- Naive signal generator (LLM stand-in) ----------

@dataclass
class NaiveSignal:
    ticker: str
    action: str          # buy | sell | hold
    conviction: float


def build_signal_frames(close: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Indicator frames, shifted one day so day t uses data through close t-1."""
    sma_fast = close.rolling(20).mean()
    sma_slow = close.rolling(100).mean()
    mom126 = close.pct_change(126)
    mom63 = close.pct_change(63)
    return {
        "uptrend": (sma_fast > sma_slow).shift(1, fill_value=False),
        "downtrend": (sma_fast < sma_slow).shift(1, fill_value=False),
        # buy conviction: +30% 6-month return -> 1.0 (so the 0.6 buy floor binds)
        "buy_conv": (mom126 / 0.30).clip(0.0, 1.0).shift(1),
        # sell conviction: -10% 3-month return -> 1.0 (so the 0.5 sell floor binds)
        "sell_conv": (-mom63 / 0.10).clip(0.0, 1.0).shift(1),
    }


def signals_for_day(frames: dict[str, pd.DataFrame], day: pd.Timestamp) -> list[NaiveSignal]:
    """One signal per watchlist ticker per day, alphabetical (the live engine
    processes signals in file order; ordering only matters when the trade budget
    binds). Sells are emitted on downtrend regardless of holdings — the engine's
    'cannot sell: no position' path gets exercised, as it would by a sloppy LLM."""
    out: list[NaiveSignal] = []
    for t in sorted(WATCHLIST):
        if frames["uptrend"].at[day, t]:
            out.append(NaiveSignal(t, "buy", float(np.nan_to_num(frames["buy_conv"].at[day, t]))))
        elif frames["downtrend"].at[day, t]:
            out.append(NaiveSignal(t, "sell", float(np.nan_to_num(frames["sell_conv"].at[day, t]))))
        else:
            out.append(NaiveSignal(t, "hold", 0.0))
    return out


# ---------- The rules engine, replicated for simulation ----------

@dataclass
class Position:
    qty: float
    basis: float  # per-share average entry price


@dataclass
class SimResult:
    orders: list[dict] = field(default_factory=list)        # share-level fills
    rejections: Counter = field(default_factory=Counter)    # reason -> count
    stop_exits: list[dict] = field(default_factory=list)
    breaker_days: list[str] = field(default_factory=list)
    budget_bound_days: list[str] = field(default_factory=list)
    buy_weights_at_fill: list[float] = field(default_factory=list)   # position weight after each buy
    sector_weights_at_fill: list[float] = field(default_factory=list)
    equity_close: pd.Series | None = None
    daily_signal_trades: Counter = field(default_factory=Counter)
    reentries_after_stop: list[dict] = field(default_factory=list)


def run_simulation(open_df: pd.DataFrame, close_df: pd.DataFrame) -> SimResult:
    frames = build_signal_frames(close_df[WATCHLIST])
    days = open_df.index[WARMUP_DAYS:]

    cash = INIT_CASH
    positions: dict[str, Position] = {}
    last_stop_day: dict[str, pd.Timestamp] = {}
    res = SimResult()
    equity_close = pd.Series(index=days, dtype=float)

    for day in days:
        opens = open_df.loc[day]
        eq_open = cash + sum(p.qty * opens[t] for t, p in positions.items())
        prev_close_idx = close_df.index.get_loc(day) - 1
        prev_closes = close_df.iloc[prev_close_idx]
        eq_prev_close = cash + sum(p.qty * prev_closes[t] for t, p in positions.items())
        intraday_pnl = (eq_open - eq_prev_close) / eq_prev_close if eq_prev_close else 0.0

        day_str = day.date().isoformat()

        # --- stop-loss sweep: first, signal-independent, budget-exempt ---
        stopped: set[str] = set()
        for t in sorted(positions):
            pos = positions[t]
            drawdown = (pos.basis - opens[t]) / pos.basis
            if drawdown >= RISK["stop_loss_pct"]:
                proceeds = pos.qty * opens[t]
                cash += proceeds
                res.orders.append({"day": day_str, "ticker": t, "shares": -pos.qty,
                                   "price": opens[t], "kind": "stop"})
                res.stop_exits.append({"day": day_str, "ticker": t,
                                       "drawdown_at_exit": drawdown,
                                       "loss_dollars": (pos.basis - opens[t]) * pos.qty})
                stopped.add(t)
                last_stop_day[t] = day
                del positions[t]

        buys_frozen = intraday_pnl <= -RISK["daily_loss_circuit_breaker"]
        if buys_frozen:
            res.breaker_days.append(day_str)

        equity = cash + sum(p.qty * opens[t] for t, p in positions.items())

        sector_exposure: defaultdict[str, float] = defaultdict(float)
        for t, pos in positions.items():
            sector_exposure[SECTORS.get(t, t)] += pos.qty * opens[t]
        pending_by_ticker: defaultdict[str, float] = defaultdict(float)
        pending_by_sector: defaultdict[str, float] = defaultdict(float)
        signal_trades = 0

        # --- signal processing, same check order as apply_risk_rules ---
        for sig in signals_for_day(frames, day):
            if sig.action == "hold":
                continue
            # (watchlist check always passes here: generator only emits watchlist)
            if sig.ticker in stopped:
                res.rejections["stop-frozen (sell dup / buy blocked same day)"] += 1
                continue
            if signal_trades >= RISK["max_trades_per_day"]:
                res.rejections["max trades per day reached"] += 1
                if day_str not in res.budget_bound_days:
                    res.budget_bound_days.append(day_str)
                continue

            if sig.action == "buy":
                if buys_frozen:
                    res.rejections["circuit breaker: buys frozen"] += 1
                    continue
                if sig.conviction < RISK["min_conviction_to_buy"]:
                    res.rejections["conviction below buy threshold"] += 1
                    continue
                dollars = equity * RISK["max_position_pct"] * sig.conviction
                if dollars < SIZING["min_order_dollars"]:
                    res.rejections["sized below minimum order"] += 1
                    continue

                position_cap = equity * RISK["max_position_pct"]
                existing = positions[sig.ticker].qty * opens[sig.ticker] if sig.ticker in positions else 0.0
                would_hold = existing + pending_by_ticker[sig.ticker] + dollars
                if would_hold > position_cap + CAP_EPSILON:
                    res.rejections["position cap"] += 1
                    continue

                sector = SECTORS.get(sig.ticker, sig.ticker)
                sector_cap = equity * RISK["max_sector_pct"]
                would_expose = sector_exposure[sector] + pending_by_sector[sector] + dollars
                if would_expose > sector_cap + CAP_EPSILON:
                    res.rejections["sector cap"] += 1
                    continue

                price = opens[sig.ticker]
                qty = dollars / price
                cash -= dollars
                if sig.ticker in positions:
                    pos = positions[sig.ticker]
                    new_qty = pos.qty + qty
                    pos.basis = (pos.basis * pos.qty + dollars) / new_qty
                    pos.qty = new_qty
                else:
                    positions[sig.ticker] = Position(qty=qty, basis=price)
                    if sig.ticker in last_stop_day:
                        gap = (day - last_stop_day[sig.ticker]).days
                        if gap <= 7:
                            res.reentries_after_stop.append(
                                {"day": day_str, "ticker": sig.ticker, "days_after_stop": gap})
                res.orders.append({"day": day_str, "ticker": sig.ticker, "shares": qty,
                                   "price": price, "kind": "buy"})
                res.buy_weights_at_fill.append(would_hold / equity)
                res.sector_weights_at_fill.append(would_expose / equity)
                pending_by_ticker[sig.ticker] += dollars
                pending_by_sector[sector] += dollars
                signal_trades += 1

            elif sig.action == "sell":
                if sig.ticker not in positions:
                    res.rejections["cannot sell: no position"] += 1
                    continue
                if sig.conviction < RISK["min_conviction_to_sell"]:
                    res.rejections["conviction below sell threshold"] += 1
                    continue
                pos = positions.pop(sig.ticker)
                cash += pos.qty * opens[sig.ticker]
                res.orders.append({"day": day_str, "ticker": sig.ticker, "shares": -pos.qty,
                                   "price": opens[sig.ticker], "kind": "signal_sell"})
                signal_trades += 1

        res.daily_signal_trades[day_str] = signal_trades
        closes = close_df.loc[day]
        equity_close[day] = cash + sum(p.qty * closes[t] for t, p in positions.items())

    res.equity_close = equity_close
    return res


# ---------- vectorbt replay + verification ----------

def max_drawdown(series: pd.Series) -> float:
    return float((series / series.cummax() - 1.0).min())


def run_stress_check(open_df: pd.DataFrame, close_df: pd.DataFrame) -> int:
    """The 2.9y window never trips the circuit breaker or binds the trade budget
    under real config values — which is itself a finding, but leaves those code
    paths unproven. Rerun with deliberately silly thresholds to show each rule
    CAN fire. Does not touch config.yaml; overrides live only in this process."""
    overrides = {"daily_loss_circuit_breaker": 0.005,  # freeze buys on a -0.5% gap
                 "max_trades_per_day": 2,
                 "stop_loss_pct": 0.03}
    print(f"STRESS MODE — overriding {overrides} (config.yaml untouched)")
    RISK.update(overrides)
    res = run_simulation(open_df, close_df)

    checks = {
        "circuit_breaker_days": len(res.breaker_days),
        "buys_rejected_by_breaker": res.rejections.get("circuit breaker: buys frozen", 0),
        "days_trade_budget_bound": len(res.budget_bound_days),
        "budget_rejections": res.rejections.get("max trades per day reached", 0),
        "max_signal_trades_in_a_day": max(res.daily_signal_trades.values(), default=0),
        "stop_sells": sum(1 for o in res.orders if o["kind"] == "stop"),
    }
    print(json.dumps(checks, indent=2))
    assert checks["circuit_breaker_days"] > 0, "breaker path never fired even at 0.5%"
    assert checks["days_trade_budget_bound"] > 0, "budget path never bound even at 2/day"
    assert checks["max_signal_trades_in_a_day"] <= RISK["max_trades_per_day"]
    assert all(e["drawdown_at_exit"] >= RISK["stop_loss_pct"] for e in res.stop_exits)
    print("All stress-mode rule paths fired and held.")
    return 0


def main() -> int:
    open_df, close_df = load_prices()
    if "--stress" in sys.argv[1:]:
        return run_stress_check(open_df, close_df)
    days = open_df.index[WARMUP_DAYS:]
    print(f"Window: {days[0].date()} -> {days[-1].date()} "
          f"({len(days)} trading days, {len(days) / 252:.1f} years)")

    res = run_simulation(open_df, close_df)

    # Independent replay: hand vectorbt only the share-level orders and let it
    # re-derive cash/equity. Catches accounting bugs in the loop above.
    size = pd.DataFrame(0.0, index=days, columns=WATCHLIST)
    for o in res.orders:
        size.at[pd.Timestamp(o["day"]), o["ticker"]] += o["shares"]
    pf = vbt.Portfolio.from_orders(
        close=close_df.loc[days, WATCHLIST],
        size=size, size_type="amount",
        price=open_df.loc[days, WATCHLIST],
        init_cash=INIT_CASH, cash_sharing=True, group_by=True, call_seq="auto",
    )
    vbt_value = pf.value()
    bench = vbt.Portfolio.from_holding(close_df.loc[days, BENCHMARK], init_cash=INIT_CASH)
    bench_value = bench.value()

    drift = float((vbt_value - res.equity_close).abs().max())
    n_filled = int(pf.orders.count())
    assert n_filled == len(res.orders), (
        f"vectorbt filled {n_filled} orders but the loop emitted {len(res.orders)} — "
        "an order was rejected/altered in replay; accounting mismatch")
    assert drift < 1.0, f"loop vs vectorbt equity diverged by ${drift:.2f}"

    # --- rule verification ---
    stop_dds = [e["drawdown_at_exit"] for e in res.stop_exits]
    weights = (
        pd.DataFrame({t: size[t].cumsum() for t in WATCHLIST})
        .mul(close_df.loc[days, WATCHLIST])
        .div(vbt_value, axis=0)
    )
    sector_w = weights.T.groupby(lambda t: SECTORS.get(t, t)).sum().T

    years = len(days) / 252
    summary = {
        "window": [str(days[0].date()), str(days[-1].date())],
        "years": round(years, 2),
        "final_equity": round(float(vbt_value.iloc[-1]), 2),
        "total_return_pct": round(float(pf.total_return()) * 100, 2),
        "cagr_pct": round(((float(vbt_value.iloc[-1]) / INIT_CASH) ** (1 / years) - 1) * 100, 2),
        "max_drawdown_pct": round(float(pf.max_drawdown()) * 100, 2),
        "benchmark_total_return_pct": round(float(bench.total_return()) * 100, 2),
        "benchmark_max_drawdown_pct": round(float(bench.max_drawdown()) * 100, 2),
        "loop_vs_vbt_max_divergence_dollars": round(drift, 6),
        "orders": {
            "total_fills": len(res.orders),
            "buys": sum(1 for o in res.orders if o["kind"] == "buy"),
            "signal_sells": sum(1 for o in res.orders if o["kind"] == "signal_sell"),
            "stop_sells": sum(1 for o in res.orders if o["kind"] == "stop"),
        },
        "rejections": dict(res.rejections),
        "trade_budget": {
            "max_signal_trades_in_a_day": max(res.daily_signal_trades.values(), default=0),
            "days_budget_bound": len(res.budget_bound_days),
            "budget_bound_dates": res.budget_bound_days,
        },
        "circuit_breaker": {
            "days_fired": len(res.breaker_days),
            "dates": res.breaker_days,
        },
        "stop_losses": {
            "count": len(res.stop_exits),
            "min_drawdown_at_exit_pct": round(min(stop_dds) * 100, 2) if stop_dds else None,
            "max_drawdown_at_exit_pct": round(max(stop_dds) * 100, 2) if stop_dds else None,
            "mean_drawdown_at_exit_pct": round(float(np.mean(stop_dds)) * 100, 2) if stop_dds else None,
            "total_realized_stop_losses_dollars": round(sum(e["loss_dollars"] for e in res.stop_exits), 2),
            "by_ticker": dict(Counter(e["ticker"] for e in res.stop_exits)),
            "reentries_within_7_days": res.reentries_after_stop,
        },
        "caps": {
            "max_position_weight_at_buy_pct": round(max(res.buy_weights_at_fill, default=0) * 100, 4),
            "max_sector_weight_at_buy_pct": round(max(res.sector_weights_at_fill, default=0) * 100, 4),
            "max_position_weight_observed_pct": round(float(weights.max().max()) * 100, 2),
            "worst_drift_ticker": str(weights.max().idxmax()),
            "max_sector_weight_observed_pct": round(float(sector_w.max().max()) * 100, 2),
            "worst_drift_sector": str(sector_w.max().idxmax()),
        },
    }

    # hard verification of the caps at decision time
    assert all(w <= RISK["max_position_pct"] + 1e-9 for w in res.buy_weights_at_fill), \
        "a buy exceeded the position cap at fill time"
    assert all(w <= RISK["max_sector_pct"] + 1e-9 for w in res.sector_weights_at_fill), \
        "a buy exceeded the sector cap at fill time"
    assert all(d >= RISK["stop_loss_pct"] for d in stop_dds), \
        "a stop-loss fired above the threshold"
    assert max(res.daily_signal_trades.values(), default=0) <= RISK["max_trades_per_day"], \
        "trade budget exceeded"

    print(json.dumps(summary, indent=2))
    SUMMARY_OUT.write_text(json.dumps(summary, indent=2))
    print(f"\nAll rule assertions passed. Summary written to {SUMMARY_OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
