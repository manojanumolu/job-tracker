"""JT-02 (independent audit of c0b8ca9): the disposable-email policy must fail
safely. A missing, unreadable, empty, truncated or malformed blocklist is
never read as an empty list: sign-in (fresh and restored) and personal alert
delivery are refused for every address the list would decide, with one fixed,
non-sensitive message, and everything resumes by itself once the file is
fixed. Answers that never depend on the list (admin-listed addresses, the
big providers in NEVER_BLOCKED) are unaffected."""
import time
from html import unescape

import pytest

import email_policy
import firebase_auth
import user_alerts
from access import ACCOUNT_KEY
from config_store import job_key
from test_firebase_auth import (  # noqa: F401  (fixture re-export)
    EMAIL, GATED, PASSWORD, UID, FakeUser, _fresh_google_token, _login, _login_page, admins, google_env,
    make_id_token, no_env, ok_google_answer, ok_password_answer,
)
from test_security_audit_2026_10_13 import firebase_replies, _verified_password_answer  # noqa: F401
from test_streamlit_app import NEW_RECORD, _html, app  # noqa: F401
from test_user_alerts import _person, job, store  # noqa: F401

UNAVAILABLE = email_policy.UNAVAILABLE_MESSAGE
OTHER = "person@example.org"            # an ordinary address the list has to decide
DOMAINS = [f"throwaway{i}.example" for i in range(1200)] + ["mailinator.com"]


def _write_list(path, domains=DOMAINS, count=None, header=True, extra=""):
    lines = ["# test list"]
    if header:
        lines.append(f"# entries: {len(domains) if count is None else count}")
    path.write_text("\n".join(lines + list(domains)) + "\n" + extra, encoding="utf-8")
    return path


@pytest.fixture
def blocklist(tmp_path, monkeypatch):
    """Point the policy at a file of the test's making (cache keyed by path)."""
    def use(path):
        monkeypatch.setattr(email_policy, "BLOCKLIST_FILE", path)
        return path
    yield use
    monkeypatch.undo()


def _broken_variants(tmp_path):
    bad_utf8 = tmp_path / "bad_utf8.txt"
    bad_utf8.write_bytes(b"# entries: 1\n\xff\xfe\x00junk\n")
    return {
        "missing": tmp_path / "missing.txt",
        "unreadable (a directory)": tmp_path,
        "empty": _write_list(tmp_path / "empty.txt", domains=[]),
        "not UTF-8": bad_utf8,
        "malformed entry": _write_list(tmp_path / "malformed.txt", DOMAINS + ["not a domain!"]),
        "uppercase entry": _write_list(tmp_path / "upper.txt", DOMAINS + ["MAILINATOR.NET"]),
        "truncated (count mismatch)": _write_list(tmp_path / "trunc.txt", count=len(DOMAINS) + 500),
        "no count header": _write_list(tmp_path / "nohdr.txt", header=False),
        "too few entries": _write_list(tmp_path / "few.txt", domains=["mailinator.com"]),
    }


# ---------------------------------------------------------------------------
# the policy module
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("case", ["missing", "unreadable (a directory)", "empty", "not UTF-8", "malformed entry",
                                  "uppercase entry", "truncated (count mismatch)", "no count header", "too few entries"])
def test_a_broken_list_is_never_an_empty_list(tmp_path, blocklist, capsys, case):
    blocklist(_broken_variants(tmp_path)[case])
    with pytest.raises(email_policy.PolicyUnavailable):
        email_policy.disposable_domains()
    with pytest.raises(email_policy.PolicyUnavailable):
        email_policy.is_disposable(OTHER)
    assert email_policy.sign_in_problem(OTHER, True) == UNAVAILABLE
    assert email_policy.address_problem("someone@mailinator.com") == UNAVAILABLE
    assert email_policy.alert_address_check(OTHER) == email_policy.ALERT_UNAVAILABLE
    assert not email_policy.may_receive_alerts(OTHER)
    err = capsys.readouterr().err
    assert "unavailable" in err and "throwaway" not in err                 # a reason, never file contents


