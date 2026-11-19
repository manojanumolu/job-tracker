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
# Targeted Workday recovery (PR C): dispatch this long after a data centre's
# cooldown ends, and only when that lands this far ahead of the next window
# (otherwise the window's own scan probes it).
RECOVERY_OFFSET = timedelta(minutes=2)
RECOVERY_LEAD = timedelta(minutes=20)

BASE = Path(__file__).resolve().parent.parent          # the repository root (this file is in src/)


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
    # M7 publishes a completed orchestration marker even when individual portals
    # are deferred. Keep this projection in companies so sparse schedulers and
    # the UI read the SAME committed snapshot, without another data dependency.
    # A Workday-only recovery run (scope "workday") never counts as the window's scan.
    runs = [(c.get("scan") or {}).get("run", {}) for c in companies or [] if isinstance(c, dict) and isinstance(c.get("scan"), dict)]
    runs = [r for r in runs if isinstance(r, dict) and r.get("scope", "all") == "all"]
    completed = [r for r in runs if isinstance(r, dict) and r.get("recovery_version") == 1 and _parse(r.get("started_at"))
                 and _parse(r.get("finished_at")) and _parse(r["finished_at"]) >= _parse(r["started_at"])]
    if completed:
        return _parse(max(completed, key=lambda r: _parse(r["finished_at"]))["started_at"])
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


def chain_next(now: datetime, hours: int, last: datetime | None, companies=None, state=None) -> tuple[timedelta, str]:
    """(wait, scope) for the scheduler run: the next full-scan window as before,
    unless a Workday data-centre probe becomes due well before it - then a
    targeted scope="workday" plan run at that cooldown's end (+RECOVERY_OFFSET)."""
    now = now.astimezone(timezone.utc)
    wait = chain_wait(now, hours, last)
    if scan_due(now, hours, last):
        return wait, "all"            # a full scan is pending: it probes everything due
    at = recovery_due_at(companies, state, now)
    if at is None:
        return wait, "all"
    rec_wait = max(at + RECOVERY_OFFSET - now, timedelta(0))
    if rec_wait + RECOVERY_LEAD <= wait:
        return min(rec_wait, CHAIN_MAX_WAIT), "workday"
    return wait, "all"


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
        if not isinstance(run, dict) or run.get("scope", "all") != "all":
            continue   # a Workday-only recovery run is not "the latest scan"
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
    # (companies last stamped by a Workday-only recovery run don't count here)
    checked = [_parse(c.get("last_checked")) for c in companies if isinstance(c, dict)
               and ((c.get("scan") or {}).get("run") or {}).get("scope", "all") == "all"]
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


def _cluster(host: str) -> str:
    part = (host or "").split(".")
    return part[1] if len(part) > 2 and part[1].startswith("wd") and part[1][2:].isdigit() else ""


def _epoch(value) -> float:
    dt = _parse(value) if isinstance(value, str) else None
    return dt.timestamp() if dt else 0.0


