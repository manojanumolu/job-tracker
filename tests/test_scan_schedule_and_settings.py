"""The admin's global scan interval, the members' fixed 6-hour alert cadence,
the admin/member split of Settings, and "a new page opens at its top".

The rules under test:
  * only admins see and change the global scan interval: exactly 1, 3, 6 or
    12 hours, default 3; the server re-checks the role and the value, and an
    invalid stored value falls back to 3 (never stops or floods scanning)
  * the REAL scheduler decision (scan_schedule.plan, run by check_jobs.yml's
    plan job) follows the saved interval: one shared scan per boundary, no
    drift when GitHub starts ticks late, never an extra scan in between
  * personal alert emails go out at most every 6 hours, whatever the scan
    interval; jobs found meanwhile wait (unclaimed) for the next email; no
    empty emails; a failed send is retried at the next pass
  * members never see internal IDs, scanner configuration or diagnostics
  * a navigation run carries the scroll-to-top script; same-page actions
    (filters, pagination, dismissing, the account menu) don't
In-memory stores and a fake GitHub repository: nothing touches production,
and no email is sent."""
import json
import os
import random
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

import config_store
import scan_schedule
import user_alerts
from config_store import job_key
from test_account_onboarding import JOBS, _ready
from test_config_store import FakeRepo
from test_final_polish import WriteCountingBackend, ustore  # noqa: F401  (fixture)
from test_firebase_auth import _buttons, admins, google_env, no_env  # noqa: F401  (fixtures)
from test_phase3_user_data import A_MAIL, A_UID, B_MAIL, B_UID, _sign_in  # noqa: F401
from test_streamlit_app import NEW_RECORD, OLD_RECORD, REPO, _html, _nav, app  # noqa: F401
from test_user_alerts import A, Mailer, _person, job
from user_store import MemoryBackend, UserStore
from test_user_alerts import Identity

WORKFLOW = (REPO / ".github" / "workflows" / "check_jobs.yml").read_text("utf-8").replace("\r\n", "\n")


def u(day, h, m=0):
    return datetime(2030, 1, day, h, m, tzinfo=timezone.utc)


# ── E / F / H: the interval's values ────────────────────────────────────────

def test_exactly_four_intervals_and_the_default_is_three_hours():
    assert scan_schedule.ALLOWED_INTERVALS == (1, 3, 6, 12)
    assert scan_schedule.DEFAULT_INTERVAL == 3
    assert scan_schedule.interval_from({}) == 3 and scan_schedule.interval_from(None) == 3
    for h in (1, 3, 6, 12):
        assert scan_schedule.validate_interval(h) == h
        assert scan_schedule.interval_from({"scan_interval_hours": h}) == h


@pytest.mark.parametrize("bad", [0, 2, 4, 24, -3, 3.0, "3", "1", None, True, [3], {"h": 3}])
def test_invalid_intervals_are_rejected_and_stored_ones_fall_back_to_three(bad):
    with pytest.raises(scan_schedule.InvalidInterval):
        scan_schedule.validate_interval(bad)
    with pytest.raises(scan_schedule.InvalidInterval):
        scan_schedule.with_interval({}, bad)
    assert scan_schedule.interval_from({"scan_interval_hours": bad}) == 3     # a hand-edited file fails safe


def test_saving_the_interval_keeps_every_other_setting():
    before = {"recipient_email": "me@example.com", "other": [1, 2]}
    after = scan_schedule.with_interval(before, 6)
    assert after == {**before, "scan_interval_hours": 6} and before == {"recipient_email": "me@example.com", "other": [1, 2]}


# ── G / K: the real scheduler decision ──────────────────────────────────────

