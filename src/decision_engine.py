"""
decision_engine.py — the compliance desk.

Takes the LLM's signals file as UNTRUSTED INPUT, validates it strictly, applies
the risk rules in config.yaml, and emits approved orders. The LLM never bypasses
this module. Any validation failure => no trade + alert, never a guess.

Usage: python src/decision_engine.py signals/signals_YYYY-MM-DD.json
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError

# Explicit UTF-8 everywhere: Windows defaults file I/O and console output to a
# legacy code page, and a thesis or error string outside it must never crash or
# corrupt a run.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
DB_PATH = ROOT / "data" / "trades.db"

# Cap comparisons use a tiny dollar tolerance so an order sized exactly at a cap
# passes, and float noise alone can never tip a rejection.
CAP_EPSILON = 1e-6


# ---------- Schema (mirrors signals/schema.json; pydantic is the enforcer) ----------

class Signal(BaseModel):
    model_config = {"extra": "forbid"}

    ticker: str = Field(pattern=r"^[A-Z]{1,5}$")
    action: Literal["buy", "sell", "hold"]
    conviction: float = Field(ge=0.0, le=1.0)
    thesis: str = Field(min_length=20, max_length=500)
    sources: list[str] = Field(min_length=1)
    timestamp: str


class DailySignals(BaseModel):
    model_config = {"extra": "forbid"}

    date: str
    market_context: str = Field(max_length=1000)
    signals: list[Signal] = Field(max_length=25)


# ---------- Portfolio state ----------

def load_sector(ticker: str, run_date: str | None = None) -> str:
    """Sector from today's data/ snapshot. Unknown -> the ticker itself, so an
    unclassified position can never hide inside an existing sector bucket."""
    snap = ROOT / "data" / (run_date or date.today().isoformat()) / f"{ticker}.json"
    try:
        sector = json.loads(snap.read_text(encoding="utf-8")).get("fundamentals", {}).get("sector")
    except (OSError, ValueError):
        sector = None
    return sector or ticker


def get_portfolio_state() -> dict:
    """Current equity, positions {ticker: {qty, cost_basis (per-share avg entry),
    current_price, market_value, sector}}, intraday P&L pct vs prior close, and a
    sector map covering every ticker the engine might trade today."""
    from alpaca.trading.client import TradingClient
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY not set (see .env.example)")

    client = TradingClient(key, secret, paper=True)
    account = client.get_account()
    equity = float(account.equity)
    last_equity = float(account.last_equity or 0)
    intraday_pnl_pct = (equity - last_equity) / last_equity if last_equity else 0.0

    positions: dict[str, dict] = {}
    for p in client.get_all_positions():
        qty = float(p.qty)
        current_price = float(p.current_price)
        market_value = float(p.market_value) if p.market_value is not None else qty * current_price
        positions[p.symbol] = {
            "qty": qty,
            "cost_basis": float(p.avg_entry_price),
            "current_price": current_price,
            "market_value": market_value,
            "sector": load_sector(p.symbol),
        }

    sectors = {t: load_sector(t) for t in CONFIG["watchlist"]}
    sectors.update({t: pos["sector"] for t, pos in positions.items()})

    return {"equity": equity, "intraday_pnl_pct": intraday_pnl_pct,
            "positions": positions, "sectors": sectors}


# ---------- Risk checks ----------

def stop_loss_sweep(portfolio: dict) -> list[dict]:
    """Sell orders for every position >= stop_loss_pct below cost basis. Runs on
    every invocation regardless of what the signals file says, and is exempt from
    max_trades_per_day — risk exits are never rationed."""
    stop_pct = CONFIG["risk"]["stop_loss_pct"]
    orders: list[dict] = []
    for ticker, pos in sorted(portfolio["positions"].items()):
        basis = pos["cost_basis"]
        if basis <= 0:
            continue
        drawdown = (basis - pos["current_price"]) / basis
        if drawdown >= stop_pct:
            orders.append({
                "ticker": ticker, "side": "sell", "qty": "all",
                "reason": f"stop-loss: {drawdown:.1%} below cost basis (limit {stop_pct:.0%})",
                "signal": None,
            })
    return orders


def apply_risk_rules(parsed: DailySignals, portfolio: dict) -> tuple[list[dict], list[dict]]:
    """Return (approved_orders, rejections). Each rejection carries a reason —
    rejections are logged and alerted, never silently dropped."""
    risk = CONFIG["risk"]
    equity = portfolio["equity"]
    positions = portfolio["positions"]
    sectors = portfolio.get("sectors", {})
    approved: list[dict] = []
    rejected: list[dict] = []

    def reject(sig: Signal, reason: str) -> None:
        rejected.append({"signal": sig.model_dump(), "reason": reason})

    def sector_of(ticker: str) -> str:
        if ticker in positions:
            return positions[ticker].get("sector") or ticker
        return sectors.get(ticker) or ticker

    # Risk exits first: independent of signals, exempt from the daily trade cap.
    stop_orders = stop_loss_sweep(portfolio)
    approved.extend(stop_orders)
    stopped = {o["ticker"] for o in stop_orders}

    # Circuit breaker: no new buys on a bad day
    buys_frozen = portfolio["intraday_pnl_pct"] <= -risk["daily_loss_circuit_breaker"]

    # Exposure already on the books, plus what this run approves as it goes.
    # Pending sells do NOT free up room: a fill is never assumed.
    sector_exposure: defaultdict[str, float] = defaultdict(float)
    for ticker, pos in positions.items():
        sector_exposure[sector_of(ticker)] += pos["market_value"]
    pending_by_ticker: defaultdict[str, float] = defaultdict(float)
    pending_by_sector: defaultdict[str, float] = defaultdict(float)

    signal_trades = 0  # stop-loss exits above don't count toward max_trades_per_day

    for sig in parsed.signals:
        if sig.action == "hold":
            continue
        if sig.ticker not in set(CONFIG["watchlist"]) | set(positions):
            reject(sig, "ticker not in watchlist or holdings")
            continue
        if sig.ticker in stopped:
            reject(sig, "position is already being closed by the stop-loss sweep"
                   if sig.action == "sell" else
                   "buy blocked: ticker hit its stop-loss this run")
            continue
        if signal_trades >= risk["max_trades_per_day"]:
            reject(sig, "max trades per day reached")
            continue

        if sig.action == "buy":
            if buys_frozen:
                reject(sig, "circuit breaker: buys frozen")
                continue
            if sig.conviction < risk["min_conviction_to_buy"]:
                reject(sig, "conviction below buy threshold")
                continue
            dollars = equity * risk["max_position_pct"] * sig.conviction
            if dollars < CONFIG["sizing"]["min_order_dollars"]:
                reject(sig, "sized below minimum order")
                continue

            position_cap = equity * risk["max_position_pct"]
            existing = positions.get(sig.ticker, {}).get("market_value", 0.0)
            would_hold = existing + pending_by_ticker[sig.ticker] + dollars
            if would_hold > position_cap + CAP_EPSILON:
                reject(sig, f"position cap: would hold ${would_hold:,.2f} of {sig.ticker} "
                            f"vs ${position_cap:,.2f} limit ({risk['max_position_pct']:.0%} of equity)")
                continue

            sector = sector_of(sig.ticker)
            sector_cap = equity * risk["max_sector_pct"]
            would_expose = sector_exposure[sector] + pending_by_sector[sector] + dollars
            if would_expose > sector_cap + CAP_EPSILON:
                reject(sig, f"sector cap: {sector} would reach ${would_expose:,.2f} "
                            f"vs ${sector_cap:,.2f} limit ({risk['max_sector_pct']:.0%} of equity)")
                continue

            approved.append({"ticker": sig.ticker, "side": "buy", "notional": round(dollars, 2),
                             "thesis": sig.thesis, "signal": sig.model_dump()})
            pending_by_ticker[sig.ticker] += dollars
            pending_by_sector[sector] += dollars
            signal_trades += 1

        elif sig.action == "sell":
            if sig.ticker not in positions:
                reject(sig, "cannot sell: no position")
                continue
            if sig.conviction < risk["min_conviction_to_sell"]:
                reject(sig, "conviction below sell threshold")
                continue
            approved.append({"ticker": sig.ticker, "side": "sell", "qty": "all",
                             "thesis": sig.thesis, "signal": sig.model_dump()})
            signal_trades += 1

    return approved, rejected


# ---------- Decision log (data/trades.db) ----------

_DECISIONS_DDL = """
CREATE TABLE IF NOT EXISTS decisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    run_date    TEXT NOT NULL,
    signal_json TEXT,
    verdict     TEXT NOT NULL CHECK (verdict IN ('approved', 'rejected')),
    reason      TEXT NOT NULL,
    order_json  TEXT
)
"""


def _write_rows(rows: list[tuple]) -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB_PATH)) as conn, conn:
        conn.execute(_DECISIONS_DDL)
        conn.executemany(
            "INSERT INTO decisions (ts, run_date, signal_json, verdict, reason, order_json) "
            "VALUES (?, ?, ?, ?, ?, ?)", rows)


def log_decisions(run_date: str, approved: list[dict], rejected: list[dict]) -> None:
    """Persist every decision this run made — approved or not — with the raw
    signal that produced it and the resulting order, if any. Stop-loss exits
    have no originating signal (signal_json NULL)."""
    ts = datetime.now(timezone.utc).isoformat()
    rows: list[tuple] = []
    for order in approved:
        signal = order.get("signal")
        order_payload = {k: v for k, v in order.items() if k != "signal"}
        rows.append((ts, run_date, json.dumps(signal) if signal else None, "approved",
                     order.get("reason", "passed all risk checks"), json.dumps(order_payload)))
    for rej in rejected:
        rows.append((ts, run_date, json.dumps(rej["signal"]), "rejected",
                     rej["reason"], None))
    _write_rows(rows)


def log_run_rejection(run_date: str, raw: str, reason: str) -> None:
    """Whole-file rejection (malformed or stale signals): one row, raw payload kept."""
    ts = datetime.now(timezone.utc).isoformat()
    _write_rows([(ts, run_date, raw, "rejected", reason, None)])


# ---------- Entry point ----------

def main() -> int:
    # Hard gate before anything else runs: this engine only ever trades paper.
    if CONFIG.get("mode") != "paper":
        print("FATAL: mode != paper. Refusing to run. See PROMOTION_CHECKLIST.md.")
        return 2

    if len(sys.argv) != 2:
        print("usage: python src/decision_engine.py <signals.json>")
        return 2

    run_date = date.today().isoformat()
    raw = Path(sys.argv[1]).read_text(encoding="utf-8")
    try:
        parsed = DailySignals.model_validate_json(raw)
    except ValidationError as e:
        print("REJECTED: signals failed validation. NO TRADES TODAY.")
        print(e)
        log_run_rejection(run_date, raw, f"signals failed schema validation: {e}")
        return 1

    if parsed.date != run_date:
        print("REJECTED: signals file is not dated today. NO TRADES TODAY.")
        log_run_rejection(run_date, raw, f"signals dated {parsed.date}, expected {run_date}")
        return 1

    portfolio = get_portfolio_state()
    approved, rejected = apply_risk_rules(parsed, portfolio)
    log_decisions(run_date, approved, rejected)

    print(json.dumps({"approved": approved, "rejected": rejected}, indent=2))
    # execute.py consumes `approved`; it re-checks the paper guard itself.
    return 0


if __name__ == "__main__":
    sys.exit(main())
