"""
Run-wide data-health ledger.

Any module that notices bad input (missing bar, failed fetch, implausible
value) records it here instead of silently substituting a default. The
validator adds its cross-checks, and the report shows the result as a single
"数据健康" line so a reader knows whether today's numbers can be trusted.

Severity:
  critical — the number drives a real-money action (regime, holdings, stops).
             The affected section is suppressed and an alert is pushed.
  warning  — shown as-is, flagged in the health line.
  repaired — a problem was detected and fixed; logged, not flagged.
"""

import logging
import threading

logger = logging.getLogger(__name__)

CRITICAL, WARNING, REPAIRED = 'critical', 'warning', 'repaired'

_lock = threading.Lock()
_issues: list[dict] = []


def record(severity: str, area: str, message: str) -> None:
    """Add one issue. Duplicate (area, message) pairs are recorded once."""
    with _lock:
        if any(i['area'] == area and i['message'] == message for i in _issues):
            return
        _issues.append({'severity': severity, 'area': area, 'message': message})
    log = logger.warning if severity != REPAIRED else logger.info
    log(f"[DataHealth:{severity}] {area}: {message}")


def reset() -> None:
    with _lock:
        _issues.clear()


def issues(severity: str | None = None) -> list[dict]:
    with _lock:
        return [dict(i) for i in _issues if severity is None or i['severity'] == severity]


def has_critical(area: str | None = None) -> bool:
    return any(area is None or i['area'] == area for i in issues(CRITICAL))


def summary() -> dict:
    """Compact view for the report: status + the flagged (non-repaired) issues."""
    flagged = [i for i in issues() if i['severity'] != REPAIRED]
    crit = [i for i in flagged if i['severity'] == CRITICAL]
    if crit:
        status = 'critical'
    elif flagged:
        status = 'warning'
    else:
        status = 'ok'
    return {
        'status': status,
        'critical': crit,
        'warnings': [i for i in flagged if i['severity'] == WARNING],
        'repaired': issues(REPAIRED),
    }