def _simulate(hours: int, days: int = 2, late=lambda: 0, scan_minutes: int = 12) -> list[datetime]:
    """Run scan_schedule.plan on every hourly tick (GitHub may start it
    ``late()`` minutes late); a scan stamps companies.json when it ends."""
    settings = {"scan_interval_hours": hours}
    companies = [{"id": "a", "last_checked": u(1, 0, 5).isoformat()}]
    scans = []
    for tick in range(24 * days):
        now = u(1, 1) + timedelta(hours=tick, minutes=late())
        if scan_schedule.plan("schedule", scan_schedule.PLAN_CRON, settings, companies, now)["scan"]:
            scans.append(now)
            # every company is stamped while the scan runs (the first about a minute in)
            companies = [{"id": "a", "last_checked": (now + timedelta(minutes=1)).isoformat()},
                         {"id": "b", "last_checked": (now + timedelta(minutes=scan_minutes)).isoformat()}]
    return scans


@pytest.mark.parametrize("hours, per_day", [(1, 24), (3, 8), (6, 4), (12, 2)])
def test_the_scheduler_scans_once_per_interval(hours, per_day):
    scans = _simulate(hours, days=2)
    assert len(scans) == 2 * per_day - (1 if hours == 1 else 0) or len(scans) == 2 * per_day   # the very first tick
    # never two scans inside one interval window
    windows = [scan_schedule.latest_boundary(t, hours) for t in scans]
    assert len(windows) == len(set(windows))


@pytest.mark.parametrize("hours", [1, 3, 6, 12])
def test_late_github_ticks_do_not_drift_or_double_scan(hours):
    rnd = random.Random(hours)
    scans = _simulate(hours, days=3, late=lambda: rnd.randint(0, 55))
    windows = [scan_schedule.latest_boundary(t, hours) for t in scans]
    assert len(windows) == len(set(windows))                       # at most one scan per window
    assert len(scans) >= 3 * 24 // hours - 1                       # and none is skipped (no drift)


def test_changing_the_interval_changes_the_next_decision_not_the_scanner_state():
    companies = [{"id": "a", "last_checked": u(2, 9, 10).isoformat()}]
    now = u(2, 10, 0)
    assert scan_schedule.plan("schedule", scan_schedule.PLAN_CRON, {"scan_interval_hours": 3}, companies, now)["scan"] is False
    assert scan_schedule.plan("schedule", scan_schedule.PLAN_CRON, {"scan_interval_hours": 1}, companies, now)["scan"] is True
    assert companies == [{"id": "a", "last_checked": u(2, 9, 10).isoformat()}]           # nothing reset


def test_run_check_always_scans_and_the_alert_tick_never_does():
    any_companies = [{"id": "a", "last_checked": u(2, 9, 59).isoformat()}]
    for hours in (1, 3, 6, 12):
        s = {"scan_interval_hours": hours}
        assert scan_schedule.plan("workflow_dispatch", "", s, any_companies, u(2, 10)) == {"scan": True, "deliver": False}
        assert scan_schedule.plan("schedule", scan_schedule.ALERTS_CRON, s, [], u(2, 10)) == {"scan": False, "deliver": True}
    assert scan_schedule.plan("push", "", {}, [], u(2, 10)) == {"scan": False, "deliver": False}


def _checkout(tmp_path, settings_text, companies_text=None):
    """What the plan / wait jobs check out: src/scan_schedule.py and data/."""
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "src" / "scan_schedule.py").write_text((REPO / "src" / "scan_schedule.py").read_text("utf-8"), "utf-8")
    (tmp_path / "data" / "settings.json").write_text(settings_text)
    if companies_text is not None:
        (tmp_path / "data" / "companies.json").write_text(companies_text)


@pytest.mark.parametrize("interval, last, expect", [(3, "2030-01-02T09:10:00+00:00", "false"),
                                                     (1, "2030-01-02T09:10:00+00:00", "true"),
                                                     (12, "2030-01-01T23:00:00+00:00", "true")])
def test_the_workflow_plan_step_reads_the_saved_interval(tmp_path, interval, last, expect):
    """The exact command the plan job runs, on files like the repository's."""
    _checkout(tmp_path, json.dumps({"recipient_email": "x", "scan_interval_hours": interval}),
              json.dumps([{"id": "a", "last_checked": last}]))
    code = ("import sys; sys.path.insert(0, 'src')\n"
            "import scan_schedule, datetime as d\n"
            "scan_schedule.datetime = type('D', (d.datetime,), {'now': classmethod(lambda c, tz=None: d.datetime(2030,1,2,10,5,tzinfo=d.timezone.utc))})\n"
            "raise SystemExit(scan_schedule.main(['plan']))")
    env = {**os.environ, "EVENT": "schedule", "SCHEDULE": scan_schedule.PLAN_CRON}
    r = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.split() == [f"scan={expect}", "deliver=false"]


