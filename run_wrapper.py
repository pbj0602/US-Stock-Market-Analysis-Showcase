#!/usr/bin/env python3
"""
Timezone-agnostic timing wrapper for the stock analysis system.

The LaunchAgent fires every 15 minutes, all day, every day (StartInterval —
no Hour/Minute/Weekday fields, so it no longer depends on what timezone the
Mac's system clock is set to). This script is the only thing that decides
whether it's actually time to run: it computes "now" in US/Eastern via pytz
(which is correct regardless of the machine's local timezone) and only
launches main.py once, within a window around 09:00 AM ET.

Previously the LaunchAgent itself fired at a hardcoded local wall-clock time
(20:45) that was calibrated assuming the Mac's system timezone was Beijing
(CST, UTC+8) — see git history. Travelling changes the system timezone
(e.g. to Stockholm/CEST, UTC+2), which silently shifted the *absolute* fire
time by the difference between the two zones, so the run landed mid-day ET
instead of pre-market. Driving the decision off pytz instead of off when
launchd happens to fire removes that failure mode entirely.

Usage:  python3 run_wrapper.py
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
    format='%(asctime)s - run_wrapper - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, 'run_wrapper.log')),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

TARGET_HOUR = 9   # 9:00 AM ET
TARGET_MIN  = 0

# Window around the target within which this firing is allowed to trigger
# the real run. Too-early firings just exit and let the next 15-min tick
# check again; too-late firings (e.g. Mac was asleep all morning) skip the
# day entirely rather than running a stale mid-day analysis.
EARLY_SKIP_S = 2 * 3600    # more than 2h before target -> not yet, try later
LATE_SKIP_S  = 90 * 60     # more than 90min after target -> too late, skip today

MARKER_PATH = os.path.join(LOG_DIR, '.run_wrapper_last_run_date')

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

    Marker formats:
      "YYYY-MM-DD"                -> done (main.py launched, or day skipped)
      "YYYY-MM-DD claimed <pid>"  -> a wrapper is sleeping until the target.
    A claim whose pid is gone (Mac rebooted / process killed while sleeping)
    is treated as not-run, so the next tick can pick the day back up instead
    of silently losing it.
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
        logger.warning(
            f"{today_et} claim by pid {parts[2]} is stale (process gone — "
            "reboot or kill while waiting) — reclaiming."
        )
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
        return  # too early — next 15-min tick will be closer

    if wait_s < -LATE_SKIP_S:
        logger.warning(
            f"{today_et} target window missed by {-wait_s/60:.0f} min "
            "(Mac likely asleep) — skipping today rather than running "
            "a stale mid-day analysis."
        )
        _mark_ran(today_et)  # don't retry every 15 min for the rest of the day
        return

    # We're in the run window — claim it (with our pid) so a concurrent/next
    # tick doesn't double-launch while we sleep. The claim only becomes a
    # "done" marker once main.py is actually launched.
    _mark_claimed(today_et)

    if wait_s > 0:
        dst_label = "EDT" if bool(now_et.dst()) else "EST"
        logger.info(
            f"US timezone: {dst_label}  |  "
            f"Target: {TARGET_HOUR:02d}:{TARGET_MIN:02d} ET  |  "
            f"Sleeping {wait_s:.0f}s ({wait_s/60:.1f} min)…"
        )
        time.sleep(wait_s)
    else:
        logger.info(
            f"Within catch-up window for {TARGET_HOUR:02d}:{TARGET_MIN:02d} ET "
            f"(delta={wait_s:.0f}s) — running now."
        )

    _mark_ran(today_et)
    logger.info("Launching main.py…")
    result = subprocess.run(
        [sys.executable, os.path.join(os.path.dirname(__file__), 'main.py')],
        cwd=os.path.dirname(__file__),
    )
    sys.exit(result.returncode)


if __name__ == '__main__':
    main()
