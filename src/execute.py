"""
execute.py — order submission against the Alpaca PAPER endpoint.

Consumes the approved orders decision_engine.py recorded in data/trades.db
(decisions table, verdict='approved', today's run_date). That table is the
durable form of the engine's output and carries the decision row id that every
execution row links back to.

Safety properties:
- Double paper guard before anything touches the network: config.yaml mode must
  be "paper" AND the constructed TradingClient must point at the paper endpoint.
  Either check failing => loud refusal, exit 2, zero submissions.
- Idempotent: client_order_id is derived from run_date+ticker+side. A re-run of
  the same day is skipped locally (executions table) and, as a backstop, Alpaca
  itself rejects a duplicate client_order_id.
- Never assumes success: every order is polled until filled / rejected /
  canceled / expired or a 60s timeout. A partial fill still open at timeout is
  recorded as partially_filled with the fill quantity; anything else still open
  is recorded as UNCONFIRMED for manual review.
- Revalidates at submission time: an approved buy whose notional violates the
  CURRENT position cap or trading budget (config may have changed since
  approval) is refused (refused_stale), logged, and left for the decision
  engine to re-approve under current rules.

Usage: python src/execute.py   (no args — submits today's approved orders)
Exit codes: 0 all filled (or nothing to do / duplicates skipped),
            1 at least one order needs attention, 2 paper guard refused.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime, timezone

import common
from common import CAP_EPSILON, get_logger, load_config, utf8_console
from trading_day import today_iso

utf8_console()
CONFIG = load_config()
DB_PATH = common.DB_PATH  # module-level alias: tests monkeypatch it per module
log = get_logger("execute")

PAPER_ENDPOINT = "https://paper-api.alpaca.markets"
POLL_TIMEOUT_S = 60.0
POLL_INTERVAL_S = 2.0

TERMINAL_FAILURES = {"rejected", "canceled", "expired"}
# Final statuses that don't demand human attention.
OK_FINALS = {"filled", "skipped_duplicate"}


class PaperGuardError(RuntimeError):
    """Either half of the paper-trading guard failed. Nothing may be submitted."""


# ---------- Paper guard ----------

def assert_paper_endpoint(client) -> None:
    """Second half of the guard: the client we actually built must point at the
    paper endpoint, independent of what any config or env var claims."""
    base = getattr(client, "_base_url", None)
    base = str(getattr(base, "value", base) or "")
    if base.rstrip("/") != PAPER_ENDPOINT:
        raise PaperGuardError(
            f"trading client base URL is {base!r}, not the paper endpoint "
            f"{PAPER_ENDPOINT!r} — refusing to submit anything")


def make_paper_client():
    """Both halves of the guard: config says paper, client is built paper=True
    and verifiably points at the paper endpoint."""
    if CONFIG.get("mode") != "paper":
        raise PaperGuardError(
            'config.yaml mode is not "paper" — refusing to construct a trading '
            "client. See PROMOTION_CHECKLIST.md.")

    from alpaca.trading.client import TradingClient

    key, secret = common.alpaca_credentials()
    client = TradingClient(key, secret, paper=True)
    assert_paper_endpoint(client)
    return client


# ---------- Execution log (data/trades.db) ----------

_EXECUTIONS_DDL = """
CREATE TABLE IF NOT EXISTS executions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               TEXT NOT NULL,
    run_date         TEXT NOT NULL,
    decision_id      INTEGER NOT NULL REFERENCES decisions(id),
    client_order_id  TEXT NOT NULL,
    ticker           TEXT NOT NULL,
    side             TEXT NOT NULL,
    request_json     TEXT,
    alpaca_order_id  TEXT,
    status           TEXT NOT NULL,
    filled_qty       REAL,
    filled_avg_price REAL,
    detail           TEXT,
    updated_ts       TEXT
)
"""


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(_EXECUTIONS_DDL)
    return conn


def load_approved_orders(run_date: str) -> list[tuple[int, dict]]:
    """Today's approved orders exactly as decision_engine logged them, oldest
    first, each paired with its decision row id."""
    with closing(_connect()) as conn:
        try:
            rows = conn.execute(
                "SELECT id, order_json FROM decisions "
                "WHERE run_date = ? AND verdict = 'approved' AND order_json IS NOT NULL "
                "ORDER BY id", (run_date,)).fetchall()
        except sqlite3.OperationalError:
            return []  # no decisions table yet: the engine has never run
    return [(decision_id, json.loads(order_json)) for decision_id, order_json in rows]


def prior_submission(client_order_id: str) -> tuple[int, str] | None:
    """(execution id, status) of any earlier attempt that may have reached
    Alpaca. Conservative on purpose: even a submit_failed or unconfirmed
    attempt blocks a same-day resubmit — clearing it is a manual decision,
    never automatic."""
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT id, status FROM executions WHERE client_order_id = ? "
            "AND status NOT IN ('skipped_duplicate', 'refused_stale') "
            "ORDER BY id DESC LIMIT 1",
            (client_order_id,)).fetchone()
    return (row[0], row[1]) if row else None


def _insert_execution(run_date: str, decision_id: int, client_order_id: str,
                      ticker: str, side: str, status: str,
                      request_json: str | None = None, detail: str | None = None) -> int:
    ts = datetime.now(timezone.utc).isoformat()
    with closing(_connect()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO executions (ts, run_date, decision_id, client_order_id, "
            "ticker, side, request_json, status, detail) VALUES (?,?,?,?,?,?,?,?,?)",
            (ts, run_date, decision_id, client_order_id, ticker, side,
             request_json, status, detail))
        return cur.lastrowid


def _update_execution(execution_id: int, *, status: str,
                      alpaca_order_id: str | None = None,
                      filled_qty: float | None = None,
                      filled_avg_price: float | None = None,
                      detail: str | None = None) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute(
            "UPDATE executions SET status = ?, "
            "alpaca_order_id = COALESCE(?, alpaca_order_id), "
            "filled_qty = COALESCE(?, filled_qty), "
            "filled_avg_price = COALESCE(?, filled_avg_price), "
            "detail = COALESCE(?, detail), updated_ts = ? WHERE id = ?",
            (status, alpaca_order_id, filled_qty, filled_avg_price, detail,
             datetime.now(timezone.utc).isoformat(), execution_id))


# ---------- Submission-time revalidation ----------

def current_limits(client) -> dict:
    """Dollar limits under TODAY's config and account state: the per-ticker
    position cap, the trading budget, and the dollars already committed
    (cost basis of open positions). Approved orders can sit in the decisions
    table while config.yaml or the account changes underneath them — the
    notional that was legal at approval time is re-checked here, at the last
    moment before money moves."""
    risk = CONFIG["risk"]
    equity = float(client.get_account().equity)
    budget = risk.get("trading_budget_dollars")
    bankroll = min(equity, budget) if budget else equity
    committed = sum(float(p.qty) * float(p.avg_entry_price)
                    for p in client.get_all_positions())
    return {"position_cap": bankroll * risk["max_position_pct"],
            "budget": budget, "committed": committed}


def revalidation_failure(order: dict, limits: dict) -> str | None:
    """Reason this approved order violates the CURRENT dollar rules, or None.
    A failure refuses the order — re-approving under current rules is the
    decision engine's job, never this module's."""
    if order["side"] != "buy":
        return None  # exits are never blocked by sizing rules
    notional = float(order["notional"])
    if notional > limits["position_cap"] + CAP_EPSILON:
        return (f"stale approval: notional ${notional:,.2f} exceeds the current "
                f"position cap ${limits['position_cap']:,.2f} — re-run the "
                f"decision engine under current config")
    budget = limits["budget"]
    if budget is not None and limits["committed"] + notional > budget + CAP_EPSILON:
        return (f"stale approval: ${notional:,.2f} would push total spend to "
                f"${limits['committed'] + notional:,.2f}, over the "
                f"${budget:,.2f} trading budget")
    return None


# ---------- Submission + confirmation ----------

def derive_client_order_id(run_date: str, ticker: str, side: str) -> str:
    """Stable id per (day, ticker, side): Alpaca rejects a duplicate
    client_order_id, so a same-day re-run cannot double-submit even if the
    local execution log were lost."""
    return f"{run_date}-{ticker}-{side}"


def _order_status(order) -> str:
    return str(getattr(order.status, "value", order.status)).lower()


def _build_request(order: dict, qty: float | None, client_order_id: str):
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    if order["side"] == "buy":
        return MarketOrderRequest(
            symbol=order["ticker"], notional=round(float(order["notional"]), 2),
            side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
            client_order_id=client_order_id)
    return MarketOrderRequest(
        symbol=order["ticker"], qty=qty, side=OrderSide.SELL,
        time_in_force=TimeInForce.DAY, client_order_id=client_order_id)


def poll_until_final(client, order_id) -> dict:
    """Poll until a terminal status or POLL_TIMEOUT_S elapses. A partial fill
    still open at timeout reports partially_filled with the fill quantity;
    anything else still open reports unconfirmed. Success is never assumed."""
    deadline = time.monotonic() + POLL_TIMEOUT_S
    last_status = "unknown"
    last_filled = 0.0
    last_price: float | None = None
    while True:
        # A transient error mid-poll must not crash the run and strand the
        # execution row: keep polling on the last known state until deadline.
        try:
            order = client.get_order_by_id(order_id)
        except Exception as exc:
            log.warning(f"poll error for {order_id} (will retry until timeout): {exc}")
        else:
            last_status = _order_status(order)
            last_filled = float(order.filled_qty or 0)
            last_price = float(order.filled_avg_price) if order.filled_avg_price else None
            if last_status == "filled":
                return {"status": "filled", "filled_qty": last_filled,
                        "filled_avg_price": last_price, "detail": None}
            if last_status in TERMINAL_FAILURES:
                return {"status": last_status, "filled_qty": last_filled,
                        "filled_avg_price": last_price,
                        "detail": f"order ended '{last_status}' at Alpaca"}
        if time.monotonic() >= deadline:
            break
        time.sleep(POLL_INTERVAL_S)

    if last_status == "partially_filled":
        return {"status": "partially_filled", "filled_qty": last_filled,
                "filled_avg_price": last_price,
                "detail": f"timed out after {POLL_TIMEOUT_S:.0f}s with "
                          f"{last_filled} filled — verify remainder manually"}
    return {"status": "unconfirmed", "filled_qty": last_filled or None,
            "filled_avg_price": last_price,
            "detail": f"UNCONFIRMED: still '{last_status}' after "
                      f"{POLL_TIMEOUT_S:.0f}s — verify at Alpaca before any re-run"}


def execute_order(client, decision_id: int, order: dict, run_date: str,
                  limits: dict | None = None) -> str:
    """Submit one approved order, confirm its outcome, log everything.
    Returns the final status recorded in the executions table."""
    from alpaca.common.exceptions import APIError

    ticker, side = order["ticker"], order["side"]
    client_order_id = derive_client_order_id(run_date, ticker, side)

    prior = prior_submission(client_order_id)
    if prior is not None:
        prior_id, prior_status = prior
        _insert_execution(run_date, decision_id, client_order_id, ticker, side,
                          "skipped_duplicate",
                          detail=f"already attempted as execution #{prior_id} "
                                 f"(status: {prior_status}) — refusing to double-submit")
        log.info(f"SKIP {ticker} {side}: {client_order_id} already attempted "
              f"(execution #{prior_id}, status {prior_status})")
        # The skip is always right, but only a filled prior attempt makes it
        # benign: anything else means today's intent is still unexecuted and
        # the run must stay red until a human resolves it.
        return "skipped_duplicate" if prior_status == "filled" else "skipped_unresolved"

    if limits is not None:
        reason = revalidation_failure(order, limits)
        if reason:
            _insert_execution(run_date, decision_id, client_order_id, ticker, side,
                              "refused_stale", detail=reason)
            log.warning(f"REFUSED {ticker} {side}: {reason}")
            return "refused_stale"
        if side == "buy":
            # Claim the dollars now, success or not: over-counting a failed
            # submit can only under-spend later in this run, never overspend.
            limits["committed"] += float(order["notional"])

    qty: float | None = None
    if side == "sell":
        raw_qty = order.get("qty", "all")
        if raw_qty == "all":
            try:
                qty = float(client.get_open_position(ticker).qty)
            except APIError as exc:
                _insert_execution(run_date, decision_id, client_order_id, ticker,
                                  side, "failed", detail=f"no open position to sell: {exc}")
                log.error(f"FAIL {ticker} sell: no open position")
                return "failed"
        else:
            qty = float(raw_qty)

    request = _build_request(order, qty, client_order_id)
    # Row goes in BEFORE the network call: if we crash mid-submit, the attempt
    # is on record and prior_submission() blocks a blind retry.
    execution_id = _insert_execution(
        run_date, decision_id, client_order_id, ticker, side, "submitting",
        request_json=request.model_dump_json(exclude_none=True))

    try:
        submitted = client.submit_order(order_data=request)
    except APIError as exc:
        # Ambiguous: Alpaca may already hold this order (duplicate
        # client_order_id from a crashed run). Look it up before giving up.
        try:
            submitted = client.get_order_by_client_id(client_order_id)
        except APIError:
            _update_execution(execution_id, status="submit_failed",
                              detail=f"submit failed: {exc}")
            log.error(f"FAIL {ticker} {side}: submit rejected by API: {exc}")
            return "submit_failed"
        _update_execution(execution_id, status="submitted",
                          alpaca_order_id=str(submitted.id),
                          detail=f"recovered existing order after submit error: {exc}")
    else:
        _update_execution(execution_id, status="submitted",
                          alpaca_order_id=str(submitted.id))

    final = poll_until_final(client, submitted.id)
    _update_execution(execution_id, status=final["status"],
                      filled_qty=final["filled_qty"],
                      filled_avg_price=final["filled_avg_price"],
                      detail=final["detail"])

    line = f"{final['status'].upper()} {ticker} {side} ({client_order_id})"
    if final["status"] == "filled":
        line += f": qty {final['filled_qty']} @ {final['filled_avg_price']}"
    elif final["detail"]:
        line += f": {final['detail']}"
    log.info(line)
    return final["status"]


# ---------- Entry point ----------

def main() -> int:
    # Hard gate before any DB or network work: both halves of the paper guard.
    try:
        client = make_paper_client()
    except PaperGuardError as exc:
        log.error(f"FATAL: {exc}")
        log.error("Refusing to submit anything. See PROMOTION_CHECKLIST.md.")
        return 2
    except RuntimeError as exc:
        log.error(f"FATAL: {exc}")
        return 2

    run_date = today_iso()
    get_logger("execute", run_date)  # attach today's file log
    orders = load_approved_orders(run_date)
    if not orders:
        log.info(f"No approved orders for {run_date} in {DB_PATH.name}. Nothing to submit.")
        return 0

    # If today's limits can't be read, nothing can be revalidated — fail
    # closed and submit nothing rather than trust approval-time sizing.
    try:
        limits = current_limits(client)
    except Exception as exc:
        log.error(f"FATAL: could not read account state to revalidate orders: {exc}")
        log.error("Nothing submitted.")
        return 1

    results: dict[str, str] = {}
    for decision_id, order in orders:
        key = f"{order['ticker']}:{order['side']}:decision#{decision_id}"
        # One order's unexpected failure (network, parsing) must not strand
        # the rest of today's approved orders unattempted.
        try:
            results[key] = execute_order(client, decision_id, order, run_date, limits)
        except Exception as exc:
            results[key] = "error"
            log.error(f"ERROR {order['ticker']} {order['side']}: unexpected failure "
                  f"(check the executions table before any re-run): {exc}")

    log.info(json.dumps({"run_date": run_date, "results": results}, indent=2))
    return 0 if all(status in OK_FINALS for status in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
