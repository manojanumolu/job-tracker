"""Stability pass (Oct 2026): regressions for the defects it found.

  * the "Alert emails" save did two writes and two reads, then made the next
    click wait for a full re-read — now one read and one write, no read-back,
    with exactly the old semantics (verified email, no backlog, tailoring kept)
  * the personal alert pass stopped for EVERYONE on one malformed record or
    one person's unexpected error — now the others always continue
  * My alerts didn't say when the next email can go out — now it does, in IST
In-memory stores: nothing is emailed and production is never touched."""
import pytest

import user_alerts
import user_store
from config_store import job_key
from test_account_onboarding import JOBS, _ready
from test_final_polish import ustore  # noqa: F401  (fixture)
from test_firebase_auth import _buttons, admins, google_env, no_env  # noqa: F401  (fixtures)
from test_phase3_user_data import A_MAIL, A_UID, _sign_in
from test_streamlit_app import _html, app  # noqa: F401
from test_user_alerts import A, B, Identity, Mailer, _person, job
from user_store import InvalidInput, MemoryBackend, UserStore


@pytest.fixture
def store():
    return UserStore(MemoryBackend(), Identity())


# ── the alert settings save ─────────────────────────────────────────────────

def test_save_alert_settings_matches_the_two_old_calls(store):
    old, new = store.for_uid(A), store.for_uid(B)
    for u, mail in ((old, "a@example.org"), (new, "b@example.org")):
        u.ensure_profile(mail, "", True)
        u.set_email_tailoring(["Pune"], [])
    old.set_watch_all(False)
    old.set_notifications(True, "general")
    fields = new.save_alert_settings(True, "general", False)
    a, b = store.backend.get(f"users/{A}"), store.backend.get(f"users/{B}")
    assert b["watch_all"] is False and fields["watch_all"] is False
    assert {k: v for k, v in b["notifications"].items() if k != "enabled_at"} == \
           {k: v for k, v in a["notifications"].items() if k != "enabled_at"}
    assert b["notifications"]["tailoring"] == {"locations": ["Pune"], "job_families": []}     # kept
    assert fields["notifications"] == b["notifications"]                                       # what was written
    stamp = b["notifications"]["enabled_at"]
    new.save_alert_settings(True, "general", True)                                            # re-saved while on
    assert store.backend.get(f"users/{B}")["notifications"]["enabled_at"] == stamp           # no backlog reset
    new.save_alert_settings(False, "general", True)
    assert store.backend.get(f"users/{B}")["notifications"]["enabled_at"] is None


def test_save_alert_settings_keeps_the_safeguards(store):
    u = store.for_uid(A)
    u.ensure_profile("a@example.org", "", False)                                  # unverified
    with pytest.raises(InvalidInput):
        u.save_alert_settings(True, "general", True)
    with pytest.raises(InvalidInput):
        u.save_alert_settings(False, "bogus", True)
    assert "notifications" not in (store.backend.get(f"users/{A}") or {}) or \
        store.backend.get(f"users/{A}")["notifications"].get("enabled") is not True


def test_the_alerts_form_is_one_read_one_write_and_no_read_back(app, google_env, ustore):
    _ready(ustore, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS, page="alerts")
    at.toggle(key="my_alerts_on").set_value(True)
    at.radio(key="my_alerts_scope").set_value("all")
    ustore.backend.reads, ustore.backend.writes = 0, []
    at.button(key="btn_my_alerts").click().run()
    assert not at.exception
    assert [p for p, _ in ustore.backend.writes] == [f"users/{A_UID}"] and ustore.backend.reads == 1
    prof = ustore.backend.get(f"users/{A_UID}")
    assert prof["notifications"]["enabled"] is True and prof["watch_all"] is True
    assert at.session_state["_pcache"]["profile"]["notifications"] == prof["notifications"]
    assert "Alert settings saved" in _html(at)
    ustore.backend.reads = 0
    at.button(key="nav_jobs").click().run()
    assert ustore.backend.reads == 0                                                 # the next click doesn't wait


# ── the personal pass never stops for everyone ──────────────────────────────

