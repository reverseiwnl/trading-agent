"""
Risk-rule tests for decision_engine. No network anywhere: portfolio state is a
hand-built fixture and main()-level tests monkeypatch get_portfolio_state and
DB_PATH. Config values come from the real config.yaml — the tests assert the
actual rules in force ($5k trading budget as the bankroll regardless of the
$100k equity => $250/ticker cap, $1k/sector cap, 5 trades/day, 8% stop,
circuit breaker at -3% of bankroll in dollars, 0.6 buy conviction floor).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pytest

import decision_engine as de
from trading_day import trading_today

TODAY = trading_today().isoformat()
YESTERDAY = (trading_today() - timedelta(days=1)).isoformat()
THESIS = "A sufficiently detailed test thesis explaining this trade."


def signal(ticker: str, action: str, conviction: float = 0.8) -> de.Signal:
    return de.Signal(ticker=ticker, action=action, conviction=conviction,
                     thesis=THESIS, sources=["test"],
                     timestamp=datetime.now(timezone.utc).isoformat())


def daily(signals: list[de.Signal], day: str = TODAY) -> de.DailySignals:
    return de.DailySignals(date=day, market_context="test context", signals=signals)


def position(qty: float, cost_basis: float, current_price: float, sector: str) -> dict:
    return {"qty": qty, "cost_basis": cost_basis, "current_price": current_price,
            "market_value": qty * current_price, "sector": sector}


WATCHLIST_SECTORS = {"AAPL": "Technology", "MSFT": "Technology", "NVDA": "Technology",
                     "GOOGL": "Communication Services", "JNJ": "Healthcare",
                     "UNH": "Healthcare"}


@pytest.fixture
def portfolio() -> dict:
    """$100k equity, flat on the day, no positions, every watchlist ticker
    classified (a buy with an UNKNOWN sector is rejected outright — covered
    by its own test)."""
    return {"equity": 100_000.0, "intraday_pnl_pct": 0.0, "positions": {},
            "sectors": dict(WATCHLIST_SECTORS)}


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db = tmp_path / "trades.db"
    monkeypatch.setattr(de, "DB_PATH", db)
    return db


def run_main(monkeypatch, signals_path) -> int:
    monkeypatch.setattr(sys, "argv", ["decision_engine.py", str(signals_path)])
    return de.main()


def db_verdicts(db) -> list[str]:
    with sqlite3.connect(db) as conn:
        return [v for (v,) in conn.execute("SELECT verdict FROM decisions").fetchall()]


# ---------- whole-file gates (via main) ----------

def test_malformed_json_means_no_trades_and_exit_1(tmp_path, tmp_db, monkeypatch):
    bad = tmp_path / "signals.json"
    bad.write_text('{"date": "' + TODAY + '", "signals": [{"ticker": ')
    monkeypatch.setattr(de, "get_portfolio_state",
                        lambda: pytest.fail("portfolio fetched despite invalid signals"))
    assert run_main(monkeypatch, bad) == 1
    verdicts = db_verdicts(tmp_db)
    assert verdicts and all(v == "rejected" for v in verdicts)


def test_signals_dated_yesterday_rejected(tmp_path, tmp_db, monkeypatch):
    stale = tmp_path / "signals.json"
    stale.write_text(daily([signal("AAPL", "buy", 0.8)], day=YESTERDAY).model_dump_json())
    monkeypatch.setattr(de, "get_portfolio_state",
                        lambda: pytest.fail("portfolio fetched despite stale signals"))
    assert run_main(monkeypatch, stale) == 1
    verdicts = db_verdicts(tmp_db)
    assert verdicts and all(v == "rejected" for v in verdicts)


def test_mode_live_hard_exits_before_anything_runs(monkeypatch):
    monkeypatch.setitem(de.CONFIG, "mode", "live")
    # argv points at a file that does not exist: if the engine got past the mode
    # gate it would raise reading it, so a clean exit 2 proves nothing ran.
    monkeypatch.setattr(sys, "argv", ["decision_engine.py", "no_such_signals.json"])
    assert de.main() == 2


# ---------- buy-side limits ----------

def test_conviction_boundary_059_rejected_060_approved(portfolio):
    parsed = daily([signal("AAPL", "buy", 0.59), signal("MSFT", "buy", 0.6)])
    approved, rejected = de.apply_risk_rules(parsed, portfolio)
    assert [o["ticker"] for o in approved] == ["MSFT"]
    assert rejected[0]["signal"]["ticker"] == "AAPL"
    assert "conviction" in rejected[0]["reason"]


def test_position_cap_counts_existing_exposure(portfolio):
    # AAPL already worth $4k market value — far over the $250/ticker cap, so
    # any further AAPL buy is rejected. JNJ (no position), same sizing, sails
    # through ($150 <= $250 cap; total spend $3,800 + $150 within budget).
    portfolio["positions"]["AAPL"] = position(20, 190.0, 200.0, "Technology")
    portfolio["sectors"] = {"JNJ": "Healthcare"}
    parsed = daily([signal("AAPL", "buy", 0.6), signal("JNJ", "buy", 0.6)])
    approved, rejected = de.apply_risk_rules(parsed, portfolio)
    assert [o["ticker"] for o in approved] == ["JNJ"]
    assert rejected[0]["signal"]["ticker"] == "AAPL"
    assert "position cap" in rejected[0]["reason"]


def test_sector_cap_counts_buys_approved_earlier_in_same_run(portfolio):
    # Tech already at $700. Two $250 tech buys (conviction 1.0): 700+250=$950
    # OK, 950+250=$1,200 > $1k sector cap (20% of the $5k bankroll).
    portfolio["positions"]["AAPL"] = position(7, 100.0, 100.0, "Technology")
    portfolio["sectors"] = {"NVDA": "Technology", "GOOGL": "Technology"}
    parsed = daily([signal("NVDA", "buy", 1.0), signal("GOOGL", "buy", 1.0)])
    approved, rejected = de.apply_risk_rules(parsed, portfolio)
    assert [o["ticker"] for o in approved] == ["NVDA"]
    assert rejected[0]["signal"]["ticker"] == "GOOGL"
    assert "sector cap" in rejected[0]["reason"]


def test_buy_sized_below_minimum_order_rejected(portfolio):
    portfolio["equity"] = 1_000.0  # 1000 * 5% * 0.6 = $30 < $50 floor
    parsed = daily([signal("AAPL", "buy", 0.6)])
    approved, rejected = de.apply_risk_rules(parsed, portfolio)
    assert approved == []
    assert "minimum order" in rejected[0]["reason"]


def test_ticker_outside_watchlist_and_holdings_rejected(portfolio):
    parsed = daily([signal("TSLA", "buy", 0.9)])
    approved, rejected = de.apply_risk_rules(parsed, portfolio)
    assert approved == []
    assert "watchlist" in rejected[0]["reason"]


# ---------- circuit breaker ----------

def test_circuit_breaker_blocks_buys_but_allows_sells(portfolio):
    portfolio["intraday_pnl_pct"] = -0.035
    portfolio["positions"]["JNJ"] = position(10, 150.0, 155.0, "Healthcare")
    parsed = daily([signal("MSFT", "buy", 0.9), signal("JNJ", "sell", 0.6)])
    approved, rejected = de.apply_risk_rules(parsed, portfolio)
    assert [(o["ticker"], o["side"]) for o in approved] == [("JNJ", "sell")]
    assert rejected[0]["signal"]["ticker"] == "MSFT"
    assert "circuit breaker" in rejected[0]["reason"]


# ---------- stop-loss sweep ----------

def test_stop_loss_fires_even_when_signal_says_hold(portfolio):
    portfolio["positions"]["MSFT"] = position(10, 100.0, 90.0, "Technology")  # -10%
    parsed = daily([signal("MSFT", "hold")])
    approved, rejected = de.apply_risk_rules(parsed, portfolio)
    assert [(o["ticker"], o["side"]) for o in approved] == [("MSFT", "sell")]
    assert "stop-loss" in approved[0]["reason"]


def test_stop_loss_boundary_fires_at_exactly_8_pct(portfolio):
    portfolio["positions"]["MSFT"] = position(10, 100.0, 92.0, "Technology")
    assert [o["ticker"] for o in de.stop_loss_sweep(portfolio)] == ["MSFT"]
    portfolio["positions"]["MSFT"]["current_price"] = 92.01  # -7.99%: holds
    assert de.stop_loss_sweep(portfolio) == []


def test_signal_sell_not_duplicated_when_stop_loss_already_selling(portfolio):
    portfolio["positions"]["MSFT"] = position(10, 100.0, 90.0, "Technology")
    parsed = daily([signal("MSFT", "sell", 0.9)])
    approved, rejected = de.apply_risk_rules(parsed, portfolio)
    assert len(approved) == 1 and approved[0]["side"] == "sell"
    assert "stop-loss" in rejected[0]["reason"]


# ---------- daily trade budget ----------

SIX_TICKERS = ["AAPL", "MSFT", "NVDA", "GOOGL", "JNJ", "UNH"]


def test_six_valid_buys_only_five_approved(portfolio):
    parsed = daily([signal(t, "buy", 0.7) for t in SIX_TICKERS])
    approved, rejected = de.apply_risk_rules(parsed, portfolio)
    assert len(approved) == 5
    assert rejected[0]["signal"]["ticker"] == "UNH"
    assert "max trades per day" in rejected[0]["reason"]


def test_stop_loss_exits_exempt_from_max_trades(portfolio):
    # 5 buys fill the daily budget AND the stop-loss exit still goes out.
    portfolio["positions"]["XOM"] = position(10, 100.0, 91.0, "Energy")  # -9%
    parsed = daily([signal(t, "buy", 0.7) for t in SIX_TICKERS])
    approved, rejected = de.apply_risk_rules(parsed, portfolio)
    sells = [o for o in approved if o["side"] == "sell"]
    buys = [o for o in approved if o["side"] == "buy"]
    assert [s["ticker"] for s in sells] == ["XOM"]
    assert len(buys) == 5  # the cap still applies to signal-driven trades
    assert any("max trades per day" in r["reason"] for r in rejected)


# ---------- trading budget (bankroll) ----------


def test_sizing_uses_bankroll_not_equity(portfolio):
    # $100k equity but $5k budget: 5000 * 5% * 0.8 = $200, not $4,000.
    parsed = daily([signal("NVDA", "buy", 0.8)])
    approved, _ = de.apply_risk_rules(parsed, portfolio)
    assert approved[0]["notional"] == 200.0


def test_budget_caps_total_spend_including_this_run(portfolio):
    # $4,700 already spent (cost basis). First $200 buy reaches $4,900 — OK.
    # Second $200 buy would reach $5,100 > $5,000 budget — rejected.
    portfolio["positions"]["XOM"] = position(47, 100.0, 100.0, "Energy")
    portfolio["sectors"] = {"JNJ": "Healthcare", "MSFT": "Technology"}
    parsed = daily([signal("JNJ", "buy", 0.8), signal("MSFT", "buy", 0.8)])
    approved, rejected = de.apply_risk_rules(parsed, portfolio)
    assert [o["ticker"] for o in approved] == ["JNJ"]
    assert rejected[0]["signal"]["ticker"] == "MSFT"
    assert "trading budget" in rejected[0]["reason"]


def test_budget_counts_cost_basis_not_market_value(portfolio):
    # Spent $4,900 at cost; the position has LOST value ($3,000 market value,
    # -38.8% would stop out but use a small drawdown instead). Budget math must
    # use the $4,900 spent, so a $200 buy is rejected even though market value
    # plus the order is well under $5k.
    portfolio["positions"]["XOM"] = position(49, 100.0, 95.0, "Energy")  # -5%: no stop
    portfolio["sectors"] = {"JNJ": "Healthcare"}
    parsed = daily([signal("JNJ", "buy", 0.8)])
    approved, rejected = de.apply_risk_rules(parsed, portfolio)
    assert approved == []
    assert "trading budget" in rejected[0]["reason"]


def test_prior_pending_buy_blocks_reapproval_and_consumes_budget(portfolio):
    # A $3,250 JNJ buy approved earlier today is still awaiting execution:
    # JNJ may not be re-approved, and the $3,250 counts against the budget.
    portfolio["sectors"] = {"JNJ": "Healthcare", "MSFT": "Technology", "NVDA": "Technology"}
    prior = {"JNJ": 3250.0}
    parsed = daily([signal("JNJ", "buy", 0.9), signal("MSFT", "buy", 0.8)])
    approved, rejected = de.apply_risk_rules(parsed, portfolio, prior)
    assert [o["ticker"] for o in approved] == ["MSFT"]  # 3250 + 200 <= 5000
    assert rejected[0]["signal"]["ticker"] == "JNJ"
    assert "already pending" in rejected[0]["reason"]

    prior_big = {"JNJ": 4900.0}
    parsed = daily([signal("NVDA", "buy", 0.8)])
    approved, rejected = de.apply_risk_rules(parsed, portfolio, prior_big)
    assert approved == []  # 4900 + 200 > 5000
    assert "trading budget" in rejected[0]["reason"]


def test_sector_cap_counts_pending_buys_from_earlier_runs(portfolio):
    # $900 of Healthcare (JNJ) approved earlier today is still awaiting
    # execution. A fill is never assumed, so a $200 UNH buy must see Healthcare
    # at $1,100 > the $1k sector cap — not at $200.
    prior = {"JNJ": 900.0}
    parsed = daily([signal("UNH", "buy", 0.8)])
    approved, rejected = de.apply_risk_rules(parsed, portfolio, prior)
    assert approved == []
    assert "sector cap" in rejected[0]["reason"]


def test_buy_with_unknown_sector_rejected(portfolio):
    # No snapshot classified NVDA today: the sector cap cannot be enforced,
    # so the buy is rejected — missing data is never a pass.
    del portfolio["sectors"]["NVDA"]
    parsed = daily([signal("NVDA", "buy", 0.9)])
    approved, rejected = de.apply_risk_rules(parsed, portfolio)
    assert approved == []
    assert "sector unknown" in rejected[0]["reason"]


def test_buy_already_filled_today_not_reapproved(portfolio):
    # MSFT's buy already executed today (so it is NOT pending), but the shared
    # per-day client_order_id means a second buy could never submit —
    # approving one would put a phantom trade in the log.
    parsed = daily([signal("MSFT", "buy", 0.9)])
    approved, rejected = de.apply_risk_rules(parsed, portfolio, {}, {"MSFT"})
    assert approved == []
    assert "already approved and executed" in rejected[0]["reason"]


def test_approved_buy_tickers_includes_filled_and_pending(tmp_db):
    de.log_decisions(TODAY, [
        {"ticker": "JNJ", "side": "buy", "notional": 200.0, "thesis": THESIS,
         "signal": signal("JNJ", "buy", 0.65).model_dump()},
        {"ticker": "MSFT", "side": "sell", "qty": "all", "thesis": THESIS,
         "signal": signal("MSFT", "sell", 0.8).model_dump()},
    ], [])
    assert de.approved_buy_tickers(TODAY) == {"JNJ"}  # sells don't count
    assert de.approved_buy_tickers(YESTERDAY) == set()


def test_signal_timestamp_must_be_iso8601():
    with pytest.raises(Exception):
        signal_kwargs = dict(ticker="AAPL", action="buy", conviction=0.8,
                             thesis=THESIS, sources=["test"],
                             timestamp="yesterday at noon-ish")
        de.Signal(**signal_kwargs)


def test_circuit_breaker_measured_in_dollars_against_bankroll(portfolio):
    # -0.2% of $100k equity is ~-$200 — trivial for the account, but it
    # exceeds 3% of the $5k bankroll ($150), so buys freeze.
    portfolio["intraday_pnl_pct"] = -0.002
    parsed = daily([signal("MSFT", "buy", 0.9)])
    approved, rejected = de.apply_risk_rules(parsed, portfolio)
    assert approved == []
    assert "circuit breaker" in rejected[0]["reason"]

    portfolio["intraday_pnl_pct"] = -0.001  # ~-$100: under the $150 threshold
    approved, rejected = de.apply_risk_rules(parsed, portfolio)
    assert [o["ticker"] for o in approved] == ["MSFT"]


def test_pending_buy_notional_reads_unfilled_approvals(tmp_db):
    run_date = TODAY
    de.log_decisions(run_date, [
        {"ticker": "JNJ", "side": "buy", "notional": 3250.0, "thesis": THESIS,
         "signal": signal("JNJ", "buy", 0.65).model_dump()},
        {"ticker": "MSFT", "side": "buy", "notional": 200.0, "thesis": THESIS,
         "signal": signal("MSFT", "buy", 0.8).model_dump()},
    ], [])
    # MSFT's buy filled; JNJ's never executed.
    with sqlite3.connect(tmp_db) as conn:
        conn.execute(de._DECISIONS_DDL)
        conn.execute("""CREATE TABLE IF NOT EXISTS executions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, run_date TEXT,
            decision_id INTEGER, client_order_id TEXT, ticker TEXT, side TEXT,
            request_json TEXT, alpaca_order_id TEXT, status TEXT,
            filled_qty REAL, filled_avg_price REAL, detail TEXT, updated_ts TEXT)""")
        conn.execute(
            "INSERT INTO executions (ts, run_date, decision_id, client_order_id, "
            "ticker, side, status) VALUES ('t', ?, 1, ?, 'MSFT', 'buy', 'filled')",
            (run_date, f"{run_date}-MSFT-buy"))
    assert de.pending_buy_notional(run_date) == {"JNJ": 3250.0}


def test_pending_buy_notional_empty_when_db_missing(tmp_db):
    assert de.pending_buy_notional(TODAY) == {}


# ---------- edge cases: empty / conflicting / malformed / extreme signals ----------


def test_empty_signals_list_is_a_valid_no_trade_day(portfolio):
    approved, rejected = de.apply_risk_rules(daily([]), portfolio)
    assert (approved, rejected) == ([], [])


def test_conflicting_buy_and_sell_for_same_ticker_both_evaluated(portfolio):
    # The engine takes signals in file order and holds no netting opinion:
    # with no position, the buy is approved and the sell rejected (nothing to
    # sell). Conflicts are visible in the log, never silently merged.
    parsed = daily([signal("AAPL", "buy", 0.8), signal("AAPL", "sell", 0.8)])
    approved, rejected = de.apply_risk_rules(parsed, portfolio)
    assert [(o["ticker"], o["side"]) for o in approved] == [("AAPL", "buy")]
    assert rejected[0]["reason"] == "cannot sell: no position"


def test_two_buys_same_ticker_same_run_second_hits_position_cap(portfolio):
    # Duplicate buys in one file: the first claims the full $250 cap
    # (conviction 1.0), so the second must see it as pending exposure.
    parsed = daily([signal("AAPL", "buy", 1.0), signal("AAPL", "buy", 1.0)])
    approved, rejected = de.apply_risk_rules(parsed, portfolio)
    assert len(approved) == 1
    assert "position cap" in rejected[0]["reason"]


def test_conviction_extremes_zero_and_one(portfolio):
    parsed = daily([signal("AAPL", "buy", 0.0), signal("MSFT", "buy", 1.0)])
    approved, rejected = de.apply_risk_rules(parsed, portfolio)
    assert [o["ticker"] for o in approved] == ["MSFT"]
    assert approved[0]["notional"] == 250.0  # full 5% of the $5k bankroll
    assert "conviction" in rejected[0]["reason"]


def test_out_of_range_conviction_fails_validation():
    for bad in (-0.1, 1.1):
        with pytest.raises(Exception):
            de.Signal(ticker="AAPL", action="buy", conviction=bad, thesis=THESIS,
                      sources=["test"],
                      timestamp=datetime.now(timezone.utc).isoformat())


def test_unknown_extra_field_rejected_by_schema():
    with pytest.raises(Exception):
        de.DailySignals(date=TODAY, market_context="ctx", signals=[],
                        execute_immediately=True)  # not in the schema: forbidden


def test_missing_required_fields_fail_validation():
    with pytest.raises(Exception):
        de.Signal(ticker="AAPL", action="buy")  # no conviction/thesis/sources/ts


def test_lowercase_or_overlong_ticker_fails_validation():
    for bad in ("aapl", "TOOLONG", "BRK.B"):
        with pytest.raises(Exception):
            de.Signal(ticker=bad, action="buy", conviction=0.8, thesis=THESIS,
                      sources=["test"],
                      timestamp=datetime.now(timezone.utc).isoformat())


def test_hold_signals_produce_no_orders_and_no_rejections(portfolio):
    parsed = daily([signal(t, "hold") for t in SIX_TICKERS])
    assert de.apply_risk_rules(parsed, portfolio) == ([], [])


# ---------- persistence ----------

def test_every_decision_persisted_to_trades_db(tmp_path, tmp_db, monkeypatch, portfolio):
    sig_file = tmp_path / "signals.json"
    sig_file.write_text(
        daily([signal("NVDA", "buy", 0.8), signal("TSLA", "buy", 0.9)]).model_dump_json())
    monkeypatch.setattr(de, "get_portfolio_state", lambda: portfolio)
    assert run_main(monkeypatch, sig_file) == 0

    with sqlite3.connect(tmp_db) as conn:
        rows = conn.execute(
            "SELECT signal_json, verdict, reason, order_json FROM decisions").fetchall()
    assert len(rows) == 2
    approved_row = next(r for r in rows if r[1] == "approved")
    assert json.loads(approved_row[0])["ticker"] == "NVDA"
    assert approved_row[2]  # reason always recorded
    assert json.loads(approved_row[3]) == {
        "ticker": "NVDA", "side": "buy", "notional": 200.0, "thesis": THESIS}
    rejected_row = next(r for r in rows if r[1] == "rejected")
    assert json.loads(rejected_row[0])["ticker"] == "TSLA"
    assert "watchlist" in rejected_row[2]
    assert rejected_row[3] is None  # no order for a rejection
