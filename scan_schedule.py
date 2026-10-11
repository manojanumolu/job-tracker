"""When the shared job scan runs, and when personal alert emails go out.

Two separate cadences (Oct 2026):

* **Global scan interval** (admins only): 1, 3, 6 or 12 hours, default 3.
  Stored as ``scan_interval_hours`` in settings.json — the shared config file
  the app already saves through the owner-checked, merge-safe GitHub path.
  It controls the ONE shared scanner; nobody gets a private scanner.
* **Personal alert cadence**: fixed at 6 hours for every member. A person is
  emailed at most once per 6 hours (with every new job since), whatever the
  global interval is. Enforced in user_alerts.py.

How the scan interval reaches the real scheduler. GitHub Actions cron lines
are static, so check_jobs.yml has two fixed schedules:

* ``0 * * * *`` — an hourly *plan* tick. A tiny job (sparse checkout of three
  small files, no dependencies, about 10 s) runs ``python3 scan_schedule.py
  plan``, which reads the interval from settings.json and the last scan time
  from companies.json. Only when a scan is due does the full scan job start.
* ``20 */6 * * *`` — a personal-alert pass every 6 hours that does not scan,
  so the 6-hour cadence holds even when the scan interval is 12 hours.

A scan is due when no scan has happened since the latest interval boundary
(UTC hours divisible by the interval: 0, 3, 6, ... for 3 hours). Boundaries
rather than "N hours since the last scan" keep the schedule from drifting
when GitHub starts a tick late (measured: often by hours). A manual
"Run check" (workflow_dispatch) always scans. Every run of the workflow
shares one concurrency group, so scans never overlap.

GitHub limits worth knowing: scheduled runs can start late or, under load,
be skipped; the hourly tick makes a missed scan catch up at the next tick.

Standard library only: the plan job runs it with the runner's own python3.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ALLOWED_INTERVALS = (1, 3, 6, 12)
DEFAULT_INTERVAL = 3
MEMBER_ALERT_HOURS = 6
# a person's next email may go out this much before the 6 hours are up, so a
# pass that GitHub starts a little early or late never skips a whole cycle
ALERT_GRACE = timedelta(minutes=30)
SETTING = "scan_interval_hours"
PLAN_CRON = "0 * * * *"
ALERTS_CRON = "20 */6 * * *"

BASE = Path(__file__).parent


class InvalidInterval(ValueError):
    """Not one of ALLOWED_INTERVALS."""


def validate_interval(value) -> int:
    """The interval an admin asked for, or InvalidInterval. Accepts only the
    exact allowed whole numbers (bools, floats, strings and negatives are
    refused)."""
    if isinstance(value, bool) or not isinstance(value, int) or value not in ALLOWED_INTERVALS:
        raise InvalidInterval("The scan interval must be 1, 3, 6 or 12 hours.")
    return value


def interval_from(settings) -> int:
    """The configured interval; anything missing or invalid falls back to the
    default (fail safe: a hand-edited file never stops or floods scanning)."""
    value = settings.get(SETTING) if isinstance(settings, dict) else None
    try:
        return validate_interval(value)
    except InvalidInterval:
        return DEFAULT_INTERVAL


def with_interval(settings, hours) -> dict:
    """settings.json with a new interval; every other field kept."""
    new = dict(settings) if isinstance(settings, dict) else {}
    new[SETTING] = validate_interval(hours)
    return new


def _parse(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def last_scan(companies) -> datetime | None:
    """When the last scan STARTED. The scanner stamps every company it checks,
    one after another, so the earliest stamp is the start of the last scan
    (a company added since has no stamp yet). Using the start, a scan that a
    late tick began just before a boundary and finished after it still counts
    for the window it began in, so the next window gets its own scan."""
    stamps = [_parse(c.get("last_checked")) for c in companies or [] if isinstance(c, dict)]
    stamps = [s for s in stamps if s]
    return min(stamps) if stamps else None


def latest_boundary(now: datetime, hours: int) -> datetime:
    """The most recent interval boundary at or before ``now`` (UTC)."""
    now = now.astimezone(timezone.utc)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return day + timedelta(hours=(now.hour // hours) * hours)


def scan_due(now: datetime, hours: int, last: datetime | None) -> bool:
    return last is None or last < latest_boundary(now, hours)


def next_scan(now: datetime, hours: int, last: datetime | None) -> datetime:
    """When the next scheduled scan should start: the next hourly plan tick if
    one is already due, else the next interval boundary. (GitHub may start
    it later than this.)"""
    now = now.astimezone(timezone.utc)
    if scan_due(now, hours, last):
        return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return latest_boundary(now, hours) + timedelta(hours=hours)


def alert_due(now: datetime, last_sent: datetime | None) -> bool:
    """May this person be emailed now? At most once per MEMBER_ALERT_HOURS —
    independent of the scan interval."""
    return last_sent is None or now - last_sent >= timedelta(hours=MEMBER_ALERT_HOURS) - ALERT_GRACE


def plan(event: str, schedule: str, settings, companies, now: datetime | None = None) -> dict:
    """What one workflow run should do: {"scan": bool, "deliver": bool}.
    * workflow_dispatch (Run check): scan (and the personal step after it)
    * the hourly tick: scan only if due by the admin's interval
    * the 6-hourly alerts tick: no scan, only the personal alert pass
    Anything else does nothing."""
    now = now or datetime.now(timezone.utc)
    if event == "workflow_dispatch":
        return {"scan": True, "deliver": False}
    if event == "schedule" and schedule == ALERTS_CRON:
        return {"scan": False, "deliver": True}
    if event == "schedule":
        return {"scan": scan_due(now, interval_from(settings), last_scan(companies)), "deliver": False}
    return {"scan": False, "deliver": False}


def _load(name: str, default):
    try:
        return json.loads((BASE / name).read_text("utf-8"))
    except Exception:
        return default


def main(argv=None) -> int:
    """``python3 scan_schedule.py plan`` — prints ``scan=true|false`` and
    ``deliver=true|false`` for $GITHUB_OUTPUT, plus a short human line on
    stderr. Never fails the run: on any error it plans nothing."""
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] != ["plan"]:
        print("usage: scan_schedule.py plan", file=sys.stderr)
        return 2
    try:
        settings, companies = _load("settings.json", {}), _load("companies.json", [])
        decision = plan(os.environ.get("EVENT", ""), os.environ.get("SCHEDULE", ""), settings, companies)
        print(f"interval {interval_from(settings)} h · last scan {last_scan(companies)} · "
              f"scan={decision['scan']} deliver={decision['deliver']}", file=sys.stderr)
    except Exception as e:                       # plan nothing rather than break
        print(f"plan failed ({type(e).__name__}); nothing runs this tick", file=sys.stderr)
        decision = {"scan": False, "deliver": False}
    print(f"scan={'true' if decision['scan'] else 'false'}")
    print(f"deliver={'true' if decision['deliver'] else 'false'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