def test_a_broken_schedule_file_plans_a_safe_default(tmp_path):
    _checkout(tmp_path, "{not json", "[]")
    env = {**os.environ, "EVENT": "schedule", "SCHEDULE": scan_schedule.PLAN_CRON}
    r = subprocess.run([sys.executable, "src/scan_schedule.py", "plan"], cwd=tmp_path, env=env, capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 0 and r.stdout.split() == ["scan=true", "deliver=false"]     # no scan yet: scan (default 3 h)
    assert "interval 3 h" in r.stderr


def test_the_workflow_wires_the_plan_to_one_shared_scanner():
    jobs = re.findall(r"^  ([a-z-]+):\n", WORKFLOW.split("\njobs:\n", 1)[1], re.M)
    assert jobs == ["plan", "scrape", "publish", "scheduler", "personal-alerts"]
    assert f'cron: "{scan_schedule.PLAN_CRON}"' in WORKFLOW and f'cron: "{scan_schedule.ALERTS_CRON}"' in WORKFLOW
    assert WORKFLOW.count("cron:") == 2 and "workflow_dispatch:" in WORKFLOW
    assert WORKFLOW.count("python src/scraper.py") == 1                          # one scanner, in one job
    assert WORKFLOW.count("python src/alerts.py") == 1 and "needs: [plan, scrape]" in WORKFLOW
    assert "needs.plan.outputs.scan == 'true'" in WORKFLOW and "needs.plan.outputs.deliver == 'true'" in WORKFLOW
    # one concurrency group for the whole workflow: scans and alert passes never overlap
    head = WORKFLOW.split("\njobs:\n", 1)[0]
    assert "concurrency:\n  group: check-jobs\n  cancel-in-progress: false" in head
    assert WORKFLOW.count("concurrency:") == 1
    plan = WORKFLOW.split("\n  plan:\n", 1)[1].split("\n  scrape:\n", 1)[0]
    assert "secrets." not in plan and "contents: read" in plan and "python3 src/scan_schedule.py plan" in plan
    assert "pip install" not in plan and "timeout-minutes: 5" in plan                      # the hourly tick stays tiny


# ── I / J / L / M: the 6-hour personal cadence ──────────────────────────────

@pytest.fixture
def store():
    return UserStore(MemoryBackend(), Identity())


def test_member_alerts_are_fixed_at_six_hours_and_independent_of_the_scan_interval():
    assert scan_schedule.MEMBER_ALERT_HOURS == 6
    src = (REPO / "src" / "user_alerts.py").read_text("utf-8")
    assert "interval_from" not in src and "scan_interval_hours" not in src     # the scan interval never enters it


@pytest.mark.parametrize("scan_hours", [1, 3, 6, 12])
def test_a_person_is_emailed_at_most_every_six_hours_whatever_the_scan_interval(store, scan_hours):
    """A day in which a new job appears at every scan, with the personal
    pass after each scan and every 6 hours."""
    _person(store, A, "alice@example.org")
    mail, seen, n = Mailer(), [], 0
    passes = []
    for hour in range(24):
        now = u(2, 0) + timedelta(hours=hour, minutes=15)
        if hour % scan_hours == 0:                       # a scan found a new job, then the personal step ran
            n += 1
            seen.append(job(n, first_seen=(now - timedelta(minutes=10)).isoformat()))
            passes.append(now)
        elif hour % 6 == 0:                              # the 6-hourly personal pass
            passes.append(now)
        else:
            continue
        user_alerts.run(store, list(seen), mail, run_id=f"r{hour}", now=now)
    assert 1 <= len(mail.sent) <= 4                       # at most one email per 6 hours
    user_alerts.run(store, list(seen), mail, run_id="last", now=u(3, 6))   # the next pass after the day
    every = [k for _, keys in mail.sent for k in keys]
    assert sorted(every) == sorted(job_key(j) for j in seen)   # every job, each exactly once, none lost


def test_jobs_found_inside_the_window_wait_unclaimed_and_go_out_together(store):
    _person(store, A, "alice@example.org")
    mail = Mailer()
    j1, j2, j3 = job(1), job(2), job(3)
    assert user_alerts.run(store, [j1], mail, run_id="r1", now=u(2, 1))["sent"] == 1
    stats = user_alerts.run(store, [j1, j2], mail, run_id="r2", now=u(2, 2))     # 1 h later: waits
    assert stats == {"waiting": 1, "waiting_jobs": 0} and len(mail.sent) == 1
    assert store.for_uid(A).delivery(job_key(j2)) is None                       # nothing claimed meanwhile
    user_alerts.run(store, [j1, j2, j3], mail, run_id="r3", now=u(2, 4))        # still inside 6 h
    assert len(mail.sent) == 1
    user_alerts.run(store, [j1, j2, j3], mail, run_id="r4", now=u(2, 7))        # 6 h on: one email, both jobs
    assert mail.sent[1] == ("alice@example.org", sorted([job_key(j2), job_key(j3)]))
    assert {store.for_uid(A).delivery(job_key(j))["state"] for j in (j1, j2, j3)} == {"sent"}


def test_the_window_allows_a_slightly_early_pass(store):
    _person(store, A, "alice@example.org")
    mail = Mailer()
    user_alerts.run(store, [job(1)], mail, run_id="r1", now=u(2, 0, 25))
    user_alerts.run(store, [job(1), job(2)], mail, run_id="r2", now=u(2, 6, 0))   # 5 h 35 min: within the grace
    assert len(mail.sent) == 2


def test_no_email_without_new_jobs_and_the_window_doesnt_move(store):
    _person(store, A, "alice@example.org")
    mail = Mailer()
    user_alerts.run(store, [job(1)], mail, run_id="r1", now=u(2, 1))
    stamp = store.backend.get(f"users/{A}")["alert_cadence"]["last_sent_at"]
    stats = user_alerts.run(store, [job(1)], mail, run_id="r2", now=u(2, 9))
    assert stats.get("nothing") == 1 and len(mail.sent) == 1                       # no empty email
    assert store.backend.get(f"users/{A}")["alert_cadence"]["last_sent_at"] == stamp


def test_a_failed_send_is_released_and_retried_at_the_next_pass(store):
    _person(store, A, "alice@example.org")
    flaky = Mailer(fail_for={"alice@example.org"})
    assert user_alerts.run(store, [job(1)], flaky, run_id="r1", now=u(2, 1)).get("failed") == 1
    assert store.for_uid(A).delivery(job_key(job(1)))["state"] == "pending"           # released
    assert "alert_cadence" not in store.backend.get(f"users/{A}")                   # no window started
    ok = Mailer()
    user_alerts.run(store, [job(1)], ok, run_id="r2", now=u(2, 1, 20))               # the very next pass
    assert ok.sent == [("alice@example.org", [job_key(job(1))])]


def test_the_cadence_stamp_is_never_written_by_the_app():
    src = (REPO / "streamlit_app.py").read_text("utf-8")
    assert "alert_cadence" not in src and "record_alert_sent" not in src


# ── C / D / E (app): the interval control is the admin's ─────────────────────

def _admin(app, google_env, ustore, admins, page="settings", **kw):
    admins(A_MAIL)
    _ready(ustore, A_UID, A_MAIL)
    return _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS, page=page, **kw)


