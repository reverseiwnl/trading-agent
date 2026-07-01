"""trading_day.py — the single definition of "today" (America/Chicago)."""

from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from trading_day import TRADING_TZ, today_iso, trading_today


def test_today_iso_is_a_calendar_date_string():
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", today_iso())


def test_today_is_chicago_not_machine_local():
    # The 2026-06-10/11 incident: a UTC host flips dates at 7 PM Chicago time.
    # Whatever the machine's zone, the project's "today" must match Chicago's.
    assert TRADING_TZ == ZoneInfo("America/Chicago")
    assert trading_today() == datetime.now(ZoneInfo("America/Chicago")).date()
    assert today_iso() == trading_today().isoformat()
