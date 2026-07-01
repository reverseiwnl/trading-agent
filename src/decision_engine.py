"""
decision_engine.py — the compliance desk.

Takes the LLM's signals file as UNTRUSTED INPUT, validates it strictly, applies
the risk rules in config.yaml, and emits approved orders. The LLM never bypasses
this module. Any validation failure => no trade + alert, never a guess.

Usage: python src/decision_engine.py signals/signals_YYYY-MM-DD.json
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

import common
from common import CAP_EPSILON, ROOT, get_logger, load_config, utf8_console
from trading_day import today_iso

utf8_console()
CONFIG = load_config()
DB_PATH = common.DB_PATH  # module-level alias: tests monkeypatch it per module
log = get_logger("decision_engine")


# ---------- Schema (mirrors signals/schema.json; pydantic is the enforcer) ----------

class Signal(BaseModel):
    model_config = {"extra": "forbid"}

    ticker: str = Field(pattern=r"^[A-Z]{1,5}$")
    action: Literal["buy", "sell", "hold"]
    conviction: float = Field(ge=0.0, le=1.0)
    thesis: str = Field(min_length=20, max_length=500)
    sources: list[str] = Field(min_length=1)
    timestamp: str

    @field_validator("timestamp")
    @classmethod
    def _timestamp_is_iso8601(cls, v: str) -> str:
        datetime.fromisoformat(v)  # raises ValueError -> ValidationError
        return v


class DailySignals(BaseModel):
    model_config = {"extra": "forbid"}

    date: str
    market_context: str = Field(max_length=1000)
    signals: list[Signal] = Field(max_length=25)


# ---------- Portfolio state ----------

def load_sector(ticker: str, run_date: str | None = None) -> str | None:
    """Sector from today's data/ snapshot, or None when unknown. A None sector
    blocks new buys (the sector cap cannot be enforced without it) and parks an
    existing position's exposure in its own bucket — it never hides inside, or
    escapes from, a real sector's cap accounting."""
    snap = ROOT / "data" / (run_date or today_iso()) / f"{ticker}.json"
    try:
        sector = json.loads(snap.read_text(encoding="utf-8")).get("fundamentals", {}).get("sector")
    except (OSError, ValueError):
        sector = None
    return sector or None


def get_portfolio_state() -> dict:
    """Current equity, positions {ticker: {qty, cost_basis (per-share avg entry),
    current_price, market_value, sector}}, intraday P&L pct vs prior close, and a
    sector map covering every ticker the engine might trade today."""
    from alpaca.trading.client import TradingClient

    key, secret = common.alpaca_credentials()
    client = TradingClient(key, secret, paper=True)
    account = client.get_account()
    equity = float(account.equity)
    last_equity = float(account.last_equity or 0)

    # Deposits/withdrawals move equity without being P&L: a withdrawal must not
    # masquerade as a crash (false breaker trip) and a deposit must not mask a
    # real loss. Subtract today's net cash flow; if the activities endpoint
    # fails, fall back to the unadjusted number (a phantom freeze is the safe
    # failure mode, a masked loss is not — and only a same-day deposit could
    # mask one).
    net_cash_flow = 0.0
    try:
        activities = client.get("/account/activities",
                                {"activity_types": "CSD,CSW", "date": today_iso()}) or []
        net_cash_flow = sum(float(a["net_amount"]) for a in activities)
    except Exception:
        pass

    intraday_pnl_dollars = equity - last_equity - net_cash_flow
    intraday_pnl_pct = intraday_pnl_dollars / last_equity if last_equity else 0.0

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
            "intraday_pnl_dollars": intraday_pnl_dollars,
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


