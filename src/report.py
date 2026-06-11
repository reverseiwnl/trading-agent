"""
report.py — daily digest + the VOO honesty benchmark.

Writes reports/digest_<YYYY-MM-DD>.md: portfolio snapshot, positions table,
today's signals, decision engine verdicts (approved AND rejected, with
reasons), execution results, and any errors from the data-pull manifest.

Benchmark: trades.db carries a hypothetical VOO position funded by the same
cash on the same dates as the paper account. Inception = account equity at the
first report run; later cash movements are mirrored from Alpaca account
activities (CSD/CSW). Each deposit converts to shares at the first ACTUAL VOO
close on/after its date, read from the data/ snapshots fetch_data.py wrote —
never an approximation. A deposit whose first close hasn't printed yet counts
at face value until it does. The digest reports cumulative P&L of the system
vs that counterfactual: this number is the honesty mechanism for the project.

Usage: python src/report.py   (no args = today's digest)
Exit codes: 0 digest written clean, 1 digest written but degraded (benchmark
            stale/unpriceable, missing manifest, ...), 2 fatal (no digest).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

from execute import PaperGuardError, make_paper_client

# Explicit UTF-8: Windows defaults to a legacy code page; snapshots, signals,
# and the digest all carry text that may not fit it.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
CONFIG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
DB_PATH = ROOT / "data" / "trades.db"
REPORTS_DIR = ROOT / "reports"
BENCHMARK = CONFIG["benchmark"]

# Execution statuses that don't demand human attention (mirrors execute.py).
EXEC_OK = {"filled", "skipped_duplicate"}

_BENCHMARK_DDL = """
CREATE TABLE IF NOT EXISTS benchmark_deposits (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    activity_id    TEXT NOT NULL UNIQUE,
    deposit_date   TEXT NOT NULL,
    amount         REAL NOT NULL,
    voo_close      REAL,
    voo_close_date TEXT,
    shares         REAL
)
"""


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute(_BENCHMARK_DDL)
    return conn


# ---------- Benchmark: deposits ----------

def ensure_inception(equity: float, run_date: str) -> bool:
    """Seed the counterfactual on the very first report run: one deposit equal
    to current account equity. Valid because the benchmark starts the day
    reporting starts — any P&L already embedded in equity at that moment is
    granted to the benchmark too, which only makes the comparison harsher.
    Returns True if seeded now."""
    with closing(_connect()) as conn, conn:
        if conn.execute("SELECT COUNT(*) FROM benchmark_deposits").fetchone()[0]:
            return False
        conn.execute(
            "INSERT INTO benchmark_deposits (activity_id, deposit_date, amount) "
            "VALUES (?, ?, ?)", ("inception", run_date, equity))
        return True


def inception_date() -> str | None:
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT deposit_date FROM benchmark_deposits ORDER BY id LIMIT 1").fetchone()
    return row["deposit_date"] if row else None


def record_deposit(activity_id: str, deposit_date: str, amount: float) -> bool:
    """Idempotent by Alpaca activity id. Withdrawals arrive as negative
    amounts and become negative shares once priced — same math, mirrored."""
    with closing(_connect()) as conn, conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO benchmark_deposits (activity_id, deposit_date, amount) "
            "VALUES (?, ?, ?)", (activity_id, deposit_date, amount))
        return cur.rowcount > 0


def fetch_cash_activities(client) -> list[dict]:
    """CSD (deposit) / CSW (withdrawal) non-trade activities from the trading
    API. alpaca-py 0.43 has no typed wrapper for this endpoint, so this uses
    the client's raw GET — same auth, same paper base URL the guard verified."""
    raw = client.get("/account/activities", {"activity_types": "CSD,CSW"})
    return [{"id": str(a["id"]), "date": str(a["date"])[:10],
             "amount": float(a["net_amount"])} for a in (raw or [])]