def test_answers_that_never_depend_on_the_list_still_work(tmp_path, blocklist, monkeypatch):
    blocklist(tmp_path / "missing.txt")
    for addr in ("someone@gmail.com", "a@outlook.com", "b@yahoo.co.in"):          # NEVER_BLOCKED providers
        assert email_policy.sign_in_problem(addr, True) == "" and email_policy.may_receive_alerts(addr)
    monkeypatch.setenv("JT_ADMIN_EMAILS", "boss@example.org")
    assert email_policy.sign_in_problem("Boss@Example.org", True) == ""           # trusted configuration
    assert email_policy.sign_in_problem("boss@example.org", False) == email_policy.UNVERIFIED_MESSAGE
    assert email_policy.sign_in_problem("no-at-sign", True) == email_policy.INVALID_MESSAGE
    assert email_policy.alert_address_check("") == email_policy.ALERT_BLOCKED


def test_a_valid_list_decides_entries_subdomains_and_providers(tmp_path, blocklist):
    blocklist(_write_list(tmp_path / "ok.txt"))
    assert len(email_policy.disposable_domains()) == len(DOMAINS)
    for addr in ("x@mailinator.com", "x@inbox.mailinator.com", "x@a.b.throwaway7.example", "X@MAILINATOR.COM."):
        assert email_policy.is_disposable(addr) and email_policy.sign_in_problem(addr, True) == email_policy.DISPOSABLE_MESSAGE
    for addr in (OTHER, "x@notmailinator.com", "x@mailinator.com.evil.example", "x@gmail.com", "x@iitb.ac.in"):
        assert not email_policy.is_disposable(addr) and email_policy.sign_in_problem(addr, True) == ""
    assert email_policy.alert_address_check("x@mailinator.com") == email_policy.ALERT_BLOCKED
    assert email_policy.alert_address_check(OTHER) == email_policy.ALERT_OK


def test_a_fixed_list_is_picked_up_without_a_restart(tmp_path, blocklist):
    path = blocklist(_write_list(tmp_path / "list.txt", DOMAINS + ["Bad Entry"]))
    assert email_policy.sign_in_problem(OTHER, True) == UNAVAILABLE                 # failure isn't cached
    _write_list(path)
    assert email_policy.sign_in_problem(OTHER, True) == ""
    path.unlink()                                                                     # and broken again
    assert email_policy.sign_in_problem(OTHER, True) == UNAVAILABLE


def test_the_shipped_list_loads_and_matches_its_header():
    domains = email_policy.disposable_domains()
    text = email_policy.BLOCKLIST_FILE.read_text("utf-8")
    header = [line for line in text.splitlines() if email_policy._COUNT_RE.match(line)]
    assert len(header) == 1 and int(email_policy._COUNT_RE.match(header[0]).group(1)) == len(domains) >= 9000


# ---------------------------------------------------------------------------
# every sign-in route, fresh and restored
# ---------------------------------------------------------------------------

def test_google_sign_in_is_refused_while_the_list_is_broken(app, google_env, admins, tmp_path, blocklist):
    admins("boss@example.org")
    blocklist(tmp_path / "missing.txt")
    fb = google_env["firebase"](ok_google_answer(email=OTHER))
    google_env["set_user"](FakeUser(token=_fresh_google_token(), email=OTHER))
    at = app([NEW_RECORD], secrets=GATED, owner=False)
    html = unescape(_html(at))
    assert _login_page(at) and UNAVAILABLE in html and "Graduate Software Engineer" not in html
    assert fb.requests == [] and ACCOUNT_KEY not in at.session_state                  # nothing linked or created
    assert "missing.txt" not in html and "disposable_domains" not in html             # nothing internal shown


def test_password_sign_in_is_refused_without_asking_firebase(app, google_env, admins, tmp_path, blocklist,
                                                              firebase_replies):
    admins("boss@example.org")
    blocklist(tmp_path / "missing.txt")
    fake = firebase_replies(signInWithPassword=_verified_password_answer(email=OTHER))
    at = app([NEW_RECORD], secrets=GATED, owner=False)
    _login(at, email=OTHER)
    assert _login_page(at) and UNAVAILABLE in unescape(_html(at)) and fake.calls == []
    assert firebase_auth.SESSION_KEY not in at.session_state


