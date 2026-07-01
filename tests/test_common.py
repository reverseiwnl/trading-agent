"""Tests for the shared runtime plumbing in common.py."""

from __future__ import annotations

import pytest

import common


def test_alpaca_credentials_missing_raises(monkeypatch):
    monkeypatch.setattr(common, "load_env", lambda: None)  # don't read a real .env
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError, match="not set"):
        common.alpaca_credentials()


def test_alpaca_credentials_from_environment(monkeypatch):
    monkeypatch.setattr(common, "load_env", lambda: None)
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret")
    assert common.alpaca_credentials() == ("test-key", "test-secret")


def test_get_logger_is_idempotent_and_writes_plain_messages(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(common, "LOGS_DIR", tmp_path / "logs")
    log = common.get_logger("unit_test_script", "2026-01-01")
    again = common.get_logger("unit_test_script", "2026-01-01")
    assert again is log
    assert len(log.handlers) == 2  # one console + one file, never stacked

    log.info("hello stdout")
    log.debug("file only")
    assert capsys.readouterr().out == "hello stdout\n"  # plain, print-compatible

    logged = (tmp_path / "logs" / "unit_test_script_2026-01-01.log").read_text(encoding="utf-8")
    assert "hello stdout" in logged and "file only" in logged  # DEBUG reaches the file


def test_config_loads_and_mode_is_paper():
    config = common.load_config()
    assert config["mode"] == "paper"
    assert 0 < config["risk"]["max_position_pct"] <= 1
