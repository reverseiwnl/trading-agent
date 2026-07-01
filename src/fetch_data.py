"""
fetch_data.py — daily pre-market data pull.

Writes per-ticker JSON snapshots into data/<YYYY-MM-DD>/ for the watchlist,
current holdings (from the Alpaca paper account), and the benchmark: latest
price, recent daily OHLCV, basic fundamentals (incl. sector — the decision
engine needs it for sector caps), and recent headlines.

Sources: Alpaca market data (primary, IEX feed), yfinance (fallback + the only
fundamentals source). Fails loudly: every per-ticker/per-field failure is
recorded in the snapshot's `errors` list and the run manifest, and the process
exits nonzero on any failure. A partial pull is reported, not papered over.

Usage: python src/fetch_data.py        (no args = today's run)
Exit codes: 0 = clean, 1 = partial (some tickers/fields failed), 2 = fatal.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from common import ROOT, alpaca_credentials, get_logger, load_config, utf8_console
from trading_day import today_iso

utf8_console()
CONFIG = load_config()
log = get_logger("fetch_data")

BARS_LOOKBACK_DAYS = 60   # calendar days fetched; trimmed to the last MAX_BARS
MAX_BARS = 30
NEWS_LOOKBACK_DAYS = 7
MAX_HEADLINES = 10

# yfinance .info is a grab-bag; persist only a curated, stable subset.
FUNDAMENTAL_KEYS = (
    "sector", "industry", "marketCap", "trailingPE", "forwardPE",
    "priceToBook", "dividendYield", "beta", "fiftyTwoWeekHigh",
    "fiftyTwoWeekLow", "earningsTimestamp", "shortName",
)


# ---------- Snapshot schema ----------

class DailyBar(BaseModel):
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float


class Headline(BaseModel):
    headline: str
    source: str = ""
    url: str = ""
    published_at: str = ""


class Snapshot(BaseModel):
    ticker: str
    as_of: str
    price: float | None = None
    price_source: Literal["alpaca", "yfinance"] | None = None
    bars: list[DailyBar] = Field(default_factory=list)
    fundamentals: dict[str, Any] = Field(default_factory=dict)
    headlines: list[Headline] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


# ---------- Alpaca clients ----------

def get_holdings() -> list[str]:
    """Symbols currently held in the Alpaca PAPER account."""
    from alpaca.trading.client import TradingClient

    key, secret = alpaca_credentials()
    client = TradingClient(key, secret, paper=True)
    return sorted(p.symbol for p in client.get_all_positions())


# ---------- Per-source fetchers (each raises on failure; caller records it) ----------

def fetch_bars_alpaca(ticker: str) -> tuple[float, list[DailyBar]]:
    """(latest trade price, recent daily bars) from Alpaca's free IEX feed."""
    from alpaca.data.enums import DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest, StockLatestTradeRequest
    from alpaca.data.timeframe import TimeFrame

    key, secret = alpaca_credentials()
    client = StockHistoricalDataClient(key, secret)

    start = datetime.now(timezone.utc) - timedelta(days=BARS_LOOKBACK_DAYS)
    barset = client.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=ticker, timeframe=TimeFrame.Day, start=start, feed=DataFeed.IEX))
    bars = [
        DailyBar(date=b.timestamp.date().isoformat(), open=b.open, high=b.high,
                 low=b.low, close=b.close, volume=b.volume)
        for b in barset.data.get(ticker, [])
    ][-MAX_BARS:]
    if not bars:
        raise RuntimeError(f"alpaca returned no daily bars for {ticker}")

    trade = client.get_stock_latest_trade(StockLatestTradeRequest(
        symbol_or_symbols=ticker, feed=DataFeed.IEX))[ticker]
    return float(trade.price), bars


def fetch_bars_yfinance(ticker: str) -> tuple[float, list[DailyBar]]:
    """Fallback (last close, recent daily bars) when Alpaca is unavailable."""
    import yfinance as yf

    hist = yf.Ticker(ticker).history(period=f"{BARS_LOOKBACK_DAYS}d", interval="1d")
    if hist.empty:
        raise RuntimeError(f"yfinance returned no history for {ticker}")
    bars = [
        DailyBar(date=idx.date().isoformat(), open=row["Open"], high=row["High"],
                 low=row["Low"], close=row["Close"], volume=row["Volume"])
        for idx, row in hist.iterrows()
    ][-MAX_BARS:]
    return bars[-1].close, bars


def fetch_fundamentals_yfinance(ticker: str) -> dict[str, Any]:
    """Curated fundamentals subset (FUNDAMENTAL_KEYS) — the only source of
    `sector`, which the decision engine's sector cap depends on."""
    import yfinance as yf

    info = yf.Ticker(ticker).info or {}
    fundamentals = {k: info[k] for k in FUNDAMENTAL_KEYS if info.get(k) is not None}
    if not fundamentals:
        raise RuntimeError(f"yfinance returned no fundamentals for {ticker}")
    return fundamentals


