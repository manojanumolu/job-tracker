"""Phase 2 functional audit: real app flows (AppTest) against edge-case data."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from test_streamlit_app import _html, _key, _nav, _seen, app  # noqa: F401  (fixture re-export)

NOW = datetime.now(timezone.utc)
GATE = {k: True for k in ("real_job_posting", "india_location", "detail_read", "detail_is_this_job",
                          "no_conflicting_experience", "not_programme_story_talent_recruiter",
                          "evidence_not_staff_context", "fresher_or_entry_evidence")}


def _rec(i, **extra):
    return {"title": f"Graduate Trainee {i}", "url": f"https://sanofi.wd3.myworkdayjobs.com/S/job/P/GT-{i}_R{1000000 + i}",
            "company": "Sanofi", "location": "Pune · India", "category": "FRESHER",
            "reason": "experience starts at 0: '0-1 years'", "date": (NOW - timedelta(minutes=i)).strftime("%b %d, %H:%M"),
            "id": f"Sanofi_GT_{i}", "notified": False, "evidence": {"checks": dict(GATE)}, **extra}


# ── malformed / partial / legacy data never crashes any page ────────────────

MALFORMED = [
    {"title": None, "url": None, "company": None},                       # all None
    {"title": "", "url": "", "company": ""},                             # empty strings
    {"title": "No URL job", "company": "Sanofi", "location": None, "category": "FRESHER"},
    {"title": "Bad date", "url": "https://x.example/j/1", "company": "Sanofi", "date": "not a date"},
    {"title": "Bad evidence", "url": "https://x.example/j/2", "company": "Sanofi", "category": "FRESHER",
     "evidence": "oops", "reason": 42},
    {"title": "Bad evidence 2", "url": "https://x.example/j/3", "company": "Sanofi", "category": "ENTRY_LEVEL",
     "evidence": {"checks": "yes", "experience_lines": None, "experience": None}},
    {"title": "javascript url", "url": "javascript:alert(1)", "company": "<b>Evil</b>"},
    {"title": "Future date", "url": "https://x.example/j/4", "company": "PwC", "date": "Dec 31, 23:59",
     "notified_at": "garbage", "first_seen": 12345},
    "not a dict", 7, None, ["list"],
]


@pytest.mark.parametrize("page", ["home", "jobs", "companies", "monitoring", "email", "settings"])
def test_malformed_records_never_crash(app, page):
    at = app(MALFORMED, page=page if page != "home" else None)
    assert not at.exception, at.exception


def test_detail_pages_of_malformed_records_render(app):
    at = app(MALFORMED)
    _nav(at, "jobs")
    for b in [b for b in at.button if b.key and b.key.startswith("crit_")]:
        at.button(key=b.key).click().run()
        assert not at.exception, (b.key, at.exception)
        assert "javascript:alert" not in _html(at)          # unsafe links never rendered
        at.button(key="btn_back").click().run()


def test_malformed_companies_never_crash(app):
    at = app([_rec(1)])
    (at.tmp_path / "companies.json").write_text(json.dumps([
        {"id": None, "name": None}, {"id": "x"}, {"name": "No id", "url": "https://n.example", "status": "weird",
                                                  "last_checked": "yesterday", "scan": "oops", "status_reason": None},
        "junk", {"id": "y", "name": "Y", "url": "ftp://bad", "status": "broken", "last_checked": NOW.isoformat()}]))
    for page in ("home", "companies", "monitoring", "settings"):
        at = _nav(at, page) if page != "home" else at
        at.run()
        assert not at.exception, (page, at.exception)


@pytest.mark.parametrize("content", ["", "{not json", "null", "{}", "[]"])
def test_corrupt_or_empty_data_files_cold_start(app, content):
    at = app([])
    for name in ("seen_jobs.json", "companies.json", "settings.json"):
        (at.tmp_path / name).write_text(content)
    at.run()
    assert not at.exception
    for page in ("jobs", "companies", "monitoring", "email", "settings"):
        _nav(at, page)


def test_missing_data_files_cold_start(app):
    at = app([])
    for name in ("seen_jobs.json", "companies.json", "settings.json"):
        (at.tmp_path / name).unlink()
    at.run()
    assert not at.exception and "No alerts yet" in _html(at)


# ── query-parameter navigation ───────────────────────────────────────────────

@pytest.mark.parametrize("query, heading", [
    ({"page": "nonsense"}, "Discover jobs"),
    ({"page": "jobs", "job": "doesnotexist"}, "Jobs"),
    ({"page": "companies", "company": "nope"}, "Companies"),
    ({"page": "companies", "view": "delete"}, "Companies"),
    ({"page": "home", "job": "x", "company": "y"}, "Discover jobs"),
    ({"theme": "neon"}, "Discover jobs"),
])
def test_bad_query_params_fall_back_safely(app, query, heading):
    at = app([_rec(1)], query=query)
    assert not at.exception
    assert heading in _html(at)


def test_stale_job_link_is_removed_from_the_url(app):
    at = app([_rec(1)], query={"page": "jobs", "job": "doesnotexist"})
    assert "job" not in at.query_params


# ── pagination / filters / dismiss ──────────────────────────────────────────

def test_dismissing_the_last_job_on_the_last_page_keeps_pagination_valid(app):
    jobs = [_rec(i) for i in range(13)]            # 12 per page -> 2 pages
    at = app(jobs)
    _nav(at, "jobs")
    at.button(key="alerts_next").click().run()
    assert "13–13 of 13" in _html(at)
    last = [b for b in at.button if b.key and b.key.startswith("dismiss_")][0]
    at.button(key=last.key).click().run()
    assert not at.exception
    assert "1–12 of 12" in _html(at)


def test_filters_reset_page_and_clear(app):
    jobs = [_rec(i) for i in range(13)] + [_rec(99, company="PwC", title="PwC Graduate",
                                               url="https://pwc.wd3.myworkdayjobs.com/G/job/K/PwC-Grad_123456WD")]
    at = app(jobs)
    _nav(at, "jobs")
    at.button(key="alerts_next").click().run()
    at.selectbox(key="j_co").set_value("PwC").run()
    html = _html(at)
    assert "1–1 of 1" in html and "PwC Graduate" in html     # back on page 1, not an empty page 2
    at.button(key="j_clear").click().run()
    assert "1–12 of 14" in _html(at)


def test_search_matches_title_company_and_location(app):
    at = app([_rec(1), _rec(2, location="Chennai · India")])
    _nav(at, "jobs")
    at.text_input(key="j_q").set_value("chennai").run()
    assert "1–1 of 1" in _html(at) and "Graduate Trainee 2" in _html(at)


def test_legacy_category_filter(app):
    legacy = {"title": "Old keyword match", "url": "https://x.example/old", "company": "Sanofi", "date": "Jul 15, 13:45",
              "id": "Sanofi_Old", "notified": True}
    at = app([_rec(1), legacy])
    _nav(at, "jobs")
    at.selectbox(key="j_cat").set_value("legacy").run()
    html = _html(at)
    assert "Old keyword match" in html and "Graduate Trainee 1" not in html


def test_dismiss_restore_round_trip_keeps_notification_state(app):
    from config_store import alert_pending
    sent = _rec(1, notified=True, notify_state="sent", notified_at=NOW.isoformat())
    at = app([sent, _rec(2)])
    at.button(key=_key("dismiss", sent)).click().run()
    _nav(at, "jobs")
    at.pills(key="jobs_tab").set_value("dismissed").run()
    at.button(key=_key("restore", sent)).click().run()
    recs = {r["title"]: r for r in _seen(at)}
    assert recs["Graduate Trainee 1"]["notified"] is True and recs["Graduate Trainee 1"]["notify_state"] == "sent"
    assert not recs["Graduate Trainee 1"].get("dismissed")
    assert alert_pending(recs["Graduate Trainee 2"])          # the untouched job still waits for its email


# ── email page ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("value", ["", "not-an-email", "a@b", "  ", "x@@y.com"])
def test_invalid_recipient_is_not_saved(app, value):
    at = app([], page="email")
    at.text_input(key="email_input").set_value(value).run()
    at.button(key="btn_save_email").click().run()
    assert json.loads((at.tmp_path / "settings.json").read_text())["recipient_email"] == "me@example.com"


def test_recipient_is_saved_trimmed(app):
    at = app([], page="email")
    at.text_input(key="email_input").set_value("  new.person@example.com  ").run()
    at.button(key="btn_save_email").click().run()
    assert json.loads((at.tmp_path / "settings.json").read_text())["recipient_email"] == "new.person@example.com"
    assert "Sending to new.person@example.com" in _html(at)


def test_failed_test_email_reports_failure_without_changing_anything(app, monkeypatch):
    import notifier

    def boom(to):
        raise RuntimeError("GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be set in environment")
    monkeypatch.setattr(notifier, "test_mail", boom)
    at = app([], page="email")
    at.button(key="btn_test").click().run()
    assert not at.exception
    assert "Email isn't configured" in _html(at)
    assert json.loads((at.tmp_path / "settings.json").read_text())["recipient_email"] == "me@example.com"


def test_email_counts_only_real_pending_alerts(app):
    records = [_rec(1), _rec(2, dismissed=True), _rec(3, notified=True), _rec(4, notify_state="claimed"),
               _rec(5, category="UNKNOWN"), {**_rec(6), "evidence": {}}]
    at = app(records, page="email")
    html = _html(at)
    assert "1 — sent after the next check" in html


# ── companies ────────────────────────────────────────────────────────────────

def test_add_company_rejects_duplicates_and_bad_urls(app):
    at = app([], page="companies")
    at.button(key="btn_open_add").click().run()
    for name, url, msg in [("Sanofi", "https://another.example/jobs", "already tracked"),
                           ("Brand New", "https://sanofi.wd3.myworkdayjobs.com/SanofiCareers", "already tracked"),
                           ("Brand New", "http//broken", "starting with https://")]:
        at.text_input(key="new_name").set_value(name).run()
        at.text_input(key="new_url").set_value(url).run()
        at.button(key="btn_add").click().run()
        assert msg in _html(at), (name, url)
    companies = json.loads((at.tmp_path / "companies.json").read_text())
    assert len(companies) == 7


def test_stop_tracking_keeps_job_history(app):
    at = app([_rec(1)], query={"page": "companies", "company": "sanofi"})
    at.button(key="btn_remove_company").click().run()
    at.button(key="btn_remove").click().run()
    assert not at.exception
    assert not any(c["id"] == "sanofi" for c in json.loads((at.tmp_path / "companies.json").read_text()))
    assert len(_seen(at)) == 1                                   # history kept -> never re-emailed
    _nav(at, "jobs")
    at.button(key=_key("crit", _rec(1))).click().run()
    assert "This company is no longer tracked" in _html(at)
