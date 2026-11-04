"""Firebase revocation ends email sign-ins (Nov 2026 audit): a password change
moved Firebase's tokens-valid-after time, but an existing jt_session kept
working. A session that began before that time is now refused on restore, and
an open tab is signed out at the next account check. Google sign-ins are not
affected. Firebase and Firestore are fakes; no browser is started."""
import time
from types import SimpleNamespace

import pytest

from access import ACCOUNT_KEY
from test_firebase_auth import GATED, _login_page, google_env, make_id_token, no_env  # noqa: F401
from test_phase3_user_data import A_MAIL, A_UID, B_MAIL, B_UID, GoogleUser, _sign_in
from test_session_persistence_delete_account import JOBS, _cookie_written, _ready, _reload, _sessions, jar, store  # noqa: F401
from test_streamlit_app import _html, app  # noqa: F401
from user_store import FirebaseIdentity, MemoryBackend, UserStore, UserStoreError, session_revoked

DAY = 86400


class Identity:
    def __init__(self, valid_after=0.0):
        self.valid_after, self.fail = valid_after, False

    def account(self, uid):
        if self.fail:
            raise RuntimeError("Firebase is down")
        return {"uid": uid, "email": A_MAIL, "email_verified": True, "disabled": False,
                "tokens_valid_after": self.valid_after}


def _revoke(store, uid, when=None):
    store.identity.states[uid] = {**store.identity.states[uid],
                                  "tokens_valid_after": time.time() + 5 if when is None else when}


# ── Firebase's answer ─────────────────────────────────────────────

@pytest.mark.parametrize("millis, seconds", [(1_700_000_000_000, 1_700_000_000.0), (0, 0)])
def test_the_identity_reports_the_revocation_time_in_seconds(monkeypatch, millis, seconds):
    from firebase_admin import auth
    user = SimpleNamespace(uid=A_UID, email=A_MAIL, email_verified=True, disabled=False,
                           tokens_valid_after_timestamp=millis)
    monkeypatch.setattr(auth, "get_user", lambda uid, app=None: user)
    assert FirebaseIdentity(object()).account(A_UID)["tokens_valid_after"] == seconds


@pytest.mark.parametrize("created, valid_after, revoked", [
    (100.0, 0, False),                 # never revoked
    (100.0, None, False),              # an answer without the field
    (100.0, 200.0, True),              # began before the revocation
    (200.0, 200.0, False),             # the same second: not "before"
    (300.0, 200.0, False),             # signed in again afterwards
    (None, 200.0, True),               # unknown start, and a revocation
    (None, 0, False),
    (100.0, "garbage", True),          # an answer that can't be read fails closed
])
def test_session_revoked(created, valid_after, revoked):
    assert session_revoked(created, {"tokens_valid_after": valid_after}) is revoked


def test_no_account_is_not_called_revoked_here():
    """A deleted account is refused by the caller, for its own reason."""
    assert session_revoked(100.0, None) is False


# ── UserStore.resolve_session ─────────────────────────────────────

def _session(identity, now=1_000_000.0):
    store = UserStore(MemoryBackend(), identity)
    return store, store.for_uid(A_UID).start_session(A_MAIL, "", True, now=now)[0]


def test_a_session_from_before_the_revocation_is_refused():
    identity = Identity()
    store, cookie = _session(identity)
    now = 1_000_100.0
    assert store.resolve_session(cookie, 3600, now=now)["uid"] == A_UID
    identity.valid_after = 1_000_050.0
    assert store.resolve_session(cookie, 3600, now=now) is None


def test_a_session_from_after_the_revocation_works():
    store, cookie = _session(Identity(valid_after=999_000.0))
    assert store.resolve_session(cookie, 3600, now=1_000_100.0)["uid"] == A_UID


def test_a_revocation_ends_old_sessions_but_not_a_new_one():
    identity = Identity()
    store, old = _session(identity, now=1_000_000.0)
    new = store.for_uid(A_UID).start_session(A_MAIL, "", True, now=1_000_200.0)[0]
    identity.valid_after = 1_000_100.0
    assert store.resolve_session(old, 3600, now=1_000_300.0) is None
    assert store.resolve_session(new, 3600, now=1_000_300.0)["uid"] == A_UID


