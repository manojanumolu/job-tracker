"""Phase 3: per-user data in Firestore, keyed only by the Firebase UID.
Firestore is replaced by user_store.MemoryBackend (same create-if-absent and
compare-and-set semantics); Google and Firebase are never contacted."""
import inspect
import json
import logging
import threading
import time

import pytest

import user_store
from access import ACCOUNT_KEY
from user_store import MemoryBackend, UserData, UserStore, UserStoreError
from test_firebase_auth import (  # noqa: F401  (fixture re-export)
    EMAIL, GATED, UID, FakeUser, _buttons, _dump, _fresh_google_token, _login, _login_page, admins, error_answer,
    google_env, make_id_token, no_env, ok_google_answer, ok_password_answer,
)
from test_streamlit_app import FRESHER_RECORD, NEW_RECORD, _html, _key, _nav, _seen, app  # noqa: F401

A_UID, B_UID = "UserAaaaaaaaaaaaaaaaaaaaaaaa1", "UserBbbbbbbbbbbbbbbbbbbbbbbb2"
A_MAIL, B_MAIL = "alice@example.org", "bob@example.org"
SUB = "google-sub-1234567890"


# ---------------------------------------------------------------------------
# user_store units
# ---------------------------------------------------------------------------

@pytest.fixture
def backend():
    return MemoryBackend()


def _user(backend, uid=A_UID, email=A_MAIL, verified=True):
    u = UserData(backend, uid)
    u.ensure_profile(email, "Alice", verified)
    return u


def test_first_sign_in_creates_the_profile_once(backend):
    u = UserData(backend, A_UID)
    profile, created = u.ensure_profile("Alice@Example.org", "Alice", True, now=1_000)
    assert created and profile["uid"] == A_UID and profile["email"] == A_MAIL
    assert profile["onboarding"] == {"status": "pending"} and profile["notifications"]["enabled"] is False
    again, created2 = UserData(backend, A_UID).ensure_profile(A_MAIL, "Alice A.", True, now=2_000)
    assert not created2 and again["created_at"] == profile["created_at"]
    assert again["display_name"] == "Alice A." and again["last_login_at"] != profile["last_login_at"]
    assert [p for p in backend.docs if p.count("/") == 1] == [f"users/{A_UID}"]          # one document


