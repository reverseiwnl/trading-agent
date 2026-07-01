"""
Tests for fetch_data.py's snapshot assembly and run manifest. No network: every
per-source fetcher is monkeypatched, so these cover the fallback ordering, the
never-crash error contract, malformed upstream data, and the ok/partial/failed
manifest statuses + exit codes.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import fetch_data as fd

BARS = [fd.DailyBar(date="2026-06-11", open=1.0, high=2.0, low=0.5,
                    close=1.5, volume=100.0)]
HEADLINES = [fd.Headline(headline="Something happened", source="Test",
                         url="https://example.test", published_at="2026-06-11T12:00:00Z")]


def _patch_sources(monkeypatch, *, bars_alpaca=None, bars_yf=None,
                   fundamentals=None, news_alpaca=None, news_yf=None):
    """Wire each per-source fetcher to a callable or an exception to raise."""
    def as_func(spec, default):
        if spec is None:
            return default
        if isinstance(spec, Exception):
            def raiser(ticker):
                raise spec
            return raiser
        return spec

    monkeypatch.setattr(fd, "fetch_bars_alpaca", as_func(bars_alpaca, lambda t: (1.5, BARS)))
    monkeypatch.setattr(fd, "fetch_bars_yfinance", as_func(bars_yf, lambda t: (1.4, BARS)))
    monkeypatch.setattr(fd, "fetch_fundamentals_yfinance",
                        as_func(fundamentals, lambda t: {"sector": "Technology"}))
    monkeypatch.setattr(fd, "fetch_news_alpaca", as_func(news_alpaca, lambda t: HEADLINES))
    monkeypatch.setattr(fd, "fetch_news_yfinance", as_func(news_yf, lambda t: HEADLINES))


# ---------- fetch_snapshot: fallback ordering and the never-raise contract ----------

def test_clean_snapshot_uses_alpaca_price(monkeypatch):
    _patch_sources(monkeypatch)
    snap = fd.fetch_snapshot("AAPL")
    assert (snap.price, snap.price_source, snap.errors) == (1.5, "alpaca", [])
    assert snap.fundamentals["sector"] == "Technology"
    assert len(snap.headlines) == 1


def test_alpaca_failure_falls_back_to_yfinance_and_records_error(monkeypatch):
    _patch_sources(monkeypatch, bars_alpaca=RuntimeError("alpaca down"),
                   news_alpaca=RuntimeError("news api down"))
    snap = fd.fetch_snapshot("AAPL")
    assert (snap.price, snap.price_source) == (1.4, "yfinance")
    assert snap.headlines == HEADLINES  # yfinance news fallback used
    # the failures are on record, not papered over
    assert any("alpaca bars/price failed" in e for e in snap.errors)
    assert any("alpaca news failed" in e for e in snap.errors)


def test_every_source_failing_never_raises(monkeypatch):
    boom = RuntimeError("everything is down")
    _patch_sources(monkeypatch, bars_alpaca=boom, bars_yf=boom,
                   fundamentals=boom, news_alpaca=boom, news_yf=boom)
    snap = fd.fetch_snapshot("AAPL")  # must not raise
    assert snap.price is None and snap.price_source is None
    assert snap.bars == [] and snap.headlines == [] and snap.fundamentals == {}
    assert len(snap.errors) == 5  # both bar sources, fundamentals, both news sources


def test_no_news_found_is_a_valid_empty_snapshot_not_an_error(monkeypatch):
    _patch_sources(monkeypatch, news_alpaca=lambda t: [])
    snap = fd.fetch_snapshot("AAPL")
    assert snap.headlines == []
    assert snap.errors == []  # an empty news day is not a failure


# ---------- yfinance news parsing: malformed / legacy shapes ----------

def _yf_news(monkeypatch, items: list[dict]) -> list[fd.Headline]:
    # fetch_news_yfinance imports yfinance lazily, so patch the module itself
    import yfinance
    monkeypatch.setattr(yfinance, "Ticker", lambda t: SimpleNamespace(news=items))
    return fd.fetch_news_yfinance("AAPL")


def test_yfinance_news_handles_nested_flat_and_titleless_items(monkeypatch):
    items = [
        {"content": {"title": "Nested format", "canonicalUrl": {"url": "u1"},
                     "provider": {"displayName": "P1"}, "pubDate": "2026-06-11"}},
        {"title": "Legacy flat format", "link": "u2", "publisher": "P2"},
        {"content": {"summary": "no title at all"}},  # malformed: must be skipped
        {},                                            # fully empty item
    ]
    headlines = _yf_news(monkeypatch, items)
    assert [h.headline for h in headlines] == ["Nested format", "Legacy flat format"]
    assert headlines[0].source == "P1" and headlines[0].url == "u1"
    assert headlines[1].source == "P2" and headlines[1].url == "u2"


def test_yfinance_news_none_feed_yields_no_headlines(monkeypatch):
    import yfinance
    monkeypatch.setattr(yfinance, "Ticker", lambda t: SimpleNamespace(news=None))
    assert fd.fetch_news_yfinance("AAPL") == []


# ---------- main(): manifest statuses and exit codes ----------

@pytest.fixture
def run_env(tmp_path, monkeypatch):
    """Point the run at a tmp data dir with credentials 'present' and holdings
    empty; individual tests shape the per-ticker outcomes."""
    monkeypatch.setattr(fd, "ROOT", tmp_path)
    monkeypatch.setattr(fd, "alpaca_credentials", lambda: ("k", "s"))
    monkeypatch.setattr(fd, "get_holdings", lambda: [])
    return tmp_path


def read_manifest(tmp_path) -> dict:
    (manifest_path,) = tmp_path.glob("data/*/_manifest.json")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def test_main_clean_run_exit_0(run_env, monkeypatch):
    _patch_sources(monkeypatch)
    assert fd.main() == 0
    manifest = read_manifest(run_env)
    assert manifest["clean"] is True
    assert set(manifest["tickers"]) == set(fd.CONFIG["watchlist"]) | {fd.CONFIG["benchmark"]}
    assert all(s == "ok" for s in manifest["tickers"].values())


def test_main_partial_pull_exit_1_and_snapshot_written(run_env, monkeypatch):
    _patch_sources(monkeypatch, fundamentals=RuntimeError("no fundamentals"))
    assert fd.main() == 1
    manifest = read_manifest(run_env)
    assert manifest["clean"] is False
    assert all(s == "partial" for s in manifest["tickers"].values())
    # snapshots still written: a partial pull is reported, not discarded
    (snap_path,) = run_env.glob("data/*/AAPL.json")
    snap = json.loads(snap_path.read_text(encoding="utf-8"))
    assert snap["price"] == 1.5
    assert any("fundamentals failed" in e for e in snap["errors"])


def test_main_ticker_with_no_price_is_failed(run_env, monkeypatch):
    boom = RuntimeError("all price sources down")
    _patch_sources(monkeypatch, bars_alpaca=boom, bars_yf=boom)
    assert fd.main() == 1
    manifest = read_manifest(run_env)
    assert all(s == "failed" for s in manifest["tickers"].values())


def test_main_missing_credentials_fatal_exit_2(run_env, monkeypatch):
    monkeypatch.setattr(fd, "alpaca_credentials",
                        lambda: (_ for _ in ()).throw(RuntimeError("keys not set")))
    assert fd.main() == 2


def test_main_holdings_failure_recorded_but_watchlist_still_fetched(run_env, monkeypatch):
    _patch_sources(monkeypatch)
    monkeypatch.setattr(fd, "get_holdings",
                        lambda: (_ for _ in ()).throw(RuntimeError("account down")))
    assert fd.main() == 1
    manifest = read_manifest(run_env)
    assert manifest["clean"] is False
    assert any("could not fetch holdings" in e for e in manifest["errors"])
    assert all(s == "ok" for s in manifest["tickers"].values())  # pull itself worked
