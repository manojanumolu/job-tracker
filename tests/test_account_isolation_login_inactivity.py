"""New accounts start clean, account switching, the sign-in form's feedback,
and the 48-hour inactivity sign-out (2026-10-15 production report).

The report: a brand-new account showed 35 old listings and "Dismiss all
(35)". Those are the scanner's SHARED listings (data/seen_jobs.json), which
every member sees by design; nothing personal carried over. Dismissals,
preferences, follows and alerts are per Firebase UID (users/{uid} in
Firestore), and this browser session's copy of them (_pcache) is keyed by
that UID. These tests pin that, plus:
  * Sign out empties the session; signing in as someone else in the same
    tab loads only their data.
  * The sign-in form keeps the email (and, during the attempt, the
    password), shows "Signing in…" with the button disabled while Firebase is
    asked, and wipes the password once the attempt is over.
  * 48 consecutive hours without use end a sign-in, judged from server-side
    records (this session's state, the profile's last_active_at), never a
    browser value, and never a fixed time from sign-in.

All MOCKED: Firestore is user_store.MemoryBackend, Firebase Auth is the
tests' FakeFirebase, Google's st.user/st.login/st.logout are fakes. Nothing
reaches Google or Firebase."""
import sys
import time

import pytest

from access import ACCOUNT_KEY
from user_store import MemoryBackend, UserStore
from test_firebase_auth import (  # noqa: F401
    GATED, _buttons, _login, _login_page, admins, error_answer, google_env, make_id_token, no_env,
)
from test_phase3_user_data import (  # noqa: F401
    A_MAIL, A_UID, B_MAIL, B_UID, METLIFE_JOB, PWC_JOB, SUB, AccountIdentity, GoogleUser, _job_key, _password,
    _sign_in, _use_store,
)
from test_streamlit_app import _html, _key, _nav, _seen, app  # noqa: F401

JOBS = (PWC_JOB, METLIFE_JOB)
SHARED = "discover your next <em>opportunity</em>"
LIMIT = 48 * 3600


@pytest.fixture
def store(monkeypatch):
    s = UserStore(MemoryBackend(), AccountIdentity())
    _use_store(monkeypatch, s)
    return s


def _ready(store, uid, email):
    u = store.for_uid(uid)
    u.ensure_profile(email, "", True)
    u.complete_onboarding()
    return u


def _sign_out(at):
    at.button(key="btn_account_sign_out").click().run()
    assert _login_page(at)


def _switch_to(at, google_env, uid, email):
    """Sign in as someone else in the SAME browser session."""
    google_env["firebase"](_password(uid, email))
    _login(at, email=email)
    assert not _login_page(at), _html(at)[-300:]
    if "btn_onboarding_skip" in _buttons(at):
        at.button(key="btn_onboarding_skip").click().run()


# ── a new account starts clean ───────────────────────────────────────────────

def test_a_new_account_gets_a_fresh_profile_and_sees_only_shared_listings(app, google_env, store):
    alice = _ready(store, A_UID, A_MAIL)                         # someone already using the app
    alice.set_preferences("general", [], ["Hyderabad"], [])
    alice.watch("metlife", {"metlife", "pwc"})
    alice.set_watch_all(False)
    alice.dismiss({_job_key(PWC_JOB)})
    alice.set_notifications(True, "general")

    b = _sign_in(app, google_env, B_UID, B_MAIL, seen=JOBS)      # a brand-new account
    prof = store.for_uid(B_UID).profile()
    assert prof["uid"] == B_UID and prof["email"] == B_MAIL
    assert prof["preferences"]["locations"] == [] and prof["watch_all"] is False
    assert prof["notifications"]["enabled"] is False
    assert store.for_uid(B_UID).dismissed() == set() and store.for_uid(B_UID).watchlist() == set()
    html = _html(b)
    # the shared listings, said plainly: not "matches", not Alice's view
    assert SHARED in html and "the same listings every member sees" in html
    assert PWC_JOB["title"] in html and METLIFE_JOB["title"] in html          # Alice's dismissal isn't Bob's
    assert {b_.key: b_.label for b_ in b.button}["btn_mh_clear"] == "Dismiss all (2)"
    assert "Hyderabad" not in html.split('class="mh-card"', 1)[1].split("</ul>", 1)[0]
    assert b.session_state["_pcache"]["uid"] == B_UID
    assert store.for_uid(A_UID).dismissed() == {_job_key(PWC_JOB)}            # Alice untouched
    assert not any(r.get("dismissed") for r in _seen(b))                      # the shared file untouched


# ── switching accounts in one browser session ───────────────────────────────