def test_concurrent_first_sign_ins_create_one_profile(backend):
    results = []
    threads = [threading.Thread(target=lambda: results.append(UserData(backend, A_UID).ensure_profile(A_MAIL)[1]))
               for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count(True) == 1 and len(results) == 8


def test_onboarding_state(backend):
    u = _user(backend)
    assert u.profile()["onboarding"]["status"] == "pending"
    u.complete_onboarding()
    assert u.profile()["onboarding"]["status"] == "complete"
    u.ensure_profile(A_MAIL)                                              # a later sign-in keeps it
    assert u.profile()["onboarding"]["status"] == "complete"


@pytest.mark.parametrize("uid", ["", "a/b", "../users/x", "users/B", "x" * 129, "__name__", "with space", None, 42])
def test_only_firebase_uids_name_a_user(backend, uid):
    with pytest.raises(ValueError):
        UserData(backend, uid)


def test_no_operation_accepts_a_uid():
    """Isolation is structural: once bound, nothing can point a UserData
    at someone else."""
    for name, fn in inspect.getmembers(UserData, inspect.isfunction):
        if not name.startswith("_"):
            params = set(inspect.signature(fn).parameters) - {"self"}
            assert not params & {"uid", "user", "user_id", "owner", "path"}, name
    assert set(inspect.signature(UserData.set_notifications).parameters) == {"self", "enabled", "mode"}


def test_two_users_have_separate_data(backend):
    a, b = _user(backend), _user(backend, B_UID, B_MAIL)
    a.dismiss({"job-1|https://x"})
    a.watch("sanofi", {"sanofi", "metlife"})
    a.set_notifications(True, "tailored")
    a.set_preferences("tailored", ["Software engineering"], ["Bengaluru"], ["fresher"])
    assert b.dismissed() == set() and b.watchlist() == set()
    assert b.notification_settings() == {"enabled": False, "mode": "general", "email": B_MAIL}
    assert b.profile()["preferences"]["job_families"] == []
    assert a.dismissed() == {"job-1|https://x"} and a.watchlist() == {"sanofi"}
    assert all(p.startswith(f"users/{A_UID}") or p.startswith(f"users/{B_UID}") for p in backend.docs)
    assert not [p for p in backend.docs if p.startswith(f"users/{B_UID}/")]


def test_hostile_keys_stay_inside_the_users_own_tree(backend):
    a, b = _user(backend), _user(backend, B_UID, B_MAIL)
    for key in (f"../{B_UID}/dismissed/x", f"users/{B_UID}/dismissed/x", "/", "a/b/c", "__x__"):
        a.dismiss({key})
    assert b.dismissed() == set() and len(a.dismissed()) == 5
    assert all(p.startswith(f"users/{A_UID}") for p in backend.docs if p.startswith(f"users/{A_UID}") or "dismissed" in p)
    a.restore({f"users/{B_UID}/dismissed/x"})
    assert len(a.dismissed()) == 4


def test_watchlist_accepts_only_catalogue_companies(backend):
    a = _user(backend)
    for bad in ("not-tracked", "", None, "../x"):
        with pytest.raises(ValueError):
            a.watch(bad, {"sanofi"})
    a.watch("sanofi", {"sanofi"})
    a.watch("sanofi", {"sanofi"})                                         # idempotent
    assert a.watchlist() == {"sanofi"}
    assert all("sanofi" not in p for p in backend.docs)                   # IDs are hashed, the catalogue isn't copied
    a.unwatch("sanofi")
    assert a.watchlist() == set()


def test_notification_address_is_the_verified_identity(backend):
    a = _user(backend)
    assert a.set_notifications(True, "general") == {"enabled": True, "mode": "general", "email": A_MAIL}
    with pytest.raises(ValueError):
        a.set_notifications(True, "everything")
    unverified = _user(backend, B_UID, B_MAIL, verified=False)
    with pytest.raises(ValueError):
        unverified.set_notifications(True)
    assert unverified.notification_settings() == {"enabled": False, "mode": "general", "email": ""}


@pytest.mark.parametrize("bad", [["x" * 81], [""], [1], "not-a-list", [f"v{i}" for i in range(51)],
                                 ["Rocket science"], ["software engineering"]])          # only the shared vocabulary
def test_preferences_are_validated(backend, bad):
    with pytest.raises(ValueError):
        _user(backend).set_preferences("tailored", job_families=bad)


def test_general_and_tailored_preferences_are_kept(backend):
    a = _user(backend)
    a.set_preferences("tailored", ["Software engineering", "Software engineering", " Testing & QA "], ["Pune"], [])
    assert a.profile()["preferences"] == {"mode": "tailored", "job_families": ["Software engineering", "Testing & QA"],
                                          "locations": ["Pune"], "experience": []}


# --- per-user delivery ledger -------------------------------------------------

def test_delivery_claim_send_finalize(backend):
    a = _user(backend)
    assert a.claim_delivery("job-1", "run-1")
    assert not a.claim_delivery("job-1", "run-2")                         # already claimed
    assert not a.finalize_delivery("job-1", "run-2", sent=True)           # only the claimant settles it
    assert a.finalize_delivery("job-1", "run-1", sent=True)
    assert a.delivery("job-1")["state"] == "sent"
    assert not a.claim_delivery("job-1", "run-3")                         # sent is final: never twice


def test_failed_send_releases_the_claim_for_a_later_run(backend):
    a = _user(backend)
    assert a.claim_delivery("job-1", "run-1")
    assert a.finalize_delivery("job-1", "run-1", sent=False)
    assert a.delivery("job-1")["state"] == "pending"
    assert a.claim_delivery("job-1", "run-2") and not a.finalize_delivery("job-1", "run-1", sent=True)
    assert a.finalize_delivery("job-1", "run-2", sent=True)


def test_concurrent_runs_claim_once(backend):
    a = _user(backend)
    wins = []
    threads = [threading.Thread(target=lambda i=i: wins.append(a.claim_delivery("job-1", f"run-{i}"))) for i in range(10)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert wins.count(True) == 1


def test_delivery_ledger_is_per_user(backend):
    a, b = _user(backend), _user(backend, B_UID, B_MAIL)
    assert a.claim_delivery("job-1", "run-1") and b.claim_delivery("job-1", "run-1")
    a.finalize_delivery("job-1", "run-1", sent=True)
    assert b.delivery("job-1")["state"] == "claimed"


# --- failures ---------------------------------------------------------------------

class BrokenBackend:
    def __getattr__(self, name):
        def fail(*a, **k):
            raise RuntimeError(f"backend exploded for users/{A_UID} with secret-ish detail")
        return fail


def test_storage_failures_surface_as_a_safe_error(caplog):
    u = UserData(BrokenBackend(), A_UID)
    with caplog.at_level(logging.WARNING, logger="user_store"):
        for call in (lambda: u.ensure_profile(A_MAIL), u.profile, u.dismissed, lambda: u.dismiss({"k"}), u.watchlist,
                     u.notification_settings, lambda: u.claim_delivery("k", "r")):
            with pytest.raises(UserStoreError) as e:
                call()
            assert str(e.value) == user_store.UNAVAILABLE
    assert "RuntimeError" in caplog.text and A_UID not in caplog.text and "secret-ish" not in caplog.text


def test_sdk_value_errors_are_storage_errors_not_validation():
    """The Firebase SDK raises ValueError for some failures (e.g. a
    transaction that couldn't commit). Those must surface as the safe
    UserStoreError — only this module's own validation is a ValueError."""
    class SdkFailure(MemoryBackend):
        def update_if(self, *a, **k):
            raise ValueError("Failed to commit transaction in 5 attempts.")
    u = UserData(SdkFailure(), A_UID)
    u.ensure_profile(A_MAIL)
    assert u.claim_delivery("k", "r1") is True                       # create path: fine
    with pytest.raises(UserStoreError):
        u.finalize_delivery("k", "r1", sent=True)
    with pytest.raises(ValueError) as e:
        u.watch("not-tracked", {"sanofi"})
    assert isinstance(e.value, user_store.InvalidInput)


def test_identity_lookup_failures_are_safe():
    class Broken:
        def google_account(self, sub):
            raise RuntimeError("boom")
    with pytest.raises(UserStoreError):
        UserStore(MemoryBackend(), Broken()).google_account(SUB)
    assert UserStore(MemoryBackend(), None).google_account(SUB) is None


SA = {"type": "service_account", "project_id": "job-tracker-test", "private_key": "-----BEGIN PRIVATE KEY-----\nnot-real\n",
      "client_email": "svc@job-tracker-test.iam.gserviceaccount.com"}


@pytest.mark.parametrize("secrets, problem", [
    ({}, ""), (None, ""), ({"auth": {}}, ""),                                          # not set up: feature off
    ({"firebase_service_account": {"type": "service_account"}}, "incomplete"),
    ({"FIREBASE_SERVICE_ACCOUNT": "{not json"}, "incomplete"),
    ({"firebase_service_account": {**SA, "project_id": "another-project"}}, "different project"),
])
def test_configuration_without_a_usable_service_account(secrets, problem):
    store, why = user_store.configure(secrets, "job-tracker-test")
    assert store is None and problem in why
    assert "not-real" not in why and "svc@" not in why


# ---------------------------------------------------------------------------
# The app with personal data switched on (an in-memory Firestore)
# ---------------------------------------------------------------------------

class FakeIdentity:
    """Firebase Authentication lookups by Google subject ID."""
    def __init__(self):
        self.accounts, self.calls = {}, []

    def google_account(self, sub):
        self.calls.append(sub)
        return self.accounts.get(sub)


class GoogleUser(FakeUser):
    def __init__(self, sub=SUB, **kw):
        super().__init__(**kw)
        if self._claims:
            self._claims["sub"] = sub


def _use_store(monkeypatch, s, problem=""):
    """As in production: a Firebase service account is configured, and
    configure() yields ``s`` (or ``problem``). The app only consults
    configure() once a service account is present."""
    monkeypatch.setattr(user_store, "configured", lambda secrets: True)
    monkeypatch.setattr(user_store, "configure", lambda secrets, project_id="": (s, problem))


@pytest.fixture
def store(monkeypatch):
    s = UserStore(MemoryBackend(), FakeIdentity())
    _use_store(monkeypatch, s)
    return s


def _password(uid, email, verified=True):
    return ok_password_answer(localId=uid, email=email, idToken=make_id_token(uid=uid, email=email),
                              emailVerified=verified)


def _sign_in(app, google_env, uid, email, seen=(NEW_RECORD,), verified=True, page=None, **kw):
    google_env["firebase"](_password(uid, email, verified))
    at = app(list(seen), secrets=GATED, owner=False, **kw)
    _login(at, email=email)
    assert not _login_page(at), _html(at)[-400:]
    if page:
        _nav(at, page)
    return at


def _profiles(store):
    return {p: d for p, d in store.backend.docs.items() if p.count("/") == 1}


def test_without_a_service_account_nothing_changes(app, google_env):
    """Production today: no per-user store, so members stay read-only and
    nothing personal is shown."""
    at = _sign_in(app, google_env, A_UID, A_MAIL)
    _nav(at, "jobs")
    at.button(key=_key("dismiss", NEW_RECORD)).click().run()
    assert "Only admins can dismiss jobs." in _html(at) and not _seen(at)[0].get("dismissed")
    for page in ("home", "email", "settings"):
        _nav(at, page)
        assert "Your account is ready" not in _html(at) and "My alerts" not in _html(at)
        assert "Personal data" not in _html(at)


def test_first_sign_in_creates_a_member_account_automatically(app, google_env, store):
    at = _sign_in(app, google_env, A_UID, A_MAIL)
    assert list(_profiles(store)) == [f"users/{A_UID}"]
    profile = _profiles(store)[f"users/{A_UID}"]
    assert profile["email"] == A_MAIL and profile["uid"] == A_UID and profile["email_verified"] is True
    assert 'class="role admin"' not in _html(at)                           # a member, no approval needed
    assert "Your account is ready" in _html(at)
    at.button(key="btn_onboarding_done").click().run()
    assert "Your account is ready" not in _html(at)
    assert store.backend.get(f"users/{A_UID}")["onboarding"]["status"] == "complete"


def test_repeat_sign_in_returns_the_same_profile(app, google_env, store):
    _sign_in(app, google_env, A_UID, A_MAIL)
    created = store.backend.get(f"users/{A_UID}")["created_at"]
    store.backend.set(f"users/{A_UID}", {"onboarding": {"status": "complete"}}, merge=True)
    at = _sign_in(app, google_env, A_UID, A_MAIL)                         # a new session
    assert list(_profiles(store)) == [f"users/{A_UID}"]
    assert store.backend.get(f"users/{A_UID}")["created_at"] == created
    assert "Your account is ready" not in _html(at)


def test_a_members_dismissal_is_theirs_alone(app, google_env, store):
    a = _sign_in(app, google_env, A_UID, A_MAIL, seen=(NEW_RECORD, FRESHER_RECORD))
    _nav(a, "jobs")
    a.button(key=_key("dismiss", NEW_RECORD)).click().run()
    assert not a.exception and "Only admins" not in _html(a)
    assert not any(r.get("dismissed") for r in _seen(a))                   # the shared history is untouched
    assert "Graduate Software Engineer" not in _html(a)                    # hidden for Alice
    b = _sign_in(app, google_env, B_UID, B_MAIL, seen=(NEW_RECORD, FRESHER_RECORD))
    _nav(b, "jobs")
    assert "Graduate Software Engineer" in _html(b)                        # Bob still sees it
    a.pills(key="jobs_tab").set_value("dismissed").run()
    a.button(key=_key("restore", NEW_RECORD)).click().run()
    assert store.for_uid(A_UID).dismissed() == set()


def test_an_admins_dismissal_also_feeds_the_shared_alerts(app, google_env, store, admins):
    admins(A_MAIL)
    a = _sign_in(app, google_env, A_UID, A_MAIL)
    assert 'class="role admin"' in _html(a)
    _nav(a, "jobs")
    a.button(key=_key("dismiss", NEW_RECORD)).click().run()
    assert _seen(a)[0]["dismissed"] is True                                 # today's email pipeline still skips it
    assert store.for_uid(A_UID).dismissed() == {_job_key(NEW_RECORD)}
    b = _sign_in(app, google_env, B_UID, B_MAIL)                           # a member's view ignores it
    _nav(b, "jobs")
    assert "Graduate Software Engineer" in _html(b)
    b.button(key=_key("dismiss", NEW_RECORD)).click().run()
    assert store.for_uid(B_UID).dismissed() == {_job_key(NEW_RECORD)}


def _job_key(j):
    from config_store import job_key
    return job_key(j)


def test_watchlists_are_per_user(app, google_env, store):
    a = _sign_in(app, google_env, A_UID, A_MAIL, query={"page": "companies", "company": "sanofi"})
    a.button(key="btn_follow").click().run()
    assert store.for_uid(A_UID).watchlist() == {"sanofi"} and "Following" in {b.label for b in a.button}
    b = _sign_in(app, google_env, B_UID, B_MAIL, query={"page": "companies", "company": "sanofi"})
    assert store.for_uid(B_UID).watchlist() == set() and "Follow" in {x.label for x in b.button}
    a.button(key="btn_follow").click().run()
    assert store.for_uid(A_UID).watchlist() == set()


def test_notification_settings_are_per_user_and_go_to_the_verified_email(app, google_env, store):
    a = _sign_in(app, google_env, A_UID, A_MAIL, page="email")
    assert "My alerts" in _html(a) and A_MAIL in _html(a)
    assert not [t for t in a.text_input if "mail" in (t.key or "")]         # no address field to type into
    a.toggle(key="my_alerts_on").set_value(True)
    a.radio(key="my_alerts_mode").set_value("tailored")
    a.button(key="btn_my_alerts").click().run()
    assert store.for_uid(A_UID).notification_settings() == {"enabled": True, "mode": "tailored", "email": A_MAIL}
    b = _sign_in(app, google_env, B_UID, B_MAIL, page="email")
    assert store.for_uid(B_UID).notification_settings()["enabled"] is False
    assert A_MAIL not in _html(b) and B_MAIL in _html(b)


def test_an_unverified_email_cannot_turn_alerts_on(app, google_env, store):
    at = _sign_in(app, google_env, A_UID, A_MAIL, verified=False, page="email")
    assert at.toggle(key="my_alerts_on").disabled
    assert store.for_uid(A_UID).notification_settings()["enabled"] is False


@pytest.mark.parametrize("query", [{"uid": B_UID}, {"user": B_UID}, {"user_id": B_UID}, {"page": "jobs", "uid": B_UID}])
def test_a_uid_from_the_url_is_ignored(app, google_env, store, query):
    store.for_uid(B_UID).ensure_profile(B_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, query=query)
    _nav(at, "jobs")
    at.button(key=_key("dismiss", NEW_RECORD)).click().run()
    assert store.for_uid(B_UID).dismissed() == set() and store.for_uid(A_UID).dismissed() == {_job_key(NEW_RECORD)}


def test_a_forged_session_uid_is_ignored(app, google_env, store):
    """Even session-state values (server-side, but treat them as untrusted)
    can't redirect another person's writes: the gate rebuilds the account
    from its source every run."""
    at = _sign_in(app, google_env, A_UID, A_MAIL)
    at.session_state[ACCOUNT_KEY] = {**at.session_state[ACCOUNT_KEY], "uid": B_UID, "email": B_MAIL}
    at.session_state["_profile_uid"] = B_UID
    _nav(at, "jobs")
    at.button(key=_key("dismiss", NEW_RECORD)).click().run()
    assert store.for_uid(B_UID).dismissed() == set() and store.for_uid(A_UID).dismissed() == {_job_key(NEW_RECORD)}
    assert f"users/{B_UID}" not in _profiles(store)


def test_visitors_stay_blocked_with_personal_data_on(app, google_env, store):
    at = app([NEW_RECORD], secrets=GATED, owner=False, query={"uid": A_UID})
    assert _login_page(at) and "Graduate Software Engineer" not in _html(at) and store.backend.docs == {}


def test_admin_stays_admin_and_member_stays_member(app, google_env, store, admins, monkeypatch):
    import notifier
    sent = []
    monkeypatch.setattr(notifier, "test_mail", lambda to: sent.append(to))
    monkeypatch.setenv("ALERT_RECIPIENT", "alerts@example.org")
    admins(A_MAIL)
    a = _sign_in(app, google_env, A_UID, A_MAIL, page="email")
    a.button(key="btn_test").click().run()
    assert sent == ["alerts@example.org"]
    b = _sign_in(app, google_env, B_UID, B_MAIL, page="email")
    b.button(key="btn_test").click().run()
    assert sent == ["alerts@example.org"] and "Only admins can send test emails." in _html(b)
    _nav(b, "settings")
    b.button(key="btn_clear_all").click().run()
    assert "btn_clear_all_confirm" not in _buttons(b) and not any(r.get("dismissed") for r in _seen(b))


# --- Google: the uid when the 1-hour token is too old to link -----------------

def test_returning_google_user_gets_their_uid_from_firebase(app, google_env, store):
    store.identity.accounts[SUB] = {"uid": A_UID, "disabled": False}
    fb = google_env["firebase"](ok_google_answer())
    google_env["set_user"](GoogleUser(token=make_id_token(project="google-issued", exp=time.time() - 600), email=A_MAIL))
    at = app([NEW_RECORD], secrets=GATED, owner=False)
    assert not _login_page(at) and fb.requests == []                       # nothing stale sent to Firebase
    assert at.session_state[ACCOUNT_KEY]["uid"] == A_UID and f"users/{A_UID}" in _profiles(store)
    at.run()
    assert store.identity.calls == [SUB]                                   # looked up by Google's ID, once


def test_disabled_firebase_account_is_refused(app, google_env, store):
    """The owner blocks someone by disabling their Firebase account."""
    store.identity.accounts[SUB] = {"uid": A_UID, "disabled": True}
    google_env["set_user"](GoogleUser(token=make_id_token(project="google-issued", exp=time.time() - 600), email=A_MAIL))
    at = app([NEW_RECORD], secrets=GATED, owner=False)
    assert _login_page(at) and "This account has been disabled." in _html(at)
    assert "Graduate Software Engineer" not in _html(at) and ACCOUNT_KEY not in at.session_state


def test_disabled_account_is_refused_at_a_fresh_google_sign_in(app, google_env):
    """Even without personal data: Firebase refuses a disabled account when
    the fresh Google sign-in is linked, and so does the app."""
    google_env["firebase"](error_answer("USER_DISABLED"))
    google_env["set_user"](GoogleUser(token=_fresh_google_token(), email=A_MAIL))
    at = app([NEW_RECORD], secrets=GATED, owner=False)
    assert _login_page(at) and "This account has been disabled." in _html(at)


def test_google_user_without_a_firebase_account_gets_no_personal_data(app, google_env, store):
    google_env["set_user"](GoogleUser(token=make_id_token(project="google-issued", exp=time.time() - 600), email=A_MAIL))
    at = app([NEW_RECORD], secrets=GATED, owner=False, page="settings")
    assert not _login_page(at) and store.backend.docs == {}
    assert "needs a linked Firebase account" in _html(at)


# --- Firestore failures fail safely -------------------------------------------------

def test_firestore_outage_never_falls_back_to_shared_data(app, google_env, monkeypatch, admins):
    broken = UserStore(BrokenBackend(), FakeIdentity())
    _use_store(monkeypatch, broken)
    at = _sign_in(app, google_env, A_UID, A_MAIL)
    assert not at.exception and "Graduate Software Engineer" in _html(at)  # the app still works
    _nav(at, "jobs")
    at.button(key=_key("dismiss", NEW_RECORD)).click().run()
    assert user_store.UNAVAILABLE in _html(at) and not _seen(at)[0].get("dismissed")
    _nav(at, "settings")
    assert user_store.UNAVAILABLE in _html(at)


def test_a_store_that_cannot_start_fails_safely(app, google_env, monkeypatch, admins):
    _use_store(monkeypatch, None, "Firestore couldn't be started")
    admins(A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL)
    assert not at.exception and 'class="role admin"' in _html(at)
    _nav(at, "jobs")
    at.button(key=_key("dismiss", NEW_RECORD)).click().run()
    assert _seen(at)[0]["dismissed"] is True                               # admins keep today's shared behaviour
    _nav(at, "settings")
    assert user_store.UNAVAILABLE in _html(at)


def test_sign_out_then_another_person_uses_their_own_data(app, google_env, store):
    at = _sign_in(app, google_env, A_UID, A_MAIL)
    _nav(at, "jobs")
    at.button(key=_key("dismiss", NEW_RECORD)).click().run()
    at.button(key="btn_account_sign_out").click().run()
    google_env["firebase"](_password(B_UID, B_MAIL))
    _login(at, email=B_MAIL)
    _nav(at, "jobs")
    assert "Graduate Software Engineer" in _html(at) and at.session_state["_profile_uid"] == B_UID
    assert A_UID not in json.dumps(at.session_state.to_dict(), default=str)


# --- the real Admin SDK wiring (runs where firebase-admin is installed, e.g. CI) ---

def test_a_bad_service_account_key_fails_safely():
    pytest.importorskip("firebase_admin")
    store, why = user_store.configure({"firebase_service_account": SA}, "job-tracker-test")
    assert store is None and why == "Firestore couldn't be started" and "not-real" not in why


def test_firestore_backend_create_is_create_if_absent():
    exceptions = pytest.importorskip("google.api_core.exceptions")

    class Doc:
        def __init__(self, store, path):
            self.store, self.path = store, path

        def create(self, data):
            if self.path in self.store:
                raise exceptions.AlreadyExists("exists")
            self.store[self.path] = data

    class Client:
        def __init__(self):
            self.store = {}

        def document(self, path):
            return Doc(self.store, path)

    backend = user_store.FirestoreBackend(Client())
    assert backend.create(f"users/{A_UID}", {"a": 1}) is True
    assert backend.create(f"users/{A_UID}", {"a": 2}) is False and backend.client.store[f"users/{A_UID}"] == {"a": 1}


def test_firestore_rules_deny_all_client_access():
    from test_streamlit_app import REPO
    rules = (REPO / "firestore.rules").read_text("utf-8")
    assert "allow read, write: if false;" in rules and "if true" not in rules and "request.auth" not in rules


# ---------------------------------------------------------------------------
# Phase 3b: personal views, account state, admin non-access
# ---------------------------------------------------------------------------

PWC_JOB = {**NEW_RECORD, "company_id": "pwc"}                                    # Software, Pune, ENTRY_LEVEL
METLIFE_JOB = {**FRESHER_RECORD, "company_id": "metlife", "title": "Trainee Data Analyst"}   # Data, Hyderabad


class AccountIdentity(FakeIdentity):
    """FakeIdentity that also answers Firebase account state by UID."""
    def __init__(self):
        super().__init__()
        self.states = {}

    def account(self, uid):
        return self.states.get(uid, {"uid": uid, "email": "", "email_verified": True, "disabled": False})


@pytest.fixture
def store2(monkeypatch):
    s = UserStore(MemoryBackend(), AccountIdentity())
    _use_store(monkeypatch, s)
    return s


def test_views_follow_the_persons_company_scope(app, google_env, store2):
    a = _sign_in(app, google_env, A_UID, A_MAIL, seen=(PWC_JOB, METLIFE_JOB))
    store2.for_uid(A_UID).watch("metlife", {"metlife", "pwc"})
    store2.for_uid(A_UID).set_watch_all(False)
    _nav(a, "jobs")
    html = _html(a)
    assert "Trainee Data Analyst" in html and "Graduate Software Engineer" not in html
    assert "Showing 1 followed company" in html
    b = _sign_in(app, google_env, B_UID, B_MAIL, seen=(PWC_JOB, METLIFE_JOB), page="jobs")
    assert "Trainee Data Analyst" in _html(b) and "Graduate Software Engineer" in _html(b)


def test_views_follow_tailored_preferences(app, google_env, store2):
    a = _sign_in(app, google_env, A_UID, A_MAIL, seen=(PWC_JOB, METLIFE_JOB))
    store2.for_uid(A_UID).set_preferences("tailored", ["Software engineering"], [], [])
    store2.for_uid(A_UID).set_notifications(False, "tailored")
    _nav(a, "jobs")
    assert "Graduate Software Engineer" in _html(a) and "Trainee Data Analyst" not in _html(a)
    assert "your tailored preferences" in _html(a)


def test_my_alerts_saves_scope_and_preferences(app, google_env, store2):
    a = _sign_in(app, google_env, A_UID, A_MAIL, page="email")
    a.toggle(key="my_alerts_on").set_value(True)
    a.radio(key="my_alerts_scope").set_value("followed")
    a.radio(key="my_alerts_mode").set_value("tailored")
    a.multiselect(key="my_pref_families").set_value(["Data & analytics"])
    a.multiselect(key="my_pref_locations").set_value(["Hyderabad", "Remote"])
    a.multiselect(key="my_pref_experience").set_value(["fresher"])
    a.button(key="btn_my_alerts").click().run()
    view = store2.for_uid(A_UID).personal_view()
    assert view["watch_all"] is False and view["mode"] == "tailored"
    assert view["prefs"] == {"job_families": ["Data & analytics"], "locations": ["Hyderabad", "Remote"],
                             "experience": ["fresher"]}
    assert view["notifications"]["enabled"] is True and view["notifications"]["enabled_at"]
    assert store2.for_uid(B_UID).profile() is None                        # nobody else touched


def test_admin_dismiss_all_still_counts_every_job(app, google_env, store2, admins):
    admins(A_MAIL)
    a = _sign_in(app, google_env, A_UID, A_MAIL, seen=(PWC_JOB, METLIFE_JOB))
    store2.for_uid(A_UID).set_watch_all(False)                            # follows nothing -> sees nothing
    _nav(a, "settings")
    assert "btn_clear_all" in _buttons(a) and "Dismiss all 2 jobs" in {b.label for b in a.button}


def test_a_disabled_account_is_signed_out_within_minutes(app, google_env, store2):
    a = _sign_in(app, google_env, A_UID, A_MAIL)
    store2.identity.states[A_UID] = {"uid": A_UID, "email": A_MAIL, "email_verified": True, "disabled": True}
    a.run()
    assert not _login_page(a)                                             # still within the check interval
    a.session_state["_acct_state"] = {**a.session_state["_acct_state"], "at": 0}   # the interval has passed
    a.run()
    assert _login_page(a) and "This account has been disabled." in _html(a)
    _login(a, email=A_MAIL)                                               # signing in again doesn't help
    assert _login_page(a)


def test_a_deleted_account_gets_no_personal_data(app, google_env, store2):
    store2.identity.states[A_UID] = None                                  # deleted in Firebase
    a = _sign_in(app, google_env, A_UID, A_MAIL)
    assert f"users/{A_UID}" not in _profiles(store2)                       # nothing written for a deleted UID
    _nav(a, "jobs")
    a.button(key=_key("dismiss", NEW_RECORD)).click().run()
    assert "Your account was removed" in _html(a) and store2.backend.docs == {}
    assert not _seen(a)[0].get("dismissed")


def test_unknown_account_state_never_blocks(app, google_env, monkeypatch):
    """If Firebase can't be asked, nobody is locked out and nothing changes."""
    class Down(AccountIdentity):
        def account(self, uid):
            raise RuntimeError("firebase down")
    s = UserStore(MemoryBackend(), Down())
    _use_store(monkeypatch, s)
    a = _sign_in(app, google_env, A_UID, A_MAIL)
    assert not _login_page(a) and f"users/{A_UID}" in _profiles(s)


def test_admin_rights_never_reach_another_persons_data(app, google_env, store2, admins):
    store2.for_uid(B_UID).ensure_profile(B_MAIL)
    store2.for_uid(B_UID).dismiss({"secret-job|https://b.example"})
    admins(A_MAIL)
    a = _sign_in(app, google_env, A_UID, A_MAIL, query={"uid": B_UID, "user": B_UID})
    assert 'class="role admin"' in _html(a)
    for page in ("home", "jobs", "email", "settings"):
        _nav(a, page)
        assert B_MAIL not in _html(a) and "secret-job" not in _html(a) and B_UID not in _html(a)
    _nav(a, "jobs")
    a.button(key=_key("dismiss", NEW_RECORD)).click().run()
    assert store2.for_uid(B_UID).dismissed() == {"secret-job|https://b.example"}


def test_the_app_binds_personal_data_in_exactly_one_place():
    """Static guard: the only UserData the app ever creates is for the
    gate's own account UID; nothing else in the app names a uid path."""
    import re
    from test_streamlit_app import REPO
    src = (REPO / "streamlit_app.py").read_text("utf-8")
    assert re.findall(r"for_uid\((.*?)\)", src) == ['ACCOUNT["uid"]']
    assert "users/" not in src and "UserData(" not in src