def _member(app, google_env, ustore, page="settings"):
    _ready(ustore, B_UID, B_MAIL)
    return _sign_in(app, google_env, B_UID, B_MAIL, seen=JOBS, page=page)


def _pills(at, key):
    return next(b for b in at.button_group if b.key == key)


def test_members_never_get_the_interval_control_or_the_admin_section(app, google_env, ustore):
    at = _member(app, google_env, ustore)
    html = _html(at)
    assert "scan_interval_pick" not in {b.key for b in at.button_group} and "btn_save_interval" not in _buttons(at)
    for internal in ('<h2 class="section-title">Global job scan interval', '<h2 class="section-title">Administration',
                     '<h2 class="section-title">Tracker status', "Data &amp; sync", "Technical details",
                     "Firebase account", B_UID, "check_jobs", "scan_schedule", "service account", "Dismiss all"):
        assert internal not in html, internal
    # ... but their own settings are all there
    for mine in ("Profile avatar", "Your job search", "Appearance", "Signed in with"):
        assert mine in html, mine
    assert at.text_input(key="display_name_input") is not None
    assert {"btn_set_prefs", "btn_set_cos", "btn_set_alerts"} <= _buttons(at)


def test_a_member_cannot_change_the_interval_by_any_route(app, google_env, ustore, monkeypatch):
    """No control is drawn; a forged session value or a query parameter
    changes nothing, and nothing is written."""
    repo = FakeRepo({"data/settings.json": {"recipient_email": "me@example.com"}})
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(config_store, "_get_repo", lambda token: repo)
    _ready(ustore, B_UID, B_MAIL)
    at = _sign_in(app, google_env, B_UID, B_MAIL, seen=JOBS, page="settings",
                  query={"scan_interval_hours": "1", "scan_interval_pick": "1"})
    at.session_state["scan_interval_pick"] = 1
    at.run()
    assert not at.exception and repo.commits == []
    assert "scan_interval_hours" not in repo.data("data/settings.json")