def test_switching_from_a_to_b_carries_nothing_over(app, google_env, store):
    _ready(store, A_UID, A_MAIL)
    _ready(store, B_UID, B_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    at.button(key=_key("dismiss", PWC_JOB)).click().run()                     # A dismisses a job...
    store.for_uid(A_UID).set_preferences("general", [], ["Hyderabad"], [])     # ...and narrows Home
    at.run()
    assert store.for_uid(A_UID).dismissed() == {_job_key(PWC_JOB)}
    assert at.session_state["_pcache"]["uid"] == A_UID

    _sign_out(at)
    left = at.session_state.to_dict()
    for key in ("_pcache", "_pcache_next", "_profile_uid", "_last_active", "_activity_saved", ACCOUNT_KEY,
                "_firebase_user"):
        assert key not in left, key
    assert not left.get("login_pw") and not left.get("login_email")          # the form is drawn again, empty

    _switch_to(at, google_env, B_UID, B_MAIL)
    html = _html(at)
    assert at.session_state[ACCOUNT_KEY]["uid"] == B_UID and at.session_state["_pcache"]["uid"] == B_UID
    assert at.session_state["_profile_uid"] == B_UID
    assert PWC_JOB["title"] in html and METLIFE_JOB["title"] in html          # A's dismissal isn't B's
    assert SHARED in html                                                    # A's preferences aren't B's
    assert A_MAIL not in html
    assert store.for_uid(B_UID).dismissed() == set()

    _sign_out(at)                                                            # and back to A: A's own data
    _switch_to(at, google_env, A_UID, A_MAIL)
    html = _html(at)
    assert METLIFE_JOB["title"] in html and PWC_JOB["title"] not in html
    assert "here are your <em>matches</em>" in html


def test_a_cached_copy_of_another_uid_is_never_shown(app, google_env, store):
    """Defence in depth: even a leftover copy of someone else's data in this
    session (which sign-out prevents) is read again for the right UID."""
    _ready(store, A_UID, A_MAIL).dismiss({_job_key(PWC_JOB), _job_key(METLIFE_JOB)})
    _ready(store, B_UID, B_MAIL)
    at = _sign_in(app, google_env, B_UID, B_MAIL, seen=JOBS)
    at.session_state["_pcache"] = {"uid": A_UID, "at": time.time(), "profile": store.for_uid(A_UID).profile(),
                                   "dismissed": store.for_uid(A_UID).dismissed(), "watchlist": set()}
    at.run()
    assert at.session_state["_pcache"]["uid"] == B_UID
    assert PWC_JOB["title"] in _html(at) and METLIFE_JOB["title"] in _html(at)


# ── the sign-in form ─────────────────────────────────────────────────────────

def test_while_firebase_is_asked_the_form_shows_signing_in_and_keeps_its_fields(app, google_env, store, monkeypatch):
    """The page as it stands DURING the request (the fake Firebase stops the
    run there): "Signing in…", the button disabled, both fields still
    filled — nothing cleared on click."""
    seen = {}

    def frozen(email, password, **kw):
        seen["email"], seen["password"] = email, password
        raise KeyboardInterrupt("stop the run mid-request")
    monkeypatch.setattr(sys.modules["firebase_auth"], "sign_in_with_password", frozen)
    at = app([], secrets=GATED, owner=False)
    at.text_input(key="login_email").set_value(A_MAIL)
    at.text_input(key="login_pw").set_value("Correct-horse-9")
    at.button(key="btn_login_email").click().run()
    assert seen == {"email": A_MAIL, "password": "Correct-horse-9"}
    assert "Signing in…" in _html(at) and 'class="login-busy"' in _html(at)
    button = next(b for b in at.button if b.key == "btn_login_email")
    google = next(b for b in at.button if b.key == "btn_login_google")
    assert button.disabled and google.disabled                                 # no second submission
    assert at.text_input(key="login_email").value == A_MAIL
    assert at.text_input(key="login_pw").value == "Correct-horse-9"


def test_a_failed_sign_in_keeps_the_email_and_wipes_the_password(app, google_env):
    fb = google_env["firebase"](error_answer("INVALID_LOGIN_CREDENTIALS"))
    at = app([], secrets=GATED, owner=False)
    _login(at, email=A_MAIL, password="wrong-password-123")
    assert _login_page(at) and len(fb.requests) == 1
    assert 'class="login-err"' in _html(at) and "Signing in…" not in _html(at)
    assert at.text_input(key="login_email").value == A_MAIL                   # no retyping the address
    assert at.text_input(key="login_pw").value == ""                         # the attempt is over
    assert not next(b for b in at.button if b.key == "btn_login_email").disabled
    assert "wrong-password-123" not in str(at.session_state.to_dict())


def test_empty_fields_are_refused_without_asking_firebase(app, google_env):
    fb = google_env["firebase"](error_answer("INVALID_LOGIN_CREDENTIALS"))
    at = app([], secrets=GATED, owner=False)
    at.text_input(key="login_email").set_value(A_MAIL)
    at.button(key="btn_login_email").click().run()
    assert fb.requests == [] and "Enter your email address and password." in _html(at)
    assert at.text_input(key="login_email").value == A_MAIL


def test_a_successful_sign_in_opens_the_app_and_keeps_no_password(app, google_env, store):
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    left = at.session_state.to_dict()
    assert "login_pw" not in left and "_login_pending" not in left and "_login_clear_pw" not in left
    assert 'class="mh-hero"' in _html(at.run()) or 'class="hero mh-hero"' in _html(at)


def test_continue_with_google_still_only_starts_google(app, google_env):
    at = app([], secrets=GATED, owner=False)
    at.button(key="btn_login_google").click().run()
    assert google_env["calls"]["login"] and "Signing in…" not in _html(at)


# ── 48-hour inactivity ───────────────────────────────────────────────────────

def test_use_keeps_a_sign_in_alive_and_is_recorded_on_the_server(app, google_env, store):
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    stamp = store.for_uid(A_UID).profile().get("last_active_at")
    assert stamp                                                            # recorded on the person's profile
    _idle_for(at, store, LIMIT - 120)                                        # idle for 47 h 58 min...
    at.session_state["_activity_saved"] = 0
    _nav(at, "jobs")                                                         # ...then a click
    assert not _login_page(at) and time.time() - at.session_state["_last_active"] < 30
    fb = at.session_state["_firebase_user"]
    assert fb["expires_at"] == pytest.approx(time.time() + LIMIT, abs=60)     # the email sign-in moves on with use


def _idle_for(at, store, seconds):
    """Everything the server knows says the last use was ``seconds`` ago."""
    store.for_uid(A_UID).record_activity(time.time() - seconds)
    at.session_state["_last_active"] = time.time() - seconds
    del at.session_state["_pcache"]                                          # this session reads it again


def test_48_hours_idle_in_an_open_tab_signs_out(app, google_env, store):
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    _idle_for(at, store, LIMIT + 5)
    at.run()
    assert _login_page(at) and "signed out after 2 days without activity" in _html(at)
    left = at.session_state.to_dict()
    assert ACCOUNT_KEY not in left and "_pcache" not in left and "_firebase_user" not in left
    assert PWC_JOB["title"] not in _html(at)


def test_a_restored_google_sign_in_idle_for_48_hours_must_sign_in_again(app, google_env, store):
    store.identity.accounts[SUB] = {"uid": A_UID, "disabled": False}
    _ready(store, A_UID, A_MAIL).record_activity(time.time() - LIMIT - 3600)   # last used 49 h ago
    google_env["firebase"](error_answer("INVALID_IDP_RESPONSE"))
    old = make_id_token(project="google-issued", exp=time.time() - 600, iat=time.time() - 5 * 86400)
    google_env["set_user"](GoogleUser(token=old, email=A_MAIL))              # Streamlit's 30-day cookie
    at = app(list(JOBS), secrets=GATED, owner=False)
    assert google_env["calls"]["logout"] == 1                               # Google's cookie is cleared too
    at.run()
    assert _login_page(at)
    assert PWC_JOB["title"] not in _html(at)


def test_a_restored_google_sign_in_used_recently_stays(app, google_env, store):
    store.identity.accounts[SUB] = {"uid": A_UID, "disabled": False}
    _ready(store, A_UID, A_MAIL).record_activity(time.time() - 20 * 3600)   # used yesterday
    old = make_id_token(project="google-issued", exp=time.time() - 600, iat=time.time() - 5 * 86400)
    google_env["set_user"](GoogleUser(token=old, email=A_MAIL))              # signed in 5 days ago
    at = app(list(JOBS), secrets=GATED, owner=False)
    assert not _login_page(at) and google_env["calls"]["logout"] == 0        # not a fixed limit from sign-in


def test_a_fresh_google_sign_in_after_a_long_absence_is_let_in(app, google_env, store):
    from test_firebase_auth import ok_google_answer
    store.identity.accounts[SUB] = {"uid": A_UID, "disabled": False}
    _ready(store, A_UID, A_MAIL).record_activity(time.time() - 10 * 86400)
    google_env["firebase"](ok_google_answer(localId=A_UID, email=A_MAIL))
    fresh = make_id_token(project="google-issued", iat=time.time() - 5)
    google_env["set_user"](GoogleUser(token=fresh, email=A_MAIL))
    at = app(list(JOBS), secrets=GATED, owner=False)
    assert not _login_page(at) and google_env["calls"]["logout"] == 0


def test_no_activity_record_is_never_treated_as_inactivity(app, google_env, store):
    """Before anything is recorded (or without per-user data) inactivity
    can't be proven: the sign-in is kept and recording starts."""
    store.identity.accounts[SUB] = {"uid": A_UID, "disabled": False}
    _ready(store, A_UID, A_MAIL)
    old = make_id_token(project="google-issued", exp=time.time() - 600, iat=time.time() - 5 * 86400)
    google_env["set_user"](GoogleUser(token=old, email=A_MAIL))
    at = app(list(JOBS), secrets=GATED, owner=False)
    assert not _login_page(at) and store.for_uid(A_UID).profile().get("last_active_at")


def test_a_browser_value_cannot_postpone_the_limit(app, google_env, store):
    """The only inputs are server-side; a future timestamp (as if forged) is ignored."""
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    _idle_for(at, store, LIMIT + 5)
    store.for_uid(A_UID).record_activity(time.time() + 30 * 86400)          # a forged future stamp
    at.run()
    assert _login_page(at)


def test_sign_out_is_immediate(app, google_env, store):
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    _sign_out(at)
    assert "signed out after 2 days" not in _html(at)
    at.run()
    assert _login_page(at)