def recovery_due_at(companies, state, now: datetime | None = None) -> datetime | None:
    """When a targeted Workday recovery run is next worth dispatching, or None.

    Mirrors the scanner's per-data-centre gating from published data only (the
    scanner re-checks everything itself). A data centre is gated while its newest
    API 429 is newer than its newest real healthy scan; its probe becomes eligible
    when the latest cooldown among its throttled hosts ends. A data centre is only
    due if NO run (full or targeted) has started since that moment - so every
    cooldown expiry earns at most one targeted run: no dispatch loops, no repeats."""
    if not isinstance(state, dict) or not isinstance(state.get("hosts"), dict):
        return None
    hosts, records = state["hosts"], state.get("companies") if isinstance(state.get("companies"), dict) else {}
    members: dict[str, set] = {}
    for c in companies or []:
        rec = (c.get("scan") or {}).get("recovery") if isinstance(c, dict) and isinstance(c.get("scan"), dict) else None
        if not isinstance(rec, dict) or {m.strip() for m in str(rec.get("discovery_method", "")).split(",")} != {"workday"}:
            continue   # Workday API companies only (sitemaps are not gated)
        for h in str(rec.get("host", "")).split(", "):
            if _cluster(h):
                members.setdefault(_cluster(h), set()).add((c.get("id"), h))
    runs = [((c.get("scan") or {}).get("run") or {}) for c in companies or [] if isinstance(c, dict) and isinstance(c.get("scan"), dict)]
    last_run = max([_epoch(r.get("started_at")) for r in runs if isinstance(r, dict)] + [0.0])
    due = []
    for name, pairs in members.items():
        api_hosts = {h for _, h in pairs}
        limited = [o for h, o in hosts.items() if h in api_hosts and isinstance(o, dict) and o.get("failure_category") == "rate_limit"]
        if not limited:
            continue
        throttled_at = max(_epoch(o.get("observed_at")) for o in limited)
        healthy = [_epoch((records.get(cid) or {}).get("last_attempt")) for cid, _ in pairs
                   if (records.get(cid) or {}).get("outcome") in ("healthy", "degraded")]
        if healthy and max(healthy) > throttled_at:
            continue   # released by a real healthy scan
        until = max(_epoch(o.get("cooldown_until")) for o in limited)
        if until > last_run:
            due.append(until)
    return datetime.fromtimestamp(min(due), timezone.utc) if due else None


def plan(event: str, schedule: str, settings, companies, now: datetime | None = None,
         mode: str = "scan", scope: str = "all", state=None) -> dict:
    """What one workflow run should do: {"scan": bool, "deliver": bool}.
    * workflow_dispatch with mode "plan" (the scan scheduler chain): like
      the plan tick — scan only if due
    * workflow_dispatch otherwise (Run check): scan (and the personal step after it)
    * the hourly tick: scan only if due by the admin's interval
    * the 6-hourly alerts tick: no scan, only the personal alert pass
    Anything else does nothing."""
    now = now or datetime.now(timezone.utc)
    if scope == "workday":
        # Targeted Workday recovery: never a full scan, never the personal pass.
        if event != "workflow_dispatch":
            return {"scan": False, "deliver": False}
        if mode == "plan":   # the scheduler's dispatch: only while a data-centre probe is really due
            at = recovery_due_at(companies, state, now)
            return {"scan": at is not None and at <= now, "deliver": False}
        return {"scan": True, "deliver": False}   # an operator's explicit recovery run
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
        # the data checkout (JT_DATA_ROOT; see data_location.py — not imported:
        # the plan job checks out this one file of code)
        root = Path(os.environ.get("JT_DATA_ROOT", "").strip() or BASE)
        return json.loads((root / "data" / name).read_text("utf-8"))
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
        state = _load("workday_scan_state.json", None)
        hours, last = interval_from(settings), last_scan(companies)
        wait, scope = chain_next(now, hours, last, companies, state)
        line = (f"interval {hours} h · last scan {last} · due={scan_due(now, hours, last)} · "
                f"Workday recovery due {recovery_due_at(companies, state, now)} · "
                f"dispatch a plan run (scope {scope}) at {(now + wait).isoformat(timespec='minutes')}")
    except Exception as e:
        wait, scope = CHAIN_RETRY, "all"
        line = f"wait failed ({type(e).__name__}); retrying in {CHAIN_RETRY}"
    print(line, file=sys.stderr)
    _summary(line, "Scan scheduler")
    print(f"seconds={int(wait.total_seconds())}")
    print(f"at={(now + wait).isoformat(timespec='minutes')}")
    print(f"scope={scope}")
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
        scope = os.environ.get("SCOPE", "") or "all"
        state = _load("workday_scan_state.json", None)
        decision = plan(event, schedule, settings, companies, mode=mode, scope=scope, state=state)
        hours, last = interval_from(settings), last_scan(companies)
        now = datetime.now(timezone.utc)
        trigger = (f"schedule {schedule}" if event == "schedule"
                   else f"{event} mode={mode} scope={scope}" if event == "workflow_dispatch" else event or "unknown")
        if scope == "workday":
            trigger += f" · Workday recovery due at {recovery_due_at(companies, state, now)}"
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
