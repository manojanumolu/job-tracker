"""Runs the real Streamlit app (AppTest) against a *copy* of the repo in a
temp directory, with no GitHub token, so production JSON is never touched."""
import hashlib
import json
import shutil
from pathlib import Path

import pytest

st_testing = pytest.importorskip("streamlit.testing.v1")

REPO = Path(__file__).resolve().parent.parent
APP_FILES = ["streamlit_app.py", "config_store.py", "notifier.py", "job_classifier.py"]

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
    for name in APP_FILES:
        shutil.copy(REPO / name, tmp_path / name)
    shutil.copytree(REPO / "assets", tmp_path / "assets")
    shutil.copy(REPO / "companies.json", tmp_path / "companies.json")
    (tmp_path / "settings.json").write_text(json.dumps({"recipient_email": "me@example.com"}))

    import streamlit as st
    st.cache_data.clear()  # the app's GitHub snapshot cache is process-wide

    def make(seen):
        (tmp_path / "seen_jobs.json").write_text(json.dumps(seen))
        at = st_testing.AppTest.from_file(str(tmp_path / "streamlit_app.py"), default_timeout=30)
        at.tmp_path = tmp_path
        return at.run()
    return make


def _html(at) -> str:
    return "\n".join(e.proto.body for e in at.get("html"))


def _seen(at):
    return json.loads((at.tmp_path / "seen_jobs.json").read_text("utf-8"))


def _dismiss_key(job: dict) -> str:
    from config_store import job_key
    return "dismiss_" + hashlib.sha1(job_key(job).encode("utf-8")).hexdigest()[:16]


def _crit_buttons(at):
    return [b for b in at.button if (b.key or "").startswith("crit_")]


def test_renders_old_and_new_records(app):
    at = app([OLD_RECORD, NEW_RECORD])
    assert not at.exception
    html = _html(at)
    assert "2 postings" in html
    assert "Junior Associate - Evidence Synthesis" in html          # old record, no category
    assert "Graduate Software Engineer" in html and "Entry level" in html
    assert "Pune · India" in html
    assert 'href="https://pwc.example/job/2?src=a&amp;x=1"' in html  # escaped query string
    # newest first
    assert html.index("Graduate Software Engineer") < html.index("Junior Associate")


def test_empty_state(app):
    at = app([])
    assert not at.exception
    assert "No alerts yet" in _html(at)
    assert "0 postings" in _html(at)
    assert not [b for b in at.button if b.key == "btn_clear_all"]    # nothing to page or clear


def test_missing_and_malformed_fields(app):
    at = app([{"title": "Graduate Trainee"}, {}, "junk", {"title": None, "url": "javascript:alert(1)"}])
    assert not at.exception
    html = _html(at)
    assert "3 postings" in html                       # non-dict junk skipped
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


def test_duplicate_ids_do_not_crash_and_remove_only_the_selected(app):
    a = {**OLD_RECORD, "id": "Sanofi_Associate_–_Evidence_Synthesis,_C", "url": "https://x/R1",
         "title": "Associate – Evidence Synthesis, Clinical Outcomes"}
    b = {**a, "url": "https://x/R2", "title": "Associate – Evidence Synthesis, Clinical Oncology"}
    at = app([a, b])
    assert not at.exception and _dismiss_key(a) != _dismiss_key(b)
    at.button(key=_dismiss_key(b)).click().run()
    assert not at.exception
    seen = _seen(at)
    assert len(seen) == 2                               # records kept for dedup
    assert [j.get("dismissed", False) for j in seen] == [False, True]
    assert "1 posting " in _html(at) and "Removed here, but not saved permanently" in _html(at)


def test_clear_all_hides_but_keeps_dedup_history(app):
    at = app([OLD_RECORD, NEW_RECORD])
    at.button(key="btn_clear_all").click().run()
    assert not at.exception
    seen = _seen(at)
    assert len(seen) == 2 and all(j["dismissed"] for j in seen)
    assert "No alerts yet" in _html(at)
    # the scraper still treats them as seen -> no duplicate emails
    assert {(j["company"], j["title"]) for j in seen} == {("Sanofi", OLD_RECORD["title"]),
                                                          ("PwC", NEW_RECORD["title"])}


def test_pagination_counts(app):
    jobs = [{**NEW_RECORD, "id": f"j{i}", "url": f"https://x/{i}", "title": f"Graduate {i}"}
            for i in range(23)]
    at = app(jobs)
    assert "1–10 of 23" in _html(at)
    at.button(key="alerts_next").click().run()
    at.button(key="alerts_next").click().run()
    assert "21–23 of 23" in _html(at)
    assert at.button(key="alerts_next").disabled


