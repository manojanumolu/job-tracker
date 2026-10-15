"""An email sign-in survives a reload, and people can delete their own
account (2026-10-15).

Reload: Streamlit's session_state lives per browser connection, so an email
sign-in kept only there was lost on every refresh (Google's survived in
Streamlit's own cookie). A successful email sign-in now gets a server-side
session (the token's sha256 under the person's own data) and the browser
keeps "<uid>.<token>" in the jt_session cookie; a new connection is let in
only if that session is live AND Firebase still vouches for the account.

Delete account: from the account menu and Settings → Account management,
one flow — a typed DELETE plus a fresh proof of identity — that deletes the
person's own data and their Firebase account, and nothing else.

All MOCKED: Firestore is MemoryBackend, Firebase Auth (REST and Admin SDK)
are FakeFirebase / AccountIdentity, the browser's cookie is st.context.cookies
patched. The real-browser checks (Chromium against a local mock Identity
Toolkit and mock Google IdP) are described in the commit, not here."""
import time

import pytest
import streamlit

from access import ACCOUNT_KEY
from user_store import MemoryBackend, UserStore
from test_firebase_auth import (  # noqa: F401
    GATED, _buttons, _login, _login_page, admins, error_answer, google_env, make_id_token, no_env,
)
from test_phase3_user_data import (  # noqa: F401
    A_MAIL, A_UID, B_MAIL, B_UID, METLIFE_JOB, PWC_JOB, SUB, AccountIdentity, GoogleUser, _job_key, _password,
    _sign_in, _use_store,
)
from test_streamlit_app import _html, _nav, _seen, app  # noqa: F401

JOBS = (PWC_JOB, METLIFE_JOB)
LIMIT = 48 * 3600


@pytest.fixture
def store(monkeypatch):
    s = UserStore(MemoryBackend(), AccountIdentity())
    _use_store(monkeypatch, s)
    for uid, mail in ((A_UID, A_MAIL), (B_UID, B_MAIL)):                 # what Firebase holds for them
        s.identity.states[uid] = {"uid": uid, "email": mail, "email_verified": True, "disabled": False}
    return s


@pytest.fixture
def jar(monkeypatch):
    """The browser's cookies as the server sees them on a NEW connection."""
    cookies = {}
    monkeypatch.setattr(type(streamlit.context), "cookies", property(lambda self: cookies))
    return cookies


def _ready(store, uid, email):
    u = store.for_uid(uid)
    u.ensure_profile(email, "", True)
    u.complete_onboarding()
    u.set_watch_all(True)
    return u


def _cookie_written(at) -> str | None:
    """The value the page's cookie script sets (None: no script; "": cleared)."""
    import re
    for body in _html(at).split("\n"):
        m = re.search(r'document\.cookie = "jt_session=([^;]*);', body)
        if m:
            return m.group(1)
    return None


def _sessions(store, uid):
    return [p for p in store.backend.docs if p.startswith(f"users/{uid}/sessions/")]


def _reload(app, jar, seen=JOBS):
    """A browser refresh: a brand-new connection carrying only the cookie."""
    return app(list(seen), secrets=GATED, owner=False)


# ── an email sign-in survives a refresh ─────────────────────────────────────