def mirror_deposits(client, inception: str) -> tuple[int, list[str]]:
    """Mirror post-inception cash movements into the counterfactual. Activity
    on/before the inception date is already inside the seeded equity and is
    skipped. Returns (rows added, warnings)."""
    try:
        activities = fetch_cash_activities(client)
    except Exception as e:
        return 0, [f"could not fetch account activities — benchmark deposits may be stale: {e}"]
    added = sum(
        record_deposit(a["id"], a["date"], a["amount"])
        for a in activities if a["date"] > inception)
    return added, []


# ---------- Benchmark: pricing ----------

def load_voo_closes() -> tuple[list[tuple[str, float]], float | None, str]:
    """(sorted daily closes [(date, close), ...], latest trade price, snapshot
    date) from the most recent data/<date>/<BENCHMARK>.json. These are actual
    market prices written by fetch_data.py — the benchmark's only source."""
    snaps = sorted(ROOT.glob(f"data/*/{BENCHMARK}.json"), key=lambda p: p.parent.name)
    if not snaps:
        raise FileNotFoundError(
            f"no {BENCHMARK} snapshot under data/ — run fetch_data.py first")
    snap = json.loads(snaps[-1].read_text(encoding="utf-8"))
    closes = sorted((b["date"], float(b["close"])) for b in snap.get("bars", []))
    price = snap.get("price")
    if price is None:
        price = closes[-1][1] if closes else None
    return closes, (float(price) if price is not None else None), snaps[-1].parent.name


def price_open_deposits(closes: list[tuple[str, float]]) -> list[str]:
    """Convert each still-unpriced deposit to shares at the first actual close
    on/after its date. A deposit can only be priced if its date falls inside
    the bar window we hold — if it predates the window, the true first close
    isn't in our data and guessing would be an approximation. Returns warnings."""
    warnings: list[str] = []
    if not closes:
        return warnings
    window_start = closes[0][0]
    with closing(_connect()) as conn, conn:
        rows = conn.execute(
            "SELECT id, deposit_date, amount FROM benchmark_deposits "
            "WHERE shares IS NULL ORDER BY deposit_date, id").fetchall()
        for row in rows:
            if row["deposit_date"] < window_start:
                warnings.append(
                    f"benchmark deposit of {row['amount']:.2f} on {row['deposit_date']} "
                    f"predates available {BENCHMARK} history (oldest bar {window_start}) "
                    f"— cannot price it honestly; counted at face value")
                continue
            match = next(((d, c) for d, c in closes if d >= row["deposit_date"]), None)
            if match is None:
                continue  # first close hasn't printed yet; face value meanwhile
            close_date, close = match
            conn.execute(
                "UPDATE benchmark_deposits SET voo_close = ?, voo_close_date = ?, "
                "shares = ? WHERE id = ?",
                (close, close_date, row["amount"] / close, row["id"]))
    return warnings


def benchmark_state(voo_price: float | None) -> dict | None:
    """Current state of the counterfactual: net deposits, shares held, cash
    still awaiting its first close, and total value at voo_price (None if the
    position can't be valued)."""
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM benchmark_deposits ORDER BY deposit_date, id").fetchall()
    if not rows:
        return None
    shares = sum(r["shares"] for r in rows if r["shares"] is not None)
    unpriced_cash = sum(r["amount"] for r in rows if r["shares"] is None)
    value = shares * voo_price + unpriced_cash if voo_price is not None else None
    if voo_price is None and shares == 0:
        value = unpriced_cash  # nothing invested yet: value is just the cash
    return {"inception": rows[0]["deposit_date"],
            "deposits": sum(r["amount"] for r in rows),
            "shares": shares, "unpriced_cash": unpriced_cash, "value": value}


# ---------- Account state ----------

def snapshot_account(client) -> dict:
    a = client.get_account()
    equity = float(a.equity)
    last_equity = float(a.last_equity or 0)
    return {"equity": equity, "cash": float(a.cash),
            "intraday_pnl_pct": (equity - last_equity) / last_equity if last_equity else 0.0}