def test_refresh_reloads_data_without_restart(app):
    at = app([OLD_RECORD])
    assert "1 posting " in _html(at)
    # the scraper commits new data while the app is running
    (at.tmp_path / "seen_jobs.json").write_text(json.dumps([OLD_RECORD, NEW_RECORD]))
    at.button(key="btn_refresh").click().run()
    assert not at.exception
    assert "2 postings" in _html(at)
    assert "Showing the latest data" in _html(at)


def test_run_check_without_token_is_friendly(app):
    at = app([])
    at.button(key="btn_run_check").click().run()
    assert not at.exception
    assert "no GitHub token is configured" in _html(at)


def test_test_mail_errors_are_friendly(app):
    at = app([])
    assert "Not sent this session" in _html(at)
    at.button(key="btn_test").click().run()
    assert not at.exception
    html = _html(at)
    assert "Email isn't configured for this app" in html
    assert "GMAIL_APP_PASSWORD" not in html and "RuntimeError" not in html
    assert "Failed " in html                              # delivery status reflects the real outcome


def test_toast_is_shown_once_without_blocking(app):
    at = app([])
    at.text_input(key="email_input").set_value("not-an-email").run()
    at.button(key="btn_save_email").click().run()
    assert "Enter a valid email address" in _html(at)
    at.run()                                             # next interaction: toast gone
    assert "Enter a valid email address" not in _html(at)


def test_save_email_without_token_saves_locally(app):
    at = app([])
    at.text_input(key="email_input").set_value("new@example.com").run()
    at.button(key="btn_save_email").click().run()
    assert not at.exception
    assert json.loads((at.tmp_path / "settings.json").read_text("utf-8"))["recipient_email"] == "new@example.com"
    assert "not saved permanently" in _html(at)
    assert "No GitHub token" in _html(at)                # honest sync status, no secrets shown


def test_add_and_remove_company(app):
    at = app([])
    at.text_input(key="new_name").set_value("Infosys & Co <x>").run()
    at.text_input(key="new_url").set_value("javascript:alert(1)").run()
    at.button(key="btn_add").click().run()
    assert "starting with https://" in _html(at)
    # the form keeps its values after a validation error; fix the URL and retry
    assert at.text_input(key="new_name").value == "Infosys & Co <x>"
    at.text_input(key="new_url").set_value("https://careers.infosys.com").run()
    at.button(key="btn_add").click().run()
    assert not at.exception
    companies = json.loads((at.tmp_path / "companies.json").read_text("utf-8"))
    assert companies[-1]["name"] == "Infosys & Co <x>"
    assert companies[-1]["url"] == "https://careers.infosys.com" and companies[-1]["status"] == "unknown"
    html = _html(at)
    assert "Infosys &amp; Co &lt;x&gt;" in html
    assert 'href="https://careers.infosys.com"' in html   # portal links to the real career page
    assert "Waiting" in html                              # not checked yet
    assert at.text_input(key="new_name").value == ""      # form cleared after adding

    at.button(key="btn_toggle_remove").click().run()
    at.checkbox(key=f"rm_{companies[-1]['id']}").check().run()
    at.button(key="btn_remove").click().run()
    assert not at.exception
    after = json.loads((at.tmp_path / "companies.json").read_text("utf-8"))
    assert [c["name"] for c in after] == [c["name"] for c in companies[:-1]]


def test_company_with_empty_name_does_not_crash(app, tmp_path):
    at = app([])
    (at.tmp_path / "companies.json").write_text(json.dumps([{"id": "x", "name": "", "url": "ftp://bad"}]))
    at.run()
    assert not at.exception
    assert "Unnamed" in _html(at)
    assert "Invalid career page URL" in _html(at)


