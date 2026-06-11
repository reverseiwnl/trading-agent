"""
Kill-switch tests for flatten.py. No network: the Alpaca client is a fake and
all DB writes go through execute's helpers, so patching execute.DB_PATH to a
tmp file covers everything flatten touches.
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderSide

import execute as ex
import flatten as fl
from trading_day import today_iso

TODAY = today_iso()


# ---------- fakes ----------

class FakeFlattenClient:
    """Paper client with positions. Every submitted sell polls straight to
    `poll_status` ('filled' fills the full position qty). `events` records
    call order so tests can assert cancels happen before sells."""
    _base_url = SimpleNamespace(value="https://paper-api.alpaca.markets")

    def __init__(self, positions: dict[str, float], poll_status: str = "filled",
                 open_orders: int = 0):
        self.positions = dict(positions)
        self.poll_status = poll_status
        self.open_orders = open_orders
        self.submissions: list = []
        self.events: list[str] = []

    def cancel_orders(self):
        self.events.append("cancel_orders")
        canceled, self.open_orders = self.open_orders, 0
        return [SimpleNamespace(id=f"old-{i}") for i in range(canceled)]

    def get_orders(self, filter=None):
        self.events.append("get_orders")
        return [SimpleNamespace(id=f"open-{i}") for i in range(self.open_orders)]

    def get_all_positions(self):
        return [SimpleNamespace(symbol=t, qty=str(q))
                for t, q in self.positions.items()]

    def submit_order(self, order_data):
        self.events.append(f"submit:{order_data.symbol}")
        self.submissions.append(order_data)
        return SimpleNamespace(id=f"alp-{order_data.symbol}", status="accepted",
                               filled_qty="0", filled_avg_price=None)

    def get_order_by_id(self, order_id):
        symbol = str(order_id).removeprefix("alp-")
        if self.poll_status == "filled":
            return SimpleNamespace(id=order_id, status="filled",
                                   filled_qty=str(self.positions[symbol]),
                                   filled_avg_price="100.00")
        return SimpleNamespace(id=order_id, status=self.poll_status,
                               filled_qty="0", filled_avg_price=None)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "trades.db"
    monkeypatch.setattr(ex, "DB_PATH", db)  # flatten does all DB work via execute
    monkeypatch.setattr(ex, "POLL_TIMEOUT_S", 5.0)
    monkeypatch.setattr(ex, "POLL_INTERVAL_S", 0.0)
    return db


def run_main(monkeypatch, client) -> int:
    monkeypatch.setattr(fl, "make_paper_client", lambda: client)
    return fl.main()


def rows(db, table: str) -> list[dict]:
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        try:
            out = conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()
        except sqlite3.OperationalError:
            return []
    return [dict(r) for r in out]


# ---------- paper guard ----------

def test_mode_not_paper_refuses_and_touches_nothing(tmp_db, monkeypatch):
    monkeypatch.setitem(ex.CONFIG, "mode", "live")
    # make_paper_client NOT patched: the real guard must fire first.
    assert fl.main() == 2
    assert rows(tmp_db, "executions") == []
    assert rows(tmp_db, "decisions") == []


# ---------- happy path ----------

def test_closes_every_position_and_logs_full_audit_trail(tmp_db, monkeypatch):
    client = FakeFlattenClient({"AAPL": 10.0, "MSFT": 5.5}, open_orders=2)
    assert run_main(monkeypatch, client) == 0

    # both positions sold at market, full current qty, flatten-specific ids
    assert [(r.symbol, float(r.qty), r.side) for r in client.submissions] == \
        [("AAPL", 10.0, OrderSide.SELL), ("MSFT", 5.5, OrderSide.SELL)]
    assert client.submissions[0].client_order_id == f"flatten-{TODAY}-AAPL-1"
    assert client.submissions[1].client_order_id == f"flatten-{TODAY}-MSFT-1"

    decisions = rows(tmp_db, "decisions")
    executions = rows(tmp_db, "executions")
    assert len(decisions) == len(executions) == 2
    assert all(d["verdict"] == "approved" for d in decisions)
    assert all("kill-switch" in d["reason"] for d in decisions)
    # order_json must stay NULL so execute.py never re-submits a flatten
    assert all(d["order_json"] is None for d in decisions)
    assert [e["decision_id"] for e in executions] == [d["id"] for d in decisions]
    assert all(e["status"] == "filled" for e in executions)
    assert json.loads(executions[0]["request_json"])["symbol"] == "AAPL"


def test_open_orders_canceled_before_any_sell(tmp_db, monkeypatch):
    client = FakeFlattenClient({"AAPL": 10.0}, open_orders=3)
    assert run_main(monkeypatch, client) == 0
    assert client.events[0] == "cancel_orders"
    assert client.events.index("cancel_orders") < client.events.index("submit:AAPL")


def test_no_positions_is_already_flat(tmp_db, monkeypatch):
    client = FakeFlattenClient({})
    assert run_main(monkeypatch, client) == 0
    assert client.submissions == []
    # stale orders still get canceled, and the cancel is verified complete
    assert client.events == ["cancel_orders", "get_orders"]
    assert rows(tmp_db, "executions") == []


def test_refuses_to_sell_while_orders_still_open(tmp_db, monkeypatch):
    # Cancellation is async at Alpaca: if orders are STILL open after the
    # cancel wait, selling into them could leave the account not flat —
    # refuse and exit 1 without submitting anything.
    class StuckCancel(FakeFlattenClient):
        def cancel_orders(self):
            self.events.append("cancel_orders")
            return [SimpleNamespace(id="old-0")]  # requested, but stays open

    monkeypatch.setattr(fl, "CANCEL_WAIT_S", 0.0)
    client = StuckCancel({"AAPL": 10.0}, open_orders=1)
    assert run_main(monkeypatch, client) == 1
    assert client.submissions == []
    assert rows(tmp_db, "executions") == []


def test_short_position_closed_with_buy_to_cover(tmp_db, monkeypatch):
    # Shorting is disallowed upstream, but the kill switch must close whatever
    # the account holds: a -7 qty short closes with a BUY for 7, never a sell.
    class ShortAware(FakeFlattenClient):
        def get_order_by_id(self, order_id):
            return SimpleNamespace(id=order_id, status="filled",
                                   filled_qty="7", filled_avg_price="100.00")

    client = ShortAware({"MSFT": -7.0})
    assert run_main(monkeypatch, client) == 0
    (req,) = client.submissions
    assert (req.symbol, float(req.qty), req.side) == ("MSFT", 7.0, OrderSide.BUY)


# ---------- unhappy outcomes ----------

def test_unconfirmed_close_means_not_flat_exit_1(tmp_db, monkeypatch):
    client = FakeFlattenClient({"AAPL": 10.0}, poll_status="accepted")
    monkeypatch.setattr(ex, "POLL_TIMEOUT_S", 0.0)
    assert run_main(monkeypatch, client) == 1
    (row,) = rows(tmp_db, "executions")
    assert row["status"] == "unconfirmed"


def test_submit_failure_recorded_exit_1(tmp_db, monkeypatch):
    class SubmitFails(FakeFlattenClient):
        def submit_order(self, order_data):
            raise APIError("insufficient something")

    client = SubmitFails({"AAPL": 10.0})
    assert run_main(monkeypatch, client) == 1
    (row,) = rows(tmp_db, "executions")
    assert row["status"] == "submit_failed"
    assert "kill-switch submit failed" in row["detail"]


# ---------- re-run safety ----------

def test_rerun_after_failed_close_gets_fresh_id_and_current_qty(tmp_db, monkeypatch):
    client = FakeFlattenClient({"AAPL": 10.0}, poll_status="canceled")
    assert run_main(monkeypatch, client) == 1  # close attempt died at Alpaca

    # position partially gone by the second attempt; only the remainder sells
    client.positions["AAPL"] = 4.0
    client.poll_status = "filled"
    assert run_main(monkeypatch, client) == 0

    ids = [r.client_order_id for r in client.submissions]
    assert ids == [f"flatten-{TODAY}-AAPL-1", f"flatten-{TODAY}-AAPL-2"]
    assert float(client.submissions[1].qty) == 4.0
    statuses = [r["status"] for r in rows(tmp_db, "executions")]
    assert statuses == ["canceled", "filled"]
