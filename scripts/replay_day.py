"""
replay_day.py — offline, read-only replay of the decision pipeline.

Feeds a historical signals file and its day's data/ snapshots through the REAL
validation + risk-rule code (decision_engine.DailySignals and apply_risk_rules)
against a synthetic portfolio, and prints the verdicts the engine would emit.
No network, no .env, no writes to trades.db — safe to run anywhere, and
deterministic for a given signals file. Useful for verifying a fresh clone and
for inspecting how the rules treat a past day's signals.

Usage:
    python scripts/replay_day.py                    # latest signals file
    python scripts/replay_day.py signals/signals_2026-06-12.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from pydantic import ValidationError  # noqa: E402

import decision_engine as de  # noqa: E402

REPLAY_EQUITY = 100_000.0  # synthetic paper account: flat day, no positions


def pick_signals_file() -> Path:
    if len(sys.argv) == 2:
        return Path(sys.argv[1])
    candidates = sorted(ROOT.glob("signals/signals_*.json"))
    if not candidates:
        sys.exit("no signals/signals_*.json found; pass a path explicitly")
    return candidates[-1]


def replay_portfolio(run_date: str) -> dict:
    """Synthetic flat portfolio; sectors read from the day's real snapshots so
    the sector cap exercises the same data path as a live run."""
    sectors = {t: de.load_sector(t, run_date) for t in de.CONFIG["watchlist"]}
    return {"equity": REPLAY_EQUITY, "intraday_pnl_pct": 0.0,
            "intraday_pnl_dollars": 0.0, "positions": {}, "sectors": sectors}


def main() -> int:
    signals_path = pick_signals_file()
    try:
        parsed = de.DailySignals.model_validate_json(
            signals_path.read_text(encoding="utf-8"))
    except OSError as e:
        sys.exit(f"cannot read {signals_path}: {e}")
    except ValidationError as e:
        print(f"REPLAY: {signals_path.name} FAILS schema validation — a live run "
              f"would reject the whole file and trade nothing:\n{e}")
        return 1

    portfolio = replay_portfolio(parsed.date)
    approved, rejected = de.apply_risk_rules(parsed, portfolio)

    print(f"REPLAY {signals_path.name} (dated {parsed.date}) against a synthetic "
          f"${REPLAY_EQUITY:,.0f} account, no positions, flat day")
    print(f"bankroll: min(equity, budget) = "
          f"${min(REPLAY_EQUITY, de.CONFIG['risk'].get('trading_budget_dollars') or REPLAY_EQUITY):,.0f}")
    print(f"market context: {parsed.market_context[:200]}")
    print()
    print(json.dumps({"approved": approved, "rejected": rejected}, indent=2))
    print()
    print(f"summary: {len(parsed.signals)} signals -> {len(approved)} approved, "
          f"{len(rejected)} rejected, "
          f"{len(parsed.signals) - len(approved) - len(rejected)} hold (no order)")
    print("read-only replay: nothing was written to trades.db and no order exists")
    return 0


if __name__ == "__main__":
    sys.exit(main())