def test_one_persons_unexpected_error_never_stops_the_others(store, monkeypatch):
    _person(store, A, "alice@example.org")
    _person(store, B, "bob@example.org")
    real = user_alerts.select_jobs

    def flaky(seen, view, dismissed, since):
        if view["notifications"].get("enabled") and dismissed == {"boom"}:
            raise KeyError("unexpected")
        return real(seen, view, dismissed, since)
    store.for_uid(A).dismiss({"boom"})
    monkeypatch.setattr(user_alerts, "select_jobs", flaky)
    mail = Mailer()
    stats = user_alerts.run(store, [job(1)], mail, run_id="r1")
    assert stats.get("error") == 1 and stats.get("sent") == 1
    assert mail.sent == [("bob@example.org", [job_key(job(1))])]


def test_a_malformed_record_is_skipped_not_fatal(store):
    _person(store, A, "alice@example.org")
    mail = Mailer()
    stats = user_alerts.run(store, ["junk", None, 42, job(1)], mail, run_id="r1")
    assert stats.get("sent") == 1 and mail.sent == [("alice@example.org", [job_key(job(1))])]


# ── when the next email can go out ──────────────────────────────────────────

def test_my_alerts_says_when_the_next_email_can_go_out(app, google_env, ustore):
    u = _ready(ustore, A_UID, A_MAIL)
    u.set_notifications(True, "general")
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS, page="alerts")
    html = _html(at)
    assert "At most one email every 6 hours" in html and "after the next scan that finds new jobs" in html
    import time
    u.record_alert_sent(time.time() - 3600)                       # emailed an hour ago
    at.session_state["_pcache"] = {**at.session_state["_pcache"], "at": 0}
    user_store_snapshot = ustore.for_uid(A_UID).snapshot()
    at.session_state["_pcache"] = {"uid": A_UID, "at": time.time(), **user_store_snapshot}
    at.button(key="nav_alerts").click().run()
    html = _html(at)
    assert "your next email can go out after <b class=\"num\">" in html and " IST</b>" in html


# ── an overdue scan is described truthfully (no countdown) ──────────────────

@pytest.mark.parametrize("hours_ago, overdue", [(0.2, False), (5, True)])
def test_an_overdue_scan_says_so_without_promising_a_time(app, hours_ago, overdue):
    from datetime import datetime, timedelta, timezone
    from test_ui_redesign import _set_companies
    now = datetime.now(timezone.utc)
    at = app([])
    # the last scan started right after the latest 3-hour boundary, or two windows ago
    start = now - timedelta(hours=hours_ago) if not overdue else now - timedelta(hours=hours_ago)
    if not overdue:
        boundary = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=now.hour % 3)
        start = boundary + (now - boundary) / 2
    _set_companies(at, [{"id": "a", "name": "Alpha", "url": "https://a.example/jobs", "status": "active",
                         "last_checked": start.isoformat()}])
    at.run()
    at.button(key="nav_monitoring").click().run()
    html = _html(at)
    assert ("Overdue — GitHub may start it later" in html) is overdue
    assert "GitHub may start it later" in html and "due now" not in html.lower()


# ── a job's page lists each experience fact once ────────────────────────────

def test_job_detail_lists_each_experience_line_once(app):
    import hashlib
    from test_streamlit_app import NEW_RECORD
    rec = {**NEW_RECORD, "evidence": {**(NEW_RECORD.get("evidence") or {}),
                                      "experience": ["0 to 1 years", "0 to 1 years", "0-2 years"],
                                      "experience_lines": ["Years of Experience: 0 to 1 years",
                                                           "Years of Experience :0 to 1 years",
                                                           "Qualifications: Any Graduation"]}}
    jid = hashlib.sha1(job_key(rec).encode("utf-8")).hexdigest()[:12]
    at = app([rec], query={"page": "jobs", "job": jid})
    html = _html(at)
    assert not at.exception and "<dt>Experience</dt><dd>0 to 1 years, 0-2 years</dd>" in html
    assert html.count('<div class="quote">Years of Experience') == 1
