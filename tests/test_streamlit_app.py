"""Runs the real Streamlit app (AppTest) against a *copy* of the repo in a
temp directory, with no GitHub token, so production JSON is never touched."""
import hashlib
import json
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

st_testing = pytest.importorskip("streamlit.testing.v1")

REPO = Path(__file__).resolve().parent.parent
APP_FILES = ["streamlit_app.py", "config_store.py", "notifier.py", "job_classifier.py", "scraper.py",
             "sources.py", "identity.py", "locations.py", "repo_sync.py", "access.py", "firebase_auth.py",
             "user_store.py", "job_filters.py"]
OWNER_PASSWORD = "correct horse battery staple"

OLD_RECORD = {  # shape written before the classifier existed
    "title": "Junior Associate - Evidence Synthesis",
    "url": "https://sanofi.example/job/R1",
    "company": "Sanofi", "date": "Jul 15, 13:45",
    "id": "Sanofi_Junior_Associate_-_Evidence_Synthesis", "notified": True,
}
NEW_RECORD = {
    "title": "Graduate Software Engineer", "url": "https://pwc.example/job/2?src=a&x=1",
    "company": "PwC", "location": "Pune · India", "category": "ENTRY_LEVEL",
    "reason": "entry-level signal: 'Graduate'", "date": "Sep 28, 19:16",
    "id": "PwC_Graduate_Software_Engineer",
    "evidence": {"checks": {"detail_read": True, "fresher_or_entry_evidence": True}},
}
FRESHER_RECORD = {
    "title": "Trainee Analyst", "url": "https://metlife.example/job/9",
    "company": "MetLife", "location": "Hyderabad · India", "category": "FRESHER",
    "reason": "fresher signal: 'Freshers welcome'", "date": "Sep 27, 10:00",
    "id": "MetLife_Trainee_Analyst",
}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GMAIL_ADDRESS", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    monkeypatch.delenv("ALERT_RECIPIENT", raising=False)
    monkeypatch.delenv("FIREBASE_WEB_API_KEY", raising=False)
    monkeypatch.delenv("FIREBASE_PROJECT_ID", raising=False)
    monkeypatch.delenv("JT_ADMIN_EMAILS", raising=False)
    monkeypatch.setenv("JT_OWNER_PASSWORD", OWNER_PASSWORD)
    for name in APP_FILES:
        shutil.copy(REPO / name, tmp_path / name)
    shutil.copytree(REPO / "assets", tmp_path / "assets")
    shutil.copy(REPO / "companies.json", tmp_path / "companies.json")
    (tmp_path / "settings.json").write_text(json.dumps({"recipient_email": "me@example.com"}))

    import streamlit as st
    st.cache_data.clear()  # the app's GitHub snapshot cache is process-wide
    st.cache_resource.clear()  # ... and so are its sign-in / Run check limits

    def make(seen, page=None, query=None, owner=True, secrets=None):
        """owner=True starts the session signed in as the owner (the
        behaviour tests are about the dashboard, not the sign-in)."""
        (tmp_path / "seen_jobs.json").write_text(json.dumps(seen))
        at = st_testing.AppTest.from_file(str(tmp_path / "streamlit_app.py"), default_timeout=30)
        at.tmp_path = tmp_path
        if owner:
            at.session_state["_owner_until"] = time.time() + 3600
        for k, v in (secrets or {}).items():   # Streamlit secrets (st.secrets) for this run
            at.secrets[k] = v
        for k, v in (query or {}).items():
            at.query_params[k] = v
        at.run()
        if page:
            at.button(key=f"nav_{page}").click().run()
        return at
    return make


def _html(at) -> str:
    return "\n".join(e.proto.body for e in at.get("html"))


def _seen(at):
    return json.loads((at.tmp_path / "seen_jobs.json").read_text("utf-8"))


def _companies(at):
    return json.loads((at.tmp_path / "companies.json").read_text("utf-8"))


def _key(prefix: str, job: dict) -> str:
    from config_store import job_key
    return f"{prefix}_" + hashlib.sha1(job_key(job).encode("utf-8")).hexdigest()[:16]


