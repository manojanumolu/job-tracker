"""P0 (2026-10-12): a day-old "Last scan" / "Scans delayed" on the first
page after an idle spell, "6h ago" / "All portals healthy" after Refresh,
and scans every ~6 h instead of every 3 h.

Two causes, both reproduced here:
* the shared-data cache served its last copy however old it was (the
  server process outlives idle hours) — only Refresh read GitHub again;
* GitHub created only 5 of ~17 top-of-the-hour plan ticks, and the app
  called a scan missed that way "healthy" until it was 10 h old.
"""
import json
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import scan_schedule
import user_alerts  # noqa: F401  (the personal alert pass still imports cleanly)
from snapshot_cache import SnapshotCache
from test_streamlit_app import _html, app  # noqa: F401  (fixture re-export)

REPO = Path(__file__).resolve().parent.parent
WORKFLOW = (REPO / ".github/workflows/check_jobs.yml").read_text("utf-8")
IST = timezone(timedelta(hours=5, minutes=30))


def u(d, h, m=0):
    return datetime(2026, 10, d, h, m, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _cache(results, clock, spawned):
    calls = []

    def fetch():
        calls.append(clock.t)
        r = results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    return SnapshotCache(fetch, ttl_s=300, max_stale_s=900, retry_s=30, clock=clock, spawn=spawned.append), calls


# ── the shared-data cache: what the first page after an idle spell shows ────

def test_first_page_after_idle_hours_reads_the_current_scan_not_the_old_copy():
    """The incident: the copy was read when the 12:16 UTC scan was the latest;
    hours later a page load must show the 06:58 scan, without a Refresh."""
    clock, spawned = Clock(), []
    old = {"companies.json": [{"id": "a", "last_checked": "2026-10-11T12:16:25+00:00"}]}
    new = {"companies.json": [{"id": "a", "last_checked": "2026-10-12T06:58:12+00:00"}]}
    c, calls = _cache([old, new], clock, spawned)
    assert c.get() == old
    clock.t += 6 * 3600                               # nobody opened the app for 6 hours
    assert c.get() == new                             # this very page shows the new scan
    assert len(calls) == 2 and not c.stale()


def test_a_copy_younger_than_the_bound_is_still_served_at_once():
    clock, spawned = Clock(), []
    c, calls = _cache([{"a": 1}, {"a": 2}], clock, spawned)
    c.get()
    clock.t += 899
    assert c.get() == {"a": 1} and len(calls) == 1 and len(spawned) == 1   # background refresh only


def test_refresh_picks_up_a_newer_persisted_scan():
    clock, spawned = Clock(), []
    c, _ = _cache([{"companies.json": [1]}, {"companies.json": [2]}], clock, spawned)
    c.get()
    assert c.refresh_now() == {"companies.json": [2]}


def test_github_down_keeps_the_old_copy_marked_stale_and_never_fresh():
    """Refresh cannot turn old data into new evidence: a failed read keeps
    the old copy and says it is old; it is retried at most every 30 s."""
    clock, spawned = Clock(), []
    c, calls = _cache([{"a": 1}, RuntimeError("down"), {}, {"a": 3}], clock, spawned)
    c.get()
    clock.t += 1000
    assert c.get() == {"a": 1} and c.stale()          # tried, failed: old copy, flagged
    clock.t += 5
    assert c.get() == {"a": 1} and len(calls) == 2    # no second wait within retry_s
    assert c.refresh_now() == {"a": 1} and c.stale()  # an empty read is not evidence either
    assert c.refresh_now() == {"a": 3} and not c.stale()


def test_the_app_bounds_its_shared_data_cache():
    src = (REPO / "streamlit_app.py").read_text("utf-8")
    m = re.search(r"SnapshotCache\(lambda: fetch_remote_json\(_SHARED_FILES\), ttl_s=(\d+), max_stale_s=(\d+)\)", src)
    assert m and int(m.group(1)) == 300 and int(m.group(2)) <= 900


# ── the scheduler ──────────────────────────────────────────────────────────

def test_the_plan_tick_is_off_the_hour_and_twice_an_hour():
    assert scan_schedule.PLAN_CRON == "17,47 * * * *" and scan_schedule.PLAN_MINUTES == (17, 47)
    assert f'cron: "{scan_schedule.PLAN_CRON}"' in WORKFLOW and 'cron: "0 * * * *"' not in WORKFLOW


def test_personal_alert_cadence_is_unchanged():
    assert scan_schedule.ALERTS_CRON == "20 */6 * * *" and f'cron: "{scan_schedule.ALERTS_CRON}"' in WORKFLOW
    assert scan_schedule.MEMBER_ALERT_HOURS == 6
    assert scan_schedule.alert_due(u(12, 12), u(12, 6, 30)) is True                # 5 h 30 min: inside the grace
    assert scan_schedule.alert_due(u(12, 12), u(12, 6, 31)) is False
    assert scan_schedule.plan("schedule", scan_schedule.ALERTS_CRON, {}, [], u(12, 6, 20)) == {"scan": False, "deliver": True}


@pytest.mark.parametrize("minute", [17, 47])
def test_both_plan_minutes_scan_when_due(minute):
    companies = [{"id": "a", "last_checked": u(12, 6, 58).isoformat()}]
    assert scan_schedule.plan("schedule", scan_schedule.PLAN_CRON, {}, companies, u(12, 9, minute))["scan"] is True
    assert scan_schedule.plan("schedule", scan_schedule.PLAN_CRON, {}, companies, u(12, 8, minute))["scan"] is False


def test_the_incident_scan_is_overdue_and_the_next_slot_is_the_next_tick():
    """Last scan started 06:58 UTC; the 09:00 and 12:00 windows passed with
    no tick from GitHub. At 13:30 UTC (7:00 PM IST) that is overdue."""
    last = u(12, 6, 58)
    now = u(12, 13, 30)
    assert scan_schedule.scan_due(now, 3, last) and scan_schedule.scan_overdue(now, 3, last)
    nxt = scan_schedule.next_scan(now, 3, last)
    assert nxt == u(12, 13, 47) and nxt.astimezone(IST).strftime("%H:%M") == "19:17"


def test_overdue_counts_from_the_first_missed_window_not_the_latest():
    last = u(12, 6, 58)                                # missed 09:00; now just past 12:00
    assert scan_schedule.scan_overdue(u(12, 12, 10), 3, last)
    assert not scan_schedule.scan_overdue(u(12, 10, 29), 3, last)        # 09:00 + 1 h 29 min
    assert scan_schedule.scan_overdue(u(12, 10, 30), 3, last)            # 09:00 + 1 h 30 min
    assert not scan_schedule.scan_overdue(u(12, 8, 59), 3, last)         # not even due yet
    assert scan_schedule.scan_overdue(u(12, 8, 59), 3, None)             # never scanned


def test_interval_boundaries_are_utc_whatever_zone_now_is_given_in():
    last = u(12, 6, 58)
    ist_now = u(12, 9, 0).astimezone(IST)              # 14:30 IST is the 09:00 UTC boundary
    assert scan_schedule.latest_boundary(ist_now, 3) == u(12, 9)
    assert scan_schedule.scan_due(ist_now, 3, last)
    assert not scan_schedule.scan_due(ist_now - timedelta(seconds=1), 3, last)
    assert not scan_schedule.scan_due(u(12, 9, 30), 3, u(12, 9, 0))     # a scan started at the boundary counts


def test_a_scan_that_overran_a_boundary_counts_for_the_window_it_started_in():
    companies = [{"id": "a", "last_checked": u(12, 8, 58).isoformat()},
                 {"id": "b", "last_checked": u(12, 9, 3).isoformat()}]
    assert scan_schedule.last_scan(companies) == u(12, 8, 58)
    assert scan_schedule.plan("schedule", scan_schedule.PLAN_CRON, {}, companies, u(12, 9, 17))["scan"] is True


def test_the_plan_step_never_records_a_scan():
    """A scheduler-only run reads files and prints a decision; it cannot write
    a scan time (read-only token) and plan() does not touch its inputs."""
    plan_job = WORKFLOW.split("\n  plan:\n", 1)[1].split("\n  check:\n", 1)[0]
    assert "contents: read" in plan_job and "contents: write" not in plan_job
    assert "scraper.py" not in plan_job and "alerts.py" not in plan_job
    companies = [{"id": "a", "last_checked": u(12, 6, 58).isoformat()}]
    before = json.dumps(companies)
    scan_schedule.plan("schedule", scan_schedule.PLAN_CRON, {}, companies, u(12, 13, 17))
    assert json.dumps(companies) == before


def test_a_failed_scrape_publishes_nothing():
    """scraper.py only stamps its local checkout; the results (and so every
    last_checked) reach GitHub through alerts.py, which runs only after a
    successful scrape. A failed or skipped scan leaves the last scan as it was."""
    check = WORKFLOW.split("\n  check:\n", 1)[1].split("\n  personal-alerts:\n", 1)[0]
    publish = check.split("- name: Publish results and send email alerts", 1)[1]
    assert "if: ${{ !cancelled() && steps.scrape.outcome == 'success' }}" in publish.split("run:", 1)[0]
    assert "if: ${{ needs.plan.outputs.scan == 'true' }}" in check


# ── the dashboard ──────────────────────────────────────────────────────────

def _stamp(tmp_path, ago: timedelta, status="active"):
    now = datetime.now(timezone.utc)
    companies = json.loads((REPO / "companies.json").read_text("utf-8"))
    for i, c in enumerate(companies):
        c["status"] = status
        c["last_checked"] = (now - ago - timedelta(seconds=10 * i)).isoformat()
    (tmp_path / "companies.json").write_text(json.dumps(companies))
    return len(companies)


def _overall(html):
    return re.search(r'<span class="chip live (\w+)"><span class="dot \w+"></span>([^<]+)</span>', html).groups()


def test_a_six_hour_old_scan_is_overdue_not_healthy(app, tmp_path):
    n = _stamp(tmp_path, timedelta(hours=6))
    html = _html(app([]))
    assert _overall(html) == ("delayed", "Scan overdue")
    assert f"{n} healthy" in html                      # the portals themselves answered fine
    assert "Last scan 6h ago" in html


def test_a_day_old_scan_is_delayed(app, tmp_path):
    n = _stamp(tmp_path, timedelta(hours=25))
    html = _html(app([]))
    assert _overall(html) == ("delayed", "Scans delayed") and f"{n} delayed" in html
    assert "Last scan 1 day ago" in html


def test_a_recent_scan_is_healthy(app, tmp_path):
    n = _stamp(tmp_path, timedelta(minutes=20))
    html = _html(app([]))
    assert _overall(html) == ("healthy", "All portals healthy") and f"{n} healthy" in html


def test_portal_counts_follow_each_portals_own_result(app, tmp_path):
    now = datetime.now(timezone.utc)
    companies = json.loads((REPO / "companies.json").read_text("utf-8"))
    for c in companies:
        c["status"], c["last_checked"] = "active", (now - timedelta(minutes=20)).isoformat()
    companies[0]["status"] = "failing"
    (tmp_path / "companies.json").write_text(json.dumps(companies))
    html = _html(app([]))
    assert f"{len(companies) - 1} healthy · 1 failing" in html
    assert _overall(html)[0] == "failing"
