"""
Tests for report.py, centered on the VOO benchmark — the honesty mechanism.
No network: deposits/closes are fed in directly, trades.db is a tmp file, and
the digest is rendered from constructed state.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import date, datetime, timezone

import pytest

import decision_engine as de
import execute as ex
import report as rp

TODAY = date.today().isoformat()
THESIS = "A sufficiently detailed test thesis explaining this trade."

ACCOUNT = {"equity": 101_000.0, "cash": 1_000.0, "intraday_pnl_pct": 0.001}


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "trades.db"
    for mod in (rp, de, ex):
        monkeypatch.setattr(mod, "DB_PATH", db)
    return db


def deposits(db) -> list[dict]:
    with closing(sqlite3.connect(db)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT * FROM benchmark_deposits ORDER BY id").fetchall()]


# ---------- deposits ----------

def test_inception_seeded_exactly_once(tmp_db):
    assert rp.ensure_inception(100_000.0, "2026-06-10") is True
    assert rp.ensure_inception(123_456.0, "2026-06-11") is False  # never reseeds
    (row,) = deposits(tmp_db)
    assert (row["activity_id"], row["deposit_date"], row["amount"]) == \
        ("inception", "2026-06-10", 100_000.0)
    assert row["shares"] is None  # unpriced until a close on/after its date exists
    assert rp.inception_date() == "2026-06-10"


def test_record_deposit_idempotent_by_activity_id(tmp_db):
    assert rp.record_deposit("act-1", "2026-06-12", 5_000.0) is True
    assert rp.record_deposit("act-1", "2026-06-12", 5_000.0) is False
    assert len(deposits(tmp_db)) == 1


def test_mirror_deposits_skips_activity_inside_inception(tmp_db):
    class FakeClient:
        def get(self, path, params):
            assert path == "/account/activities"
            return [
                {"id": "a0", "date": "2026-06-10", "net_amount": "100000"},  # = inception
                {"id": "a1", "date": "2026-06-12", "net_amount": "5000"},
                {"id": "a2", "date": "2026-06-15", "net_amount": "-2000"},   # withdrawal
            ]

    rp.ensure_inception(100_000.0, "2026-06-10")
    added, warnings = rp.mirror_deposits(FakeClient(), "2026-06-10")
    assert (added, warnings) == (2, [])
    amounts = {d["activity_id"]: d["amount"] for d in deposits(tmp_db)}
    assert amounts == {"inception": 100_000.0, "a1": 5_000.0, "a2": -2_000.0}
    # second mirror adds nothing
    assert rp.mirror_deposits(FakeClient(), "2026-06-10") == (0, [])


def test_mirror_deposits_failure_is_a_warning_not_a_crash(tmp_db):
    class Broken:
        def get(self, path, params):
            raise RuntimeError("activities endpoint down")

    rp.ensure_inception(100_000.0, "2026-06-10")
    added, warnings = rp.mirror_deposits(Broken(), "2026-06-10")
    assert added == 0
    assert "benchmark deposits may be stale" in warnings[0]


# ---------- pricing ----------

CLOSES = [("2026-06-09", 490.0), ("2026-06-10", 500.0), ("2026-06-12", 505.0)]


def test_deposit_priced_at_first_actual_close_on_or_after_date(tmp_db):
    rp.ensure_inception(100_000.0, "2026-06-10")
    rp.record_deposit("a1", "2026-06-11", 1_010.0)  # 06-11 has no bar (holiday/weekend)
    assert rp.price_open_deposits(CLOSES) == []
    rows = {d["activity_id"]: d for d in deposits(tmp_db)}
    assert rows["inception"]["voo_close_date"] == "2026-06-10"
    assert rows["inception"]["shares"] == pytest.approx(100_000.0 / 500.0)
    # weekend deposit rolls forward to the NEXT close, never back to a cheaper one
    assert rows["a1"]["voo_close_date"] == "2026-06-12"
    assert rows["a1"]["shares"] == pytest.approx(1_010.0 / 505.0)


def test_deposit_with_no_close_yet_stays_cash(tmp_db):
    rp.ensure_inception(100_000.0, "2026-06-13")  # after the last bar we hold
    assert rp.price_open_deposits(CLOSES) == []
    (row,) = deposits(tmp_db)
    assert row["shares"] is None
    state = rp.benchmark_state(505.0)
    assert state["unpriced_cash"] == 100_000.0
    assert state["value"] == 100_000.0  # face value until its first close prints


def test_deposit_older_than_bar_window_never_guessed(tmp_db):
    rp.record_deposit("old", "2026-04-01", 1_000.0)  # before the oldest bar
    warnings = rp.price_open_deposits(CLOSES)
    assert any("cannot price it honestly" in w for w in warnings)
    (row,) = deposits(tmp_db)
    assert row["shares"] is None  # NOT priced at the wrong (window-start) close


def test_pricing_is_idempotent(tmp_db):
    rp.ensure_inception(100_000.0, "2026-06-10")
    rp.price_open_deposits(CLOSES)
    first = deposits(tmp_db)
    rp.price_open_deposits([("2026-06-12", 999.0)])  # later, different prices
    assert deposits(tmp_db) == first  # priced once, at the original close, forever


# ---------- valuation ----------

def test_benchmark_state_math(tmp_db):
    rp.ensure_inception(100_000.0, "2026-06-10")   # -> 200 shares @ 500
    rp.record_deposit("a1", "2026-06-13", 1_000.0)  # no close yet -> cash
    rp.price_open_deposits(CLOSES)
    state = rp.benchmark_state(510.0)
    assert state["deposits"] == 101_000.0
    assert state["shares"] == pytest.approx(200.0)
    assert state["unpriced_cash"] == 1_000.0
    assert state["value"] == pytest.approx(200.0 * 510.0 + 1_000.0)


def test_benchmark_state_empty_is_none(tmp_db):
    assert rp.benchmark_state(510.0) is None


# ---------- VOO snapshot loading ----------

def test_load_voo_closes_uses_latest_snapshot(tmp_path, monkeypatch, tmp_db):
    monkeypatch.setattr(rp, "ROOT", tmp_path)
    for d, closes, price in [("2026-06-09", [("2026-06-08", 488.0)], 489.0),
                             ("2026-06-10", [("2026-06-09", 490.0)], 500.5)]:
        snap_dir = tmp_path / "data" / d
        snap_dir.mkdir(parents=True)
        (snap_dir / "VOO.json").write_text(json.dumps({
            "ticker": "VOO", "price": price,
            "bars": [{"date": bd, "close": c, "open": c, "high": c, "low": c,
                      "volume": 1} for bd, c in closes]}))
    closes, price, snap_date = rp.load_voo_closes()
    assert snap_date == "2026-06-10"
    assert price == 500.5
    assert closes == [("2026-06-09", 490.0)]


def test_load_voo_closes_missing_raises(tmp_path, monkeypatch, tmp_db):
    monkeypatch.setattr(rp, "ROOT", tmp_path)
    with pytest.raises(FileNotFoundError):
        rp.load_voo_closes()


# ---------- digest rendering ----------

def seed_day(tmp_db) -> None:
    """One approved buy, one rejection, and a filled execution — written
    exactly the way production writes them."""
    signal = {"ticker": "AAPL", "action": "buy", "conviction": 0.8, "thesis": THESIS,
              "sources": ["test"], "timestamp": datetime.now(timezone.utc).isoformat()}
    approved = [{"ticker": "AAPL", "side": "buy", "notional": 4000.0,
                 "thesis": THESIS, "signal": signal}]
    rejected = [{"signal": {**signal, "ticker": "NVDA", "conviction": 0.4},
                 "reason": "conviction below buy threshold"}]
    de.log_decisions(TODAY, approved, rejected)
    eid = ex._insert_execution(TODAY, 1, f"{TODAY}-AAPL-buy", "AAPL", "buy", "submitting")
    ex._update_execution(eid, status="filled", alpaca_order_id="alp-1",
                         filled_qty=18.5, filled_avg_price=216.22)


def render(tmp_db, **overrides) -> str:
    rp.ensure_inception(100_000.0, "2026-06-10")
    rp.price_open_deposits(CLOSES)  # -> 200 shares @ 500
    kwargs = dict(
        run_date=TODAY, account=ACCOUNT,
        positions=[{"ticker": "AAPL", "qty": 18.5, "avg_entry": 216.22, "price": 220.0,
                    "market_value": 4070.0, "unrealized_pl": 69.93,
                    "unrealized_pl_pct": 0.0175}],
        bench=rp.benchmark_state(502.5), voo_price=502.5,
        voo_label="latest actual VOO price, snapshot " + TODAY,
        signals={"date": TODAY, "market_context": "Calm tape.",
                 "signals": [{"ticker": "AAPL", "action": "buy", "conviction": 0.8,
                              "thesis": THESIS}]},
        decisions=rp.load_decisions(TODAY), executions=rp.load_executions(TODAY),
        manifest={"errors": ["could not fetch holdings"], "tickers": {"UNH": "partial"}},
        warnings=["a report warning"])
    kwargs.update(overrides)
    return rp.render_digest(**kwargs)


def test_digest_shows_verdicts_executions_benchmark_and_errors(tmp_db):
    seed_day(tmp_db)
    digest = render(tmp_db)

    # benchmark: system pnl = 101000-100000 = +1000; bench = 200*502.5 = 100500
    # -> bench pnl +500; system ahead by +500. The honesty number must be exact.
    assert "**System minus benchmark: +$500.00**" in digest
    assert "+$1,000.00 (+1.00%)" in digest      # system cumulative P&L
    assert "+$500.00 (+0.50%)" in digest        # VOO counterfactual P&L
    assert "200.0000 shares" in digest

    # verdicts: approved AND rejected with reasons
    assert "### Approved (1)" in digest and "$4,000.00" in digest
    assert "### Rejected (1)" in digest
    assert "conviction below buy threshold" in digest

    # executions, signals, manifest errors, report warnings
    assert "| AAPL | buy | filled | 18.5 | $216.22 |" in digest
    assert "Calm tape." in digest
    assert "could not fetch holdings" in digest
    assert "data pull for UNH: partial" in digest
    assert "a report warning" in digest


def test_digest_when_system_lags_benchmark_shows_negative(tmp_db):
    digest = render(tmp_db, account={**ACCOUNT, "equity": 100_100.0})
    # system pnl +100 vs bench +500 -> honesty number is negative
    assert "**System minus benchmark: -$400.00**" in digest


def test_digest_handles_a_day_where_nothing_ran(tmp_db):
    digest = render(tmp_db, signals=None, decisions=[], executions=[],
                    manifest=None, warnings=[])
    assert "No signals file for today" in digest
    assert "decision engine did not run today" in digest
    assert "No executions recorded today." in digest
    assert "no data manifest for today" in digest


def test_digest_shows_whole_file_rejection(tmp_db):
    de.log_run_rejection(TODAY, "{not json", "signals failed schema validation: boom")
    digest = render(tmp_db)
    assert "signals failed schema validation: boom" in digest