def _qp(at, key):
    """A query parameter as a list: AppTest returns ["x"] up to Streamlit 1.64
    and "x" from 1.65 — the value checked is the same either way."""
    v = at.query_params[key]
    return list(v) if isinstance(v, (list, tuple)) else [v]


def _nav(at, page):
    at.button(key=f"nav_{page}").click().run()
    assert not at.exception
    return at


# ── navigation / information architecture ───────────────────────────────────

def test_every_page_renders_and_updates_the_url(app):
    at = app([OLD_RECORD, NEW_RECORD])
    assert not at.exception and "Discover jobs" in _html(at)
    for page, heading in [("jobs", "Jobs"), ("companies", "Companies"), ("monitoring", "Monitoring"),
                          ("email", "Email &amp; Notifications"), ("settings", "Settings"),
                          ("home", "Discover your next <em>opportunity</em>")]:
        _nav(at, page)
        assert f'<h1 class="page-title">{heading}</h1>' in _html(at)
        assert _qp(at, "page") == [page]


def test_home_is_only_about_jobs(app):
    """Company management, monitoring detail and email settings live on
    their own pages, not on Home."""
    at = app([NEW_RECORD])
    keys = {b.key for b in at.button}
    assert not keys & {"btn_add", "btn_save_email", "btn_test", "btn_clear_all", "btn_theme"}
    assert not [t for t in at.text_input if t.key in ("new_name", "new_url", "email_input")]
    html = _html(at)
    assert "Latest jobs" in html and "Graduate Software Engineer" in html


def test_deep_link_to_a_company_page(app):
    at = app([], query={"page": "companies", "company": "sanofi"})
    assert not at.exception
    html = _html(at)
    assert '<h1 class="detail-title">Sanofi</h1>' in html and "Portal health" in html


def test_unknown_deep_links_fall_back_safely(app):
    at = app([], query={"page": "nope", "job": "missing"})
    assert not at.exception and "Discover jobs" in _html(at)
    at = app([], query={"page": "companies", "company": "missing"})
    assert not at.exception and '<h1 class="page-title">Companies</h1>' in _html(at)


# ── job list rendering (real data, escaping, legacy records) ────────────────

def test_renders_old_and_new_records(app):
    at = app([OLD_RECORD, NEW_RECORD])
    assert not at.exception
    html = _html(at)
    assert "Junior Associate - Evidence Synthesis" in html          # old record, no category
    assert "Keyword match" in html                                 # labelled honestly, not "verified"
    assert "Graduate Software Engineer" in html and "Entry level" in html
    assert "Pune · India" in html
    assert 'href="https://pwc.example/job/2?src=a&amp;x=1"' in html  # escaped query string
    assert html.index("Graduate Software Engineer") < html.index("Junior Associate")  # newest first
    _nav(at, "jobs")
    assert "2 jobs found by the tracker · 2 active · 0 dismissed" in _html(at)


def test_empty_state(app):
    at = app([])
    assert not at.exception
    assert "No alerts yet" in _html(at)
    _nav(at, "settings")
    assert at.button(key="btn_clear_all").disabled                 # nothing to dismiss


def test_missing_and_malformed_fields(app):
    at = app([{"title": "Graduate Trainee"}, {}, "junk", {"title": None, "url": "javascript:alert(1)"}], page="jobs")
    assert not at.exception
    html = _html(at)
    assert "3 jobs found by the tracker" in html                    # non-dict junk skipped
    assert "Untitled posting" in html
    assert "javascript:" not in html
    assert "No link" in html


def test_special_characters_escaped(app):
    at = app([{**NEW_RECORD, "title": 'Dev <img src=x onerror=alert(1)> & "R&D"',
               "company": "Johnson & Johnson <India>"}])
    assert not at.exception
    html = _html(at)
    assert "<img src=x" not in html
    assert "Dev &lt;img src=x onerror=alert(1)&gt; &amp; &quot;R&amp;D&quot;" in html
    assert "Johnson &amp; Johnson &lt;India&gt;" in html


# ── dismiss / restore / clear (dedup history is never deleted) ───────────────

