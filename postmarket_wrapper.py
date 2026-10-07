#!/usr/bin/env python3
"""
Timezone-agnostic timing wrapper for the post-market runner.

Same rationale as run_wrapper.py: the LaunchAgent fires every 15 minutes
all day (StartInterval, no local-time fields), and this script is the one
that decides — via pytz US/Eastern — whether it's actually time to run,
targeting 16:45 ET (45 min after the 4pm close, letting closing prints
settle). This replaces a plist that fired at a hardcoded local wall-clock
time calibrated for a Beijing-timezone Mac, which silently misfires by
however many hours the system timezone has drifted from that assumption
(e.g. while travelling).

Usage:  python3 postmarket_wrapper.py
"""

import subprocess
import sys
import time
import os
from datetime import datetime, date
import pytz
import logging

LOG_DIR = os.path.join(os.path.dirname(__file__), 'logs')
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - postmarket_wrapper - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, 'postmarket_wrapper.log')),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

TARGET_HOUR = 16  # 4:45 PM ET
TARGET_MIN  = 45

EARLY_SKIP_S = 2 * 3600
LATE_SKIP_S  = 90 * 60

MARKER_PATH = os.path.join(LOG_DIR, '.postmarket_wrapper_last_run_date')

_US_HOLIDAYS = {
    date(2026, 1, 1),
    date(2026, 1, 19),
    date(2026, 2, 16),
    date(2026, 4, 3),
    date(2026, 5, 25),
    date(2026, 6, 19),  # Juneteenth
    date(2026, 7, 3),
    date(2026, 9, 7),
    date(2026, 11, 26),
    date(2026, 12, 25),
}


def _is_us_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in _US_HOLIDAYS


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _already_ran_today(today_et: date) -> bool:
    """True if today's run is done, or claimed by a wrapper that is still alive.

    Same marker scheme as run_wrapper.py: "YYYY-MM-DD" = done,
    "YYYY-MM-DD claimed <pid>" = sleeping until target. A claim whose pid is
    gone (reboot / kill while sleeping) is reclaimed by the next tick.
    """
    try:
        with open(MARKER_PATH) as f:
            parts = f.read().split()
    except FileNotFoundError:
        return False
    if not parts or parts[0] != today_et.isoformat():
        return False
    if len(parts) == 3 and parts[1] == 'claimed':
        if _pid_alive(int(parts[2])):
            return True
        logger.warning(f"{today_et} claim by pid {parts[2]} is stale — reclaiming.")
        return False
    return True


def _mark_ran(today_et: date) -> None:
    with open(MARKER_PATH, 'w') as f:
        f.write(today_et.isoformat())


def _mark_claimed(today_et: date) -> None:
    with open(MARKER_PATH, 'w') as f:
        f.write(f"{today_et.isoformat()} claimed {os.getpid()}")


def main():
    us_eastern = pytz.timezone('US/Eastern')
    now_et = datetime.now(us_eastern)
    today_et = now_et.date()

    if not _is_us_trading_day(today_et):
        return

    if _already_ran_today(today_et):
        return

    target_et = now_et.replace(
        hour=TARGET_HOUR, minute=TARGET_MIN, second=0, microsecond=0
    )
    wait_s = (target_et - now_et).total_seconds()

    if wait_s > EARLY_SKIP_S:
        return

    if wait_s < -LATE_SKIP_S:
        logger.warning(
            f"{today_et} target window missed by {-wait_s/60:.0f} min — "
            "skipping today (signal outcomes still update on the next run; "
            f"to backfill the review: post_market_runner.py {today_et})."
        )
        _mark_ran(today_et)
        return

    _mark_claimed(today_et)

    if wait_s > 0:
        logger.info(f"Sleeping {wait_s:.0f}s until {TARGET_HOUR:02d}:{TARGET_MIN:02d} ET…")
        time.sleep(wait_s)
    else:
        logger.info(f"Within catch-up window (delta={wait_s:.0f}s) — running now.")

    _mark_ran(today_et)
    logger.info("Launching post_market_runner.py…")
    result = subprocess.run(
        [sys.executable, os.path.join(os.path.dirname(__file__), 'post_market_runner.py')],
        cwd=os.path.dirname(__file__),
    )
    sys.exit(result.returncode)


if __name__ == '__main__':
    main()
