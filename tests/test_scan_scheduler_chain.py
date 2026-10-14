"""The scan scheduler chain (scan_scheduler.yml): GitHub Actions only.

GitHub's cron dropped or delayed most ticks (no Check Jobs run 06:55-15:33
UTC on 2026-10-12; Movie Ticket Radar's */5 cron landed 3-6 h apart). The
chain waits for each window itself and dispatches a gated plan run, so the
cadence no longer depends on the cron queue - and never scans a window twice.
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import scan_schedule

REPO = Path(__file__).resolve().parent.parent
WF = REPO / ".github" / "workflows"
CHAIN = (WF / "scan_scheduler.yml").read_text("utf-8")
CHECK = (WF / "check_jobs.yml").read_text("utf-8")


def u(d, h, m=0):
    return datetime(2026, 10, d, h, m, tzinfo=timezone.utc)


# ── how long a scheduler run waits ──────────────────────────────────────────

def test_after_a_scan_it_waits_for_the_next_window_plus_two_minutes():
    assert scan_schedule.chain_wait(u(12, 9, 10), 3, u(12, 9, 3)) == timedelta(hours=2, minutes=52)   # -> 12:02


def test_while_a_scan_is_due_it_looks_again_in_half_an_hour():
    # the scan it just asked for is still running (not published yet), or it failed
    assert scan_schedule.chain_wait(u(12, 12, 2), 3, u(12, 9, 3)) == timedelta(minutes=30)
    assert scan_schedule.chain_wait(u(12, 12, 2), 3, None) == timedelta(minutes=30)


def test_a_long_interval_is_waited_in_pieces_under_githubs_six_hour_job_limit():
    assert scan_schedule.chain_wait(u(12, 0, 10), 12, u(12, 0, 3)) == timedelta(hours=5)
    assert scan_schedule.CHAIN_MAX_WAIT < timedelta(hours=6)


def test_boundaries_are_utc_whatever_zone_now_is_in():
    ist = timezone(timedelta(hours=5, minutes=30))
    now = u(12, 9, 10).astimezone(ist)                        # 14:40 IST
    assert scan_schedule.chain_wait(now, 3, u(12, 9, 3)) == timedelta(hours=2, minutes=52)


def _scans(hours, cron_ticks=(), scan_ok=lambda at: True):
    """A day of the chain's plan runs merged with any GitHub ticks, in time
    order, each through scan_schedule.plan. A successful scan publishes its
    start time; a failed one publishes nothing (as in check_jobs.yml). The
    concurrency group runs them one after another, so each plan sees what
    the one before it published."""
    settings = {"scan_interval_hours": hours}
    published = [{"id": "a", "last_checked": u(12, 0, 1).isoformat()}]   # scanned in the 00:00 window
    scans, t = [], u(12, 0, 10)
    chain_next = t + scan_schedule.chain_wait(t, hours, scan_schedule.last_scan(published))
    ticks = sorted(cron_ticks)
    while True:
        nxt = min([chain_next] + ticks[:1])
        if nxt >= u(13, 0):
            return scans
        from_chain = nxt == chain_next
        if not from_chain:
            ticks.pop(0)
        event, sched, mode = (("workflow_dispatch", "", "plan") if from_chain
                              else ("schedule", scan_schedule.PLAN_CRON, "scan"))
        if scan_schedule.plan(event, sched, settings, published, nxt, mode=mode)["scan"]:
            scans.append(nxt)
            if scan_ok(nxt):
                published = [{"id": "a", "last_checked": (nxt + timedelta(seconds=30)).isoformat()}]
        if from_chain:                                     # the next scheduler run reads main a minute later
            after = nxt + timedelta(minutes=1)
            chain_next = after + scan_schedule.chain_wait(after, hours, scan_schedule.last_scan(published))


@pytest.mark.parametrize("hours, per_day", [(1, 23), (3, 7), (6, 3), (12, 1)])
def test_the_chain_scans_once_per_window_right_after_it_opens(hours, per_day):
    scans = _scans(hours)
    assert len(scans) == per_day                            # every window after the 00:00 one
    assert all(s.minute == 2 for s in scans)                # two minutes after the window opens
    windows = [scan_schedule.latest_boundary(s, hours) for s in scans]
    assert len(windows) == len(set(windows))


def test_github_ticks_arriving_too_never_add_a_scan():
    ticks = [u(12, h, m) for h in range(24) for m in (17, 47)]     # every tick GitHub could send
    assert _scans(3, cron_ticks=ticks) == _scans(3)


def test_a_failed_scan_is_retried_about_half_an_hour_later():
    scans = _scans(3, scan_ok=lambda at: at != u(12, 3, 2))
    in_window = [s for s in scans if scan_schedule.latest_boundary(s, 3) == u(12, 3)]
    assert in_window == [u(12, 3, 2), u(12, 3, 33)]


def test_the_wait_command_prints_outputs_and_never_fails(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "src" / "scan_schedule.py").write_text((REPO / "src" / "scan_schedule.py").read_text("utf-8"), "utf-8")
    (tmp_path / "data" / "settings.json").write_text("{}")
    summary = tmp_path / "summary.md"
    env = {**os.environ, "GITHUB_STEP_SUMMARY": str(summary)}

    def run(companies_text):
        (tmp_path / "data" / "companies.json").write_text(companies_text)
        r = subprocess.run([sys.executable, "src/scan_schedule.py", "wait"], cwd=tmp_path, env=env,
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        return dict(line.split("=", 1) for line in r.stdout.split())

    for broken in ("{broken", json.dumps([{"id": "a", "last_checked": None}])):
        out = run(broken)                                    # no usable scan time: due -> look again in 30 min
        assert int(out["seconds"]) == 1800 and re.match(r"\d{4}-\d\d-\d\dT\d\d:\d\d", out["at"])
    out = run(json.dumps([{"id": "a", "last_checked": datetime.now(timezone.utc).isoformat()}]))
    assert 0 < int(out["seconds"]) <= 3 * 3600 + 120
    assert "Scan scheduler:" in summary.read_text("utf-8")


# ── the workflows ──────────────────────────────────────────────────────────

def test_the_chain_is_dispatch_only_one_at_a_time_and_minimally_privileged():
    on = CHAIN.split("\non:\n", 1)[1].split("\nconcurrency:", 1)[0]
    assert "workflow_dispatch:" in on and "schedule:" not in on
    # push starts it only when the chain's own file changes (data commits never do)
    assert 'paths: [".github/workflows/scan_scheduler.yml"]' in on and "branches: [main]" in on
    assert "group: scan-scheduler" in CHAIN and "cancel-in-progress: false" in CHAIN
    head = CHAIN.split("\njobs:", 1)[0]
    assert "\npermissions: {}\n" in head                       # nothing by default ...
    perms = CHAIN.split("\n    permissions:\n", 1)[1].split("\n    steps:", 1)[0]
    assert re.findall(r"^      (\w+): (\w+)", perms, re.M) == [("contents", "read"), ("actions", "write")]
    assert CHAIN.count("permissions:") == 2 and "persist-credentials: false" in CHAIN
    assert "secrets." not in CHAIN                            # the run's own token, nothing else
    assert "python3 src/scan_schedule.py wait" in CHAIN and 'sleep "$WAIT_SECONDS"' in CHAIN


def test_the_chain_dispatches_a_gated_plan_then_hands_over():
    plan_at = CHAIN.index("gh workflow run check_jobs.yml")
    hand_at = CHAIN.index("gh workflow run scan_scheduler.yml")
    assert plan_at < hand_at and "-f mode=plan" in CHAIN[plan_at:hand_at]
    hand_step = CHAIN[CHAIN.rindex("- name:", 0, hand_at):]
    assert "if: ${{ !cancelled() }}" in hand_step                 # cancelling a run stops the chain
    timeout = int(re.search(r"timeout-minutes: (\d+)", CHAIN).group(1))
    assert scan_schedule.CHAIN_MAX_WAIT.total_seconds() / 60 + 10 < timeout < 360


def test_every_check_jobs_run_restarts_a_stopped_chain_and_cannot_fail():
    job = CHECK.split("\n  scheduler:\n", 1)[1].split("\n  personal-alerts:\n", 1)[0]
    assert "continue-on-error: true" in job and "needs: plan" in job
    perms = job.split("permissions:", 1)[1].split("steps:", 1)[0]
    assert re.findall(r"^      (\w+): (\w+)", perms, re.M) == [("actions", "write")]
    assert "--workflow scan_scheduler.yml" in job and "gh workflow run scan_scheduler.yml" in job
    assert "secrets." not in job and '"${active:-0}" = "0"' in job


def test_no_external_scheduler_anywhere():
    texts = [p.read_text("utf-8") for p in list(WF.glob("*.yml")) + list((REPO / "docs").glob("*.md"))]
    texts.append((REPO / "src" / "scan_schedule.py").read_text("utf-8"))
    for t in texts:
        assert "cron-job" not in t.lower() and "EXTERNAL_SCHEDULER" not in t
    assert not (REPO / "docs" / "EXTERNAL_SCHEDULER.md").exists()


def test_scan_cadence_and_personal_alerts_are_unchanged():
    assert scan_schedule.DEFAULT_INTERVAL == 3 and scan_schedule.MEMBER_ALERT_HOURS == 6
    assert f'cron: "{scan_schedule.ALERTS_CRON}"' in CHECK and scan_schedule.ALERTS_CRON == "20 */6 * * *"
    assert scan_schedule.plan("schedule", scan_schedule.ALERTS_CRON, {}, [], u(12, 6, 20)) == {"scan": False, "deliver": True}