def test_restored_sessions_cannot_bypass_and_resume_once_fixed(app, google_env, admins, tmp_path, blocklist):
    """A verified member already signed in (Google cookie / password
    session) is refused on the next page load while the list is broken —
    and gets back in by themselves once it is fixed."""
    admins("boss@example.org")
    path = blocklist(_write_list(tmp_path / "list.txt"))
    google_env["firebase"](ok_google_answer(email=OTHER))
    google_env["set_user"](FakeUser(token=_fresh_google_token(), email=OTHER))
    at = app([NEW_RECORD], secrets=GATED, owner=False)
    assert not _login_page(at) and at.session_state[ACCOUNT_KEY]["email"] == OTHER     # a working list: in
    path.unlink()
    at.run()
    assert _login_page(at) and UNAVAILABLE in unescape(_html(at)) and ACCOUNT_KEY not in at.session_state
    _write_list(path)
    at.run()
    assert not _login_page(at) and at.session_state[ACCOUNT_KEY]["email"] == OTHER


def test_a_restored_password_session_is_refused_while_broken(app, google_env, admins, tmp_path, blocklist):
    admins("boss@example.org")
    blocklist(tmp_path / "missing.txt")
    at = app([NEW_RECORD], secrets=GATED, owner=False)
    at.session_state[firebase_auth.SESSION_KEY] = {"uid": UID, "email": OTHER, "provider": "password",
                                                   "email_verified": True, "expires_at": time.time() + 3600}
    at.run()
    assert _login_page(at) and ACCOUNT_KEY not in at.session_state and UNAVAILABLE in unescape(_html(at))


def test_gmail_and_the_admin_still_get_in_while_broken(app, google_env, admins, tmp_path, blocklist):
    admins(EMAIL)
    blocklist(tmp_path / "missing.txt")
    google_env["firebase"](ok_google_answer())
    google_env["set_user"](FakeUser(token=_fresh_google_token()))                      # the admin (example.com)
    at = app([NEW_RECORD], secrets=GATED, owner=False)
    assert not _login_page(at) and 'class="role admin"' in _html(at)
    admins("boss@example.org")
    google_env["firebase"](ok_google_answer(email="someone@gmail.com"))
    google_env["set_user"](FakeUser(token=_fresh_google_token(), email="someone@gmail.com"))
    at = app([NEW_RECORD], secrets=GATED, owner=False)
    assert not _login_page(at) and "Graduate Software Engineer" in _html(at)


# ---------------------------------------------------------------------------
# alert delivery
# ---------------------------------------------------------------------------

def test_no_personal_alert_is_sent_while_the_list_is_broken(store, tmp_path, blocklist):
    blocklist(tmp_path / "missing.txt")
    _person(store, "uidA" * 7, OTHER)
    _person(store, "uidB" * 7, "fine@gmail.com")
    sent = []
    stats = user_alerts.run(store, [job(1)], lambda jobs, to: sent.append(to), run_id="r1")
    assert sent == ["fine@gmail.com"] and stats["policy_unavailable"] == 1
    assert store.for_uid("uidA" * 7).delivery(job_key(job(1))) is None              # nothing claimed: sent once fixed
    _write_list(tmp_path / "missing.txt")
    blocklist(tmp_path / "missing.txt")
    stats = user_alerts.run(store, [job(1)], lambda jobs, to: sent.append(to), run_id="r2")
    assert sent == ["fine@gmail.com", OTHER] and stats["sent"] == 1


def test_the_cli_reports_a_broken_list_as_a_failure(monkeypatch, store, tmp_path, blocklist):
    blocklist(tmp_path / "missing.txt")
    _person(store, "uidA" * 7, OTHER)
    monkeypatch.setattr(user_alerts, "configure", lambda *a, **k: (store, ""))
    monkeypatch.setattr(user_alerts, "run", lambda *a, **k: {"policy_unavailable": 1})
    assert user_alerts.main([]) == 1