def test_duplicate_ids_do_not_crash_and_remove_only_the_selected(app):
    a = {**OLD_RECORD, "id": "Sanofi_Associate_–_Evidence_Synthesis,_C", "url": "https://x/R1",
         "title": "Associate – Evidence Synthesis, Clinical Outcomes"}
    b = {**a, "url": "https://x/R2", "title": "Associate – Evidence Synthesis, Clinical Oncology"}
    at = app([a, b])
    assert not at.exception and _key("dismiss", a) != _key("dismiss", b)
    at.button(key=_key("dismiss", b)).click().run()
    assert not at.exception
    seen = _seen(at)
    assert len(seen) == 2                               # records kept for dedup
    assert [j.get("dismissed", False) for j in seen] == [False, True]
    assert "Removed here, but not saved permanently" in _html(at)
    _nav(at, "jobs")
    assert "1 active · 1 dismissed" in _html(at)


def test_dismissed_jobs_can_be_restored_without_re_emailing(app):
    at = app([{**OLD_RECORD, "dismissed": True}, NEW_RECORD], page="jobs")
    at.pills(key="jobs_tab").set_value("dismissed").run()
    assert "Junior Associate - Evidence Synthesis" in _html(at)
    at.button(key=_key("restore", OLD_RECORD)).click().run()
    assert not at.exception
    seen = _seen(at)
    assert "dismissed" not in seen[0] and seen[0]["notified"] is True   # still counts as emailed
    assert "2 active · 0 dismissed" in _html(at)


def test_clear_all_hides_but_keeps_dedup_history(app):
    at = app([OLD_RECORD, NEW_RECORD], page="settings")
    at.button(key="btn_clear_all").click().run()
    assert not at.exception
    assert not any(j.get("dismissed") for j in _seen(at))   # the first click only asks for confirmation
    at.button(key="btn_clear_all_confirm").click().run()
    assert not at.exception
    seen = _seen(at)
    assert len(seen) == 2 and all(j["dismissed"] for j in seen)
    _nav(at, "home")
    assert "No alerts yet" in _html(at)
    # the scraper still treats them as seen -> no duplicate emails
    assert {(j["company"], j["title"]) for j in seen} == {("Sanofi", OLD_RECORD["title"]),
                                                          ("PwC", NEW_RECORD["title"])}


def test_pagination_counts(app):
    jobs = [{**NEW_RECORD, "id": f"j{i}", "url": f"https://x/{i}", "title": f"Graduate {i}"}
            for i in range(23)]
    at = app(jobs, page="jobs")
    assert "1–12 of 23" in _html(at)
    at.button(key="alerts_next").click().run()
    assert "13–23 of 23" in _html(at)
    assert at.button(key="alerts_next").disabled
    at = app(jobs)
    assert "Showing 8 of 23 jobs" in _html(at)          # Home shows a short list + link to Jobs
    at.button(key="btn_all_jobs").click().run()
    assert _qp(at, "page") == ["jobs"]


# ── filters ──────────────────────────────────────────────────────────────────

def test_search_location_category_company_and_sort(app):
    at = app([OLD_RECORD, NEW_RECORD, FRESHER_RECORD])
    assert not at.exception
    at.selectbox(key="h_cat").set_value("FRESHER").run()
    html = _html(at)
    assert "Trainee Analyst" in html and "Graduate Software Engineer" not in html
    at.selectbox(key="h_cat").set_value("legacy").run()
    html = _html(at)
    assert "Junior Associate - Evidence Synthesis" in html and "Trainee Analyst" not in html
    at.selectbox(key="h_cat").set_value("all").run()
    at.selectbox(key="h_loc").set_value("Pune").run()
    html = _html(at)
    assert "Graduate Software Engineer" in html and "Trainee Analyst" not in html
    at.selectbox(key="h_loc").set_value("All locations").run()
    at.selectbox(key="h_co").set_value("MetLife").run()
    assert "Trainee Analyst" in _html(at) and "Graduate Software Engineer" not in _html(at)
    at.button(key="h_clear").click().run()
    at.selectbox(key="h_sort").set_value("old").run()
    html = _html(at)
    assert html.index("Junior Associate") < html.index("Trainee Analyst") < html.index("Graduate Software Engineer")
    at.text_input(key="h_q").set_value("no such role").run()
    assert "No jobs match these filters" in _html(at)
    at.button(key="btn_reset_filters").click().run()
    assert not at.exception
    assert at.text_input(key="h_q").value == "" and at.selectbox(key="h_sort").value == "new"
    assert "Trainee Analyst" in _html(at) and "Graduate Software Engineer" in _html(at)


