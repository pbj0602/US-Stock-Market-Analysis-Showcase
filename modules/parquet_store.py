"""
Parquet Store — incremental OHLCV storage (PRD §4.4).

First run: fetches 2-year history and writes {ticker}.parquet.
Subsequent runs: loads existing file, determines last stored date,
fetches only the delta, appends, and saves.

Storage location: ./data/ohlcv/{ticker}.parquet
Estimated size: ~500 KB per ticker for 2 years of daily data.
"""

import logging
import os
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

_OHLCV_DIR = os.path.join("data", "ohlcv")


def _parquet_path(ticker: str) -> str:
    return os.path.join(_OHLCV_DIR, f"{ticker}.parquet")


def _ensure_dir() -> None:
    os.makedirs(_OHLCV_DIR, exist_ok=True)


def load(ticker: str) -> pd.DataFrame:
    """Return the stored OHLCV DataFrame, or empty DataFrame if none exists."""
    path = _parquet_path(ticker)
    if not os.path.exists(path):
        return pd.DataFrame()
    try:
        df = pd.read_parquet(path)
        # Flatten MultiIndex columns left behind by older yfinance versions
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.index = pd.to_datetime(df.index, utc=True)
        return df
    except Exception as e:
        logger.warning(f"[ParquetStore] Could not read {path}: {e}")
        return pd.DataFrame()


def save(ticker: str, df: pd.DataFrame) -> None:
    """Persist the DataFrame to Parquet."""
    _ensure_dir()
    try:
        df.to_parquet(_parquet_path(ticker))
    except Exception as e:
        logger.error(f"[ParquetStore] Could not write {_parquet_path(ticker)}: {e}")


def get_or_fetch(ticker: str, no_cache: bool = False) -> pd.DataFrame:
    """
    Return up-to-date daily OHLCV for *ticker*.

    Logic:
      1. Load existing Parquet (skip if no_cache=True).
      2. Determine delta start: day after last stored date (or 2 years ago).
      3. Fetch delta from yfinance.
      4. Append delta to existing, de-duplicate, sort, save.
      5. Return combined DataFrame.
    """
    existing = pd.DataFrame() if no_cache else load(ticker)

    today = datetime.utcnow().date()
    two_years_ago = today - timedelta(days=730)

    if existing.empty:
        fetch_start = two_years_ago
        logger.info(f"[ParquetStore] {ticker}: first run, fetching 2-year history from {fetch_start}")
    else:
        last_date = existing.index.max()
        if hasattr(last_date, "date"):
            last_date = last_date.date()
        fetch_start = last_date + timedelta(days=1)
        if fetch_start > today:
            logger.debug(f"[ParquetStore] {ticker}: already up-to-date (last={last_date})")
            return existing
        logger.info(f"[ParquetStore] {ticker}: incremental fetch from {fetch_start}")

    try:
        new_df = yf.download(
            ticker,
            start=str(fetch_start),
            end=str(today + timedelta(days=1)),
            interval="1d",
            auto_adjust=True,
            progress=False,
        )
        # yfinance ≥0.2.38 returns MultiIndex columns ("Close","AAPL") for
        # single-ticker downloads. Flatten to simple column names.
        if isinstance(new_df.columns, pd.MultiIndex):
            new_df.columns = new_df.columns.get_level_values(0)
    except Exception as e:
        logger.warning(f"[ParquetStore] yfinance fetch failed for {ticker}: {e}")
        return existing

    if new_df.empty:
        logger.debug(f"[ParquetStore] {ticker}: no new rows from yfinance")
        return existing

    # Normalise timezone
    new_df.index = pd.to_datetime(new_df.index, utc=True)
    new_df["Symbol"] = ticker

    combined = (
        pd.concat([existing, new_df])
        .loc[~pd.Series(pd.concat([existing, new_df]).index).duplicated(keep="last").values]
        .sort_index()
    ) if not existing.empty else new_df.sort_index()

    save(ticker, combined)
    logger.info(f"[ParquetStore] {ticker}: saved {len(combined)} rows to Parquet")
    return combined


def get_price_series(ticker: str, column: str = "Close", no_cache: bool = False) -> pd.Series:
    """Convenience: return a single price column as a named Series."""
    df = get_or_fetch(ticker, no_cache=no_cache)
    if df.empty or column not in df.columns:
        return pd.Series(name=ticker, dtype=float)
    return df[column].rename(ticker)