def test_a_session_without_a_start_time_is_refused_after_a_revocation():
    identity = Identity(valid_after=500.0)
    store, cookie = _session(identity)
    path = next(p for p in store.backend.docs if "/sessions/" in p)
    del store.backend.docs[path]["created_at"]
    assert store.resolve_session(cookie, 3600, now=1_000_100.0) is None


def test_if_firebase_cannot_answer_the_session_is_not_restored():
    identity = Identity()
    store, cookie = _session(identity)
    identity.fail = True
    with pytest.raises(UserStoreError):
        store.resolve_session(cookie, 3600, now=1_000_100.0)


def test_the_inactivity_and_30_day_limits_still_apply():
    store, cookie = _session(Identity())
    assert store.resolve_session(cookie, 3600, now=1_000_000.0 + 3599)["uid"] == A_UID
    assert store.resolve_session(cookie, 3600, now=1_000_000.0 + 3601) is None
    assert store.resolve_session(cookie, 90 * DAY, now=1_000_000.0 + 30 * DAY + 1) is None


# ── the app ───────────────────────────────────────────────────────

def test_a_refresh_after_a_password_change_shows_the_login_page(app, google_env, store, jar):
    _ready(store, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    jar["jt_session"] = _cookie_written(at)
    assert not _login_page(_reload(app, jar))                  # before the change: still signed in
    _revoke(store, A_UID)
    again = _reload(app, jar)
    assert not again.exception, again.exception
    assert _login_page(again) and ACCOUNT_KEY not in again.session_state
    assert A_MAIL not in _html(again)


def test_signing_in_again_after_the_change_works_and_survives_a_refresh(app, google_env, store, jar):
    _ready(store, A_UID, A_MAIL)
    _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    _revoke(store, A_UID, when=time.time() - 5)                # the change, then a new sign-in
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    jar["jt_session"] = _cookie_written(at)
    again = _reload(app, jar)
    assert not _login_page(again) and again.session_state[ACCOUNT_KEY]["uid"] == A_UID


def test_an_open_tab_is_signed_out_at_the_next_account_check(app, google_env, store, jar):
    _ready(store, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    assert len(_sessions(store, A_UID)) == 1
    _revoke(store, A_UID)
    at.run()
    assert not _login_page(at)                                 # the check runs every few minutes, not on every click
    at.session_state["_acct_state"] = {"uid": A_UID, "state": "ok", "at": time.time() - 3600}
    at.run()
    assert not at.exception, at.exception
    assert _login_page(at) and ACCOUNT_KEY not in at.session_state
    assert "Your password changed" in _html(at) and A_MAIL not in _html(at)
    assert _sessions(store, A_UID) == []                       # ended on the server too
    at.run()
    assert _login_page(at)                                     # and it stays that way


def test_a_revocation_of_one_account_leaves_the_other_signed_in(app, google_env, store, jar):
    _ready(store, A_UID, A_MAIL)
    _ready(store, B_UID, B_MAIL)
    a = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    cookie_a = _cookie_written(a)
    b = _sign_in(app, google_env, B_UID, B_MAIL, seen=JOBS)
    cookie_b = _cookie_written(b)
    _revoke(store, A_UID)
    jar["jt_session"] = cookie_a
    assert _login_page(_reload(app, jar))
    jar["jt_session"] = cookie_b
    other = _reload(app, jar)
    assert not _login_page(other) and other.session_state[ACCOUNT_KEY]["uid"] == B_UID


def test_a_google_sign_in_is_not_affected(app, google_env, store, jar):
    _ready(store, A_UID, A_MAIL)
    _revoke(store, A_UID, when=time.time() + DAY)
    google_env["set_user"](GoogleUser(token=make_id_token(project="google-issued", exp=time.time() + 3600), email=A_MAIL))
    at = app(list(JOBS), secrets=GATED, owner=False)
    assert not at.exception, at.exception
    assert not _login_page(at) and at.session_state[ACCOUNT_KEY]["provider"] == "google.com"
    at.session_state["_acct_state"] = {"uid": A_UID, "state": "ok", "at": time.time() - 3600}
    at.run()
    assert not at.exception and not _login_page(at)