def snapshot_positions(client) -> list[dict]:
    out = []
    for p in client.get_all_positions():
        qty = float(p.qty)
        price = float(p.current_price)
        market_value = float(p.market_value) if p.market_value is not None else qty * price
        basis = float(p.avg_entry_price)
        cost = basis * qty
        upl = float(p.unrealized_pl) if p.unrealized_pl is not None else market_value - cost
        upl_pct = (float(p.unrealized_plpc) if p.unrealized_plpc is not None
                   else (upl / cost if cost else 0.0))
        out.append({"ticker": p.symbol, "qty": qty, "avg_entry": basis, "price": price,
                    "market_value": market_value, "unrealized_pl": upl,
                    "unrealized_pl_pct": upl_pct})
    return sorted(out, key=lambda r: r["ticker"])


# ---------- Today's artifacts (signals file, trades.db, manifest) ----------

def load_signals(run_date: str) -> dict | None:
    path = ROOT / "signals" / f"signals_{run_date}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        return {"error": f"signals file exists but is not valid JSON: {e}"}


def _query(sql: str, params: tuple) -> list[sqlite3.Row]:
    with closing(_connect()) as conn:
        try:
            return conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError:
            return []  # table doesn't exist yet: that stage never ran


def load_decisions(run_date: str) -> list[dict]:
    out = []
    for r in _query("SELECT * FROM decisions WHERE run_date = ? ORDER BY id", (run_date,)):
        signal = None
        if r["signal_json"]:
            try:
                parsed = json.loads(r["signal_json"])
                signal = parsed if isinstance(parsed, dict) else None
            except ValueError:
                signal = None  # whole-file rejection row: raw payload, not a signal
        out.append({"verdict": r["verdict"], "reason": r["reason"], "signal": signal,
                    "order": json.loads(r["order_json"]) if r["order_json"] else None})
    return out


def load_executions(run_date: str) -> list[dict]:
    return [dict(r) for r in _query(
        "SELECT * FROM executions WHERE run_date = ? ORDER BY id", (run_date,))]


def load_manifest(run_date: str) -> dict | None:
    path = ROOT / "data" / run_date / "_manifest.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {"errors": ["manifest exists but is not valid JSON"], "tickers": {}}


# ---------- Rendering ----------

def _usd(x: float) -> str:
    return f"${x:,.2f}"


def _signed_usd(x: float) -> str:
    return f"{'+' if x >= 0 else '-'}${abs(x):,.2f}"


def _pct(x: float) -> str:
    return f"{x:+.2%}"


def _md(s: object) -> str:
    return str(s).replace("|", "\\|").replace("\n", " ").strip()


def _render_benchmark(account: dict, bench: dict | None,
                      voo_price: float | None, voo_label: str) -> list[str]:
    L = [f"## System vs {BENCHMARK} counterfactual", ""]
    if bench is None:
        return L + ["**BENCHMARK UNAVAILABLE** — no deposits recorded yet.", ""]
    L += [f"Counterfactual: every cash deposit into the paper account buys {BENCHMARK} "
          f"at the first actual close on/after the deposit date. "
          f"Inception {bench['inception']}, net deposits {_usd(bench['deposits'])}.", ""]
    if bench["value"] is None:
        return L + [f"**BENCHMARK VALUE UNAVAILABLE** — no {BENCHMARK} price on hand; "
                    f"run fetch_data.py and regenerate.", ""]
    deposits = bench["deposits"]
    sys_pnl = account["equity"] - deposits
    bench_pnl = bench["value"] - deposits
    L += ["| | System | " + f"{BENCHMARK} counterfactual |",
          "|---|---:|---:|",
          f"| Current value | {_usd(account['equity'])} | {_usd(bench['value'])} |",
          f"| Cumulative P&L | {_signed_usd(sys_pnl)} ({_pct(sys_pnl / deposits) if deposits else 'n/a'}) "
          f"| {_signed_usd(bench_pnl)} ({_pct(bench_pnl / deposits) if deposits else 'n/a'}) |",
          "",
          f"**System minus benchmark: {_signed_usd(sys_pnl - bench_pnl)}** "
          f"(positive = the system is beating buy-and-hold {BENCHMARK})", "",
          f"- {BENCHMARK} position: {bench['shares']:.4f} shares, valued at "
          f"{_usd(voo_price)} ({voo_label})"]
    if bench["unpriced_cash"]:
        L += [f"- {_usd(bench['unpriced_cash'])} deposited but awaiting its first "
              f"{BENCHMARK} close — counted at face value until then"]
    return L + [""]