def apply_risk_rules(parsed: DailySignals, portfolio: dict,
                     prior_buys: dict[str, float] | None = None,
                     prior_buy_tickers: set[str] | None = None) -> tuple[list[dict], list[dict]]:
    """Return (approved_orders, rejections). Each rejection carries a reason —
    rejections are logged and alerted, never silently dropped.

    prior_buys maps ticker -> notional of buys already approved earlier today
    but not yet verifiably filled. They consume budget AND sector headroom
    (a fill is never assumed for sells, so a pending buy must be assumed for
    exposure), and block a same-day re-approval.

    prior_buy_tickers is every ticker with ANY approved buy today, filled or
    not (defaults to prior_buys' keys). One buy per ticker per day: execute.py
    derives client_order_id from (date, ticker, side), so a second approval
    could never actually execute — rejecting it keeps the log honest.

    When risk.trading_budget_dollars is set, the engine's bankroll is
    min(equity, budget): sizing, position/sector caps, and the circuit breaker
    all scale to the bankroll, and total spend (cost basis of open positions
    plus buys pending execution) can never exceed the budget."""
    risk = CONFIG["risk"]
    equity = portfolio["equity"]
    budget = risk.get("trading_budget_dollars")
    bankroll = min(equity, budget) if budget else equity
    prior_buys = prior_buys or {}
    prior_buy_tickers = (set(prior_buys) if prior_buy_tickers is None
                         else prior_buy_tickers | set(prior_buys))
    positions = portfolio["positions"]
    sectors = portfolio.get("sectors", {})
    approved: list[dict] = []
    rejected: list[dict] = []

    def reject(sig: Signal, reason: str) -> None:
        rejected.append({"signal": sig.model_dump(), "reason": reason})

    def sector_of(ticker: str) -> str | None:
        """Real sector, or None when no snapshot classified the ticker."""
        if ticker in positions:
            return positions[ticker].get("sector")
        return sectors.get(ticker)

    def sector_bucket(ticker: str) -> str:
        """Exposure bucket: the real sector, or the ticker's own bucket when
        unclassified — unknown exposure never hides inside a real sector."""
        return sector_of(ticker) or ticker

    # Risk exits first: independent of signals, exempt from the daily trade cap.
    stop_orders = stop_loss_sweep(portfolio)
    approved.extend(stop_orders)
    stopped = {o["ticker"] for o in stop_orders}

    # Circuit breaker: no new buys on a bad day. With a budget, a "bad day" is
    # measured in dollars against the bankroll (the account's idle cash would
    # otherwise dilute the percentage ~equity/bankroll-fold and the breaker
    # could never trip).
    if budget:
        pnl_dollars = portfolio.get("intraday_pnl_dollars")
        if pnl_dollars is None:
            pct = portfolio["intraday_pnl_pct"]
            pnl_dollars = equity * pct / (1 + pct) if pct > -1 else -equity
        buys_frozen = pnl_dollars <= -risk["daily_loss_circuit_breaker"] * bankroll
    else:
        buys_frozen = portfolio["intraday_pnl_pct"] <= -risk["daily_loss_circuit_breaker"]

    # Exposure already on the books, plus what this run approves as it goes.
    # Pending sells do NOT free up room: a fill is never assumed. Buys approved
    # earlier today count toward their sector for the same reason.
    sector_exposure: defaultdict[str, float] = defaultdict(float)
    for ticker, pos in positions.items():
        sector_exposure[sector_bucket(ticker)] += pos["market_value"]
    pending_by_ticker: defaultdict[str, float] = defaultdict(float)
    pending_by_sector: defaultdict[str, float] = defaultdict(float)
    for ticker, notional in prior_buys.items():
        pending_by_sector[sector_bucket(ticker)] += notional

    # Budget accounting is in SPEND terms (cost basis + buys awaiting
    # execution), not market value: "$5k able to be spent" means dollars out
    # the door, and a winner's appreciation neither frees nor consumes budget.
    spent = sum(p["qty"] * p["cost_basis"] for p in positions.values())
    spent += sum(prior_buys.values())
    pending_spend = 0.0

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
            if sig.ticker in prior_buys:
                reject(sig, "an approved buy for this ticker is already pending "
                            "execution today")
                continue
            if sig.ticker in prior_buy_tickers:
                reject(sig, "a buy for this ticker was already approved and executed "
                            "today — one buy per ticker per day (the shared "
                            "client_order_id could never submit a second one)")
                continue
            if buys_frozen:
                reject(sig, "circuit breaker: buys frozen")
                continue
            if sig.conviction < risk["min_conviction_to_buy"]:
                reject(sig, "conviction below buy threshold")
                continue
            dollars = bankroll * risk["max_position_pct"] * sig.conviction
            if dollars < CONFIG["sizing"]["min_order_dollars"]:
                reject(sig, "sized below minimum order")
                continue

            position_cap = bankroll * risk["max_position_pct"]
            existing = positions.get(sig.ticker, {}).get("market_value", 0.0)
            would_hold = existing + pending_by_ticker[sig.ticker] + dollars
            if would_hold > position_cap + CAP_EPSILON:
                reject(sig, f"position cap: would hold ${would_hold:,.2f} of {sig.ticker} "
                            f"vs ${position_cap:,.2f} limit ({risk['max_position_pct']:.0%} of bankroll)")
                continue

            sector = sector_of(sig.ticker)
            if sector is None:
                reject(sig, "sector unknown — no data snapshot classifies this "
                            "ticker, so the sector cap cannot be enforced; no trade")
                continue
            sector_cap = bankroll * risk["max_sector_pct"]
            would_expose = sector_exposure[sector] + pending_by_sector[sector] + dollars
            if would_expose > sector_cap + CAP_EPSILON:
                reject(sig, f"sector cap: {sector} would reach ${would_expose:,.2f} "
                            f"vs ${sector_cap:,.2f} limit ({risk['max_sector_pct']:.0%} of bankroll)")
                continue

            if budget is not None:
                would_spend = spent + pending_spend + dollars
                if would_spend > budget + CAP_EPSILON:
                    reject(sig, f"trading budget: spend would reach ${would_spend:,.2f} "
                                f"vs ${budget:,.2f} budget")
                    continue

            approved.append({"ticker": sig.ticker, "side": "buy", "notional": round(dollars, 2),
                             "thesis": sig.thesis, "signal": sig.model_dump()})
            pending_by_ticker[sig.ticker] += dollars
            pending_by_sector[sector] += dollars
            pending_spend += dollars
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