def test_admins_choose_exactly_four_intervals_and_saving_writes_only_the_setting(app, google_env, ustore, admins,
                                                                                  monkeypatch):
    import github
    repo = FakeRepo({"data/settings.json": {"recipient_email": "me@example.com"},
                     "data/companies.json": json.loads((REPO / "data" / "companies.json").read_text("utf-8")),
                     "data/seen_jobs.json": list(JOBS)})
    dispatched = []

    class NoDispatch:                                # a save must never start a scan
        def __init__(self, *a, **k):
            dispatched.append("Github()")
    monkeypatch.setattr(github, "Github", NoDispatch)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(config_store, "_get_repo", lambda token: repo)
    at = _admin(app, google_env, ustore, admins)
    pick = _pills(at, "scan_interval_pick")
    assert list(pick.options) == ["Every hour", "Every 3 hours", "Every 6 hours", "Every 12 hours"]
    assert pick.value == 3 and "<dt>Current interval</dt><dd><b>Every 3 hours</b></dd>" in _html(at)
    companies_before, seen_before = repo.files["data/companies.json"], repo.files["data/seen_jobs.json"]
    for hours in (6, 12, 1, 3):
        pick = _pills(at, "scan_interval_pick")
        pick.set_value(hours).run()
        at.button(key="btn_save_interval").click().run()
        assert not at.exception
        assert repo.data("data/settings.json") == {"recipient_email": "me@example.com", "scan_interval_hours": hours}
        assert f"<dt>Current interval</dt><dd><b>{'Every hour' if hours == 1 else f'Every {hours} hours'}</b></dd>" in _html(at)
        assert "Scan interval saved" in _html(at) and "no scan was started" in _html(at)
    assert all(m.startswith("chore: set the scan interval to") and m.endswith("[skip ci]") for m in repo.commits)
    assert len(repo.commits) == 4 and dispatched == []                                # no scan started
    assert repo.files["data/companies.json"] == companies_before and repo.files["data/seen_jobs.json"] == seen_before


def test_a_demoted_admin_cannot_save_with_a_page_drawn_while_admin(app, google_env, ustore, admins, monkeypatch):
    """The role is checked when the button is pressed, not when it was drawn."""
    repo = FakeRepo({"data/settings.json": {"recipient_email": "me@example.com"}})
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(config_store, "_get_repo", lambda token: repo)
    at = _admin(app, google_env, ustore, admins)
    _pills(at, "scan_interval_pick").set_value(1).run()
    monkeypatch.setenv("JT_ADMIN_EMAILS", "someone.else@example.org")
    at.button(key="btn_save_interval").click().run()
    assert repo.commits == [] and "scan_interval_hours" not in repo.data("data/settings.json")
    assert "Only admins can change the scan interval" in _html(at)


