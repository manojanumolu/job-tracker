"""Security hardening (Oct 2026, after the Phase 0 audit).

Until real sign-in exists, every change made from the dashboard needs the
owner password (JT_OWNER_PASSWORD), checked on the server; the alert
recipient comes from the private ALERT_RECIPIENT secret and is never logged
or shown in full to visitors; the system mailbox only ever writes to that
recipient; Run check is owner-only with one cooldown for the whole app."""
import json
import logging
import time
from pathlib import Path

import pytest

import access
import alerts
import config_store
import notifier
from access import AttemptLimiter, Cooldown, alert_recipient, mask_email, redact_emails
from test_alerts import NEW, FakeRemote, Mailer, data  # noqa: F401  (fixture re-export)
from test_streamlit_app import FRESHER_RECORD, NEW_RECORD, OWNER_PASSWORD, _companies, _html, _key, _nav, _seen, app  # noqa: F401

REPO = Path(__file__).resolve().parent.parent
SECRET_ADDR = "private.person@example.org"


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


# ---------------------------------------------------------------------------
# access.py — password, limits, recipient, masking
# ---------------------------------------------------------------------------

def test_password_must_match_exactly(monkeypatch):
    monkeypatch.setenv("JT_OWNER_PASSWORD", OWNER_PASSWORD)
    assert access.owner_configured()
    assert access.password_matches(OWNER_PASSWORD)
    for wrong in ("", "correct horse battery stapl", OWNER_PASSWORD.upper(), OWNER_PASSWORD + " x", None, 123):
        assert not access.password_matches(wrong)