def test_company_states_come_from_scraper_status(app):
    at = app([])
    (at.tmp_path / "companies.json").write_text(json.dumps([
        {"id": "a", "name": "Alpha", "url": "https://a.example", "status": "active"},
        {"id": "b", "name": "Beta", "url": "https://b.example", "status": "broken"},
        {"id": "c", "name": "Gamma", "url": "https://c.example", "status": "unknown"},
    ]))
    at.run()
    html = _html(at)
    assert ">Active<" in html.replace("</span>Active<", ">Active<") and "Error" in html and "Waiting" in html
    assert "2 of 3 career pages responding" in html and "67%" in html and "Degraded" in html


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
    assert "2 postings" in _html(at)                        # remote data shown
    # displaying doesn't rewrite the deployed checkout
    assert json.loads((at.tmp_path / "seen_jobs.json").read_text("utf-8")) == [OLD_RECORD]

    at.button(key=_dismiss_key(OLD_RECORD)).click().run()   # the old record (listed second)
    assert not at.exception
    remote = repo.data("seen_jobs.json")
    assert [j["title"] for j in remote] == [OLD_RECORD["title"], NEW_RECORD["title"]]
    assert [j.get("dismissed", False) for j in remote] == [True, False]
    assert repo.commits == ["chore: dismiss 1 alert(s)"]
    assert "Removed 1 alert(s)" in _html(at) and "1 posting " in _html(at)
    # a successful save brings the local copy up to date with GitHub
    assert json.loads((at.tmp_path / "seen_jobs.json").read_text("utf-8")) == remote


def test_search_and_category_filters(app):
    at = app([OLD_RECORD, NEW_RECORD, FRESHER_RECORD])
    assert not at.exception
    assert at.pills(key="job_filter").value == "all"
    at.pills(key="job_filter").set_value("FRESHER").run()
    html = _html(at)
    assert "Trainee Analyst" in html and "Graduate Software Engineer" not in html
    assert "1–1 of 1" in html
    at.pills(key="job_filter").set_value("Sanofi").run()              # company chip
    html = _html(at)
    assert "Junior Associate - Evidence Synthesis" in html and "Trainee Analyst" not in html
    at.pills(key="job_filter").set_value("all").run()
    at.text_input(key="job_search").set_value("pune").run()           # matches location
    html = _html(at)
    assert "Graduate Software Engineer" in html and "Trainee Analyst" not in html
    at.text_input(key="job_search").set_value("no such role").run()
    assert "No matching jobs in this view" in _html(at)
    at.button(key="btn_reset_filters").click().run()
    assert not at.exception
    assert at.text_input(key="job_search").value == ""
    assert "Trainee Analyst" in _html(at) and "Graduate Software Engineer" in _html(at)


def test_filter_chip_counts_use_real_categories(app):
    at = app([OLD_RECORD, NEW_RECORD, FRESHER_RECORD])
    labels = " | ".join(at.pills(key="job_filter").proto.options[i].content
                        for i in range(len(at.pills(key="job_filter").proto.options)))
    assert "All (3)" in labels and "Fresher only (1)" in labels and "Entry level (1)" in labels
    assert "Sanofi (1)" in labels and "PwC (1)" in labels


def test_reason_and_badges(app):
    at = app([OLD_RECORD, FRESHER_RECORD])
    html = _html(at)
    assert "Freshers welcome" in html                     # classifier reason shown
    assert "Hyderabad · India" in html
    # legacy record: no invented reason, just where it was posted
    assert "Direct posting on sanofi.example" in html
    assert "Emailed" in html and "Email pending" in html  # real notified flag


def test_view_criteria_dialog(app):
    at = app([FRESHER_RECORD, OLD_RECORD])
    crit = _crit_buttons(at)
    assert len(crit) == 2
    crit[0].click().run()                                 # newest first -> OLD_RECORD
    assert not at.exception
    assert "no eligibility trace is available" in _html(at)

    at = app([FRESHER_RECORD])
    _crit_buttons(at)[0].click().run()
    assert not at.exception
    html = _html(at)
    assert "Classifier reason trace" in html and "fresher signal: &#x27;Freshers welcome&#x27;" in html


def test_metrics_are_computed_from_real_data(app):
    at = app([OLD_RECORD, NEW_RECORD])
    html = _html(at)
    n = len(json.loads((REPO / "companies.json").read_text("utf-8")))
    assert f"{n} Portals Tracked" in html
    assert "career pages responding" in html
    # no placeholder numbers from the design mock-up
    assert "99.8" not in html and "712" not in html and "candidates clicked" not in html


def test_duplicate_company_rejected_and_theme_toggle(app):
    at = app([])
    first = json.loads((REPO / "companies.json").read_text("utf-8"))[0]["name"]
    at.text_input(key="new_name").set_value(first.upper()).run()
    at.text_input(key="new_url").set_value("https://example.com/careers").run()
    at.button(key="btn_add").click().run()
    assert "is already tracked" in _html(at)
    assert len(json.loads((at.tmp_path / "companies.json").read_text("utf-8"))) == \
        len(json.loads((REPO / "companies.json").read_text("utf-8")))
    at.button(key="btn_theme").click().run()
    assert not at.exception and at.session_state.dark_mode is True
    assert "#0e1220" in _html(at)                         # dark tokens applied
