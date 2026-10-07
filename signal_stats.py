#!/usr/bin/env python3
"""
Signal Statistics CLI (PRD §8.3).

Usage:
    python signal_stats.py

Prints:
  - Overall signal accuracy (% of BUY signals with positive T+5 return)
  - Accuracy by market state (BULL / BEAR / RANGE)
  - Accuracy by signal score bucket (0-0.6, 0.6-0.7, 0.7-0.8, 0.8-1.0)
  - Best and worst performing tickers by average signal return
"""

import sqlite3
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_DB_PATH = "data/signal_log.db"


def _query(sql: str, params=()):
    if not os.path.exists(_DB_PATH):
        print(f"[signal_stats] Database not found at {_DB_PATH}. Run the analysis first.")
        sys.exit(1)
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows


def _pct(n, total):
    return f"{n}/{total} ({n/total*100:.1f}%)" if total else "N/A"


def main():
    print("=" * 60)
    print("SIGNAL PERFORMANCE STATISTICS")
    print("=" * 60)

    # -- Overall accuracy --------------------------------------------------
    rows = _query(
        """SELECT signal, signal_return
           FROM signal_log
           WHERE signal_return IS NOT NULL AND signal IN ('BUY','SELL')"""
    )

    if not rows:
        print("No outcome data yet. Run weekly compute_outcomes() first.")
        return

    buy_rows = [r for r in rows if r["signal"] == "BUY"]
    buy_correct = [r for r in buy_rows if r["signal_return"] > 0]
    sell_rows = [r for r in rows if r["signal"] == "SELL"]
    sell_correct = [r for r in sell_rows if r["signal_return"] < 0]  # price fell

    print(f"\nOverall BUY accuracy : {_pct(len(buy_correct), len(buy_rows))}")
    print(f"Overall SELL accuracy: {_pct(len(sell_correct), len(sell_rows))}")

    # -- By market state ---------------------------------------------------
    print("\n--- Accuracy by Market State (BUY signals) ---")
    for state in ("BULL", "BEAR", "RANGE"):
        state_rows = _query(
            """SELECT signal_return FROM signal_log
               WHERE signal='BUY' AND market_state=? AND signal_return IS NOT NULL""",
            (state,),
        )
        correct = sum(1 for r in state_rows if r["signal_return"] > 0)
        print(f"  {state:<8}: {_pct(correct, len(state_rows))}")

    # -- By score bucket ---------------------------------------------------
    print("\n--- Accuracy by Score Bucket (BUY signals) ---")
    buckets = [
        ("0.0 – 0.6", 0.0, 0.6),
        ("0.6 – 0.7", 0.6, 0.7),
        ("0.7 – 0.8", 0.7, 0.8),
        ("0.8 – 1.0", 0.8, 1.01),
    ]
    for label, lo, hi in buckets:
        brows = _query(
            """SELECT signal_return FROM signal_log
               WHERE signal='BUY' AND score>=? AND score<? AND signal_return IS NOT NULL""",
            (lo, hi),
        )
        correct = sum(1 for r in brows if r["signal_return"] > 0)
        print(f"  Score {label}: {_pct(correct, len(brows))}")

    # -- Best / worst tickers ----------------------------------------------
    print("\n--- Average Signal Return by Ticker (BUY, top 5 best) ---")
    ticker_rows = _query(
        """SELECT ticker, AVG(signal_return) as avg_ret, COUNT(*) as cnt
           FROM signal_log
           WHERE signal='BUY' AND signal_return IS NOT NULL
           GROUP BY ticker
           HAVING cnt >= 2
           ORDER BY avg_ret DESC
           LIMIT 5"""
    )
    for r in ticker_rows:
        print(f"  {r['ticker']:<8} avg={r['avg_ret']*100:+.2f}%  (n={r['cnt']})")

    print("\n--- Average Signal Return by Ticker (BUY, top 5 worst) ---")
    worst_rows = _query(
        """SELECT ticker, AVG(signal_return) as avg_ret, COUNT(*) as cnt
           FROM signal_log
           WHERE signal='BUY' AND signal_return IS NOT NULL
           GROUP BY ticker
           HAVING cnt >= 2
           ORDER BY avg_ret ASC
           LIMIT 5"""
    )
    for r in worst_rows:
        print(f"  {r['ticker']:<8} avg={r['avg_ret']*100:+.2f}%  (n={r['cnt']})")

    print()


if __name__ == "__main__":
    main()
