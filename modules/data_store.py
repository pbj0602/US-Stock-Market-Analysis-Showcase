"""
SQLite-based Data Store
Caches OHLCV data, signals, factor scores, and backtest results.
Eliminates redundant yfinance API calls across runs.
"""

import sqlite3
import pandas as pd
import numpy as np
from datetime import datetime, date, timedelta
import logging
import os
import json

logger = logging.getLogger(__name__)


class DataStore:
    """Persistent SQLite store for all quant data."""

    def __init__(self, db_path: str = "data/quant.db"):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.db_path = db_path
        self._init_db()

    # ─── Connection ───────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    # ─── Schema ───────────────────────────────────────────────────────

    def _init_db(self):
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS price_data (
                    symbol      TEXT NOT NULL,
                    date        TEXT NOT NULL,
                    open        REAL,
                    high        REAL,
                    low         REAL,
                    close       REAL,
                    volume      INTEGER,
                    adj_close   REAL,
                    PRIMARY KEY (symbol, date)
                );

                CREATE TABLE IF NOT EXISTS signals (
                    symbol      TEXT NOT NULL,
                    date        TEXT NOT NULL,
                    signal      TEXT NOT NULL,
                    score       REAL,
                    confidence  TEXT,
                    metadata    TEXT,
                    PRIMARY KEY (symbol, date)
                );

                CREATE TABLE IF NOT EXISTS portfolio_history (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    date            TEXT NOT NULL,
                    symbol          TEXT NOT NULL,
                    shares          REAL,
                    price           REAL,
                    value           REAL,
                    weight          REAL,
                    unrealized_pnl  REAL
                );

                CREATE TABLE IF NOT EXISTS backtest_results (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_name   TEXT NOT NULL,
                    run_date        TEXT DEFAULT (datetime('now')),
                    start_date      TEXT,
                    end_date        TEXT,
                    initial_capital REAL,
                    final_value     REAL,
                    total_return    REAL,
                    cagr            REAL,
                    sharpe          REAL,
                    sortino         REAL,
                    max_drawdown    REAL,
                    calmar          REAL,
                    win_rate        REAL,
                    profit_factor   REAL,
                    num_trades      INTEGER,
                    benchmark_return REAL,
                    alpha           REAL,
                    params          TEXT
                );

                CREATE TABLE IF NOT EXISTS factor_scores (
                    symbol          TEXT NOT NULL,
                    date            TEXT NOT NULL,
                    momentum_12_1   REAL,
                    momentum_1m     REAL,
                    value_pe        REAL,
                    value_pb        REAL,
                    quality_roe     REAL,
                    low_vol         REAL,
                    technical       REAL,
                    composite_score REAL,
                    PRIMARY KEY (symbol, date)
                );

                CREATE INDEX IF NOT EXISTS idx_price_symbol_date
                    ON price_data(symbol, date);
                CREATE INDEX IF NOT EXISTS idx_signals_date
                    ON signals(date);
                CREATE INDEX IF NOT EXISTS idx_factor_date
                    ON factor_scores(date);
            """)

    # ─── Price Data ───────────────────────────────────────────────────

    def save_price_data(self, df: pd.DataFrame):
        """
        Persist OHLCV rows.  df must contain at minimum:
        Symbol, Date, Open, High, Low, Close, Volume
        """
        if df.empty:
            return

        records = []
        for _, row in df.iterrows():
            d = row.get('Date') or row.get('date')
            date_str = str(pd.to_datetime(d).date())
            records.append((
                str(row.get('Symbol', row.get('symbol', ''))),
                date_str,
                _safe_float(row.get('Open',  row.get('open'))),
                _safe_float(row.get('High',  row.get('high'))),
                _safe_float(row.get('Low',   row.get('low'))),
                _safe_float(row.get('Close', row.get('close'))),
                _safe_int(row.get('Volume',  row.get('volume'))),
                _safe_float(row.get('Adj Close', row.get('Close', row.get('close')))),
            ))

        with self._connect() as conn:
            conn.executemany(
                """INSERT OR REPLACE INTO price_data
                   (symbol, date, open, high, low, close, volume, adj_close)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                records,
            )

        logger.debug(f"Saved {len(records)} price rows to DB")

    def get_price_data(
        self,
        symbols,
        start_date=None,
        end_date=None,
    ) -> pd.DataFrame:
        """Retrieve cached OHLCV data as a DataFrame."""
        if isinstance(symbols, str):
            symbols = [symbols]

        placeholders = ",".join("?" * len(symbols))
        params = list(symbols)
        query = f"""
            SELECT symbol AS Symbol, date AS Date,
                   open AS Open, high AS High, low AS Low,
                   close AS Close, volume AS Volume, adj_close AS Adj_Close
            FROM price_data
            WHERE symbol IN ({placeholders})
        """
        if start_date:
            query += " AND date >= ?"
            params.append(str(start_date))
        if end_date:
            query += " AND date <= ?"
            params.append(str(end_date))
        query += " ORDER BY symbol, date"

        with self._connect() as conn:
            df = pd.read_sql_query(query, conn, params=params)

        if not df.empty:
            df["Date"] = pd.to_datetime(df["Date"])
        return df

    def is_data_cached(self, symbol: str, start_date, end_date) -> bool:
        """Return True if we have sufficient rows for this symbol/range."""
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT COUNT(*) FROM price_data WHERE symbol=? AND date>=? AND date<=?",
                (symbol, str(start_date), str(end_date)),
            )
            count = cur.fetchone()[0]

        expected = max(
            1,
            int((pd.to_datetime(end_date) - pd.to_datetime(start_date)).days * 5 / 7) - 5,
        )
        return count >= expected

    # ─── Signals ──────────────────────────────────────────────────────

    def save_signals(self, signals_df: pd.DataFrame, date_val=None):
        """Persist daily signal output."""
        if signals_df.empty:
            return
        date_str = str(date_val or date.today())

        records = []
        for _, row in signals_df.iterrows():
            meta = {
                col: row.get(col)
                for col in ["RSI", "MACD", "BB_Position", "Score", "Confidence"]
                if col in signals_df.columns
            }
            records.append((
                str(row.get("Symbol", "")),
                date_str,
                str(row.get("Signal", "HOLD")),
                _safe_float(row.get("Score", 0.5)),
                str(row.get("Confidence", "LOW")),
                json.dumps({k: v for k, v in meta.items() if v is not None}),
            ))

        with self._connect() as conn:
            conn.executemany(
                """INSERT OR REPLACE INTO signals
                   (symbol, date, signal, score, confidence, metadata)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                records,
            )

    def get_signals(
        self,
        start_date=None,
        end_date=None,
        symbol: str = None,
    ) -> pd.DataFrame:
        query = "SELECT * FROM signals WHERE 1=1"
        params = []
        if symbol:
            query += " AND symbol=?"
            params.append(symbol)
        if start_date:
            query += " AND date>=?"
            params.append(str(start_date))
        if end_date:
            query += " AND date<=?"
            params.append(str(end_date))
        query += " ORDER BY date, symbol"

        with self._connect() as conn:
            return pd.read_sql_query(query, conn, params=params)

    # ─── Backtest Results ─────────────────────────────────────────────

    def save_backtest_result(self, result: dict):
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO backtest_results
                   (strategy_name, start_date, end_date, initial_capital, final_value,
                    total_return, cagr, sharpe, sortino, max_drawdown, calmar,
                    win_rate, profit_factor, num_trades, benchmark_return, alpha, params)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    result.get("strategy_name"),
                    str(result.get("start_date", "")),
                    str(result.get("end_date", "")),
                    result.get("initial_capital", 100_000),
                    result.get("final_value", 0),
                    result.get("total_return", 0),
                    result.get("cagr", 0),
                    result.get("sharpe", 0),
                    result.get("sortino", 0),
                    result.get("max_drawdown", 0),
                    result.get("calmar", 0),
                    result.get("win_rate", 0),
                    result.get("profit_factor", 0),
                    result.get("num_trades", 0),
                    result.get("benchmark_return", 0),
                    result.get("alpha", 0),
                    json.dumps(result.get("params", {})),
                ),
            )

    def get_backtest_results(self) -> pd.DataFrame:
        with self._connect() as conn:
            return pd.read_sql_query(
                "SELECT * FROM backtest_results ORDER BY run_date DESC", conn
            )

    # ─── Factor Scores ────────────────────────────────────────────────

    def save_factor_scores(self, df: pd.DataFrame, date_val=None):
        if df.empty:
            return
        date_str = str(date_val or date.today())

        records = [
            (
                str(row.get("Symbol", "")),
                date_str,
                _safe_float(row.get("momentum_12_1")),
                _safe_float(row.get("momentum_1m")),
                _safe_float(row.get("value_pe")),
                _safe_float(row.get("value_pb")),
                _safe_float(row.get("quality_roe")),
                _safe_float(row.get("low_vol")),
                _safe_float(row.get("technical")),
                _safe_float(row.get("composite_score", 0.5)),
            )
            for _, row in df.iterrows()
        ]

        with self._connect() as conn:
            conn.executemany(
                """INSERT OR REPLACE INTO factor_scores
                   (symbol, date, momentum_12_1, momentum_1m, value_pe, value_pb,
                    quality_roe, low_vol, technical, composite_score)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                records,
            )

    def get_factor_scores(self, start_date=None, end_date=None) -> pd.DataFrame:
        query = "SELECT * FROM factor_scores WHERE 1=1"
        params = []
        if start_date:
            query += " AND date>=?"
            params.append(str(start_date))
        if end_date:
            query += " AND date<=?"
            params.append(str(end_date))
        query += " ORDER BY date, symbol"

        with self._connect() as conn:
            return pd.read_sql_query(query, conn, params=params)

    # ─── Portfolio History ────────────────────────────────────────────

    def save_portfolio_history(self, snapshot_df: pd.DataFrame, date_val=None):
        if snapshot_df.empty:
            return
        date_str = str(date_val or date.today())

        records = [
            (
                date_str,
                str(row.get("Symbol", "")),
                _safe_float(row.get("Shares")),
                _safe_float(row.get("Current_Price")),
                _safe_float(row.get("Position_Value")),
                _safe_float(row.get("Weight")),
                _safe_float(row.get("Unrealized_PnL")),
            )
            for _, row in snapshot_df.iterrows()
        ]

        with self._connect() as conn:
            conn.executemany(
                """INSERT INTO portfolio_history
                   (date, symbol, shares, price, value, weight, unrealized_pnl)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                records,
            )

    def get_portfolio_history(self, start_date=None, end_date=None) -> pd.DataFrame:
        query = "SELECT * FROM portfolio_history WHERE 1=1"
        params = []
        if start_date:
            query += " AND date>=?"
            params.append(str(start_date))
        if end_date:
            query += " AND date<=?"
            params.append(str(end_date))
        query += " ORDER BY date, symbol"

        with self._connect() as conn:
            return pd.read_sql_query(query, conn, params=params)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _safe_float(v) -> float:
    try:
        return float(v) if v is not None and not (isinstance(v, float) and np.isnan(v)) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _safe_int(v) -> int:
    try:
        return int(v) if v is not None else 0
    except (TypeError, ValueError):
        return 0