def pending_buy_notional(run_date: str) -> dict[str, float]:
    """Ticker -> notional of buys approved earlier today (decisions table) with
    no verifiably filled execution yet. These consume budget and block same-day
    re-approval. Conservative on purpose: an approved-but-unconfirmed buy still
    claims its dollars — over-counting can only under-spend, never overspend.
    Same-day re-approvals share one client_order_id slot, so max() not sum()."""
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            rows = conn.execute(
                "SELECT order_json FROM decisions WHERE run_date = ? "
                "AND verdict = 'approved' AND order_json IS NOT NULL", (run_date,)).fetchall()
            try:
                filled = {t for (t,) in conn.execute(
                    "SELECT ticker FROM executions WHERE run_date = ? AND side = 'buy' "
                    "AND status IN ('filled', 'partially_filled')", (run_date,))}
            except sqlite3.OperationalError:
                filled = set()  # no executions table yet: nothing has run
    except sqlite3.OperationalError:
        return {}  # no decisions table yet: the engine has never run
    pending: dict[str, float] = {}
    for (order_json,) in rows:
        order = json.loads(order_json)
        if order.get("side") == "buy" and order["ticker"] not in filled:
            pending[order["ticker"]] = max(pending.get(order["ticker"], 0.0),
                                           float(order["notional"]))
    return pending


def approved_buy_tickers(run_date: str) -> set[str]:
    """Every ticker with ANY approved buy today, filled or not. Used to block
    a same-day re-approval after a fill: client_order_id is (date, ticker,
    side), so a second buy could never submit — approving it would put an
    order in the log that silently cannot happen."""
    try:
        with closing(sqlite3.connect(DB_PATH)) as conn:
            rows = conn.execute(
                "SELECT order_json FROM decisions WHERE run_date = ? "
                "AND verdict = 'approved' AND order_json IS NOT NULL", (run_date,)).fetchall()
    except sqlite3.OperationalError:
        return set()  # no decisions table yet: the engine has never run
    return {order["ticker"] for (order_json,) in rows
            if (order := json.loads(order_json)).get("side") == "buy"}


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
    """Validate today's signals file, apply the risk rules, log and print the
    verdicts. Returns the process exit code (0 decided / 1 rejected / 2 fatal)."""
    # Hard gate before anything else runs: this engine only ever trades paper.
    if CONFIG.get("mode") != "paper":
        log.error("FATAL: mode != paper. Refusing to run. See PROMOTION_CHECKLIST.md.")
        return 2

    if len(sys.argv) != 2:
        log.error("usage: python src/decision_engine.py <signals.json>")
        return 2

    run_date = today_iso()
    get_logger("decision_engine", run_date)  # attach today's file log
    signals_path = Path(sys.argv[1])
    try:
        raw = signals_path.read_text(encoding="utf-8")
    except OSError as e:
        log.error(f"REJECTED: cannot read signals file {signals_path}. NO TRADES TODAY.")
        log.error(str(e))
        log_run_rejection(run_date, str(signals_path), f"signals file unreadable: {e}")
        return 1
    try:
        parsed = DailySignals.model_validate_json(raw)
    except ValidationError as e:
        log.error("REJECTED: signals failed validation. NO TRADES TODAY.")
        log.error(str(e))
        log_run_rejection(run_date, raw, f"signals failed schema validation: {e}")
        return 1

    if parsed.date != run_date:
        log.error("REJECTED: signals file is not dated today. NO TRADES TODAY.")
        log_run_rejection(run_date, raw, f"signals dated {parsed.date}, expected {run_date}")
        return 1

    try:
        portfolio = get_portfolio_state()
    except Exception as e:
        log.error(f"FATAL: could not read portfolio state, no decisions made: {e}")
        return 2

    log.debug(f"run {run_date}: {len(parsed.signals)} signals, equity "
              f"{portfolio['equity']:.2f}, intraday {portfolio['intraday_pnl_pct']:+.4%}, "
              f"positions {sorted(portfolio['positions'])}")
    approved, rejected = apply_risk_rules(parsed, portfolio, pending_buy_notional(run_date),
                                          approved_buy_tickers(run_date))
    log_decisions(run_date, approved, rejected)
    for order in approved:
        log.debug(f"APPROVED {order['ticker']} {order['side']}: "
                  f"{order.get('reason', order.get('thesis', ''))}")
    for rej in rejected:
        log.debug(f"REJECTED {rej['signal']['ticker']} {rej['signal']['action']}: "
                  f"{rej['reason']}")

    # stdout contract: execute.py's input is the decisions table, but humans
    # and the routine read this JSON verbatim — keep it the last thing printed.
    log.info(json.dumps({"approved": approved, "rejected": rejected}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