def test_filter_options_come_from_the_data(app):
    at = app([OLD_RECORD, NEW_RECORD, FRESHER_RECORD])
    assert at.selectbox(key="h_loc").options == ["All locations", "Hyderabad", "India", "Pune"]
    assert at.selectbox(key="h_co").options == ["All companies", "MetLife", "PwC", "Sanofi"]


# ── job details ──────────────────────────────────────────────────────────────

def test_job_details_show_real_fields_only(app):
    at = app([FRESHER_RECORD, OLD_RECORD])
    at.button(key=_key("crit", FRESHER_RECORD)).click().run()
    assert not at.exception
    html = _html(at)
    assert '<h1 class="detail-title">Trainee Analyst</h1>' in html
    assert "Why this matched" in html and "fresher signal: &#x27;Freshers welcome&#x27;" in html
    assert "Hyderabad · India" in html and "Email alert" in html
    assert _qp(at, "page") == ["jobs"] and at.query_params["job"]
    at.button(key="btn_back").click().run()
    assert "Discover jobs" in _html(at)                  # back to where it was opened

    at.button(key=_key("crit", OLD_RECORD)).click().run()
    html = _html(at)
    assert "No classifier trace was recorded for this job" in html
    assert "Not captured for this posting" in html        # no invented location
    at.button(key="detail_dismiss").click().run()
    assert not at.exception and _seen(at)[1].get("dismissed") is True


def test_job_detail_links_to_company(app):
    at = app([OLD_RECORD])
    at.button(key=_key("crit", OLD_RECORD)).click().run()
    at.button(key="btn_job_company").click().run()
    assert not at.exception
    assert '<h1 class="detail-title">Sanofi</h1>' in _html(at)
    assert "Workday API" in _html(at)                            # real scraper config
    assert "Playwright fallback" not in _html(at)                # no landing-page fallback any more


# ── companies ────────────────────────────────────────────────────────────────

def test_add_and_remove_company(app):
    at = app([], page="companies")
    at.button(key="btn_open_add").click().run()
    assert "Add a company" in _html(at) and _qp(at, "view") == ["add"]
    at.text_input(key="new_name").set_value("Infosys & Co <x>").run()
    at.text_input(key="new_url").set_value("javascript:alert(1)").run()
    at.button(key="btn_add").click().run()
    assert "starting with https://" in _html(at)
    assert at.text_input(key="new_name").value == "Infosys & Co <x>"   # values kept after an error
    at.text_input(key="new_url").set_value("https://careers.infosys.com").run()
    at.text_input(key="new_website").set_value("https://www.infosys.com").run()
    at.button(key="btn_add").click().run()
    assert not at.exception
    companies = _companies(at)
    added = companies[-1]
    assert added["name"] == "Infosys & Co <x>" and added["url"] == "https://careers.infosys.com"
    assert added["website"] == "https://www.infosys.com" and added["status"] == "unknown"
    html = _html(at)
    assert '<h1 class="page-title">Companies</h1>' in html          # returned to the list
    assert "Infosys &amp; Co &lt;x&gt;" in html and "New</span>" in html and "Pending" in html
    assert 'href="https://www.infosys.com"' in html and "Not set" in html   # website column

    view_key = "view_c_" + hashlib.sha1(added["id"].encode()).hexdigest()[:16]
    at.button(key=view_key).click().run()
    html = _html(at)
    assert 'href="https://careers.infosys.com"' in html and 'href="https://www.infosys.com"' in html
    at.button(key="btn_remove_company").click().run()
    assert "Stop tracking <b>Infosys &amp; Co &lt;x&gt;</b>?" in _html(at)   # asks first
    at.button(key="btn_remove").click().run()
    assert not at.exception
    assert [c["name"] for c in _companies(at)] == [c["name"] for c in companies[:-1]]
    assert _qp(at, "page") == ["companies"] and "company" not in at.query_params