def _render_positions(positions: list[dict]) -> list[str]:
    L = ["## Positions", ""]
    if not positions:
        return L + ["No open positions.", ""]
    L += ["| Ticker | Qty | Avg entry | Price | Market value | Unrealized P&L |",
          "|---|---:|---:|---:|---:|---:|"]
    for p in positions:
        L += [f"| {p['ticker']} | {p['qty']:g} | {_usd(p['avg_entry'])} | {_usd(p['price'])} "
              f"| {_usd(p['market_value'])} | {_signed_usd(p['unrealized_pl'])} "
              f"({_pct(p['unrealized_pl_pct'])}) |"]
    return L + [""]


def _render_signals(signals: dict | None) -> list[str]:
    L = ["## Today's signals", ""]
    if signals is None:
        return L + ["No signals file for today — the research step did not run.", ""]
    if "error" in signals and "signals" not in signals:
        return L + [f"**{_md(signals['error'])}**", ""]
    if signals.get("market_context"):
        L += [f"> {_md(signals['market_context'])}", ""]
    sigs = signals.get("signals") or []
    if not sigs:
        return L + ["Signals file contains no signals.", ""]
    L += ["| Ticker | Action | Conviction | Thesis |", "|---|---|---:|---|"]
    for s in sigs:
        L += [f"| {_md(s.get('ticker', '?'))} | {_md(s.get('action', '?'))} "
              f"| {s.get('conviction', '?')} | {_md(s.get('thesis', ''))} |"]
    return L + [""]


def _render_decisions(decisions: list[dict]) -> list[str]:
    L = ["## Decision engine verdicts", ""]
    if not decisions:
        return L + ["No decisions recorded — the decision engine did not run today.", ""]
    approved = [d for d in decisions if d["verdict"] == "approved"]
    rejected = [d for d in decisions if d["verdict"] == "rejected"]

    L += [f"### Approved ({len(approved)})", ""]
    if approved:
        L += ["| Ticker | Side | Size | Reason |", "|---|---|---:|---|"]
        for d in approved:
            order = d["order"] or {}
            size = (_usd(order["notional"]) if order.get("notional") is not None
                    else f"qty {order.get('qty', '?')}")
            L += [f"| {_md(order.get('ticker', '?'))} | {_md(order.get('side', '?'))} "
                  f"| {size} | {_md(d['reason'])} |"]
    else:
        L += ["None."]
    L += ["", f"### Rejected ({len(rejected)})", ""]
    if rejected:
        L += ["| Ticker | Action | Conviction | Reason |", "|---|---|---:|---|"]
        for d in rejected:
            sig = d["signal"] or {}
            L += [f"| {_md(sig.get('ticker', '—'))} | {_md(sig.get('action', '—'))} "
                  f"| {sig.get('conviction', '—')} | {_md(d['reason'])} |"]
    else:
        L += ["None."]
    return L + [""]


