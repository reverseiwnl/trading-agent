"""
flatten.py — kill switch. One command: close every open position at market.

Usage: python src/flatten.py

Behavior:
- Same double paper guard as execute.py — refuses (exit 2) unless config says
  paper AND the client verifiably points at the paper endpoint.
- Cancels ALL open orders first and WAITS until none remain open (cancellation
  is async at Alpaca — a pending_cancel order can still fill). If anything is
  still open after CANCEL_WAIT_S it refuses to proceed (exit 1) rather than
  sell into pending orders.
- Then submits a market order closing the freshly-fetched quantity of every
  open position (sell a long, buy to cover a short), polls each to
  confirmation (success never assumed), and logs every close to
  data/trades.db as a decision row (verdict 'approved', reason 'kill-switch')
  with a linked execution row — the same audit trail as normal trading, so
  the digest picks flattens up automatically.
- client_order_id is flatten-<date>-<ticker>-<attempt n>, so a re-run after a
  failed close can still act: open orders were just canceled and only the
  current remaining qty is sold, so a re-run cannot double-sell.

Exit codes: 0 flat (all closes filled, or nothing to close),
            1 at least one close needs attention — NOT flat, check the log,
            2 paper guard refused.
"""

from __future__ import annotations

import sys
import time
from contextlib import closing
from datetime import datetime, timezone

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
from trading_day import today_iso

# Cancellation is asynchronous at Alpaca: cancel_orders() requests it, but an
# order can sit pending_cancel — and still FILL — afterwards. Selling into
# that would defeat the flatten, so wait until nothing is open (or refuse).
CANCEL_WAIT_S = 30.0
CANCEL_POLL_INTERVAL_S = 1.0


def log_flatten_decision(run_date: str, ticker: str, qty: float, side: str) -> int:
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
             f"kill-switch: manual flatten — {side} all {qty:g} {ticker} at market"))
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


def close_position(client, ticker: str, qty: float, side: str, run_date: str) -> str:
    """Market order closing one position's full current quantity (sell a long,
    buy to cover a short), confirm, log. Returns the final recorded status."""
    from alpaca.common.exceptions import APIError
    from alpaca.trading.enums import OrderSide, TimeInForce
    from alpaca.trading.requests import MarketOrderRequest

    decision_id = log_flatten_decision(run_date, ticker, qty, side)
    client_order_id = f"flatten-{run_date}-{ticker}-{next_attempt(run_date, ticker)}"
    request = MarketOrderRequest(symbol=ticker, qty=qty,
                                 side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                                 time_in_force=TimeInForce.DAY,
                                 client_order_id=client_order_id)
    # Row goes in BEFORE the network call, same as execute.py: a crash
    # mid-submit still leaves the attempt on record.
    execution_id = _insert_execution(
        run_date, decision_id, client_order_id, ticker, side, "submitting",
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
    line = f"{final['status'].upper()} {ticker} {side} ({client_order_id})"
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
    except RuntimeError as exc:
        print(f"FATAL: {exc}")
        return 2

    run_date = today_iso()

    # Open orders first: an unfilled buy turning into a position mid-flatten,
    # or a stale sell stacking onto ours, would defeat the point.
    canceled = client.cancel_orders()
    print(f"canceled {len(canceled or [])} open order(s)")

    # Cancellation is async: confirm nothing is still open before reading
    # positions, or a late fill could leave the account not flat.
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest

    deadline = time.monotonic() + CANCEL_WAIT_S
    while True:
        still_open = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN)) or []
        if not still_open:
            break
        if time.monotonic() >= deadline:
            print(f"NOT FLAT: {len(still_open)} order(s) still open "
                  f"{CANCEL_WAIT_S:.0f}s after cancel — refusing to sell into "
                  f"pending orders. Re-run flatten.py.")
            return 1
        time.sleep(CANCEL_POLL_INTERVAL_S)

    positions = []
    for p in client.get_all_positions():
        qty = float(p.qty)
        # A short (negative qty) is closed by buying it back; selling abs(qty)
        # would double the short. Shorting is disallowed upstream, but the
        # kill switch must handle whatever the account actually holds.
        positions.append((p.symbol, abs(qty), "sell" if qty > 0 else "buy"))
    if not positions:
        print("No open positions — already flat.")
        return 0

    statuses = {ticker: close_position(client, ticker, qty, side, run_date)
                for ticker, qty, side in sorted(positions)}

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
