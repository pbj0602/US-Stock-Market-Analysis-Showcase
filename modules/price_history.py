"""
Clean daily OHLCV from yfinance — every daily-bar fetch goes through here.

yfinance's daily endpoint has two failure modes that each produced a wrong
report on 2026-09-23:

  1. Before the open it appends a placeholder bar for today with Close=NaN.
     Any comparison against NaN is False, so SPY failed all five bullish
     checks and the regime came out "BEAR, strength 100%" with SPY 0.8% off
     its high.
  2. It can drop a whole finished session (9/22 was missing while the hourly
     endpoint had it), so "today's" sector move was really the prior day's.

get_daily() removes in-progress bars, fills dropped sessions from hourly
bars, and records anything it could not fix in data_health.
"""

import logging

import pandas as pd
import yfinance as yf

from modules import data_health
from modules.trading_calendar import last_completed_session, sessions_between

logger = logging.getLogger(__name__)

# Only look for dropped sessions this far back — older gaps barely move
# 20/50/200-day averages, and the hourly endpoint only reaches back ~2 years.
_GAP_LOOKBACK_SESSIONS = 10
# A finished session moving more than this is almost always a bad print.
_MAX_PLAUSIBLE_MOVE_PCT = 25.0


def get_daily(symbol: str, period: str = '3mo', repair: bool = True) -> pd.DataFrame:
    """
    Daily bars for completed sessions only, indexed like Ticker.history().
    Returns an empty DataFrame on failure (and records it).
    """
    area = f'行情 {symbol}'
    try:
        hist = yf.Ticker(symbol).history(period=period, interval='1d')
    except Exception as e:
        data_health.record(data_health.WARNING, area, f'日线获取失败：{e}')
        return pd.DataFrame()
    if hist.empty:
        data_health.record(data_health.WARNING, area, '日线无数据（代码可能已变更/退市）')
        return hist

    hist = hist.dropna(subset=['Close'])
    last_done = last_completed_session()
    hist = hist[[ts.date() <= last_done for ts in hist.index]]
    if hist.empty:
        data_health.record(data_health.WARNING, area, '没有已收盘的日线')
        return hist

    if repair:
        hist = _fill_missing_sessions(symbol, hist, last_done, area)

    _check_last_move(symbol, hist, area)
    return hist


def _fill_missing_sessions(symbol: str, hist: pd.DataFrame, last_done, area: str) -> pd.DataFrame:
    have = {ts.date() for ts in hist.index}
    window = sessions_between(hist.index[0].date(), last_done)[-_GAP_LOOKBACK_SESSIONS:]
    missing = [d for d in window if d not in have]
    if not missing:
        return hist

    rebuilt = _daily_from_hourly(symbol, missing)
    fixed = [d for d in missing if d in rebuilt.index]
    still_missing = [d for d in missing if d not in rebuilt.index]

    if fixed:
        tz = hist.index.tz
        rows = rebuilt.loc[fixed].copy()
        rows.index = pd.DatetimeIndex([pd.Timestamp(d).tz_localize(tz) for d in fixed])
        hist = pd.concat([hist, rows.reindex(columns=hist.columns, fill_value=0)]).sort_index()
        data_health.record(
            data_health.REPAIRED, area,
            f"日线缺失 {', '.join(d.strftime('%m/%d') for d in fixed)}，已用小时线补齐（成交量为估算）",
        )
    if still_missing:
        sev = data_health.CRITICAL if symbol == 'SPY' else data_health.WARNING
        data_health.record(
            sev, area,
            f"日线缺失 {', '.join(d.strftime('%m/%d') for d in still_missing)}，无法补齐",
        )
    return hist


def _daily_from_hourly(symbol: str, days: list) -> pd.DataFrame:
    try:
        h = yf.Ticker(symbol).history(period='1mo', interval='1h')
    except Exception as e:
        logger.warning(f'[price_history] hourly fetch failed for {symbol}: {e}')
        return pd.DataFrame()
    if h.empty:
        return pd.DataFrame()
    h = h.dropna(subset=['Close'])
    h = h[[ts.date() in set(days) for ts in h.index]]
    if h.empty:
        return pd.DataFrame()
    daily = h.groupby(h.index.date).agg(
        Open=('Open', 'first'), High=('High', 'max'), Low=('Low', 'min'),
        Close=('Close', 'last'), Volume=('Volume', 'sum'),
    )
    return daily


def _check_last_move(symbol: str, hist: pd.DataFrame, area: str) -> None:
    if len(hist) < 2 or symbol.startswith('^'):
        return
    prev, last = hist['Close'].iloc[-2], hist['Close'].iloc[-1]
    if prev > 0:
        move = (last - prev) / prev * 100
        if abs(move) > _MAX_PLAUSIBLE_MOVE_PCT:
            data_health.record(
                data_health.WARNING, area,
                f'最近一日涨跌 {move:+.1f}% 异常，可能是错误报价',
            )
