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

* ``17,47 * * * *`` — a *plan* tick twice an hour. A tiny job (sparse
  checkout of three small files, no dependencies, about 10 s) runs ``python3
  scan_schedule.py plan``, which reads the interval from settings.json and
  the last scan time from companies.json. Only when a scan is due does the full scan job start.
* ``20 */6 * * *`` — a personal-alert pass every 6 hours that does not scan,
  so the 6-hour cadence holds even when the scan interval is 12 hours.

A scan is due when no scan has happened since the latest interval boundary
(UTC hours divisible by the interval: 0, 3, 6, ... for 3 hours). Boundaries
rather than "N hours since the last scan" keep the schedule from drifting
when GitHub starts a tick late (measured: often by hours). A manual
"Run check" (workflow_dispatch) always scans. Every run of the workflow
shares one concurrency group, so scans never overlap.

The scan scheduler chain (scan_scheduler.yml, Oct 2026) carries the real
cadence, GitHub Actions only — the same idea as Movie Ticket Radar's
self-dispatched segments. One small run at a time waits (``python3
scan_schedule.py wait``) until the next window opens plus CHAIN_OFFSET,
dispatches check_jobs.yml with input ``mode=plan`` and then dispatches the
next scheduler run before it exits. ``mode=plan`` is gated exactly like a
plan tick, so the chain, a late cron tick and a retry can never start a
second scan in one window. Dispatched runs don't go through GitHub's cron
queue, which is what drops and delays the ticks. "Run check" sends no
input (``scan``) and scans at once, as before. The cron ticks stay as the
safety net: every Check Jobs run restarts the chain if no scheduler run is
active.

GitHub limits worth knowing: scheduled runs can start late or, under load,
be dropped — GitHub's docs name the start of every hour as the busiest time.
Measured 2026-10-11/12 with the old ``0 * * * *`` tick: of ~17 ticks due in
14 hours GitHub created 5 runs (none cancelled; no tick at all from 00:11 to
06:55 UTC), and a ``:20`` alert tick started 5 h 43 min late. So the plan tick
is off the hour and twice an hour: each drop costs half an hour, not a whole
one. We can't make GitHub start a run; ``scan_overdue`` makes a missed scan
visible in the app instead of calling it healthy.

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
PLAN_CRON = "17,47 * * * *"
PLAN_MINUTES = (17, 47)              # the minutes PLAN_CRON fires at, in order
# a scan is "overdue" once the first window it missed opened this long ago:
# the window's first tick (:17) plus room for GitHub's usual lateness
OVERDUE_GRACE = timedelta(minutes=90)
ALERTS_CRON = "20 */6 * * *"
# the scheduler chain: dispatch this long after a window opens; if a scan is
# still due when it looks (one in progress, or the last one failed), look
# again after CHAIN_RETRY; never wait longer than CHAIN_MAX_WAIT in one run
# (GitHub ends a job after 6 h)
CHAIN_OFFSET = timedelta(minutes=2)
CHAIN_RETRY = timedelta(minutes=30)
CHAIN_MAX_WAIT = timedelta(hours=5)

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


def scan_overdue(now: datetime, hours: int, last: datetime | None) -> bool:
    """True when a scan was due and has not happened for OVERDUE_GRACE: no
    scan started since the first interval boundary after ``last``. Counted
    from that first missed boundary, so a scan missed at 09:00 is still
    overdue at 12:10 (not "just due again")."""
    if not scan_due(now, hours, last):
        return False
    if last is None:
        return True
    missed = latest_boundary(last, hours) + timedelta(hours=hours)
    return now.astimezone(timezone.utc) - missed >= OVERDUE_GRACE


def next_tick(now: datetime) -> datetime:
    """The next plan tick (PLAN_MINUTES past each UTC hour) after ``now``."""
    hour = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    for h in (hour, hour + timedelta(hours=1)):
        for m in PLAN_MINUTES:
            t = h + timedelta(minutes=m)
            if t > now:
                return t
    raise AssertionError("unreachable")


