"""
Signal Performance Tracking (PRD §8.3).

Every signal generated (BUY/SELL/HOLD) is logged to ./data/signal_log.db (SQLite).

Schema:
  id, timestamp, ticker, signal, score, component_scores (JSON),
  market_state, vix_at_signal, entry_price, confidence,
  outcome_price, outcome_date, signal_return, return_5d, return_20d

The post-market runner calls update_outcomes() daily: for BUY signals from
5 / 20 trading days ago it fetches actual closes and records the realized
return at each horizon. These horizons match the strategy's 1–3 week swing
holding period — the old same-day open→close "win rate" measured nothing.
"""

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

_DB_PATH = "data/signal_log.db"

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS signal_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp        TEXT NOT NULL,
    ticker           TEXT NOT NULL,
    signal           TEXT NOT NULL,
    score            REAL,
    component_scores TEXT,
    market_state     TEXT,
    vix_at_signal    REAL,
    outcome_price    REAL,
    outcome_date     TEXT,
    signal_return    REAL
);
CREATE INDEX IF NOT EXISTS idx_ticker ON signal_log(ticker);
CREATE INDEX IF NOT EXISTS idx_timestamp ON signal_log(timestamp);
"""

# Columns added by the 2026-07 feedback-loop redesign; applied idempotently.
_MIGRATIONS = [
    "ALTER TABLE signal_log ADD COLUMN entry_price REAL",
    "ALTER TABLE signal_log ADD COLUMN confidence REAL",
    "ALTER TABLE signal_log ADD COLUMN return_5d REAL",
    "ALTER TABLE signal_log ADD COLUMN return_20d REAL",
    # Which path produced the signal (VALUE / TREND / INDEX) — enables
    # win-rate stats per strategy path.
    "ALTER TABLE signal_log ADD COLUMN signal_path TEXT",
    # AI confidence review delta (negative = points deducted). Lets weekly
    # stats answer "how much money did the AI review actually save?"
    "ALTER TABLE signal_log ADD COLUMN ai_delta REAL",
]


@contextmanager
def _db(path: str = _DB_PATH):
    import os
    os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_CREATE_SQL)
        for mig in _MIGRATIONS:
            try:
                conn.execute(mig)
            except sqlite3.OperationalError:
                pass  # column already exists
        yield conn
        conn.commit()
    finally:
        conn.close()


class SignalTracker:
    """Log signals and compute performance outcomes."""

    def __init__(self, db_path: str = _DB_PATH):
        self.db_path = db_path
        # Ensure tables exist on init
        with _db(self.db_path):
            pass

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    def log_signal(
        self,
        ticker: str,
        signal: str,
        score: float,
        component_scores: Optional[dict] = None,
        market_state: str = "",
        vix: Optional[float] = None,
        entry_price: Optional[float] = None,
        confidence: Optional[float] = None,
        signal_path: Optional[str] = None,
        ai_delta: Optional[float] = None,
    ) -> int:
        """
        Insert one signal row. Returns the new row id.

        Args:
            ticker:           Stock symbol.
            signal:           "BUY", "SELL", "HOLD", or "FILTERED_LOW_VOLUME".
            score:            Composite signal score (0–1).
            component_scores: Dict of sub-component scores (stored as JSON).
            market_state:     "BULL" | "BEAR" | "SIDEWAYS".
            vix:              VIX value at time of signal.
            entry_price:      Recommended entry price (needed for outcome returns).
            confidence:       Signal confidence % after all adjustments.
            signal_path:      "VALUE" | "TREND" | "INDEX" (BUY signals only).
            ai_delta:         AI review confidence deduction (negative or None).
        """
        def _num(v):
            try:
                import pandas as pd
                if v is None or pd.isna(v):
                    return None
                return float(v)
            except (TypeError, ValueError):
                return None

        row = {
            "timestamp": datetime.now().isoformat(),
            "ticker": ticker,
            "signal": signal,
            "score": score,
            "component_scores": json.dumps(component_scores or {}),
            "market_state": market_state,
            "vix_at_signal": vix,
            "entry_price": _num(entry_price),
            "confidence": _num(confidence),
            "signal_path": signal_path or None,
            "ai_delta": _num(ai_delta),
        }
        with _db(self.db_path) as conn:
            cur = conn.execute(
                """INSERT INTO signal_log
                   (timestamp, ticker, signal, score, component_scores,
                    market_state, vix_at_signal, entry_price, confidence,
                    signal_path, ai_delta)
                   VALUES (:timestamp, :ticker, :signal, :score,
                           :component_scores, :market_state, :vix_at_signal,
                           :entry_price, :confidence, :signal_path, :ai_delta)""",
                row,
            )
            return cur.lastrowid

    # ------------------------------------------------------------------
    # Outcome computation (called daily by post_market_runner)
    # ------------------------------------------------------------------
    def update_outcomes(self, max_lookback_days: int = 60) -> dict:
        """
        Fill return_5d / return_20d for BUY signals whose horizon has elapsed.

        One batch yfinance download covers all pending tickers. Returns
        {'updated_5d': n, 'updated_20d': n}.
        """
        import pandas as pd
        import yfinance as yf

        today = datetime.now().date()
        window_start = today - timedelta(days=max_lookback_days)

        with _db(self.db_path) as conn:
            # BUY rows, plus AI-demoted rows (HOLD with ai_delta): their
            # would-be returns measure how much the AI review actually saved.
            rows = conn.execute(
                """SELECT id, ticker, timestamp, entry_price, return_5d, return_20d
                   FROM signal_log
                   WHERE (signal = 'BUY' OR ai_delta IS NOT NULL)
                     AND DATE(timestamp) >= ?
                     AND (return_5d IS NULL OR return_20d IS NULL)""",
                (str(window_start),),
            ).fetchall()

        if not rows:
            return {"updated_5d": 0, "updated_20d": 0}

        tickers = sorted({r["ticker"] for r in rows})
        try:
            hist = yf.download(
                tickers,
                start=str(window_start - timedelta(days=7)),
                end=str(today + timedelta(days=1)),
                progress=False,
                auto_adjust=True,
                group_by="column",
            )
        except Exception as e:
            logger.warning(f"[SignalTracker] outcome price download failed: {e}")
            return {"updated_5d": 0, "updated_20d": 0}
        if hist is None or hist.empty:
            logger.warning("[SignalTracker] outcome price download returned no data")
            return {"updated_5d": 0, "updated_20d": 0}

        closes = hist["Close"]
        if isinstance(closes, pd.Series):  # single ticker
            closes = closes.to_frame(name=tickers[0])
        closes.index = pd.to_datetime(closes.index).date

        updated = {"updated_5d": 0, "updated_20d": 0}
        with _db(self.db_path) as conn:
            for r in rows:
                try:
                    tkr = r["ticker"]
                    if tkr not in closes.columns:
                        continue
                    series = closes[tkr].dropna()
                    sig_date = datetime.fromisoformat(r["timestamp"]).date()
                    # Trading days strictly after the signal date
                    after = series[[d > sig_date for d in series.index]]

                    entry = r["entry_price"]
                    if not entry or entry <= 0:
                        # Legacy rows without entry price: use signal-day close
                        on_day = series[[d <= sig_date for d in series.index]]
                        if on_day.empty:
                            continue
                        entry = float(on_day.iloc[-1])

                    sets, params = [], []
                    if r["return_5d"] is None and len(after) >= 5:
                        ret5 = (float(after.iloc[4]) - entry) / entry
                        sets += ["return_5d=?", "outcome_price=?", "outcome_date=?", "signal_return=?"]
                        params += [round(ret5, 6), round(float(after.iloc[4]), 4),
                                   str(after.index[4]), round(ret5, 6)]
                        updated["updated_5d"] += 1
                    if r["return_20d"] is None and len(after) >= 20:
                        ret20 = (float(after.iloc[19]) - entry) / entry
                        sets.append("return_20d=?")
                        params.append(round(ret20, 6))
                        updated["updated_20d"] += 1
                    if sets:
                        params.append(r["id"])
                        conn.execute(
                            f"UPDATE signal_log SET {', '.join(sets)} WHERE id=?", params
                        )
                except Exception as e:
                    logger.warning(f"[SignalTracker] outcome calc failed for row {r['id']}: {e}")

        logger.info(
            f"[SignalTracker] outcomes updated: T+5={updated['updated_5d']}, "
            f"T+20={updated['updated_20d']} (pending rows: {len(rows)})"
        )
        return updated

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------
    def get_outcome_stats(self, days: int = 60) -> dict:
        """
        Realized BUY-signal performance over the trailing window.

        Returns {
          'n_5d', 'win_rate_5d', 'avg_return_5d',
          'n_20d', 'win_rate_20d', 'avg_return_20d',
          'by_confidence': [{'bucket', 'n', 'win_rate', 'avg_return'}],
          'by_path':       [{'bucket', 'n', 'win_rate', 'avg_return'}],
          'by_regime':     [{'bucket', 'n', 'win_rate', 'avg_return'}],
          'ai_review':     {'n_adjusted', 'n_demoted', 'demoted_avoided_loss_pct'},
        } — values None where no data.
        """
        since = str(datetime.now().date() - timedelta(days=days))

        def _agg(rows):
            vals = [r[0] for r in rows if r[0] is not None]
            if not vals:
                return None, None, 0
            wins = sum(1 for v in vals if v > 0)
            return round(wins / len(vals) * 100, 1), round(sum(vals) / len(vals) * 100, 2), len(vals)

        _PATH_ZH = {"VALUE": "价值回归", "TREND": "趋势突破", "INDEX": "大盘择时"}

        with _db(self.db_path) as conn:
            r5 = conn.execute(
                "SELECT return_5d FROM signal_log WHERE signal='BUY' AND DATE(timestamp)>=?",
                (since,),
            ).fetchall()
            r20 = conn.execute(
                "SELECT return_20d FROM signal_log WHERE signal='BUY' AND DATE(timestamp)>=?",
                (since,),
            ).fetchall()

            buckets = []
            for label, lo, hi in [("≥60", 60, 1e9), ("40-59", 40, 60), ("<40", -1, 40)]:
                rows = conn.execute(
                    """SELECT return_5d FROM signal_log
                       WHERE signal='BUY' AND DATE(timestamp)>=?
                         AND confidence IS NOT NULL AND confidence>=? AND confidence<?""",
                    (since, lo, hi),
                ).fetchall()
                wr, avg, n = _agg(rows)
                if n:
                    buckets.append({"bucket": label, "n": n, "win_rate": wr, "avg_return": avg})

            # Per strategy path (VALUE / TREND / INDEX)
            by_path = []
            for path in ("VALUE", "TREND", "INDEX"):
                rows = conn.execute(
                    """SELECT return_5d FROM signal_log
                       WHERE signal='BUY' AND DATE(timestamp)>=? AND signal_path=?""",
                    (since, path),
                ).fetchall()
                wr, avg, n = _agg(rows)
                if n:
                    by_path.append({"bucket": _PATH_ZH.get(path, path), "n": n,
                                    "win_rate": wr, "avg_return": avg})

            # Per market regime at signal time
            by_regime = []
            for regime in ("BULL", "SIDEWAYS", "BEAR"):
                rows = conn.execute(
                    """SELECT return_5d FROM signal_log
                       WHERE signal='BUY' AND DATE(timestamp)>=? AND market_state=?""",
                    (since, regime),
                ).fetchall()
                wr, avg, n = _agg(rows)
                if n:
                    by_regime.append({"bucket": regime, "n": n,
                                      "win_rate": wr, "avg_return": avg})

            # AI review accountability: how many signals it touched, and what
            # the demoted ones (BUY→HOLD) would have returned had we traded them.
            n_adjusted = conn.execute(
                "SELECT COUNT(*) FROM signal_log WHERE DATE(timestamp)>=? AND ai_delta IS NOT NULL",
                (since,),
            ).fetchone()[0]
            demoted_rows = conn.execute(
                """SELECT return_5d FROM signal_log
                   WHERE DATE(timestamp)>=? AND ai_delta IS NOT NULL AND signal='HOLD'""",
                (since,),
            ).fetchall()
            _, demoted_avg, n_demoted_scored = _agg(demoted_rows)
            n_demoted = len(demoted_rows)

        wr5, avg5, n5 = _agg(r5)
        wr20, avg20, n20 = _agg(r20)
        return {
            "n_5d": n5, "win_rate_5d": wr5, "avg_return_5d": avg5,
            "n_20d": n20, "win_rate_20d": wr20, "avg_return_20d": avg20,
            "by_confidence": buckets,
            "by_path": by_path,
            "by_regime": by_regime,
            "ai_review": {
                "n_adjusted": n_adjusted,
                "n_demoted": n_demoted,
                # Negative value = the demoted trades would have lost money →
                # the AI review saved that much per trade on average.
                "demoted_avg_return_5d": demoted_avg if n_demoted_scored else None,
            },
        }

    def get_history_line(self, days: int = 60, min_samples: int = 8) -> str:
        """Compact one-line Chinese summary for AI prompts ('' if too few samples)."""
        s = self.get_outcome_stats(days)
        if not s["n_5d"] or s["n_5d"] < min_samples:
            return ""
        line = (
            f"近{days}日BUY信号：T+5胜率{s['win_rate_5d']}%（{s['n_5d']}个，"
            f"均收益{s['avg_return_5d']:+.1f}%）"
        )
        if s["n_20d"]:
            line += f"；T+20胜率{s['win_rate_20d']}%（{s['n_20d']}个，均收益{s['avg_return_20d']:+.1f}%）"
        return line

    # ------------------------------------------------------------------
    # Legacy alias (older callers / signal_stats.py)
    # ------------------------------------------------------------------
    def compute_outcomes(self, lookback_days: int = 60, forward_days: int = 5) -> int:
        """Deprecated: use update_outcomes(). Kept for backward compatibility."""
        res = self.update_outcomes(max_lookback_days=lookback_days)
        return res.get("updated_5d", 0) + res.get("updated_20d", 0)
