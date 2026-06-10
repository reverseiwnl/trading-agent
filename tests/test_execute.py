"""
Execution tests for execute.py. No network anywhere: the Alpaca client is a
hand-rolled fake and DB_PATH points at a tmp SQLite file shared with
decision_engine, so approved orders are seeded exactly the way production
writes them (de.log_decisions) and the decision-row linkage is real.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from alpaca.common.exceptions import APIError
from alpaca.trading.enums import OrderSide

import decision_engine as de
import execute as ex

TODAY = date.today().isoformat()
THESIS = "A sufficiently detailed test thesis explaining this trade."


# ---------- fixtures / fakes ----------

def buy_order(ticker: str = "AAPL", notional: float = 4000.0) -> dict:
    return {"ticker": ticker, "side": "buy", "notional": notional, "thesis": THESIS,
            "signal": {"ticker": ticker, "action": "buy", "conviction": 0.8,
                       "thesis": THESIS, "sources": ["test"],
                       "timestamp": datetime.now(timezone.utc).isoformat()}}


def sell_all_order(ticker: str = "MSFT") -> dict:
    return {"ticker": ticker, "side": "sell", "qty": "all",
            "reason": "stop-loss: 10.0% below cost basis (limit 8%)", "signal": None}


def fake_order(status: str, filled_qty: str = "0",
               filled_avg_price: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(id="alpaca-uuid-1", status=status, filled_qty=filled_qty,
                           filled_avg_price=filled_avg_price)


class FakeAlpaca:
    """Scripted paper client: submit_order returns an accepted order; each poll
    pops the next scripted order (the last one repeats forever)."""
    _base_url = SimpleNamespace(value="https://paper-api.alpaca.markets")

    def __init__(self, polls: list[SimpleNamespace],
                 position_qty: float | None = 10.0):
        self.polls = list(polls)
        self.position_qty = position_qty
        self.submissions: list = []

    def submit_order(self, order_data):
        self.submissions.append(order_data)
        return fake_order("accepted")

    def get_order_by_id(self, order_id):
        return self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]

    def get_order_by_client_id(self, client_order_id):
        raise APIError("order not found")

    def get_open_position(self, ticker):
        if self.position_qty is None:
            raise APIError("position does not exist")
        return SimpleNamespace(qty=str(self.position_qty))


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Shared tmp trades.db for both modules; polling made instant. Timeout is
    generous so scripted sequences play out — timeout tests override it to 0."""
    db = tmp_path / "trades.db"
    monkeypatch.setattr(de, "DB_PATH", db)
    monkeypatch.setattr(ex, "DB_PATH", db)
    monkeypatch.setattr(ex, "POLL_TIMEOUT_S", 5.0)
    monkeypatch.setattr(ex, "POLL_INTERVAL_S", 0.0)
    return db


def seed_approved(orders: list[dict]) -> None:
    de.log_decisions(TODAY, orders, [])


def run_main(monkeypatch, client) -> int:
    monkeypatch.setattr(ex, "make_paper_client", lambda: client)
    return ex.main()


def execution_rows(db) -> list[dict]:
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT * FROM executions ORDER BY id").fetchall()
        except sqlite3.OperationalError:  # table never created: nothing ran
            return []
    return [dict(r) for r in rows]


# ---------- paper guard ----------

def test_mode_not_paper_exits_2_and_submits_nothing(tmp_db, monkeypatch):
    seed_approved([buy_order()])
    monkeypatch.setitem(ex.CONFIG, "mode", "live")
    # make_paper_client is NOT patched: the mode gate inside it must fire
    # before any client exists or any env var is read.
    assert ex.main() == 2
    assert execution_rows(tmp_db) == []


def test_client_not_on_paper_endpoint_refused():
    live = SimpleNamespace(_base_url=SimpleNamespace(value="https://api.alpaca.markets"))
    with pytest.raises(ex.PaperGuardError):
        ex.assert_paper_endpoint(live)
    no_url = SimpleNamespace()
    with pytest.raises(ex.PaperGuardError):
        ex.assert_paper_endpoint(no_url)


# ---------- happy path ----------

def test_buy_fills_and_links_to_decision_row(tmp_db, monkeypatch):
    seed_approved([buy_order("AAPL", 4000.0)])
    client = FakeAlpaca(polls=[fake_order("accepted"),
                               fake_order("filled", "18.5", "216.22")])
    assert run_main(monkeypatch, client) == 0

    assert len(client.submissions) == 1
    req = client.submissions[0]
    assert req.symbol == "AAPL"
    assert float(req.notional) == 4000.0
    assert req.side == OrderSide.BUY
    assert req.client_order_id == f"{TODAY}-AAPL-buy"

    (row,) = execution_rows(tmp_db)
    assert row["status"] == "filled"
    assert row["filled_qty"] == 18.5
    assert row["filled_avg_price"] == 216.22
    assert row["alpaca_order_id"] == "alpaca-uuid-1"
    with sqlite3.connect(tmp_db) as conn:
        (decision_id,) = conn.execute(
            "SELECT id FROM decisions WHERE verdict = 'approved'").fetchone()
    assert row["decision_id"] == decision_id


