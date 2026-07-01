"""common.py — shared runtime plumbing for every src/ script.

One home for the pieces every entry point was duplicating: repo paths, config
loading, UTF-8 console hardening, Alpaca credential reading, and run logging.
Behavior-neutral by design: each script keeps its own module-level CONFIG and
DB_PATH aliases (tests monkeypatch those per module), and stdout output is
unchanged — the file log under logs/ is additive.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "trades.db"
LOGS_DIR = ROOT / "logs"  # gitignored; one file per script per run date

# Cap comparisons use a tiny dollar tolerance so an order sized exactly at a
# cap passes, and float noise alone can never tip a rejection. Shared by the
# decision engine (approval time) and execute.py (submission-time revalidation)
# so the two checks can never drift apart.
CAP_EPSILON = 1e-6


def utf8_console() -> None:
    """Force stdout/stderr to UTF-8 with replacement.

    Windows defaults console output to a legacy code page (cp1252); headlines,
    theses, and API error strings routinely contain characters outside it, and
    a print must never crash a scheduled run whose output is piped to a log.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def load_config() -> dict[str, Any]:
    """Parse config.yaml (the authoritative risk rules; human-edited only)."""
    return yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


def load_env() -> None:
    """Load .env from the repo root (no-op if absent, e.g. in CI/routine)."""
    load_dotenv(ROOT / ".env")


def alpaca_credentials() -> tuple[str, str]:
    """(key, secret) for the Alpaca PAPER account, from the environment.

    Raises RuntimeError when unset so every entry point fails loudly before
    any network or DB work — a missing key is a fatal misconfiguration, never
    something to limp past.
    """
    load_env()
    key = os.environ.get("ALPACA_API_KEY", "")
    secret = os.environ.get("ALPACA_SECRET_KEY", "")
    if not key or not secret:
        raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY not set (see .env.example)")
    return key, secret


def get_logger(script: str, run_date: str | None = None) -> logging.Logger:
    """Logger for one script run: plain messages to stdout (byte-identical to
    the print() output it replaces — the routine and humans read stdout), plus
    a timestamped DEBUG file log at logs/<script>_<run_date>.log so any run
    can be reconstructed after the fact.

    Idempotent: calling again for the same script reuses the logger without
    stacking duplicate handlers (tests invoke main() repeatedly).
    """
    logger = logging.getLogger(f"trading_agent.{script}")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    if not any(isinstance(h, logging.StreamHandler)
               and getattr(h, "stream", None) is sys.stdout for h in logger.handlers):
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(logging.INFO)
        console.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(console)

    if run_date is not None:
        log_path = LOGS_DIR / f"{script}_{run_date}.log"
        if not any(isinstance(h, logging.FileHandler)
                   and Path(getattr(h, "baseFilename", "")) == log_path
                   for h in logger.handlers):
            try:
                LOGS_DIR.mkdir(parents=True, exist_ok=True)
                file_handler = logging.FileHandler(log_path, encoding="utf-8")
            except OSError:
                # A read-only filesystem must not take down the run: the
                # console output (and trades.db) still carry the full story.
                pass
            else:
                file_handler.setLevel(logging.DEBUG)
                file_handler.setFormatter(logging.Formatter(
                    "%(asctime)s %(levelname)s %(message)s"))
                logger.addHandler(file_handler)

    return logger
