"""Security audit 2026-10-13 + admin-only Scan details.

Regression tests for what the audit found and fixed:

* disposable / temporary addresses could hold an account (Google sign-in
  created a Firebase account for any Google identity, whatever its domain)
* email/password accounts with an UNVERIFIED address got member access
* a Google identity whose email Google hadn't verified got member access
* ten wrong passwords for made-up addresses locked EVERY email sign-in for
  15 minutes (one process-wide counter)

and for Settings → Scan details (administrator only). Google and Firebase
are never contacted (test_firebase_auth's fakes)."""
import json
import time
from datetime import datetime, timedelta, timezone
from html import unescape

import httpx
import pytest

import access
import email_policy
import firebase_auth
import scan_schedule
import user_alerts
from access import ACCOUNT_KEY, AccountAttemptLimiter
from firebase_auth import FirebaseConfig
from test_firebase_auth import (  # noqa: F401  (fixture re-export)
    API_KEY, EMAIL, GATED, PASSWORD, PROJECT, UID, FakeFirebase, FakeUser, _buttons, _dump, _fresh_google_token,
    _login, _login_page, admins, error_answer, google_env, make_id_token, no_env, ok_google_answer,
    ok_password_answer,
)
from test_streamlit_app import NEW_RECORD, _html, _key, _nav, _seen, app  # noqa: F401
from test_user_alerts import _person, job, store  # noqa: F401
from config_store import job_key

ADMIN = EMAIL.lower()
BOSS = "boss@example.org"
CONFIG = FirebaseConfig(API_KEY, PROJECT)
DISPOSABLE = ["someone@mailinator.com", "x@yopmail.com", "a.b@10minutemail.com", "test@guerrillamail.com",
              "Mixed.Case@MAILINATOR.COM", " padded@mailinator.com ", "dot@mailinator.com.", "sub@inbox.mailinator.com"]
ALLOWED = ["someone@gmail.com", "First.Last@googlemail.com", "a@outlook.com", "b@yahoo.co.in", "c@hotmail.com",
           "d@icloud.com", "e@proton.me", "f@rediffmail.com", "student@iitb.ac.in", "hr@pwc.com", "person@example.org"]


def _verified_password_answer(uid=UID, email=EMAIL, verified=True):
    return ok_password_answer(localId=uid, email=email,
                              idToken=make_id_token(uid=uid, email=email.lower(), email_verified=verified))


class Replies:
    """A Firebase fake that answers per REST method (signInWithPassword,
    sendOobCode, ...) and records which were called."""
    def __init__(self, **by_method):
        self.by_method, self.calls = by_method, []

    def handler(self, request: httpx.Request):
        method = request.url.path.rsplit(":", 1)[-1]
        self.calls.append((method, json.loads(request.content)))
        reply = self.by_method.get(method, {})
        return reply if isinstance(reply, httpx.Response) else httpx.Response(200, json=reply)

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handler))

    def methods(self):
        return [m for m, _ in self.calls]


@pytest.fixture
def firebase_replies(monkeypatch):
    def use(**by_method):
        fake = Replies(**by_method)
        monkeypatch.setattr(firebase_auth, "_new_client", fake.client)
        return fake
    return use


# ---------------------------------------------------------------------------
# email_policy: the one rule every route uses
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("email", DISPOSABLE)
def test_known_disposable_domains_are_rejected(email):
    assert email_policy.is_disposable(email)
    assert email_policy.sign_in_problem(email, True) == email_policy.DISPOSABLE_MESSAGE
    assert not email_policy.may_receive_alerts(email)


@pytest.mark.parametrize("email", ALLOWED)
def test_gmail_and_other_regular_addresses_still_work(email):
    assert not email_policy.is_disposable(email)
    assert email_policy.sign_in_problem(email, True) == ""
    assert email_policy.may_receive_alerts(email)


def test_big_providers_can_never_be_blocked_by_a_list_update(monkeypatch):
    listed = email_policy.disposable_domains()
    assert len(listed) > 1000 and not (email_policy.NEVER_BLOCKED & listed)
    monkeypatch.setattr(email_policy, "disposable_domains", lambda: frozenset({"gmail.com", "outlook.com"}))
    assert not email_policy.is_disposable("me@gmail.com") and not email_policy.is_disposable("me@outlook.com")