def test_sell_all_submits_position_qty(tmp_db, monkeypatch):
    seed_approved([sell_all_order("MSFT")])
    client = FakeAlpaca(polls=[fake_order("filled", "10", "91.00")], position_qty=10.0)
    assert run_main(monkeypatch, client) == 0
    req = client.submissions[0]
    assert req.symbol == "MSFT"
    assert float(req.qty) == 10.0
    assert req.side == OrderSide.SELL
    assert req.client_order_id == f"{TODAY}-MSFT-sell"


# ---------- unhappy outcomes ----------

def test_rejected_order_recorded_and_exit_1(tmp_db, monkeypatch):
    seed_approved([buy_order()])
    client = FakeAlpaca(polls=[fake_order("rejected")])
    assert run_main(monkeypatch, client) == 1
    (row,) = execution_rows(tmp_db)
    assert row["status"] == "rejected"
    assert "rejected" in row["detail"]


def test_timeout_recorded_unconfirmed_never_assumed_filled(tmp_db, monkeypatch):
    seed_approved([buy_order()])
    monkeypatch.setattr(ex, "POLL_TIMEOUT_S", 0.0)  # one poll, then deadline
    client = FakeAlpaca(polls=[fake_order("accepted")])
    assert run_main(monkeypatch, client) == 1
    (row,) = execution_rows(tmp_db)
    assert row["status"] == "unconfirmed"
    assert "UNCONFIRMED" in row["detail"]


def test_partial_fill_at_timeout_logs_fill_qty(tmp_db, monkeypatch):
    seed_approved([buy_order()])
    monkeypatch.setattr(ex, "POLL_TIMEOUT_S", 0.0)
    client = FakeAlpaca(polls=[fake_order("partially_filled", "3", "200.00")])
    assert run_main(monkeypatch, client) == 1
    (row,) = execution_rows(tmp_db)
    assert row["status"] == "partially_filled"
    assert row["filled_qty"] == 3.0


def test_sell_with_no_position_fails_without_submitting(tmp_db, monkeypatch):
    seed_approved([sell_all_order("MSFT")])
    client = FakeAlpaca(polls=[fake_order("filled")], position_qty=None)
    assert run_main(monkeypatch, client) == 1
    assert client.submissions == []
    (row,) = execution_rows(tmp_db)
    assert row["status"] == "failed"
    assert "no open position" in row["detail"]


# ---------- idempotency ----------

def test_rerun_of_same_day_cannot_double_submit(tmp_db, monkeypatch):
    seed_approved([buy_order("AAPL")])
    client = FakeAlpaca(polls=[fake_order("filled", "18.5", "216.22")])
    assert run_main(monkeypatch, client) == 0
    assert len(client.submissions) == 1

    # Same day, same approved order still in the decisions table: re-running
    # must not reach the API again, and the skip itself is on record.
    assert run_main(monkeypatch, client) == 0
    assert len(client.submissions) == 1
    rows = execution_rows(tmp_db)
    assert [r["status"] for r in rows] == ["filled", "skipped_duplicate"]
    assert rows[1]["client_order_id"] == rows[0]["client_order_id"]


def test_unconfirmed_attempt_also_blocks_resubmit(tmp_db, monkeypatch):
    # An attempt that may have reached Alpaca (UNCONFIRMED) must block a blind
    # retry just as a fill does — clearing it is a manual decision.
    seed_approved([buy_order("AAPL")])
    monkeypatch.setattr(ex, "POLL_TIMEOUT_S", 0.0)
    client = FakeAlpaca(polls=[fake_order("accepted")])
    assert run_main(monkeypatch, client) == 1
    assert run_main(monkeypatch, client) == 1  # skipped, but still needs a human
    assert len(client.submissions) == 1
    statuses = [r["status"] for r in execution_rows(tmp_db)]
    assert statuses == ["unconfirmed", "skipped_duplicate"]


def test_duplicate_rejected_by_alpaca_recovers_existing_order(tmp_db, monkeypatch):
    # Local log lost (fresh executions table) but Alpaca already has the order:
    # submit fails on the duplicate client_order_id, and we adopt + poll the
    # existing order instead of failing or double-submitting.
    class DuplicateRejecting(FakeAlpaca):
        def submit_order(self, order_data):
            self.submissions.append(order_data)
            raise APIError("client_order_id must be unique")

        def get_order_by_client_id(self, client_order_id):
            return fake_order("filled", "18.5", "216.22")

    seed_approved([buy_order("AAPL")])
    client = DuplicateRejecting(polls=[fake_order("filled", "18.5", "216.22")])
    assert run_main(monkeypatch, client) == 0
    (row,) = execution_rows(tmp_db)
    assert row["status"] == "filled"
    assert "recovered existing order" in row["detail"]