@pytest.mark.parametrize("configured", [None, "", "   ", "short-pw", "elevenchars"])
def test_missing_or_short_password_fails_closed(monkeypatch, configured):
    if configured is None:
        monkeypatch.delenv("JT_OWNER_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("JT_OWNER_PASSWORD", configured)
    assert not access.owner_configured()
    # not even the configured value itself (or an empty guess) gets in
    assert not access.password_matches(configured or "")
    assert not access.password_matches("")


def test_signin_limiter_locks_and_expires():
    clock = Clock()
    lim = AttemptLimiter(max_failures=3, window_s=60, clock=clock)
    for _ in range(3):
        assert lim.locked_for() == 0
        lim.failed()
    assert lim.locked_for() == pytest.approx(60)
    clock.t += 30
    assert lim.locked_for() == pytest.approx(30)
    clock.t += 31
    assert lim.locked_for() == 0                       # old failures fall out of the window
    lim.failed()
    lim.succeeded()
    assert lim.locked_for() == 0 and lim._failures == []


def test_cooldown_is_one_slot_and_can_be_given_back():
    clock = Clock()
    cd = Cooldown(300, clock=clock)
    assert cd.try_start() and not cd.try_start()
    assert cd.remaining() == pytest.approx(300)
    clock.t += 299
    assert not cd.try_start()
    clock.t += 1
    assert cd.try_start()
    cd.cancel()
    assert cd.remaining() == 0 and cd.try_start()


@pytest.mark.parametrize("secret, settings, expected", [
    (SECRET_ADDR, {"recipient_email": "me@example.com"}, (SECRET_ADDR, "secret")),
    (f"  {SECRET_ADDR} ", {}, (SECRET_ADDR, "secret")),
    ("", {"recipient_email": " me@example.com "}, ("me@example.com", "settings.json")),
    ("  ", {"recipient_email": "me@example.com"}, ("me@example.com", "settings.json")),
    ("", {"recipient_email": ""}, ("", "")),
    ("", {}, ("", "")),
    ("", None, ("", "")),
    ("", [], ("", "")),
    ("", {"recipient_email": None}, ("", "")),
    ("", {"recipient_email": ["x@y.z"]}, ("", "")),
])
def test_alert_recipient_prefers_the_secret(monkeypatch, secret, settings, expected):
    monkeypatch.setenv("ALERT_RECIPIENT", secret)
    assert alert_recipient(settings) == expected


@pytest.mark.parametrize("addr, masked", [
    ("manoj@gmail.com", "m•••@gmail.com"),
    ("a@b.co", "a•••@b.co"),
    ("", ""),
    ("not-an-email", "•••"),
    ("@nouser.com", "•••"),
])
def test_mask_email(addr, masked):
    assert mask_email(addr) == masked


def test_redact_emails_masks_every_address():
    err = "(550, b'5.1.1 <private.person@example.org>: user unknown'), from bot+tag@mail.example.co.in"
    out = redact_emails(err)
    assert "private.person@example.org" not in out and "bot+tag@mail.example.co.in" not in out
    assert "p•••@example.org" in out and "b•••@mail.example.co.in" in out
    assert redact_emails("no address here") == "no address here"


# ---------------------------------------------------------------------------
# Visitors (not signed in) cannot change anything
# ---------------------------------------------------------------------------

DISMISSED = {**FRESHER_RECORD, "dismissed": True}


def _blocked(at, seen_before, companies_before):
    assert not at.exception
    assert "Only the owner can" in _html(at)
    assert _seen(at) == seen_before
    assert _companies(at) == companies_before


@pytest.mark.parametrize("action", [
    "row_dismiss", "row_restore", "detail_dismiss", "detail_restore", "dismiss_all",
    "add_company", "remove_company",
])
def test_visitor_cannot_write(app, action):
    at = app([NEW_RECORD, DISMISSED], owner=False)
    seen, companies = _seen(at), _companies(at)
    if action == "row_dismiss":
        _nav(at, "jobs")
        at.button(key=_key("dismiss", NEW_RECORD)).click().run()
    elif action == "row_restore":
        _nav(at, "jobs")
        at.pills(key="jobs_tab").set_value("dismissed").run()
        at.button(key=_key("restore", DISMISSED)).click().run()
    elif action in ("detail_dismiss", "detail_restore"):
        rec = NEW_RECORD if action == "detail_dismiss" else DISMISSED
        _nav(at, "jobs")
        if rec is DISMISSED:
            at.pills(key="jobs_tab").set_value("dismissed").run()
        at.button(key=_key("crit", rec)).click().run()
        at.button(key=action).click().run()
    elif action == "dismiss_all":
        _nav(at, "settings")
        at.button(key="btn_clear_all").click().run()
        assert "btn_clear_all_confirm" not in {b.key for b in at.button}   # never reaches the confirm step
    elif action == "add_company":
        _nav(at, "companies")
        at.button(key="btn_open_add").click().run()
        at.text_input(key="new_name").set_value("Evil Corp").run()
        at.text_input(key="new_url").set_value("https://evil.example/jobs").run()
        at.button(key="btn_add").click().run()
    elif action == "remove_company":
        at = app([], owner=False, query={"page": "companies", "company": "sanofi"})
        seen, companies = _seen(at), _companies(at)
        at.button(key="btn_remove_company").click().run()
        assert "btn_remove" not in {b.key for b in at.button}
    _blocked(at, seen, companies)


def test_confirm_steps_recheck_owner_access(app):
    """A confirm button reached while signed in is re-checked when clicked:
    a sign-in that expired in between changes nothing."""
    at = app([NEW_RECORD], page="settings")
    at.button(key="btn_clear_all").click().run()
    at.session_state["_owner_until"] = time.time() - 1          # sign-in expired
    at.button(key="btn_clear_all_confirm").click().run()
    assert not _seen(at)[0].get("dismissed") and "Only the owner can" in _html(at)

    at = app([], query={"page": "companies", "company": "sanofi"})
    before = _companies(at)
    at.button(key="btn_remove_company").click().run()
    at.session_state["_owner_until"] = time.time() - 1
    at.button(key="btn_remove").click().run()
    assert _companies(at) == before


def test_visitor_cannot_send_test_email(app, monkeypatch):
    sent = []
    monkeypatch.setattr(notifier, "test_mail", lambda to: sent.append(to))
    at = app([], page="email", owner=False)
    at.button(key="btn_test").click().run()
    assert sent == [] and "Only the owner can send test emails" in _html(at)


class FakeWorkflow:
    def __init__(self, calls, ok=True):
        self.calls, self.ok = calls, ok

    def create_dispatch(self, ref):
        self.calls.append(ref)
        return self.ok


def _fake_github(monkeypatch, calls, ok=True):
    import github

    class FakeGithub:
        def __init__(self, token):
            pass

        def get_repo(self, name):
            class Repo:
                def get_workflow(_, wf):
                    return FakeWorkflow(calls, ok)
            return Repo()
    monkeypatch.setattr(github, "Github", FakeGithub)


def test_visitor_cannot_dispatch_the_workflow(app, monkeypatch):
    calls = []
    _fake_github(monkeypatch, calls)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(config_store, "fetch_remote_json", lambda *a, **k: {})
    at = app([], page="monitoring", owner=False)
    at.button(key="btn_run_check").click().run()
    assert calls == [] and "Only the owner can start a check" in _html(at)


def test_run_check_cooldown_is_shared_by_all_sessions(app, monkeypatch):
    """The 5-minute limit used to live in one browser session; a new tab
    reset it. Now it is one slot for the whole app."""
    calls = []
    _fake_github(monkeypatch, calls)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(config_store, "fetch_remote_json", lambda *a, **k: {})
    first = app([], page="monitoring")
    first.button(key="btn_run_check").click().run()
    assert calls == ["main"] and "Check started" in _html(first)
    other_tab = app([], page="monitoring")       # a fresh session on the same server
    other_tab.button(key="btn_run_check").click().run()
    assert calls == ["main"] and "A check was just started" in _html(other_tab)


def test_failed_dispatch_does_not_hold_the_cooldown(app, monkeypatch):
    calls = []
    _fake_github(monkeypatch, calls, ok=False)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(config_store, "fetch_remote_json", lambda *a, **k: {})
    at = app([], page="monitoring")
    at.button(key="btn_run_check").click().run()
    at.button(key="btn_run_check").click().run()
    assert calls == ["main", "main"] and "A check was just started" not in _html(at)


# ---------------------------------------------------------------------------
# Owner sign-in
# ---------------------------------------------------------------------------

def _sign_in(at, password):
    at.text_input(key="owner_pw").set_value(password)
    at.button(key="btn_sign_in").click().run()


def test_sign_in_and_out(app):
    at = app([NEW_RECORD], page="settings", owner=False)
    html = _html(at)
    assert "Owner access" in html and "Sign in with the owner password" in html
    _sign_in(at, "wrong password guess")
    assert "That password isn" in _html(at)
    assert "_owner_until" not in at.session_state
    _sign_in(at, OWNER_PASSWORD)
    assert at.session_state["_owner_until"] > time.time()
    html = _html(at)
    assert "Signed in as the owner" in html and OWNER_PASSWORD not in html
    # the password is never kept: not in a widget value, not in the URL
    assert "owner_pw" not in at.session_state or not at.session_state["owner_pw"]
    assert all(OWNER_PASSWORD not in str(v) for v in at.query_params.values())
    # now changes work
    _nav(at, "jobs")
    at.button(key=_key("dismiss", NEW_RECORD)).click().run()
    assert _seen(at)[0]["dismissed"] is True
    _nav(at, "settings")
    at.button(key="btn_sign_out").click().run()
    assert "_owner_until" not in at.session_state
    assert "btn_sign_in" in {b.key for b in at.button}


def test_repeated_wrong_passwords_lock_sign_in_for_every_session(app):
    at = app([], page="settings", owner=False)
    for _ in range(access.MAX_SIGNIN_FAILURES):
        _sign_in(at, "not the password!")
    _sign_in(at, OWNER_PASSWORD)                   # even the right one is refused now
    assert "Too many failed attempts" in _html(at) and "_owner_until" not in at.session_state


def test_sign_in_expires(app):
    at = app([NEW_RECORD], owner=False)
    at.session_state["_owner_until"] = time.time() - 1
    _nav(at, "jobs")
    at.button(key=_key("dismiss", NEW_RECORD)).click().run()
    assert not _seen(at)[0].get("dismissed")


def test_no_owner_password_means_read_only(app, monkeypatch):
    """Without JT_OWNER_PASSWORD (or with a short one) nobody can change
    anything — even a session that somehow carries the owner flag."""
    for value in (None, "short"):
        if value is None:
            monkeypatch.delenv("JT_OWNER_PASSWORD", raising=False)
        else:
            monkeypatch.setenv("JT_OWNER_PASSWORD", value)
        at = app([NEW_RECORD], page="settings")              # owner flag set by the fixture
        assert "Changes are turned off" in _html(at)
        assert "btn_sign_in" not in {b.key for b in at.button}
        _nav(at, "jobs")
        at.button(key=_key("dismiss", NEW_RECORD)).click().run()
        assert not _seen(at)[0].get("dismissed")


# ---------------------------------------------------------------------------
# Recipient privacy
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("page", ["home", "jobs", "companies", "monitoring", "email", "settings"])
def test_visitors_never_see_the_full_recipient(app, monkeypatch, page):
    monkeypatch.setenv("ALERT_RECIPIENT", SECRET_ADDR)
    at = app([NEW_RECORD], page=None if page == "home" else page, owner=False)
    html = _html(at)
    assert SECRET_ADDR not in html and "me@example.com" not in html
    if page == "email":
        assert "p•••@example.org" in html and "Only the owner sees the full address" in html


def test_owner_sees_the_recipient_and_test_mail_uses_the_secret(app, monkeypatch):
    monkeypatch.setenv("ALERT_RECIPIENT", SECRET_ADDR)
    sent = []
    monkeypatch.setattr(notifier, "test_mail", lambda to: sent.append(to))
    at = app([], page="email")
    assert f"Sending to {SECRET_ADDR}" in _html(at) and "Kept in a private secret" in _html(at)
    at.button(key="btn_test").click().run()
    assert sent == [SECRET_ADDR]


def test_no_recipient_means_no_test_email(app, monkeypatch):
    sent = []
    monkeypatch.setattr(notifier, "test_mail", lambda to: sent.append(to))
    at = app([], page="email")
    (at.tmp_path / "settings.json").write_text(json.dumps({"recipient_email": ""}))
    at.run()
    at.button(key="btn_test").click().run()
    assert sent == [] and "No alert recipient is set" in _html(at)


# ---------------------------------------------------------------------------
# Actions logs (public for a public repository)
# ---------------------------------------------------------------------------

def test_alert_run_uses_the_secret_and_never_logs_the_address(data, monkeypatch, caplog):
    monkeypatch.setenv("ALERT_RECIPIENT", SECRET_ADDR)
    data([dict(NEW)])
    got = []
    caplog.set_level(logging.INFO, logger="alerts")
    assert alerts.run(send=lambda jobs, to: got.append(to), publisher=FakeRemote()) == 0
    assert got == [SECRET_ADDR]
    assert SECRET_ADDR not in caplog.text and "me@example.com" not in caplog.text
    assert "p•••@example.org" in caplog.text


def test_failed_send_error_is_logged_without_the_address(data, monkeypatch, caplog):
    monkeypatch.setenv("ALERT_RECIPIENT", SECRET_ADDR)
    data([dict(NEW)])

    def refuse(jobs, to):
        raise RuntimeError(f"{{'{to}': (550, b'5.1.1 user unknown')}}")
    caplog.set_level(logging.INFO, logger="alerts")
    assert alerts.run(send=refuse, publisher=FakeRemote()) == 1
    assert SECRET_ADDR not in caplog.text and "p•••@example.org" in caplog.text


def test_legacy_settings_recipient_still_works_and_warns(data, monkeypatch, caplog):
    """Until the secret exists, alerts keep going to settings.json's address
    (so nothing stops on deploy), with a warning that doesn't print it."""
    monkeypatch.delenv("ALERT_RECIPIENT", raising=False)
    data([dict(NEW)])
    got = []
    caplog.set_level(logging.INFO, logger="alerts")
    assert alerts.run(send=lambda jobs, to: got.append(to), publisher=FakeRemote()) == 0
    assert got == ["me@example.com"]
    assert "set the ALERT_RECIPIENT secret" in caplog.text and "me@example.com" not in caplog.text


def test_missing_settings_file_is_not_fatal(data, monkeypatch, tmp_path):
    monkeypatch.setenv("ALERT_RECIPIENT", SECRET_ADDR)
    (tmp_path / "settings.json").unlink()
    data([dict(NEW)])
    mail = Mailer()
    assert alerts.run(send=mail, publisher=FakeRemote()) == 0 and mail.sent


def test_notifier_prints_masked_addresses(monkeypatch, capsys):
    monkeypatch.setattr(notifier, "_send", lambda *a: None)
    notifier.send_alerts([{"title": "T", "company": "C", "url": "https://x.example/j/1"}], SECRET_ADDR)
    notifier.test_mail(SECRET_ADDR)
    out = capsys.readouterr().out
    assert SECRET_ADDR not in out and out.count("p•••@example.org") == 2


def test_test_mail_default_recipient_prefers_the_secret(monkeypatch, tmp_path):
    monkeypatch.setattr(config_store, "SETTINGS_FILE", tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text(json.dumps({"recipient_email": "me@example.com"}))
    monkeypatch.setenv("ALERT_RECIPIENT", SECRET_ADDR)
    to = []
    monkeypatch.setattr(notifier, "_send", lambda addr, *a: to.append(addr))
    notifier.test_mail()
    assert to == [SECRET_ADDR]


# ---------------------------------------------------------------------------
# Deployment configuration
# ---------------------------------------------------------------------------

def test_workflow_passes_the_recipient_secret_only_to_the_alert_step():
    wf = (REPO / ".github" / "workflows" / "check_jobs.yml").read_text("utf-8")
    scrape, publish = wf.split("- name: Publish results and send email alerts")
    assert "ALERT_RECIPIENT: ${{ secrets.ALERT_RECIPIENT }}" in publish
    assert "ALERT_RECIPIENT" not in scrape and "GMAIL" not in scrape
    assert "echo" not in wf.lower()                      # nothing prints secrets to the log
    # the schedule, permissions and steps are otherwise unchanged
    assert 'cron: "0 */3 * * *"' in wf and "contents: write" in wf and "python scraper.py" in wf


def test_browser_never_gets_tracebacks():
    cfg = (REPO / ".streamlit" / "config.toml").read_text("utf-8")
    assert '[client]' in cfg and 'showErrorDetails = "type"' in cfg


# ---------------------------------------------------------------------------
# The test-email operation checks the owner itself (not only its button)
# ---------------------------------------------------------------------------

@pytest.fixture
def mail_layer(monkeypatch, tmp_path):
    """Mocked mail layer: records what notifier would send, and fails the
    test if anything reaches a real SMTP connection."""
    import smtplib

    def no_network(*a, **k):
        raise AssertionError("a real SMTP connection was attempted")
    monkeypatch.setattr(smtplib, "SMTP_SSL", no_network)
    monkeypatch.setattr(smtplib, "SMTP", no_network)
    sent = []
    monkeypatch.setattr(notifier, "_send", lambda to, subject, html, text: sent.append((to, subject)))
    monkeypatch.setenv("JT_OWNER_PASSWORD", OWNER_PASSWORD)
    monkeypatch.delenv("ALERT_RECIPIENT", raising=False)
    return sent


def _owner_session(offset_s=3600):
    return {access.OWNER_SESSION_KEY: time.time() + offset_s}


@pytest.mark.parametrize("session", [
    {}, {access.OWNER_SESSION_KEY: None}, {access.OWNER_SESSION_KEY: "not-a-time"},
    {access.OWNER_SESSION_KEY: 0}, {"owner": True, "is_owner": True}, None, [],
])
def test_direct_test_email_without_sign_in_is_refused(mail_layer, monkeypatch, session):
    monkeypatch.setenv("ALERT_RECIPIENT", SECRET_ADDR)
    with pytest.raises(access.OwnerRequired):
        access.send_test_email(session, {"recipient_email": "me@example.com"})
    assert mail_layer == []


def test_direct_test_email_with_expired_sign_in_is_refused(mail_layer, monkeypatch):
    monkeypatch.setenv("ALERT_RECIPIENT", SECRET_ADDR)
    with pytest.raises(access.OwnerRequired):
        access.send_test_email(_owner_session(-1), {})
    assert mail_layer == []


@pytest.mark.parametrize("password", [None, "short"])
def test_direct_test_email_refused_while_owner_access_is_not_set_up(mail_layer, monkeypatch, password):
    """A session carrying the owner flag is still refused when no (valid)
    owner password is configured — fail closed."""
    if password is None:
        monkeypatch.delenv("JT_OWNER_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("JT_OWNER_PASSWORD", password)
    monkeypatch.setenv("ALERT_RECIPIENT", SECRET_ADDR)
    with pytest.raises(access.OwnerRequired):
        access.send_test_email(_owner_session(), {})
    assert mail_layer == []


def test_owner_test_email_goes_to_the_secret_recipient(mail_layer, monkeypatch):
    monkeypatch.setenv("ALERT_RECIPIENT", SECRET_ADDR)
    to = access.send_test_email(_owner_session(), {"recipient_email": "me@example.com"})
    assert to == SECRET_ADDR
    assert mail_layer == [(SECRET_ADDR, "Test email from Fresher Job Tracker")]


def test_owner_test_email_falls_back_to_settings_recipient(mail_layer):
    to = access.send_test_email(_owner_session(), {"recipient_email": " me@example.com "})
    assert to == "me@example.com" and [t for t, _ in mail_layer] == ["me@example.com"]


def test_owner_test_email_without_a_recipient_sends_nothing(mail_layer):
    with pytest.raises(LookupError):
        access.send_test_email(_owner_session(), {"recipient_email": ""})
    assert mail_layer == []


def test_test_email_operation_takes_no_recipient_argument():
    """There is no way to pass an address in: only the session and the
    stored settings."""
    import inspect
    assert list(inspect.signature(access.send_test_email).parameters) == ["session", "settings"]


def test_app_button_sends_through_the_guarded_operation(app, monkeypatch, mail_layer):
    """The dashboard's button reaches the mail layer only via
    access.send_test_email, which re-checks the owner sign-in."""
    calls = []
    real = access.send_test_email

    def spy(session, settings=None):
        calls.append(access.owner_session_valid(session))
        return real(session, settings)
    monkeypatch.setattr(access, "send_test_email", spy)
    monkeypatch.setenv("ALERT_RECIPIENT", SECRET_ADDR)
    at = app([], page="email")
    at.button(key="btn_test").click().run()
    assert calls == [True]
    assert [t for t, _ in mail_layer] == [SECRET_ADDR]
    assert "Test email sent" in _html(at)