def test_the_vendored_list_is_well_formed():
    lines = [line for line in email_policy.BLOCKLIST_FILE.read_text("utf-8").splitlines() if not line.startswith("#")]
    assert lines and all(email_policy._DOMAIN_RE.match(d) for d in lines) and len(set(lines)) == len(lines)


@pytest.mark.parametrize("email, verified, problem", [
    ("", True, email_policy.INVALID_MESSAGE),
    ("no-at-sign", True, email_policy.INVALID_MESSAGE),
    ("trailing@", True, email_policy.INVALID_MESSAGE),
    (None, True, email_policy.INVALID_MESSAGE),
    ("someone@gmail.com", False, email_policy.UNVERIFIED_MESSAGE),
    ("someone@gmail.com", None, email_policy.UNVERIFIED_MESSAGE),
    ("someone@gmail.com", "true", email_policy.UNVERIFIED_MESSAGE),        # only a real True counts
    ("someone@mailinator.com", False, email_policy.DISPOSABLE_MESSAGE),
])
def test_sign_in_problem(email, verified, problem):
    assert email_policy.sign_in_problem(email, verified) == problem


def test_an_address_on_the_admin_list_is_never_refused_by_the_blocklist(monkeypatch):
    monkeypatch.setenv("JT_ADMIN_EMAILS", "boss@mailinator.com")
    assert email_policy.sign_in_problem("Boss@Mailinator.com", True) == ""
    assert email_policy.sign_in_problem("other@mailinator.com", True) == email_policy.DISPOSABLE_MESSAGE
    assert email_policy.sign_in_problem("boss@mailinator.com", False) == email_policy.UNVERIFIED_MESSAGE


def test_a_missing_list_is_logged_and_fails_closed(monkeypatch, tmp_path, capsys):
    """Never an empty list (JT-02): see test_jt02_policy_fail_closed."""
    monkeypatch.setattr(email_policy, "BLOCKLIST_FILE", tmp_path / "missing.txt")
    with pytest.raises(email_policy.PolicyUnavailable):
        email_policy.disposable_domains()
    assert "unavailable" in capsys.readouterr().err
    assert email_policy.sign_in_problem("someone@mailinator.com", True) == email_policy.UNAVAILABLE_MESSAGE


# ---------------------------------------------------------------------------
# Email + password: verified ownership before anything is granted
# ---------------------------------------------------------------------------

def test_unverified_password_account_gets_nothing_and_a_verification_link(firebase_replies):
    fake = firebase_replies(signInWithPassword=_verified_password_answer(verified=False), sendOobCode={})
    asked = []
    with pytest.raises(firebase_auth.EmailNotVerified) as e:
        firebase_auth.sign_in_with_password(EMAIL, PASSWORD, config=CONFIG, require_verified=True,
                                            may_send_verification=lambda a: asked.append(a) or True)
    assert e.value.sent and e.value.code == "EMAIL_NOT_VERIFIED" and asked == [EMAIL.lower()]
    assert fake.methods() == ["signInWithPassword", "sendOobCode"]
    assert fake.calls[1][1]["requestType"] == "VERIFY_EMAIL" and fake.calls[1][1]["idToken"]
    assert "idToken" not in str(e.value) and fake.calls[1][1]["idToken"] not in str(e.value)


def test_verification_email_is_throttled_and_failures_are_quiet(firebase_replies):
    fake = firebase_replies(signInWithPassword=_verified_password_answer(verified=False))
    with pytest.raises(firebase_auth.EmailNotVerified) as e:
        firebase_auth.sign_in_with_password(EMAIL, PASSWORD, config=CONFIG, require_verified=True,
                                            may_send_verification=lambda a: False)
    assert not e.value.sent and fake.methods() == ["signInWithPassword"]
    fake = firebase_replies(signInWithPassword=_verified_password_answer(verified=False),
                            sendOobCode=error_answer("TOO_MANY_ATTEMPTS_TRY_LATER"))
    with pytest.raises(firebase_auth.EmailNotVerified) as e:
        firebase_auth.sign_in_with_password(EMAIL, PASSWORD, config=CONFIG, require_verified=True,
                                            may_send_verification=lambda a: True)
    assert not e.value.sent


def test_verified_password_account_signs_in(firebase_replies):
    firebase_replies(signInWithPassword=_verified_password_answer())
    user = firebase_auth.sign_in_with_password(EMAIL, PASSWORD, config=CONFIG, require_verified=True)
    assert user.email_verified is True and user.provider == "password"


