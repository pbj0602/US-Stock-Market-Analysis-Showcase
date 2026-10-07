"""
NYSE trading calendar — the one place that knows which days are sessions.

Used to (a) decide whether a daily bar is a finished session or an in-progress
placeholder, and (b) spot sessions that a data source silently dropped.
Update the holiday table once a year.
"""

from datetime import date, datetime, time, timedelta

import pytz

ET = pytz.timezone('US/Eastern')

# Regular close is 16:00 ET; give the data vendor 15 minutes to settle the
# final print before treating today's bar as complete.
SESSION_SETTLED = time(16, 15)

US_HOLIDAYS = {
    # 2025
    date(2025, 1, 1), date(2025, 1, 9), date(2025, 1, 20), date(2025, 2, 17),
    date(2025, 4, 18), date(2025, 5, 26), date(2025, 6, 19), date(2025, 7, 4),
    date(2025, 9, 1), date(2025, 11, 27), date(2025, 12, 25),
    # 2026
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
    # 2027
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
    date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
    date(2027, 11, 25), date(2027, 12, 24),
}


def now_et() -> datetime:
    return datetime.now(ET)


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in US_HOLIDAYS


def previous_trading_day(d: date) -> date:
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def last_completed_session(now: datetime | None = None) -> date:
    """The most recent session whose closing print is final."""
    now = (now or now_et()).astimezone(ET)
    today = now.date()
    if is_trading_day(today) and now.time() >= SESSION_SETTLED:
        return today
    return previous_trading_day(today)


def sessions_between(start: date, end: date) -> list[date]:
    """Trading days in [start, end], inclusive."""
    out, d = [], start
    while d <= end:
        if is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out