def test_the_interval_drives_the_displayed_schedule(app, google_env, ustore, admins, monkeypatch):
    repo = FakeRepo({"data/settings.json": {"recipient_email": "me@example.com", "scan_interval_hours": 12},
                     "data/companies.json": json.loads((REPO / "data" / "companies.json").read_text("utf-8")),
                     "data/seen_jobs.json": list(JOBS)})
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(config_store, "_get_repo", lambda token: repo)
    at = _admin(app, google_env, ustore, admins)
    html = _html(at)
    assert "<dt>Current interval</dt><dd><b>Every 12 hours</b></dd>" in html and re.search(r"Every 12 hours · (Overdue — )?GitHub may start it later", html)
    _nav(at, "monitoring")
    assert "checked every 12 hours" in _html(at)


# ── P: no internal details for members anywhere ─────────────────────────────

def test_members_see_no_internal_identifiers_or_diagnostics_on_any_page(app, google_env, ustore):
    at = _member(app, google_env, ustore, page=None)
    for page in ("home", "jobs", "companies", "alerts", "preferences", "settings"):
        if f"nav_{page}" in _buttons(at):
            _nav(at, page)
        else:
            at.button(key=f"acct_{page}").click().run()
        html = _html(at)
        for secret in (B_UID, "Firebase account", "service account", "FIREBASE_SERVICE_ACCOUNT", "GITHUB_TOKEN",
                       "scan_interval", "Technical details"):
            assert secret not in html, (page, secret)


def test_admins_see_the_link_state_but_no_uid(app, google_env, ustore, admins):
    at = _admin(app, google_env, ustore, admins)
    html = _html(at)
    assert A_UID not in html and "<dt>Firebase account</dt><dd>Linked</dd>" in html
    assert html.count('<h2 class="section-title">Tracker status</h2>') == 1           # one status section
    assert "btn_account_sign_out_settings" not in _buttons(at)                         # sign-out lives in the menu


# ── A / B: a new destination opens at its top ───────────────────────────────

SCROLL = 'data-nav="'


def _scrolls(at) -> bool:
    return SCROLL in _html(at)


def test_every_navigation_run_carries_the_scroll_to_top(app, google_env, ustore, admins):
    at = _admin(app, google_env, ustore, admins, page=None)
    for key in ("nav_jobs", "nav_companies", "nav_monitoring", "nav_email", "nav_settings", "nav_home",
                "acct_settings", "acct_preferences", "acct_companies", "acct_alerts", "nav_jobs"):
        at.button(key=key).click().run()
        assert not at.exception and _scrolls(at), key
    detail = next(k for k in _buttons(at) if k.startswith("crit_"))
    at.button(key=detail).click().run()                                  # a job's own page is a new destination too
    assert _scrolls(at)


def test_same_page_actions_keep_the_scroll_and_the_filters(app, google_env, ustore, admins):
    at = _admin(app, google_env, ustore, admins, page="jobs")
    at.text_input(key="j_q").set_value("Engineer").run()
    assert not _scrolls(at)                                              # a filter keeps the place
    at.button(key="nav_jobs").click().run()                              # same page again: not a new destination
    assert not _scrolls(at)
    at.button(key=next(k for k in _buttons(at) if k.startswith("dismiss_"))).click().run()
    assert not _scrolls(at)                                              # dismissing keeps the place
    _nav(at, "home")
    _nav(at, "jobs")
    assert at.text_input(key="j_q").value == "Engineer"                  # the search survives a visit elsewhere


def test_pagination_keeps_the_place(app, google_env, ustore, admins):
    many = [dict(NEW_RECORD, id=f"many_{i}", url=f"https://jobs.example/many/{i}", title=f"Graduate Engineer {i}")
            for i in range(30)]
    admins(A_MAIL)
    _ready(ustore, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=many, page="jobs")
    at.button(key="alerts_next").click().run()
    assert not at.exception and not _scrolls(at) and at.session_state["jobs_page"] == 1