def test_app_refuses_an_unverified_password_account(app, google_env, firebase_replies):
    fake = firebase_replies(signInWithPassword=_verified_password_answer(verified=False), sendOobCode={})
    at = app([NEW_RECORD], secrets=GATED, owner=False)
    _login(at)
    html = unescape(_html(at))
    assert _login_page(at) and "Verify your email address first" in html and "Graduate Software Engineer" not in html
    assert ACCOUNT_KEY not in at.session_state and firebase_auth.SESSION_KEY not in at.session_state
    assert fake.methods() == ["signInWithPassword", "sendOobCode"]
    _login(at)                                       # again at once: no second email for 15 minutes
    assert fake.methods() == ["signInWithPassword", "sendOobCode", "signInWithPassword"]
    assert _login_page(at) and PASSWORD not in _dump(at)


def test_app_refuses_a_disposable_password_address_without_asking_firebase(app, google_env, firebase_replies):
    fake = firebase_replies(signInWithPassword=_verified_password_answer(email="me@mailinator.com"))
    at = app([NEW_RECORD], secrets=GATED, owner=False)
    _login(at, email="me@mailinator.com")
    assert _login_page(at) and email_policy.DISPOSABLE_MESSAGE in unescape(_html(at)) and fake.calls == []
    assert ACCOUNT_KEY not in at.session_state


def test_firebase_answering_with_a_disposable_address_is_refused(app, google_env, firebase_replies):
    """The typed address passed, but the account Firebase answered for has
    a disposable address: the answer decides."""
    firebase_replies(signInWithPassword=_verified_password_answer(email="me@yopmail.com"))
    at = app([NEW_RECORD], secrets=GATED, owner=False)
    _login(at, email="me@gmail.com")
    assert _login_page(at) and email_policy.DISPOSABLE_MESSAGE in unescape(_html(at))
    assert firebase_auth.SESSION_KEY not in at.session_state


def test_app_admits_a_verified_password_account(app, google_env, admins, firebase_replies):
    admins(BOSS)
    firebase_replies(signInWithPassword=_verified_password_answer())
    at = app([NEW_RECORD], secrets=GATED, owner=False)
    _login(at)
    assert not _login_page(at) and "Graduate Software Engineer" in _html(at)
    assert at.session_state[ACCOUNT_KEY]["email_verified"] is True and 'class="role admin"' not in _html(at)


@pytest.mark.parametrize("email, verified", [("me@mailinator.com", True), (EMAIL.lower(), False), (EMAIL.lower(), None)])
def test_a_restored_password_session_is_rechecked_every_run(app, google_env, admins, email, verified):
    """A session that already holds a password sign-in (made before this
    policy, or tampered with) is refused on its next run."""
    admins(BOSS)
    at = app([NEW_RECORD], secrets=GATED, owner=False)
    at.session_state[firebase_auth.SESSION_KEY] = {"uid": UID, "email": email, "provider": "password",
                                                   "email_verified": verified, "expires_at": time.time() + 3600}
    at.run()
    assert _login_page(at) and ACCOUNT_KEY not in at.session_state and firebase_auth.SESSION_KEY not in at.session_state


# ---------------------------------------------------------------------------
# Google sign-in: the same rule, before anything is created in Firebase
# ---------------------------------------------------------------------------

def test_google_sign_in_with_a_disposable_address_is_refused_before_firebase(app, google_env, admins):
    admins(BOSS)
    fb = google_env["firebase"](ok_google_answer(email="me@mailinator.com"))
    google_env["set_user"](FakeUser(token=_fresh_google_token(), email="me@mailinator.com"))
    at = app([NEW_RECORD], secrets=GATED, owner=False)
    html = unescape(_html(at))
    assert _login_page(at) and email_policy.DISPOSABLE_MESSAGE in html and "Graduate Software Engineer" not in html
    assert fb.requests == []                                 # no Firebase account is linked or created
    assert ACCOUNT_KEY not in at.session_state and "me@mailinator.com" not in html
    at.run()                                                 # stays refused; no retry behind the person's back
    assert _login_page(at) and fb.requests == []
    assert "btn_login_google" in _buttons(at)                # they can still choose another Google account


def test_google_identity_with_an_unverified_email_is_refused(app, google_env, admins):
    admins(BOSS)
    fb = google_env["firebase"](ok_google_answer())
    google_env["set_user"](FakeUser(token=_fresh_google_token(), email="someone@gmail.com", verified=False))
    at = app([NEW_RECORD], secrets=GATED, owner=False)
    assert _login_page(at) and "isn't verified" in unescape(_html(at)) and fb.requests == []