def test_email_sign_in_creates_a_server_session_and_a_cookie(app, google_env, store, jar):
    _ready(store, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    value = _cookie_written(at)
    assert value and value.startswith(A_UID + ".") and len(_sessions(store, A_UID)) == 1
    token = value.split(".", 1)[1]
    doc = store.backend.docs[_sessions(store, A_UID)[0]]
    assert token not in str(doc) and doc["uid"] == A_UID and doc["provider"] == "password"   # only the hash is stored
    assert "Correct-horse" not in value and "password" not in value


def test_a_refresh_keeps_the_same_person_signed_in(app, google_env, store, jar):
    _ready(store, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    jar["jt_session"] = _cookie_written(at)
    fb = google_env["firebase"](error_answer("INVALID_LOGIN_CREDENTIALS"))     # any password call would fail
    for _ in range(3):
        again = _reload(app, jar)
        assert not _login_page(again), _html(again)[-300:]
        assert again.session_state[ACCOUNT_KEY]["uid"] == A_UID and again.session_state["_pcache"]["uid"] == A_UID
        for page in ("jobs", "companies", "alerts", "settings", "home"):
            _nav(again, page)
            assert not _login_page(again)
    assert fb.requests == []                                               # no password was ever re-sent


@pytest.mark.parametrize("bad", ["", "garbage", f"{A_UID}.", f"{A_UID}.{'x' * 43}", f"{B_UID}.{'y' * 43}",
                                 "../users/x.aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"])
def test_a_forged_or_malformed_cookie_signs_nobody_in(app, google_env, store, jar, bad):
    _ready(store, A_UID, A_MAIL)
    _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    jar["jt_session"] = bad
    at = _reload(app, jar)
    assert _login_page(at) and ACCOUNT_KEY not in at.session_state


def test_a_token_works_only_under_its_own_uid(app, google_env, store, jar):
    _ready(store, A_UID, A_MAIL)
    _ready(store, B_UID, B_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    token = _cookie_written(at).split(".", 1)[1]
    jar["jt_session"] = f"{B_UID}.{token}"                                  # A's token, B's UID
    assert _login_page(_reload(app, jar))


def test_48_hours_idle_ends_the_browser_session(app, google_env, store, jar):
    u = _ready(store, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    jar["jt_session"] = _cookie_written(at)
    key = at.session_state["_session_key"]
    u.touch_session(key, now=time.time() - LIMIT - 5)
    store.backend.docs[f"users/{A_UID}/sessions/{key}"]["last_active_at"] = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - LIMIT - 5))
    again = _reload(app, jar)
    assert _login_page(again) and _cookie_written(again) == ""                # and the stale cookie is cleared


def test_use_keeps_the_browser_session_alive(app, google_env, store, jar):
    _ready(store, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    key = at.session_state["_session_key"]
    path = f"users/{A_UID}/sessions/{key}"
    store.backend.docs[path]["last_active_at"] = "2026-01-01T00:00:00Z"
    at.session_state["_activity_saved"] = 0
    _nav(at, "jobs")
    assert store.backend.docs[path]["last_active_at"] != "2026-01-01T00:00:00Z"


def test_a_restored_session_still_obeys_the_inactivity_limit(app, google_env, store, jar):
    _ready(store, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    jar["jt_session"] = _cookie_written(at)
    again = _reload(app, jar)
    assert again.session_state["_last_active"] <= time.time()               # the server's record, not "now"
    again.session_state["_last_active"] = time.time() - LIMIT - 5
    store.for_uid(A_UID).record_activity(time.time() - LIMIT - 5)
    del again.session_state["_pcache"]
    again.run()
    assert _login_page(again)


@pytest.mark.parametrize("change", ["disabled", "deleted", "email", "unverified"])
def test_firebase_is_asked_before_a_session_is_restored(app, google_env, store, jar, change):
    _ready(store, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    jar["jt_session"] = _cookie_written(at)
    acct = store.identity.states[A_UID]
    store.identity.states[A_UID] = (None if change == "deleted" else
                                    {**acct, "disabled": True} if change == "disabled" else
                                    {**acct, "email": "someone.else@example.org"} if change == "email" else
                                    {**acct, "email_verified": False})
    assert _login_page(_reload(app, jar))
    assert _sessions(store, A_UID) == []                                    # and that session is ended


def test_sign_out_ends_the_session_for_good(app, google_env, store, jar):
    _ready(store, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    jar["jt_session"] = _cookie_written(at)
    at.button(key="btn_account_sign_out").click().run()
    assert _login_page(at) and _sessions(store, A_UID) == [] and _cookie_written(at) == ""
    again = _reload(app, jar)                                              # the old cookie replayed
    assert _login_page(again) and ACCOUNT_KEY not in again.session_state


def test_switching_accounts_in_one_browser_keeps_each_ones_own(app, google_env, store, jar):
    _ready(store, A_UID, A_MAIL).dismiss({_job_key(PWC_JOB)})
    _ready(store, B_UID, B_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    jar["jt_session"] = _cookie_written(at)
    at.button(key="btn_account_sign_out").click().run()
    google_env["firebase"](_password(B_UID, B_MAIL))
    _login(at, email=B_MAIL)
    jar["jt_session"] = _cookie_written(at)
    again = _reload(app, jar)
    assert again.session_state[ACCOUNT_KEY]["uid"] == B_UID and again.session_state["_pcache"]["uid"] == B_UID
    assert PWC_JOB["title"] in _html(again)                                # A's dismissal isn't B's


def test_google_sign_in_needs_no_app_cookie(app, google_env, store, jar):
    """Google sign-ins persist in Streamlit's own signed cookie (st.login);
    a reload with it is signed in, and no jt_session is made for them."""
    store.identity.accounts[SUB] = {"uid": A_UID, "disabled": False}
    _ready(store, A_UID, A_MAIL)
    old = make_id_token(project="google-issued", exp=time.time() - 600, iat=time.time() - 3600)
    google_env["set_user"](GoogleUser(token=old, email=A_MAIL))
    for _ in range(2):
        at = app(list(JOBS), secrets=GATED, owner=False)
        assert not _login_page(at) and at.session_state[ACCOUNT_KEY]["uid"] == A_UID
    assert _sessions(store, A_UID) == [] and not _cookie_written(at)


# ── delete account ───────────────────────────────────────────────────────────

def _open_from_menu(at):
    at.button(key="btn_account_delete").click().run()
    assert at.session_state.page == "settings" and "This permanently deletes your account." in _html(at)


def _confirm(at, word="DELETE", password=None):
    at.text_input(key="delete_confirm_text").set_value(word)
    if password is not None:
        at.text_input(key="delete_pw").set_value(password)
    at.button(key="btn_delete_confirm").click().run()


def _docs_of(store, uid):
    return [p for p in store.backend.docs if p == f"users/{uid}" or p.startswith(f"users/{uid}/")]


def test_both_entry_points_open_the_same_confirmation(app, google_env, store, jar):
    _ready(store, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    assert "btn_account_delete" in _buttons(at)                            # the account menu
    _open_from_menu(at)
    html = _html(at)
    for part in ("can’t be undone", "followed companies and dismissed jobs", "Not deleted: the shared job listings"):
        assert part in html, part
    at.button(key="btn_delete_cancel").click().run()
    assert "btn_delete_open" in _buttons(at) and "btn_delete_confirm" not in _buttons(at)
    at.button(key="btn_delete_open").click().run()                         # Settings → Account management
    assert "btn_delete_confirm" in _buttons(at) and "This permanently deletes your account." in _html(at)


def test_cancel_and_wrong_confirmation_change_nothing(app, google_env, store, jar):
    _ready(store, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    before = sorted(_docs_of(store, A_UID))
    _open_from_menu(at)
    _confirm(at, word="delete", password="Correct-horse-9")
    assert "Type DELETE to confirm." in _html(at) and sorted(_docs_of(store, A_UID)) == before
    assert store.identity.states[A_UID] is not None and not _login_page(at)
    at.button(key="btn_delete_cancel").click().run()
    assert sorted(_docs_of(store, A_UID)) == before


def test_a_wrong_password_deletes_nothing(app, google_env, store, jar):
    _ready(store, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    before = sorted(_docs_of(store, A_UID))
    _open_from_menu(at)
    google_env["firebase"](error_answer("INVALID_LOGIN_CREDENTIALS"))
    _confirm(at, password="not-my-password")
    assert "That password isn&#x27;t right." in _html(at)
    assert sorted(_docs_of(store, A_UID)) == before and store.identity.states[A_UID] is not None
    assert at.text_input(key="delete_pw").value == ""                     # wiped after the attempt


def test_deleting_removes_only_this_persons_data_and_account(app, google_env, store, jar):
    a = _ready(store, A_UID, A_MAIL)
    a.watch("pwc", {"pwc", "metlife"})
    a.dismiss({_job_key(PWC_JOB)})
    a.set_notifications(True, "general")
    b = _ready(store, B_UID, B_MAIL)
    b.dismiss({_job_key(METLIFE_JOB)})
    b_before = {p: dict(store.backend.docs[p]) for p in _docs_of(store, B_UID)}
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    jar["jt_session"] = _cookie_written(at)
    seen_before = _seen(at)
    _open_from_menu(at)
    google_env["firebase"](_password(A_UID, A_MAIL))                        # the password proves it's A
    _confirm(at, password="Correct-horse-9")
    assert _login_page(at) and "Your account and personal data were deleted." in _html(at)
    assert _docs_of(store, A_UID) == [] and store.identity.states[A_UID] is None
    assert {p: store.backend.docs[p] for p in _docs_of(store, B_UID)} == b_before    # B untouched
    assert _seen(at) == seen_before                                        # shared listings untouched
    left = at.session_state.to_dict()
    assert ACCOUNT_KEY not in left and "_pcache" not in left and "_firebase_user" not in left
    assert _cookie_written(at) == ""
    assert _login_page(_reload(app, jar))                                  # the old cookie is useless


def test_the_deleted_account_is_always_the_signed_in_one(app, google_env, store, jar):
    """Nothing on the page names the account: tampering with what the page
    holds can't redirect the deletion to someone else."""
    _ready(store, A_UID, A_MAIL)
    _ready(store, B_UID, B_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    _open_from_menu(at)
    assert {t.key for t in at.text_input if (t.key or "").startswith("delete")} == {"delete_confirm_text", "delete_pw"}
    at.session_state[ACCOUNT_KEY] = {**at.session_state[ACCOUNT_KEY], "uid": B_UID, "email": B_MAIL}
    google_env["firebase"](_password(A_UID, A_MAIL))
    _confirm(at, password="Correct-horse-9")
    assert store.identity.states[B_UID] is not None and _docs_of(store, B_UID)        # B is intact
    assert store.identity.states[A_UID] is None                                         # only A was deleted


def test_a_password_of_another_account_is_refused(app, google_env, store, jar):
    _ready(store, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    _open_from_menu(at)
    google_env["firebase"](_password(B_UID, A_MAIL))                        # Firebase answers for another UID
    _confirm(at, password="whatever-123")
    assert "belongs to a different account" in _html(at) and store.identity.states[A_UID] is not None


def test_data_removal_failing_is_reported_and_the_account_kept(app, google_env, store, jar, monkeypatch):
    _ready(store, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    _open_from_menu(at)

    def boom(paths):
        raise RuntimeError("firestore down")
    monkeypatch.setattr(store.backend, "delete_many", boom)
    store.for_uid(A_UID).watch("pwc", {"pwc"})                              # something to delete in a batch
    google_env["firebase"](_password(A_UID, A_MAIL))
    _confirm(at, password="Correct-horse-9")
    assert "Your account was NOT deleted" in _html(at) and not _login_page(at)
    assert store.identity.states[A_UID] is not None


def test_firebase_refusing_is_reported_honestly(app, google_env, store, jar, monkeypatch):
    _ready(store, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    _open_from_menu(at)

    def refuse(uid):
        raise RuntimeError("auth down")
    monkeypatch.setattr(store.identity, "delete_account", refuse)
    google_env["firebase"](_password(A_UID, A_MAIL))
    _confirm(at, password="Correct-horse-9")
    html = _html(at)
    assert "your sign-in account couldn&#x27;t be removed" in html
    assert not _login_page(at) and store.identity.states[A_UID] is not None


def test_google_account_needs_a_recent_sign_in(app, google_env, store, jar):
    store.identity.accounts[SUB] = {"uid": A_UID, "disabled": False}
    _ready(store, A_UID, A_MAIL)
    old = make_id_token(project="google-issued", exp=time.time() - 600, iat=time.time() - 3600)
    google_env["set_user"](GoogleUser(token=old, email=A_MAIL))
    at = app(list(JOBS), secrets=GATED, owner=False)
    _open_from_menu(at)
    assert "needs a recent sign-in" in _html(at) and "delete_pw" not in {t.key for t in at.text_input}
    _confirm(at)
    assert "sign out, continue with Google again" in _html(at) and store.identity.states[A_UID] is not None


def test_a_recent_google_sign_in_can_delete(app, google_env, store, jar):
    store.identity.accounts[SUB] = {"uid": A_UID, "disabled": False}
    _ready(store, A_UID, A_MAIL)
    fresh = make_id_token(project="google-issued", exp=time.time() - 1, iat=time.time() - 120)
    google_env["set_user"](GoogleUser(token=fresh, email=A_MAIL))
    at = app(list(JOBS), secrets=GATED, owner=False)
    _open_from_menu(at)
    _confirm(at)
    assert store.identity.states[A_UID] is None and _docs_of(store, A_UID) == []
    assert google_env["calls"]["logout"] == 1                             # Google's own cookie is cleared


def test_admins_cannot_delete_their_account_here(app, google_env, store, jar, admins):
    admins(A_MAIL)
    _ready(store, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    _open_from_menu(at)
    assert "Admin accounts can’t be deleted here" in _html(at) and "btn_delete_confirm" not in _buttons(at)
    assert store.identity.states[A_UID] is not None