def next_scan(now: datetime, hours: int, last: datetime | None) -> datetime:
    """When the next scheduled scan should start: the next plan tick if one
    is already due, else when the scheduler chain dispatches the next
    window's scan. (GitHub may start it later than this.)"""
    now = now.astimezone(timezone.utc)
    if scan_due(now, hours, last):
        return next_tick(now)
    return latest_boundary(now, hours) + timedelta(hours=hours) + CHAIN_OFFSET


def chain_wait(now: datetime, hours: int, last: datetime | None) -> timedelta:
    """How long the scheduler run waits before dispatching a plan run: until
    the next window opens (+ CHAIN_OFFSET), or CHAIN_RETRY when a scan is
    still due (in progress now, or failed — the plan run then retries it),
    never more than CHAIN_MAX_WAIT."""
    now = now.astimezone(timezone.utc)
    if scan_due(now, hours, last):
        return CHAIN_RETRY
    target = latest_boundary(now, hours) + timedelta(hours=hours) + CHAIN_OFFSET
    return min(max(target - now, timedelta(0)), CHAIN_MAX_WAIT)


def latest_run(companies) -> dict | None:
    """The latest COMPLETED scan, from the record scraper.run_all keeps in
    each company's "scan" field: {"started_at", "finished_at" (datetimes),
    "jobs_found", "new_jobs", "portals", "portals_failed" (ints)}. That
    record reaches GitHub only after the whole scan has finished, so a scan
    still running is never described. None when there is no usable record
    — or when a later scan left none (older scanner code), so a count from
    an earlier scan is never passed off as the latest one."""
    best = None
    for c in companies or []:
        scan = c.get("scan") if isinstance(c, dict) else None
        run = scan.get("run") if isinstance(scan, dict) else None
        if not isinstance(run, dict):
            continue
        started, finished = _parse(run.get("started_at")), _parse(run.get("finished_at"))
        counts = {k: run.get(k, 0) for k in ("jobs_found", "new_jobs", "portals", "portals_failed")}
        if (started is None or finished is None or finished < started
                or any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in counts.values())):
            continue
        if best is None or finished > best["finished_at"]:
            best = {"started_at": started, "finished_at": finished, **counts}
    if best is None:
        return None
    # every company a scan checks is stamped before that scan's record is
    # written: a later stamp means a later scan that left no record
    checked = [_parse(c.get("last_checked")) for c in companies if isinstance(c, dict)]
    if any(t and t > best["finished_at"] for t in checked):
        return None
    return best


def next_scan_state(now: datetime, hours: int, last: datetime | None) -> tuple[str, datetime | None]:
    """(state, time) of the next scheduled scan, as the real scheduler sees
    it (``last`` = when the last scan started, as for scan_due):

      "scheduled", T  no scan is due; the chain dispatches at T (window +
                      CHAIN_OFFSET) — also while a window has just opened
                      and T is still ahead
      "due", T        T (the missed window + CHAIN_OFFSET) has passed: a scan
                      should be running or starting; the chain retries every
                      CHAIN_RETRY and the plan ticks are the safety net
      "overdue", T    no scan for OVERDUE_GRACE after the window it missed
      "overdue", None no scan has ever been recorded
    """
    now = now.astimezone(timezone.utc)
    if not scan_due(now, hours, last):
        return "scheduled", latest_boundary(now, hours) + timedelta(hours=hours) + CHAIN_OFFSET
    if last is None:
        return "overdue", None
    expected = latest_boundary(last, hours) + timedelta(hours=hours) + CHAIN_OFFSET
    if now < expected:
        return "scheduled", expected
    return ("overdue" if scan_overdue(now, hours, last) else "due"), expected


def alert_due(now: datetime, last_sent: datetime | None) -> bool:
    """May this person be emailed now? At most once per MEMBER_ALERT_HOURS —
    independent of the scan interval."""
    return last_sent is None or now - last_sent >= timedelta(hours=MEMBER_ALERT_HOURS) - ALERT_GRACE


