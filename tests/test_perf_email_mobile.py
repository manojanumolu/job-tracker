"""Click performance, the email templates and the responsive navigation
(Oct 14, 2026).

Measured causes of lag, fixed here:
  * "Dismiss all" wrote each job to Firestore one after another (27 jobs =
    27 round trips): now one batched commit
  * "Send test email" drew the whole page twice per click (inline handler +
    st.rerun): now an on_click callback, one run — and one email per 30 s
    for the whole app, so a double click never sends two
  * "Run check" could start a second full scan while one was running: it
    now looks at the newest Check Jobs runs first and starts nothing then
Emails: a job card per posting (company, category badge, title, a details
panel with location / eligibility / when it was found, the button and the
site it opens), every value escaped, only http(s) job links; the test email
is a delivery check with no jobs and no links.
Navigation: a menu button opens/closes the desktop sidebar (?menu=hidden);
phones get five tab slots with an admin's Monitor and Email under "More"."""
import json
import re
import time
from datetime import datetime, timezone

import pytest

import config_store
import notifier
import user_store
from notifier import build_alert_email, build_test_email
from test_notifier import _job, _parse
from test_streamlit_app import FRESHER_RECORD, NEW_RECORD, OLD_RECORD, _html, app  # noqa: F401  (fixture)


# ── Firestore: "Dismiss all" is one batched write ────────────────────────────

class CountingBackend(user_store.MemoryBackend):
    def __init__(self):
        super().__init__()
        self.calls = []

    def set(self, path, data, merge=False):
        self.calls.append("set")
        return super().set(path, data, merge)

    def delete(self, path):
        self.calls.append("delete")
        return super().delete(path)

    def set_many(self, docs):
        self.calls.append(("set_many", len(docs)))
        return super().set_many(docs)

    def delete_many(self, paths):
        paths = list(paths)
        self.calls.append(("delete_many", len(paths)))
        return super().delete_many(paths)


UID = "uid_perf_1"


def test_dismissing_many_jobs_is_one_write_and_one_job_is_a_plain_write():
    backend = CountingBackend()
    user = user_store.UserData(backend, UID)
    keys = {f"job{i}|https://x.example/{i}" for i in range(27)}
    user.dismiss(keys)
    assert backend.calls == [("set_many", 27)]                          # was 27 sequential writes
    assert user.dismissed() == keys                                      # same documents as before
    backend.calls.clear()
    user.restore(keys - {"job0|https://x.example/0"})
    assert backend.calls == [("delete_many", 26)] and user.dismissed() == {"job0|https://x.example/0"}
    backend.calls.clear()
    user.dismiss({"one|https://x.example/1"})
    user.restore({"one|https://x.example/1"})
    assert backend.calls == ["set", "delete"]


def test_a_backend_without_batches_still_works():
    class Plain(user_store.MemoryBackend):
        set_many = delete_many = None

    plain = Plain()
    del Plain.set_many, Plain.delete_many                                # no batch methods at all
    user = user_store.UserData(plain, UID)
    user.dismiss({"a|u1", "b|u2"})
    assert user.dismissed() == {"a|u1", "b|u2"}
    user.restore({"a|u1", "b|u2"})
    assert user.dismissed() == set()


def test_the_firestore_batches_respect_the_500_write_limit():
    class FakeBatch:
        def __init__(self, log):
            self.log, self.n = log, 0

        def set(self, ref, data):
            self.n += 1

        def delete(self, ref):
            self.n += 1

        def commit(self):
            self.log.append(self.n)

    class FakeClient:
        def __init__(self):
            self.commits = []

        def batch(self):
            return FakeBatch(self.commits)

        def document(self, path):
            return path

    client = FakeClient()
    fs = user_store.FirestoreBackend(client)
    fs.set_many({f"users/u/dismissed/{i}": {"job_key": str(i)} for i in range(1203)})
    fs.delete_many([f"users/u/dismissed/{i}" for i in range(501)])
    assert client.commits == [500, 500, 203, 500, 1]


# ── Send test email: one run, one email, no scan ────────────────────────────