def fetch_news_alpaca(ticker: str) -> list[Headline]:
    """Recent headlines (last NEWS_LOOKBACK_DAYS, max MAX_HEADLINES) from the
    Alpaca news API — the primary news source."""
    from alpaca.data.historical.news import NewsClient
    from alpaca.data.requests import NewsRequest

    key, secret = alpaca_credentials()
    client = NewsClient(key, secret)
    start = datetime.now(timezone.utc) - timedelta(days=NEWS_LOOKBACK_DAYS)
    news_set = client.get_news(NewsRequest(symbols=ticker, start=start, limit=MAX_HEADLINES))
    items = news_set.data.get("news", [])
    return [
        Headline(headline=n.headline, source=n.source or "",
                 url=str(n.url or ""), published_at=n.created_at.isoformat())
        for n in items
    ]


def fetch_news_yfinance(ticker: str) -> list[Headline]:
    """Fallback headlines from yfinance; tolerates both the old flat item
    format and the newer `content`-nested one, skipping items with no title."""
    import yfinance as yf

    headlines: list[Headline] = []
    for item in (yf.Ticker(ticker).news or [])[:MAX_HEADLINES]:
        content = item.get("content", item)  # newer yfinance nests under "content"
        title = content.get("title") or item.get("title")
        if not title:
            continue
        url = (content.get("canonicalUrl") or {}).get("url", "") or item.get("link", "")
        provider = (content.get("provider") or {}).get("displayName", "") or item.get("publisher", "")
        headlines.append(Headline(headline=title, source=provider, url=url,
                                  published_at=str(content.get("pubDate", ""))))
    return headlines


# ---------- Snapshot assembly ----------

def fetch_snapshot(ticker: str) -> Snapshot:
    """Build one ticker's snapshot. Never raises: failures land in .errors."""
    snap = Snapshot(ticker=ticker, as_of=datetime.now(timezone.utc).isoformat())

    try:
        snap.price, snap.bars = fetch_bars_alpaca(ticker)
        snap.price_source = "alpaca"
    except Exception as e:
        snap.errors.append(f"alpaca bars/price failed: {e}")
        try:
            snap.price, snap.bars = fetch_bars_yfinance(ticker)
            snap.price_source = "yfinance"
        except Exception as e2:
            snap.errors.append(f"yfinance bars/price fallback failed: {e2}")

    try:
        snap.fundamentals = fetch_fundamentals_yfinance(ticker)
    except Exception as e:
        snap.errors.append(f"fundamentals failed: {e}")

    try:
        snap.headlines = fetch_news_alpaca(ticker)
    except Exception as e:
        snap.errors.append(f"alpaca news failed: {e}")
        try:
            snap.headlines = fetch_news_yfinance(ticker)
        except Exception as e2:
            snap.errors.append(f"yfinance news fallback failed: {e2}")

    return snap


# ---------- Entry point ----------

def main() -> int:
    """Pull today's snapshots for watchlist + holdings + benchmark; write the
    run manifest. Returns the process exit code (0 clean / 1 partial / 2 fatal)."""
    run_date = today_iso()
    get_logger("fetch_data", run_date)  # attach today's file log
    out_dir = ROOT / "data" / run_date
    out_dir.mkdir(parents=True, exist_ok=True)

    fatal_errors: list[str] = []
    try:
        alpaca_credentials()
    except RuntimeError as e:
        log.error(f"FATAL: {e}")
        return 2

    try:
        holdings = get_holdings()
    except Exception as e:
        holdings = []
        fatal_errors.append(f"could not fetch holdings from Alpaca paper account: {e}")

    tickers = sorted(set(CONFIG["watchlist"]) | set(holdings) | {CONFIG["benchmark"]})
    log.debug(f"run {run_date}: fetching {tickers} (holdings: {holdings})")

    statuses: dict[str, str] = {}
    for ticker in tickers:
        snap = fetch_snapshot(ticker)
        (out_dir / f"{ticker}.json").write_text(snap.model_dump_json(indent=2),
                                                encoding="utf-8")
        if snap.price is None:
            statuses[ticker] = "failed"          # no price at all = unusable
        elif snap.errors:
            statuses[ticker] = "partial"
        else:
            statuses[ticker] = "ok"
        log.debug(f"{ticker}: price={snap.price} source={snap.price_source} "
                  f"bars={len(snap.bars)} headlines={len(snap.headlines)}")
        log.info(f"{ticker}: {statuses[ticker]}"
                 + (f" ({'; '.join(snap.errors)})" if snap.errors else ""))

    clean = not fatal_errors and all(s == "ok" for s in statuses.values())
    manifest = {
        "date": run_date,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "holdings": holdings,
        "tickers": statuses,
        "errors": fatal_errors,
        "clean": clean,
    }
    (out_dir / "_manifest.json").write_text(json.dumps(manifest, indent=2),
                                            encoding="utf-8")

    for err in fatal_errors:
        log.error(f"ERROR: {err}")
    log.info(f"wrote {len(tickers)} snapshots to {out_dir} — "
             f"{'CLEAN' if clean else 'PARTIAL PULL'}")
    return 0 if clean else 1


if __name__ == "__main__":
    sys.exit(main())