def _render_executions(executions: list[dict]) -> list[str]:
    L = ["## Execution results", ""]
    if not executions:
        return L + ["No executions recorded today.", ""]
    L += ["| Ticker | Side | Status | Filled qty | Avg price | Detail |",
          "|---|---|---|---:|---:|---|"]
    for e in executions:
        price = _usd(e["filled_avg_price"]) if e["filled_avg_price"] is not None else "—"
        qty = f"{e['filled_qty']:g}" if e["filled_qty"] is not None else "—"
        L += [f"| {e['ticker']} | {e['side']} | {e['status']} | {qty} | {price} "
              f"| {_md(e['detail'] or '')} |"]
    bad = [e for e in executions if e["status"] not in EXEC_OK]
    if bad:
        L += ["", f"**{len(bad)} execution(s) need attention** — see status/detail above."]
    return L + [""]


def _render_errors(manifest: dict | None, warnings: list[str]) -> list[str]:
    L = ["## Data & run errors", ""]
    issues: list[str] = []
    if manifest is None:
        issues.append("no data manifest for today — fetch_data.py may not have run")
    else:
        issues += manifest.get("errors", [])
        issues += [f"data pull for {t}: {status}"
                   for t, status in sorted(manifest.get("tickers", {}).items())
                   if status != "ok"]
    issues += warnings
    if not issues:
        return L + ["None — data pull was clean and the report ran without warnings.", ""]
    return L + [f"- {_md(i)}" for i in issues] + [""]


def render_digest(run_date: str, account: dict, positions: list[dict],
                  bench: dict | None, voo_price: float | None, voo_label: str,
                  signals: dict | None, decisions: list[dict],
                  executions: list[dict], manifest: dict | None,
                  warnings: list[str]) -> str:
    L = [f"# Daily digest — {run_date}", "",
         f"_Paper trading. Generated by src/report.py at "
         f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}._", "",
         "## Portfolio snapshot", "",
         f"- Equity: {_usd(account['equity'])}",
         f"- Cash: {_usd(account['cash'])}",
         f"- Intraday P&L vs prior close: {_pct(account['intraday_pnl_pct'])}", ""]
    L += _render_benchmark(account, bench, voo_price, voo_label)
    L += _render_positions(positions)
    L += _render_signals(signals)
    L += _render_decisions(decisions)
    L += _render_executions(executions)
    L += _render_errors(manifest, warnings)
    return "\n".join(L)


# ---------- Entry point ----------

def main() -> int:
    run_date = date.today().isoformat()
    warnings: list[str] = []

    try:
        client = make_paper_client()  # read-only here, but the guard still applies
    except PaperGuardError as exc:
        print(f"FATAL: {exc}")
        return 2
    try:
        account = snapshot_account(client)
        positions = snapshot_positions(client)
    except Exception as e:
        print(f"FATAL: could not read paper account, no digest written: {e}")
        return 2

    if ensure_inception(account["equity"], run_date):
        print(f"benchmark inception: seeded {_usd(account['equity'])} on {run_date}")
    _, w = mirror_deposits(client, inception_date())
    warnings += w

    try:
        closes, voo_price, snap_date = load_voo_closes()
    except FileNotFoundError as e:
        closes, voo_price, snap_date = [], None, ""
        warnings.append(str(e))
    if snap_date and snap_date != run_date:
        warnings.append(f"latest {BENCHMARK} snapshot is from {snap_date}, not today "
                        f"— benchmark valued at a stale price")
    warnings += price_open_deposits(closes)
    voo_label = f"latest actual {BENCHMARK} price, snapshot {snap_date}" if snap_date else ""

    digest = render_digest(
        run_date, account, positions, benchmark_state(voo_price), voo_price, voo_label,
        load_signals(run_date), load_decisions(run_date), load_executions(run_date),
        load_manifest(run_date), warnings)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REPORTS_DIR / f"digest_{run_date}.md"
    out_path.write_text(digest, encoding="utf-8")
    print(f"wrote {out_path}")
    for w in warnings:
        print(f"WARNING: {w}")
    return 1 if warnings else 0


if __name__ == "__main__":
    sys.exit(main())
