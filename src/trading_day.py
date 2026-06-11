"""trading_day.py — one definition of "today" for every script.

Every run_date stamp, signal-date check, and data path uses the calendar date
in America/Chicago (the project's scheduling timezone, per CLAUDE.md).

date.today() is machine-local: on a UTC host it flips to the next day at
7 PM Chicago time, which let an evening run stamp the next day's run_date
onto stale data (observed on the 2026-06-10/11 boundary — see DESIGN.md).
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

TRADING_TZ = ZoneInfo("America/Chicago")


def trading_today() -> date:
    """Today's date in the trading timezone."""
    return datetime.now(TRADING_TZ).date()


def today_iso() -> str:
    """Today's date (YYYY-MM-DD) in the trading timezone."""
    return trading_today().isoformat()