def test_google_sign_in_with_gmail_still_works(app, google_env, admins):
    admins(BOSS)
    fb = google_env["firebase"](ok_google_answer(email="someone@gmail.com"))
    google_env["set_user"](FakeUser(token=_fresh_google_token(), email="someone@gmail.com"))
    at = app([NEW_RECORD], secrets=GATED, owner=False)
    assert not at.exception and not _login_page(at) and "Graduate Software Engineer" in _html(at)
    assert len(fb.requests) == 1 and at.session_state[ACCOUNT_KEY]["uid"] == UID     # linked as before


def test_a_restored_google_session_with_a_disposable_address_is_refused(app, google_env, admins):
    """Signed in before the policy existed (Streamlit's 30-day cookie): the
    next page load refuses it; nothing is deleted."""
    admins(BOSS)
    google_env["firebase"](ok_google_answer())
    google_env["set_user"](FakeUser(token=_fresh_google_token(), email="old@yopmail.com"))
    at = app([NEW_RECORD], secrets=GATED, owner=False)
    assert _login_page(at) and ACCOUNT_KEY not in at.session_state


def test_the_admin_is_never_locked_out_by_the_blocklist(app, google_env, admins):
    admins("boss@mailinator.com")
    google_env["firebase"](ok_google_answer(email="boss@mailinator.com"))
    google_env["set_user"](FakeUser(token=_fresh_google_token(), email="boss@mailinator.com"))
    at = app([NEW_RECORD], secrets=GATED, owner=False)
    assert not _login_page(at) and 'class="role admin"' in _html(at)


@pytest.mark.parametrize("route", ["google", "password"])
def test_every_sign_in_route_applies_the_same_policy(app, google_env, admins, firebase_replies, route):
    admins(BOSS)
    address = "same.person@guerrillamail.com"
    if route == "google":
        fb = google_env["firebase"](ok_google_answer(email=address))
        google_env["set_user"](FakeUser(token=_fresh_google_token(), email=address))
        at = app([NEW_RECORD], secrets=GATED, owner=False)
        assert fb.requests == []
    else:
        fake = firebase_replies(signInWithPassword=_verified_password_answer(email=address))
        at = app([NEW_RECORD], secrets=GATED, owner=False)
        _login(at, email=address)
        assert fake.calls == []
    assert _login_page(at) and email_policy.DISPOSABLE_MESSAGE in unescape(_html(at))
    assert ACCOUNT_KEY not in at.session_state


# ---------------------------------------------------------------------------
# Alerts: never to a disposable address (accounts that predate the policy)
# ---------------------------------------------------------------------------

def test_personal_alerts_skip_disposable_addresses(store):
    _person(store, "uidA" * 7, "old@mailinator.com")
    _person(store, "uidB" * 7, "fine@gmail.com")
    sent = []
    stats = user_alerts.run(store, [job(1)], lambda jobs, to: sent.append(to), run_id="r1")
    assert sent == ["fine@gmail.com"] and stats["blocked_address"] == 1
    assert store.for_uid("uidA" * 7).delivery(job_key(job(1))) is None          # nothing claimed for them


# ---------------------------------------------------------------------------
# Sign-in throttling: one address's failures never lock anyone else out
# ---------------------------------------------------------------------------

def test_failures_lock_only_their_own_address():
    clock = [1000.0]
    lim = AccountAttemptLimiter(per_account=5, overall=100, window_s=900, clock=lambda: clock[0])
    for _ in range(5):
        lim.failed("Victim@Example.org")
    assert lim.locked_for("victim@example.org") > 0 and lim.locked_for("other@example.org") == 0
    clock[0] += 901
    assert lim.locked_for("victim@example.org") == 0


def test_the_overall_ceiling_still_stops_spraying():
    clock = [0.0]
    lim = AccountAttemptLimiter(per_account=5, overall=20, window_s=900, clock=lambda: clock[0])
    for i in range(20):
        lim.failed(f"user{i}@example.org")
    assert lim.locked_for("fresh@example.org") > 0
    lim2 = AccountAttemptLimiter(per_account=5, overall=20, window_s=900, clock=lambda: clock[0])
    for _ in range(4):
        lim2.failed("a@example.org")
    lim2.succeeded("A@example.org")
    assert lim2._failures == {}