def _no_github(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("GitHub must not be called by a test email")
    monkeypatch.setattr(config_store, "_get_repo", boom)
    monkeypatch.setattr(config_store, "fetch_remote_json", lambda *a, **k: {})


def test_a_double_click_sends_one_test_email_and_starts_no_scan(app, monkeypatch):
    sent = []
    monkeypatch.setattr(notifier, "test_mail", lambda to: sent.append(to))
    _no_github(monkeypatch)
    at = app([], page="email")
    at.button(key="btn_test").click().run()
    assert not at.exception and sent == ["me@example.com"] and "Test email sent" in _html(at)
    at.button(key="btn_test").click().run()                              # the second click of a double click
    assert sent == ["me@example.com"] and "A test email was just sent" in _html(at)


def test_a_failed_test_email_can_be_retried_at_once(app, monkeypatch):
    calls = []

    def fail_then_send(to):
        calls.append(to)
        if len(calls) == 1:
            raise OSError("smtp down")
    monkeypatch.setattr(notifier, "test_mail", fail_then_send)
    _no_github(monkeypatch)
    at = app([], page="email")
    at.button(key="btn_test").click().run()
    assert "Couldn't send the test email" in _html(at)
    at.button(key="btn_test").click().run()                              # nothing was sent: no cooldown
    assert len(calls) == 2 and "Test email sent" in _html(at)


def test_the_test_email_button_is_a_callback():
    from test_streamlit_app import REPO
    src = (REPO / "streamlit_app.py").read_text("utf-8")
    assert 'key="btn_test", icon=":material/send:", use_container_width=True,\n                      on_click=_send_test_click)' in src
    body = src[src.index("def _send_test_click"):src.index("\ndef page_email")]
    assert "st.rerun" not in body and "_trigger_scrape" not in body and "owner_only" in body


# ── Run check: never a second scan while one is running ─────────────────────

class _Run:
    def __init__(self, status, minutes_ago=3):
        self.status = status
        self.run_started_at = datetime.fromtimestamp(time.time() - minutes_ago * 60, tz=timezone.utc)


def _fake_github(monkeypatch, runs, dispatched, runs_error=False):
    class Workflow:
        def get_runs(self):
            if runs_error:
                raise RuntimeError("no actions:read")
            return iter(runs)

        def create_dispatch(self, ref):
            dispatched.append(ref)
            return True

    class Repo:
        def get_workflow(self, name):
            assert name == "check_jobs.yml"
            return Workflow()
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(config_store, "_get_repo", lambda token: Repo())
    monkeypatch.setattr(config_store, "fetch_remote_json", lambda *a, **k: {})


@pytest.mark.parametrize("status", ["queued", "in_progress", "waiting", "pending", "requested"])
def test_run_check_starts_nothing_while_a_check_is_running(app, monkeypatch, status):
    dispatched = []
    _fake_github(monkeypatch, [_Run(status), _Run("completed", 200)], dispatched)
    at = app([], page="monitoring")
    at.button(key="btn_run_check").click().run()
    assert not at.exception and dispatched == []
    assert "already running on GitHub Actions (started 3 min ago)" in _html(at) and "No second check" in _html(at)
    at.button(key="btn_run_check").click().run()                         # nothing started: no cooldown either
    assert dispatched == [] and "already running" in _html(at)


def test_run_check_starts_one_scan_and_says_it_keeps_running(app, monkeypatch):
    dispatched = []
    _fake_github(monkeypatch, [_Run("completed", 30), _Run("completed", 200)], dispatched)
    at = app([], page="monitoring")
    at.button(key="btn_run_check").click().run()
    assert dispatched == ["main"]
    assert "keeps running after this message" in _html(at)
    at.button(key="btn_run_check").click().run()                         # the 5-minute app-wide limit
    assert dispatched == ["main"] and "A check was just started" in _html(at)


def test_if_github_cannot_list_runs_run_check_works_as_before(app, monkeypatch):
    dispatched = []
    _fake_github(monkeypatch, [], dispatched, runs_error=True)
    at = app([], page="monitoring")
    at.button(key="btn_run_check").click().run()
    assert dispatched == ["main"] and not at.exception


# ── the emails ────────────────────────────────────────────────────────────────

def test_the_alert_email_cards_carry_only_real_data():
    jobs = [_job(first_seen="2026-10-07T18:41:00+00:00"),
            _job(title="Associate", company="", location="", category="", reason="", url="javascript:alert(1)")]
    subject, html, text = build_alert_email(jobs)
    checker = _parse(html)
    assert not checker.errors and checker.stack == []
    assert checker.links == [jobs[0]["url"]]                             # job links only, no tracking/app links
    assert "<img" not in html and "<script" not in html.lower()
    assert ">Sanofi</td>" in html and "sanofi.wd3.myworkdayjobs.com</td>" in html   # the site the button opens
    assert "8 Oct 2026, 12:11 AM IST" in html and "Found:    8 Oct 2026, 12:11 AM IST" in text
    assert html.count("FOUND") == 0 and html.count(">Found<") == 1      # only the job that has a first_seen
    assert "Open the company’s career page to apply." in html and "View job" in html
    assert "From Sanofi." in html and "From Sanofi." in text
    assert "set as the alert recipient" not in html                     # true for personal alerts too
    assert "job alerts are turned on for this address" in html and "turned on for this address" in text


@pytest.mark.parametrize("value", ["", "not a date", None, 5])
def test_an_unknown_found_time_is_left_out(value):
    _, html, text = build_alert_email([_job(first_seen=value)])
    assert ">Found<" not in html and "Found:" not in text


def test_the_summary_line_names_the_companies_naturally():
    one = build_alert_email([_job(company="PwC"), _job(company="PwC", url="https://x.example/2")])[1]
    two = build_alert_email([_job(company="PwC"), _job(company="Sanofi")])[1]
    many = build_alert_email([_job(company=f"Co{i}", url=f"https://x.example/{i}") for i in range(6)])[1]
    assert "From PwC." in one and "From PwC and Sanofi." in two and "From Co0, Co1, Co2, Co3 and 2 more." in many
    none = build_alert_email([_job(company="")])[1]
    assert "From ." not in none and "From " not in none.split("New job alert")[1].split("</p>")[1][:20]


def test_long_and_hostile_text_is_escaped_clipped_and_wraps():
    title = '<img src=x onerror=alert(1)> "R&D" ' + "IN-Associate_IA_Internal_Audit_" * 12
    _, html, text = build_alert_email([_job(title=title, company='A&B <Corp>', location="Pune <b>India</b>")])
    assert "<img" not in html                                            # no logos given: no image at all
    assert "&lt;img src=x onerror=alert(1)&gt; &quot;R&amp;D&quot;" in html  # shown as text, never markup
    assert "A&amp;B &lt;Corp&gt;" in html and "Pune &lt;b&gt;India&lt;/b&gt;" in html
    h2 = re.search(r"<h2 [^>]*>(.*?)</h2>", html, re.S)
    assert "overflow-wrap:anywhere" in h2.group(0) and h2.group(1).endswith("…")
    assert len(h2.group(1)) <= notifier._MAX_TITLE + 20


def test_header_injection_is_impossible_through_the_subject():
    subject, _, _ = build_alert_email([_job(title="Graduate\r\nBcc: victim@example.com", company="X\nY")])
    assert "\n" not in subject and "\r" not in subject


def test_the_test_email_is_a_delivery_check_only():
    subject, html, text = build_test_email(datetime(2026, 10, 14, 9, 40, tzinfo=timezone.utc))
    assert subject == "Test email from Fresher Job Tracker"              # unchanged
    checker = _parse(html)
    assert not checker.errors and checker.stack == [] and checker.links == []
    assert "Delivery test passed" in html and "DELIVERY TEST PASSED" in text
    assert "Your job alerts can reach you" in html
    assert "working correctly" in html and "working correctly" in text
    assert "14 Oct 2026, 3:10 PM IST" in html and "14 Oct 2026, 3:10 PM IST" in text
    assert "View job" not in html and "JOB 1" not in html and "<img" not in html   # no fake jobs, no images
    assert "no jobs were scanned" in html and "no jobs were scanned" in text      # truthful about what a test is
    for title, note in notifier._TEST_PROMISES:                                    # what real alerts do, nothing more
        assert title in html and title in text


def test_both_emails_share_the_frame_and_stay_inline():
    for _, html, _ in (build_alert_email([_job()]), build_test_email()):
        assert "<!DOCTYPE html>" in html and 'name="viewport"' in html and "max-width:600px" in html
        assert "Fresher Job Tracker</td>" in html                       # the brand row, text only
        assert "background-image:linear-gradient(90deg,#4f46e5,#0d9488)" in html   # the lead card's accent line
        assert 'bgcolor="#4f46e5"' in html                               # ... with a solid fallback
        style = html[html.index("<style>"):html.index("</style>")]
        assert "@media only screen and (max-width: 480px)" in style
        assert "url(" not in style and "@import" not in style and "http" not in style


def test_the_alert_summary_counts_the_kinds_of_role():
    _, html, _ = build_alert_email([_job(category="FRESHER"), _job(category="FRESHER", url="https://x.example/2"),
                                    _job(category="ENTRY_LEVEL", url="https://x.example/3")])
    assert ">2 Fresher</span>" in html and ">1 Entry level</span>" in html
    assert ">Job alert</td>" in html


# ── the background writer: an admin's dismissal doesn't wait for GitHub ─────

def test_an_admins_dismissal_commits_in_the_background_and_reports_failures():
    from test_streamlit_app import REPO
    src = (REPO / "streamlit_app.py").read_text("utf-8")
    soon = src[src.index("def _save_change_soon"):src.index("def _report_background_saves")]
    assert "if not is_owner():" in soon.split("def commit")[0]          # the role is checked in the click's own run
    assert 'ThreadPoolExecutor(max_workers=1' in src                   # one writer: commits never race each other
    body = src[src.index("def change_dismissal"):src.index("def toast(")]
    assert "_save_change_soon" in body and body.index("if dismiss:") < body.index("_save_change_soon")
    assert "_report_background_saves()\npage = st.session_state.page" in src


# ── navigation ────────────────────────────────────────────────────────────────

def _keys(at):
    return {b.key for b in at.button}


def _hides_sidebar(at):
    """The closed state's rule: the sidebar folds to zero width (it stays in
    the page, so its menu button stays visible at the window's edge)."""
    return 'section[data-testid="stSidebar"] { width: 0 !important; min-width: 0 !important; max-width: 0 !important;' in _html(at)


def test_the_menu_button_closes_and_opens_the_desktop_navigation(app):
    at = app([NEW_RECORD], page="jobs")
    assert {"btn_nav_toggle", "btn_drawer_open", "nav_jobs"} <= _keys(at)
    assert at.button(key="btn_nav_toggle").label == "Hide navigation" and not _hides_sidebar(at)
    at.button(key="btn_nav_toggle").click().run()
    assert not at.exception and at.session_state.nav_collapsed is True and _hides_sidebar(at)
    assert at.button(key="btn_nav_toggle").label == "Show navigation"
    assert at.query_params["menu"] in ("hidden", ["hidden"]) and at.session_state.page == "jobs"
    at.button(key="btn_nav_toggle").click().run()
    assert at.session_state.nav_collapsed is False and not _hides_sidebar(at) and "menu" not in at.query_params


def test_a_link_with_menu_hidden_opens_with_the_navigation_closed(app):
    at = app([NEW_RECORD], query={"page": "companies", "menu": "hidden"})
    assert at.session_state.nav_collapsed is True and at.session_state.page == "companies" and _hides_sidebar(at)


def test_the_phone_drawer_holds_every_destination_and_closes_after_a_choice(app):
    at = app([NEW_RECORD])
    assert not {k for k in _keys(at) if k.startswith(("drw_", "mob_"))}         # closed: nothing drawn
    at.button(key="btn_drawer_open").click().run()
    assert not at.exception and at.session_state.nav_drawer is True
    drawer = {k for k in _keys(at) if k.startswith("drw_")}
    assert drawer == {"drw_home", "drw_jobs", "drw_companies", "drw_monitoring", "drw_email", "drw_settings"}
    assert {"btn_drawer_close", "btn_drawer_backdrop"} <= _keys(at)
    at.button(key="btn_drawer_backdrop").click().run()                          # the dimmed page closes it
    assert at.session_state.nav_drawer is False and not {k for k in _keys(at) if k.startswith("drw_")}
    at.button(key="btn_drawer_open").click().run()
    at.button(key="drw_email").click().run()
    assert at.session_state.page == "email" and at.session_state.nav_drawer is False and not at.exception


def test_the_phone_and_menu_css_rules():
    from test_streamlit_app import REPO
    src = (REPO / "streamlit_app.py").read_text("utf-8")
    css = src[src.index('_CSS = """') + 10:src.index('"""', src.index('_CSS = """') + 10)]
    assert "<" not in css                                                # st.html would drop the stylesheet
    phone = css[css.index("@media (max-width: 767px) {\n  section[data-testid=\"stSidebar\"]"):]
    assert ".jt-toast { left: 16px; right: 16px; top: 74px; bottom: auto;" in phone
    assert "padding-bottom: 104px !important" in phone                   # room to scroll past Cloud's badges
    assert ".st-key-mnav" not in css and "mob_more" not in src           # the row of phone tabs is gone
    assert ".st-key-btn_nav_toggle { display: none !important; }" in css # phones: the drawer's button instead


# ── Oct 15: the desktop menu button sits in the sidebar; the email mark ──────

def test_the_desktop_menu_button_lives_in_the_sidebar(app):
    """Open: inside the sidebar, at its upper right (not over the page).
    Closed: at the window's left edge, with a gutter so it covers nothing.
    Both drawn by one sidebar fragment, so a click redraws only it."""
    from test_streamlit_app import REPO
    src = (REPO / "streamlit_app.py").read_text("utf-8")
    sidebar = src[src.index("with st.sidebar:\n    sidebar_toggle()"):]
    assert sidebar.index("sidebar_toggle()") < sidebar.index("logo = _logo_data_uri()")     # first thing in the sidebar
    frag = src[src.index("def sidebar_toggle"):src.index("# the desktop navigation's open/closed state")]
    assert "@st.fragment" in src[src.index("def sidebar_toggle") - 40:src.index("def sidebar_toggle")]
    assert 'key="btn_nav_toggle"' in frag and "_NAV_CLOSED_CSS if collapsed else _NAV_OPEN_CSS" in frag
    controls = src[src.index("def nav_controls"):src.index("def sidebar_toggle")]
    assert "btn_nav_toggle" not in controls.split('"""', 2)[2]          # the page area keeps only the phone button
    import re
    open_css = re.search(r'_NAV_OPEN_CSS = """(.*?)"""', src, re.S).group(1)
    closed_css = re.search(r'_NAV_CLOSED_CSS = """(.*?)"""', src, re.S).group(1)
    assert "@media (min-width: 1024px)" in open_css and "left: calc(var(--sidebar-w) - 50px)" in open_css
    assert "@media (min-width: 768px)" in closed_css and "top: 18px; left: 14px" in closed_css
    assert "padding-left: 72px !important" in closed_css                 # the page never sits under the button
    at = app([NEW_RECORD], page="jobs")
    assert "left: calc(var(--sidebar-w) - 50px)" in _html(at) and not _hides_sidebar(at)
    at.button(key="btn_nav_toggle").click().run()
    assert _hides_sidebar(at) and "top: 18px; left: 14px" in _html(at)


def test_the_emails_show_a_briefcase_not_initials():
    for _, html, text in (build_alert_email([_job()]), build_test_email()):
        head = html[html.index('title="Fresher Job Tracker"') - 200:html.index("Fresher Job Tracker</td>")]
        assert ">FJ<" not in html and "FJ" not in head
        assert notifier._BRIEFCASE in head and 'bgcolor="#4f46e5"' in head  # the purple tile keeps its size
        assert 'width="30" height="30"' in head
        assert "<img" not in html                                        # drawn, never an image to fetch