def plan(event: str, schedule: str, settings, companies, now: datetime | None = None,
         mode: str = "scan") -> dict:
    """What one workflow run should do: {"scan": bool, "deliver": bool}.
    * workflow_dispatch with mode "plan" (the scan scheduler chain): like
      the plan tick — scan only if due
    * workflow_dispatch otherwise (Run check): scan (and the personal step after it)
    * the hourly tick: scan only if due by the admin's interval
    * the 6-hourly alerts tick: no scan, only the personal alert pass
    Anything else does nothing."""
    now = now or datetime.now(timezone.utc)
    if event == "workflow_dispatch" and mode == "plan":
        return {"scan": scan_due(now, interval_from(settings), last_scan(companies)), "deliver": False}
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


def _summary(line: str, label: str = "Scan plan") -> None:
    """Also show the decision on the run's summary page (never fails)."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            print(f"**{label}:** {line}", file=f)
    except OSError:
        pass


def wait_main() -> int:
    """``python3 scan_schedule.py wait`` — the scheduler run's wait, as
    ``seconds=N`` and ``at=<UTC time>`` for $GITHUB_OUTPUT. Never fails: on
    any error it waits CHAIN_RETRY, so the chain keeps going."""
    now = datetime.now(timezone.utc)
    try:
        settings, companies = _load("settings.json", {}), _load("companies.json", [])
        hours, last = interval_from(settings), last_scan(companies)
        wait = chain_wait(now, hours, last)
        line = (f"interval {hours} h · last scan {last} · due={scan_due(now, hours, last)} · "
                f"dispatch a plan run at {(now + wait).isoformat(timespec='minutes')}")
    except Exception as e:
        wait = CHAIN_RETRY
        line = f"wait failed ({type(e).__name__}); retrying in {CHAIN_RETRY}"
    print(line, file=sys.stderr)
    _summary(line, "Scan scheduler")
    print(f"seconds={int(wait.total_seconds())}")
    print(f"at={(now + wait).isoformat(timespec='minutes')}")
    return 0


def main(argv=None) -> int:
    """``python3 scan_schedule.py plan`` — prints ``scan=true|false`` and
    ``deliver=true|false`` for $GITHUB_OUTPUT, plus a short human line on
    stderr. Never fails the run: on any error it plans nothing.
    ``python3 scan_schedule.py wait`` — see wait_main."""
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["wait"]:
        return wait_main()
    if argv[:1] != ["plan"]:
        print("usage: scan_schedule.py plan|wait", file=sys.stderr)
        return 2
    try:
        settings, companies = _load("settings.json", {}), _load("companies.json", [])
        event, schedule = os.environ.get("EVENT", ""), os.environ.get("SCHEDULE", "")
        mode = os.environ.get("MODE", "") or "scan"
        decision = plan(event, schedule, settings, companies, mode=mode)
        hours, last = interval_from(settings), last_scan(companies)
        now = datetime.now(timezone.utc)
        trigger = (f"schedule {schedule}" if event == "schedule"
                   else f"{event} mode={mode}" if event == "workflow_dispatch" else event or "unknown")
        # the log says what fired this run and why it did or didn't scan
        # (a plan-only run is not a scan); counts and times only, no personal data
        line = (f"trigger {trigger} · interval {hours} h · last scan {last} · window opened "
                f"{latest_boundary(now, hours)} · overdue={scan_overdue(now, hours, last)} · "
                f"scan={decision['scan']} deliver={decision['deliver']}")
        print(line, file=sys.stderr)
        _summary(line)
    except Exception as e:                       # plan nothing rather than break
        print(f"plan failed ({type(e).__name__}); nothing runs this tick", file=sys.stderr)
        decision = {"scan": False, "deliver": False}
    print(f"scan={'true' if decision['scan'] else 'false'}")
    print(f"deliver={'true' if decision['deliver'] else 'false'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