def test_wrong_passwords_for_other_addresses_never_block_a_real_sign_in(app, google_env, admins, firebase_replies):
    """Before: ten wrong passwords for made-up addresses locked every email
    sign-in for 15 minutes."""
    admins(BOSS)
    firebase_replies(signInWithPassword=error_answer("INVALID_LOGIN_CREDENTIALS"))
    at = app([NEW_RECORD], secrets=GATED, owner=False)
    for i in range(16):                              # the 16th is a 6th try for attacker0
        _login(at, email=f"attacker{i % 3}@example.org", password="wrong-guess-123")
    assert "Too many failed attempts" in unescape(_html(at))          # the guessed addresses are locked
    firebase_replies(signInWithPassword=_verified_password_answer())
    _login(at)
    assert not _login_page(at) and at.session_state[ACCOUNT_KEY]["email"] == EMAIL.lower()


def test_one_address_is_locked_after_five_wrong_passwords(app, google_env, firebase_replies):
    fake = firebase_replies(signInWithPassword=error_answer("INVALID_LOGIN_CREDENTIALS"))
    at = app([NEW_RECORD], secrets=GATED, owner=False)
    for _ in range(6):
        _login(at, password="wrong-guess-123")
    assert fake.methods().count("signInWithPassword") == 5 and "Too many failed attempts" in unescape(_html(at))


# ---------------------------------------------------------------------------
# Scan details: the record, the schedule, and the admin-only section
# ---------------------------------------------------------------------------