def test_add_company_validation(app):
    first = json.loads((REPO / "companies.json").read_text("utf-8"))[0]
    at = app([], page="companies")
    at.button(key="btn_open_add").click().run()
    at.text_input(key="new_name").set_value(first["name"].upper()).run()
    at.text_input(key="new_url").set_value("https://example.com/careers").run()
    at.button(key="btn_add").click().run()
    assert "is already tracked" in _html(at)
    at.text_input(key="new_name").set_value("Brand New").run()
    at.text_input(key="new_url").set_value(first["url"] + "/").run()
    at.button(key="btn_add").click().run()
    assert "That career page is already tracked" in _html(at)
    at.text_input(key="new_url").set_value("https://brand.example/jobs").run()
    at.text_input(key="new_website").set_value("brand.example").run()
    at.button(key="btn_add").click().run()
    assert "website must be a full URL" in _html(at)
    assert len(_companies(at)) == len(json.loads((REPO / "companies.json").read_text("utf-8")))


def test_company_search(app):
    at = app([], page="companies")
    at.text_input(key="co_q").set_value("sanofi").run()
    html = _html(at)
    assert "sanofi.wd3.myworkdayjobs.com" in html and "metlifecareers.com" not in html


def test_company_with_empty_name_does_not_crash(app):
    at = app([])
    (at.tmp_path / "companies.json").write_text(json.dumps([{"id": "x", "name": "", "url": "ftp://bad"}]))
    _nav(at, "companies")
    assert "Unnamed" in _html(at)
    assert "Invalid career page URL" in _html(at)


def test_statuses_come_from_scraper_data(app):
    now = datetime.now(timezone.utc)
    at = app([])
    (at.tmp_path / "companies.json").write_text(json.dumps([
        {"id": "a", "name": "Alpha", "url": "https://a.example", "status": "active", "last_checked": now.isoformat()},
        {"id": "b", "name": "Beta", "url": "https://b.example", "status": "broken", "last_checked": now.isoformat(),
         "status_reason": "career page shows a bot challenge (e.g. Cloudflare)"},
        {"id": "c", "name": "Gamma", "url": "https://c.example", "status": "unknown", "last_checked": ""},
        {"id": "d", "name": "Delta", "url": "https://d.example", "status": "active",
         "last_checked": (now - timedelta(hours=12)).isoformat()},
        {"id": "e", "name": "Epsilon", "url": "https://e.example", "status": "failing", "last_checked": now.isoformat(),
         "status_reason": "5 of 6 job pages could not be read (last: HTTP 429)"},
        {"id": "f", "name": "Zeta", "url": "https://f.example", "status": "needs_config", "last_checked": now.isoformat(),
         "status_reason": "no job postings found on this page — set the company's job search page URL"},
    ]))
    _nav(at, "monitoring")
    html = _html(at)
    for label in ("Healthy", "Broken", "Failing", "Needs configuration", "Pending", "Delayed"):
        assert f"<i></i>{label}</span>" in html
    assert "3 portals failing" in html and "Not scanned yet" in html
    # the scraper's own reason is shown, not a generic "failed"
    assert "bot challenge" in html and "HTTP 429" in html and "set the company&#x27;s job search page" in html
    assert "Disabled" not in html                          # no invented states
    _nav(at, "companies")
    at.pills(key="co_status").set_value("failing").run()
    html = _html(at)
    assert "Beta" in html and "Epsilon" in html and "Zeta" in html and "Alpha" not in html


# ── scans ────────────────────────────────────────────────────────────────────

def test_refresh_reloads_data_without_restart(app):
    at = app([OLD_RECORD])
    assert "Graduate Software Engineer" not in _html(at)
    (at.tmp_path / "seen_jobs.json").write_text(json.dumps([OLD_RECORD, NEW_RECORD]))
    at.button(key="btn_refresh").click().run()
    assert not at.exception
    assert "Graduate Software Engineer" in _html(at)
    assert "Showing the latest data" in _html(at)


def test_run_check_without_token_is_friendly(app):
    at = app([], page="monitoring")
    at.button(key="btn_run_check").click().run()
    assert not at.exception
    assert "no GitHub token is configured" in _html(at)


