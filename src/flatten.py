"""
flatten.py — kill switch. One command: close every open position at market.

Usage: python src/flatten.py

Behavior:
- Same double paper guard as execute.py — refuses (exit 2) unless config says
  paper AND the client verifiably points at the paper endpoint.
- Cancels ALL open orders first, so nothing pending can fill after the flatten
  and no earlier unfilled sell can stack on top of the ones submitted here.
- Then submits a market sell for the freshly-fetched quantity of every open
  position, polls each to confirmation (success never assumed), and logs every
  close to data/trades.db as a decision row (verdict 'approved', reason
  'kill-switch') with a linked execution row — the same audit trail as normal
  trading, so the digest picks flattens up automatically.
- client_order_id is flatten-<date>-<ticker>-<attempt n>, so a re-run after a
  failed close can still act: open orders were just canceled and only the
  current remaining qty is sold, so a re-run cannot double-sell.

Exit codes: 0 flat (all closes filled, or nothing to close),
            1 at least one close needs attention — NOT flat, check the log,
            2 paper guard refused.
"""

from __future__ import annotations

import sys
from contextlib import closing
from datetime import date, datetime, timezone

# Explicit UTF-8 console: an API error string outside the legacy Windows code
# page must never crash the kill switch of all things.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from decision_engine import _DECISIONS_DDL
# All DB access goes through execute's helpers, so flatten shares its DB_PATH
# (and tests only have to patch execute.DB_PATH).
from execute import (
    PaperGuardError,
    _connect,
    _insert_execution,
    _update_execution,
    make_paper_client,
    poll_until_final,
)


def log_flatten_decision(run_date: str, ticker: str, qty: float) -> int:
    """Kill-switch closes get a decision row like any other trade, so each
    execution links back to an auditable 'why'. Returns the decision id.

    order_json stays NULL on purpose: execute.py treats any approved decision
    row with order_json as a pending order for the day, and must never re-sell
    a flatten on a same-day re-run. The full order is on the execution row's
    request_json instead."""
    with closing(_connect()) as conn, conn:
        conn.execute(_DECISIONS_DDL)
        cur = conn.execute(
            "INSERT INTO decisions (ts, run_date, signal_json, verdict, reason, order_json) "
            "VALUES (?, ?, NULL, 'approved', ?, NULL)",
            (datetime.now(timezone.utc).isoformat(), run_date,
             f"kill-switch: manual flatten — sell all {qty:g} {ticker} at market"))
        return cur.lastrowid


def next_attempt(run_date: str, ticker: str) -> int:
    """1-based attempt counter so a re-run after a failed close gets a fresh
    client_order_id instead of being blocked by the duplicate check."""
    prefix = f"flatten-{run_date}-{ticker}-"
    with closing(_connect()) as conn:
        (count,) = conn.execute(
            "SELECT COUNT(*) FROM executions WHERE client_order_id LIKE ?",
            (prefix + "%",)).fetchone()
    return count + 1


def close_position(client, ticker: str, qty: float, run_date: str) -> str:
    """Market-sell one position's full current quantity, confirm, log.
    Returns the final status recorded in the executions table."""
    from alpaca.common.exceptions import APIError
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    decision_id = log_flatten_decision(run_date, ticker, qty)
    client_order_id = f"flatten-{run_date}-{ticker}-{next_attempt(run_date, ticker)}"
    request = MarketOrderRequest(symbol=ticker, qty=qty, side=OrderSide.SELL,
                                 time_in_force=TimeInForce.DAY,
                                 client_order_id=client_order_id)
    # Row goes in BEFORE the network call, same as execute.py: a crash
    # mid-submit still leaves the attempt on record.
    execution_id = _insert_execution(
        run_date, decision_id, client_order_id, ticker, "sell", "submitting",
        request_json=request.model_dump_json(exclude_none=True), detail="kill-switch")

    try:
        submitted = client.submit_order(order_data=request)
    except APIError as exc:
        _update_execution(execution_id, status="submit_failed",
                          detail=f"kill-switch submit failed: {exc}")
        print(f"FAIL {ticker}: submit rejected by API: {exc}")
        return "submit_failed"
    _update_execution(execution_id, status="submitted",
                      alpaca_order_id=str(submitted.id))

    final = poll_until_final(client, submitted.id)
    _update_execution(execution_id, status=final["status"],
                      filled_qty=final["filled_qty"],
                      filled_avg_price=final["filled_avg_price"],
                      detail=final["detail"])
    line = f"{final['status'].upper()} {ticker} sell ({client_order_id})"
    if final["status"] == "filled":
        line += f": qty {final['filled_qty']} @ {final['filled_avg_price']}"
    elif final["detail"]:
        line += f": {final['detail']}"
    print(line)
    return final["status"]


def main() -> int:
    try:
        client = make_paper_client()
    except PaperGuardError as exc:
        print(f"FATAL: {exc}")
        print("Refusing to flatten anything. See PROMOTION_CHECKLIST.md.")
        return 2

    run_date = date.today().isoformat()

    # Open orders first: an unfilled buy turning into a position mid-flatten,
    # or a stale sell stacking onto ours, would defeat the point.
    canceled = client.cancel_orders()
    print(f"canceled {len(canceled or [])} open order(s)")

    positions = [(p.symbol, abs(float(p.qty))) for p in client.get_all_positions()]
    if not positions:
        print("No open positions — already flat.")
        return 0

    statuses = {ticker: close_position(client, ticker, qty, run_date)
                for ticker, qty in sorted(positions)}

    if all(s == "filled" for s in statuses.values()):
        print(f"FLAT: closed {len(statuses)} position(s).")
        return 0
    bad = {t: s for t, s in statuses.items() if s != "filled"}
    print(f"NOT FLAT: {len(bad)} close(s) need attention: {bad}")
    print("Re-running flatten.py is safe: it cancels open orders and sells only "
          "the remaining quantity.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