def u(d, h, m=0, s=0):
    return datetime(2026, 10, d, h, m, s, tzinfo=timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _co(cid, checked, run=None, status="active"):
    scan = {"postings_seen": 10, "accepted": 1}
    if run is not None:
        scan["run"] = run
    return {"id": cid, "name": cid.title(), "url": f"https://{cid}.example/jobs", "status": status,
            "last_checked": _iso(checked), "scan": scan}


def _run(started, finished, found=24, failed=0, portals=2):
    return {"started_at": _iso(started), "finished_at": _iso(finished), "jobs_found": found, "new_jobs": 3,
            "portals": portals, "portals_failed": failed}


def test_latest_run_is_the_latest_completed_scan():
    old, new = _run(u(13, 3, 2), u(13, 3, 9), found=10), _run(u(13, 6, 2), u(13, 6, 8), found=24)
    companies = [_co("a", u(13, 6, 4), new), _co("b", u(13, 6, 7), new), _co("c", u(13, 3, 5), old)]
    run = scan_schedule.latest_run(companies)
    assert run["finished_at"] == u(13, 6, 8) and run["jobs_found"] == 24 and run["portals_failed"] == 0


@pytest.mark.parametrize("companies", [
    [],
    [_co("a", u(13, 6, 4))],                                                  # no record (older scanner)
    [_co("a", u(13, 9, 4)), _co("b", u(13, 6, 4), _run(u(13, 6, 2), u(13, 6, 8)))],   # a later scan left none
    [_co("a", u(13, 6, 4), {"started_at": "x", "finished_at": _iso(u(13, 6, 8)), "jobs_found": 3})],
    [_co("a", u(13, 6, 4), {**_run(u(13, 6, 2), u(13, 6, 8)), "jobs_found": -1})],
    [_co("a", u(13, 6, 4), {**_run(u(13, 6, 2), u(13, 6, 8)), "jobs_found": True})],
    [_co("a", u(13, 6, 4), {**_run(u(13, 6, 2), u(13, 6, 8)), "jobs_found": "24"})],
    [_co("a", u(13, 6, 4), _run(u(13, 6, 9), u(13, 6, 8)))],                 # finished before it started
])
def test_latest_run_never_guesses(companies):
    assert scan_schedule.latest_run(companies) is None


@pytest.mark.parametrize("now, last, state, when", [
    # 3-hour windows open at 00, 03, 06, 09 ... UTC; the chain dispatches 2 min later
    (u(13, 7, 30), u(13, 6, 2), "scheduled", u(13, 9, 2)),
    (u(13, 9, 1), u(13, 6, 2), "scheduled", u(13, 9, 2)),                  # window open, dispatch still ahead
    (u(13, 9, 20), u(13, 6, 2), "due", u(13, 9, 2)),                       # running or starting
    (u(13, 10, 40), u(13, 6, 2), "overdue", u(13, 9, 2)),                  # 90 min past the window
    (u(13, 13, 0), u(13, 6, 2), "overdue", u(13, 9, 2)),                   # still the FIRST missed window
    (u(13, 7, 30), None, "overdue", None),
])
def test_next_scan_state_follows_the_real_scheduler(now, last, state, when):
    assert scan_schedule.next_scan_state(now, 3, last) == (state, when)
    if state == "scheduled" and last and now >= scan_schedule.latest_boundary(now, 3) + scan_schedule.CHAIN_OFFSET:
        assert when == scan_schedule.next_scan(now, 3, last)                  # the same answer as the app's chips


def test_next_scan_state_uses_the_configured_interval():
    assert scan_schedule.next_scan_state(u(13, 7, 30), 6, u(13, 6, 2)) == ("scheduled", u(13, 12, 2))
    assert scan_schedule.next_scan_state(u(13, 7, 30), 1, u(13, 7, 2)) == ("scheduled", u(13, 8, 2))


def test_the_scanner_records_each_completed_scan(monkeypatch):
    import config_store
    import scraper
    saved = {}
    monkeypatch.setattr(config_store, "load_companies", lambda: [{"id": "a", "name": "A", "url": "https://a.example"},
                                                                 {"id": "b", "name": "B", "url": "https://b.example"}])
    monkeypatch.setattr(config_store, "load_seen_jobs", lambda: [])
    monkeypatch.setattr(config_store, "save_companies", lambda c, commit=False: saved.setdefault("companies", c))
    monkeypatch.setattr(config_store, "save_seen_jobs", lambda s, commit=False: saved.setdefault("seen", s))
    monkeypatch.setattr(scraper.time, "sleep", lambda s: None)

    def scan_company(c):
        s = scraper.Scan(c["name"])
        if c["id"] == "a":
            s.sources_ok = 1
            s.jobs = [{"company": "A", "title": f"Graduate Engineer {i}", "url": f"https://a.example/j/{i}"}
                      for i in range(3)]
        else:
            s.fail("failing", "listing page timed out")
        return s
    monkeypatch.setattr(scraper, "scan_company", scan_company)
    scraper.run_all()
    runs = [c["scan"]["run"] for c in saved["companies"]]
    assert runs[0] == runs[1]
    run = runs[0]
    assert (run["jobs_found"], run["new_jobs"], run["portals"], run["portals_failed"]) == (3, 3, 2, 1)
    assert run["started_at"] <= min(c["last_checked"] for c in saved["companies"]) <= run["finished_at"]
    assert scan_schedule.latest_run(saved["companies"])["jobs_found"] == 3
    import repo_sync
    assert "scan" in repo_sync.SCAN_FIELDS                    # the record travels with the existing merge


def _write_companies(tmp_path, companies):
    (tmp_path / "data" / "companies.json").write_text(json.dumps(companies), "utf-8")   # the app fixture's data folder


def _recent_scan(found=24, failed=0):
    now = datetime.now(timezone.utc)
    start = scan_schedule.latest_boundary(now, 3) + timedelta(minutes=2)
    if start > now - timedelta(minutes=1):                     # keep the scan in the past
        start -= timedelta(hours=3)
    end = start + timedelta(minutes=6)
    run = _run(start, end, found=found, failed=failed, portals=2)
    return [_co("sanofi", start + timedelta(minutes=2), run), _co("pwc", start + timedelta(minutes=5), run)], start, end


def _admin_settings(app, google_env, admins, tmp_path, companies, **kw):
    _write_companies(tmp_path, companies)
    admins(f"{BOSS}, {ADMIN}")
    google_env["firebase"](ok_google_answer())
    google_env["set_user"](FakeUser(token=_fresh_google_token()))
    return app([NEW_RECORD], secrets=GATED, owner=False, page="settings", **kw)


def _section(at) -> str:
    html = unescape(_html(at))
    start = html.find("Scan details")
    return html[start:html.find("</dl>", start)] if start >= 0 else ""


IST = timezone(timedelta(hours=5, minutes=30))


def ist_full(dt):
    """The app's format: '13 Oct 2026, 11:08 AM IST'."""
    local = dt.astimezone(IST)
    return f"{local.day} {local:%b %Y}, " + local.strftime("%I:%M %p").lstrip("0") + " IST"


def test_admin_sees_last_next_and_jobs_found(app, google_env, admins, tmp_path):
    companies, start, end = _recent_scan(found=24)
    at = _admin_settings(app, google_env, admins, tmp_path, companies)
    assert not at.exception and 'class="role admin"' in _html(at)
    section = _section(at)
    assert section, "the Scan details section is missing"
    assert "Last scan" in section and ist_full(end) in section
    assert "Jobs found" in section and '<span class="num">24</span>' in section
    state, when = scan_schedule.next_scan_state(datetime.now(timezone.utc), 3, start)
    assert state == "scheduled" and ist_full(when) in section and "IST" in section


def test_scan_details_say_unavailable_instead_of_guessing(app, google_env, admins, tmp_path):
    now = datetime.now(timezone.utc)
    legacy = [_co("sanofi", now - timedelta(minutes=30))]                 # a scan from before the record
    at = _admin_settings(app, google_env, admins, tmp_path, legacy)
    section = _section(at)
    assert "Jobs found" in section and "Unavailable" in section and "next completed scan" in section


def test_scan_details_report_failed_portals_and_overdue_scans(app, google_env, admins, tmp_path):
    long_ago = datetime.now(timezone.utc) - timedelta(hours=9)
    run = _run(long_ago, long_ago + timedelta(minutes=5), found=0, failed=2, portals=2)
    companies = [_co("sanofi", long_ago + timedelta(minutes=1), run, "failing"),
                 _co("pwc", long_ago + timedelta(minutes=2), run, "failing")]
    at = _admin_settings(app, google_env, admins, tmp_path, companies)
    section = _section(at)
    assert "no portal could be read" in section and "Overdue" in section
    assert '<span class="num">0</span>' not in section                      # never "0 jobs" for a failed scan


def test_members_never_get_scan_details(app, google_env, admins, tmp_path, monkeypatch):
    companies, _, _ = _recent_scan(found=24)
    _write_companies(tmp_path, companies)
    admins(BOSS)
    google_env["firebase"](ok_google_answer())
    google_env["set_user"](FakeUser(token=_fresh_google_token(), email="member@example.org"))
    computed = []
    import scan_schedule as ss
    real = ss.latest_run
    monkeypatch.setattr(ss, "latest_run", lambda c: computed.append(1) or real(c))
    for at in (app([NEW_RECORD], secrets=GATED, owner=False, page="settings"),
               app([NEW_RECORD], secrets=GATED, owner=False, query={"page": "settings"})):     # direct address
        assert not at.exception and not _login_page(at) and at.session_state.page == "settings"
        html = unescape(_html(at))
        assert "Scan details" not in html and "Jobs found" not in html and "set_scan_details" not in html
    assert computed == []                                  # nothing was even read for a member


def test_a_forged_admin_record_never_shows_scan_details(app, google_env, admins, tmp_path):
    companies, _, _ = _recent_scan()
    _write_companies(tmp_path, companies)
    admins(BOSS)
    google_env["firebase"](ok_google_answer())
    google_env["set_user"](FakeUser(token=_fresh_google_token(), email="member@example.org"))
    at = app([NEW_RECORD], secrets=GATED, owner=False)
    at.session_state[ACCOUNT_KEY] = {"email": BOSS, "email_verified": True, "name": "", "uid": UID,
                                     "provider": "google.com"}
    _nav(at, "settings")
    assert "Scan details" not in unescape(_html(at))


def test_visitors_and_the_legacy_public_view_never_get_scan_details(app, google_env, admins, tmp_path):
    companies, _, _ = _recent_scan()
    _write_companies(tmp_path, companies)
    admins(BOSS)
    at = app([NEW_RECORD], secrets=GATED, owner=False, query={"page": "settings"})
    assert _login_page(at) and "Scan details" not in unescape(_html(at))


def test_scan_details_start_nothing_and_call_nothing():
    """Opening Settings never starts a scan or polls GitHub: the section is
    computed from the shared copy every page already has."""
    import ast
    from test_streamlit_app import REPO
    tree = ast.parse((REPO / "streamlit_app.py").read_text("utf-8"))
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    for name in ("scan_details_rows", "scan_details_card"):
        names = {getattr(n, "id", None) or getattr(n, "attr", None) for n in ast.walk(funcs[name])}
        assert not names & {"_trigger_scrape", "create_dispatch", "_get_repo", "fetch_remote_json", "refresh_now",
                            "_refresh_data", "httpx", "requests", "get_workflow"}, name
    assert "is_owner" in {getattr(n, "id", None) for n in ast.walk(funcs["scan_details_card"])}