def test_metrics_are_computed_from_real_data(app):
    at = app([OLD_RECORD, NEW_RECORD])
    n = len(json.loads((REPO / "companies.json").read_text("utf-8")))
    html = _html(at)
    assert "Companies monitored" in html and f'<div class="v">{n}</div>' in html
    assert "Verified" not in html                          # no verification concept exists
    assert "99.8" not in html and "712" not in html and "candidates clicked" not in html


# ── email & notifications ────────────────────────────────────────────────────

def test_test_mail_errors_are_friendly(app):
    at = app([], page="email")
    assert "Not sent this session" in _html(at)
    at.button(key="btn_test").click().run()
    assert not at.exception
    html = _html(at)
    assert "Email isn't configured for this app" in html
    assert "GMAIL_APP_PASSWORD" not in html and "RuntimeError" not in html
    assert "Failed at" in html


def test_toast_is_shown_once_without_blocking(app):
    at = app([], page="email")
    at.button(key="btn_test").click().run()              # no Gmail credentials -> error toast
    assert "Email isn't configured for this app" in _html(at)
    at.run()                                             # next interaction: toast gone
    assert "Email isn't configured for this app" not in _html(at)


def test_recipient_is_read_only_and_never_written(app):
    """The repository is public, so the app no longer writes the alert
    address anywhere; it only shows where alerts go and how to change it."""
    at = app([], page="email")
    assert not at.exception
    assert not [t for t in at.text_input if t.key == "email_input"]
    assert "btn_save_email" not in {b.key for b in at.button}
    html = _html(at)
    assert "Alerts on" in html and "Sending to me@example.com" in html
    assert "Read from settings.json, which is public" in html and "ALERT_RECIPIENT" in html
    assert json.loads((at.tmp_path / "settings.json").read_text("utf-8")) == {"recipient_email": "me@example.com"}


def test_delivery_summary_uses_notified_flags(app):
    at = app([OLD_RECORD, NEW_RECORD], page="email")
    html = _html(at)
    assert "Covered a job found Jul 15, 13:45 UTC" in html   # only OLD_RECORD was emailed
    assert "1 — sent after the next check" in html           # NEW_RECORD is still pending


# ── settings ─────────────────────────────────────────────────────────────────

def test_theme_toggle_and_engine_facts(app):
    at = app([], page="settings")
    html = _html(at)                                          # read-only facts from scraper.py
    assert "Workday API for PokerStars, PwC, Sanofi" in html
    assert "Accenture job-search API for Accenture" in html and "Zoho Recruit career site for NPCI" in html
    at.button(key="btn_theme").click().run()
    assert not at.exception and at.session_state.dark_mode is True
    assert "#0b1020" in _html(at)


# ── GitHub sync ──────────────────────────────────────────────────────────────

def test_stale_local_copy_is_refreshed_from_github_and_merged_on_dismiss(app, monkeypatch):
    """The production bug: the app's checkout is hours old. It must show the
    jobs the scraper committed since, and dismissing one alert must not
    overwrite GitHub with the stale copy (which would re-email new jobs)."""
    import config_store
    from test_config_store import FakeRepo

    remote_jobs = [OLD_RECORD, NEW_RECORD]
    repo = FakeRepo({"seen_jobs.json": remote_jobs,
                     "companies.json": json.loads((REPO / "companies.json").read_text("utf-8")),
                     "settings.json": {"recipient_email": "me@example.com"}})
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(config_store, "_get_repo", lambda token: repo)

    at = app([OLD_RECORD])                                  # local copy is stale
    assert not at.exception
    assert "Graduate Software Engineer" in _html(at)        # remote data shown
    # displaying doesn't rewrite the deployed checkout
    assert json.loads((at.tmp_path / "seen_jobs.json").read_text("utf-8")) == [OLD_RECORD]

    at.button(key=_key("dismiss", OLD_RECORD)).click().run()
    assert not at.exception
    remote = repo.data("seen_jobs.json")
    assert [j["title"] for j in remote] == [OLD_RECORD["title"], NEW_RECORD["title"]]
    assert [j.get("dismissed", False) for j in remote] == [True, False]
    assert repo.commits == ["chore: dismiss 1 alert(s)"]
    assert "Removed 1 alert(s)" in _html(at)
    # a successful save brings the local copy up to date with GitHub
    assert json.loads((at.tmp_path / "seen_jobs.json").read_text("utf-8")) == remote
