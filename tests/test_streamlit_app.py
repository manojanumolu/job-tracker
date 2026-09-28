"""Runs the real Streamlit app (AppTest) against a *copy* of the repo in a
temp directory, with no GitHub token, so production JSON is never touched."""
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


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GMAIL_ADDRESS", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    for name in APP_FILES:
        shutil.copy(REPO / name, tmp_path / name)
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
    return json.loads((at.tmp_path / "seen_jobs.json").read_text())


def test_renders_old_and_new_records(app):
    at = app([OLD_RECORD, NEW_RECORD])
    assert not at.exception
    html = _html(at)
    assert "2 total" in html
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
    assert "0 total" in _html(at)


def test_missing_and_malformed_fields(app):
    at = app([{"title": "Graduate Trainee"}, {}, "junk", {"title": None, "url": "javascript:alert(1)"}])
    assert not at.exception
    html = _html(at)
    assert "3 total" in html                          # non-dict junk skipped
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
    assert not at.exception and len(at.checkbox) == 2
    at.checkbox[0].check().run()                        # newest first -> b
    at.button(key="btn_remove_selected").click().run()
    assert not at.exception
    seen = _seen(at)
    assert len(seen) == 2                               # records kept for dedup
    assert [j.get("dismissed", False) for j in seen] == [False, True]
    assert "1 total" in _html(at)


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
    assert "1 total" in _html(at)
    # the scraper commits new data while the app is running
    (at.tmp_path / "seen_jobs.json").write_text(json.dumps([OLD_RECORD, NEW_RECORD]))
    at.button(key="btn_refresh").click().run()
    assert not at.exception
    assert "2 total" in _html(at)
    assert "Showing the latest data" in _html(at)


def test_run_check_without_token_is_friendly(app):
    at = app([])
    at.button(key="btn_run_check").click().run()
    assert not at.exception
    assert "no GitHub token is configured" in _html(at)


def test_test_mail_errors_are_friendly(app):
    at = app([])
    at.button(key="btn_test").click().run()
    assert not at.exception
    html = _html(at)
    assert "Email isn't configured for this app" in html
    assert "GMAIL_APP_PASSWORD" not in html and "RuntimeError" not in html


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
    assert json.loads((at.tmp_path / "settings.json").read_text())["recipient_email"] == "new@example.com"
    assert "not saved permanently" in _html(at)


def test_add_and_remove_company(app):
    at = app([])
    at.button(key="btn_toggle_add").click().run()
    at.text_input(key="new_name").set_value("Infosys & Co <x>").run()
    at.text_input(key="new_url").set_value("javascript:alert(1)").run()
    at.button(key="btn_add").click().run()
    assert "starting with https://" in _html(at)
    # the form stays open after a validation error; fix the URL and retry
    at.text_input(key="new_url").set_value("https://careers.infosys.com").run()
    at.button(key="btn_add").click().run()
    assert not at.exception
    companies = json.loads((at.tmp_path / "companies.json").read_text())
    assert companies[-1]["name"] == "Infosys & Co <x>"
    assert "Infosys &amp; Co &lt;x&gt;" in _html(at)

    at.button(key="btn_toggle_remove").click().run()
    at.checkbox(key=f"rm_{companies[-1]['id']}").check().run()
    at.button(key="btn_remove").click().run()
    assert not at.exception
    after = json.loads((at.tmp_path / "companies.json").read_text())
    assert [c["name"] for c in after] == [c["name"] for c in companies[:-1]]


def test_company_with_empty_name_does_not_crash(app, tmp_path):
    at = app([])
    (at.tmp_path / "companies.json").write_text(json.dumps([{"id": "x", "name": "", "url": "ftp://bad"}]))
    at.run()
    assert not at.exception
    assert "Unnamed" in _html(at)


def test_stale_local_copy_is_refreshed_from_github_and_merged_on_dismiss(app, monkeypatch):
    """The production bug: the app's checkout is hours old. It must show the
    jobs the scraper committed since, and dismissing one alert must not
    overwrite GitHub with the stale copy (which would re-email new jobs)."""
    import config_store
    from test_config_store import FakeRepo

    remote_jobs = [OLD_RECORD, NEW_RECORD]
    repo = FakeRepo({"seen_jobs.json": remote_jobs,
                     "companies.json": json.loads((REPO / "companies.json").read_text()),
                     "settings.json": {"recipient_email": "me@example.com"}})
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(config_store, "_get_repo", lambda token: repo)

    at = app([OLD_RECORD])                                  # local copy is stale
    assert not at.exception
    assert "2 total" in _html(at)                           # remote data shown
    # displaying doesn't rewrite the deployed checkout
    assert json.loads((at.tmp_path / "seen_jobs.json").read_text()) == [OLD_RECORD]

    at.checkbox[1].check().run()                            # the old record (listed second)
    at.button(key="btn_remove_selected").click().run()
    assert not at.exception
    remote = repo.data("seen_jobs.json")
    assert [j["title"] for j in remote] == [OLD_RECORD["title"], NEW_RECORD["title"]]
    assert [j.get("dismissed", False) for j in remote] == [True, False]
    assert repo.commits == ["chore: dismiss 1 alert(s)"]
    assert "Removed 1 alert(s)" in _html(at) and "1 total" in _html(at)
    # a successful save brings the local copy up to date with GitHub
    assert json.loads((at.tmp_path / "seen_jobs.json").read_text()) == remote
