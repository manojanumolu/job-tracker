"""Firebase Authentication spike. Firebase is never contacted: every REST
call goes through an httpx.MockTransport that answers like Identity Toolkit."""
import base64
import os
from html import unescape
import json
import logging
import time

import httpx
import pytest

import firebase_auth
from firebase_auth import FirebaseAuthError, FirebaseConfig, FirebaseConfigError
import notifier
from access import ACCOUNT_KEY
from test_streamlit_app import NEW_RECORD, OWNER_PASSWORD, _html, _key, _nav, _seen, app  # noqa: F401  (fixture re-export)

API_KEY = "test-placeholder-firebase-web-api-key"   # deliberately not shaped like a real Google key
PROJECT = "job-tracker-test"
OTHER_PROJECT = "movie-ticket-radar"
UID = "Xf3kQ9bLm2RzT8vW1yNcA7pD4eH5"
EMAIL = "Person@Example.com"
PASSWORD = "s3cret-Firebase-pw"
GOOGLE_ID_TOKEN = "google.id.token-from-st-login"
CONFIG = FirebaseConfig(API_KEY, PROJECT)


def _b64(obj) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


def make_id_token(project=PROJECT, uid=UID, provider="password", exp=None, **extra) -> str:
    claims = {"iss": f"https://securetoken.google.com/{project}", "aud": project, "sub": uid,
              "user_id": uid, "exp": exp if exp is not None else time.time() + 3600,
              "email": EMAIL.lower(), "firebase": {"sign_in_provider": provider}, **extra}
    return f"{_b64({'alg': 'RS256'})}.{_b64(claims)}.signature"


def ok_password_answer(**over):
    return {"kind": "identitytoolkit#VerifyPasswordResponse", "localId": UID, "email": EMAIL,
            "idToken": make_id_token(), "refreshToken": "refresh-token-xyz", "expiresIn": "3600",
            "registered": True, **over}


def ok_google_answer(**over):
    return {"localId": UID, "email": EMAIL, "providerId": "google.com", "emailVerified": True,
            "idToken": make_id_token(provider="google.com"), "refreshToken": "refresh-token-xyz",
            "oauthIdToken": "raw-google-token", **over}


def error_answer(message, status=400):
    return httpx.Response(status, json={"error": {"code": status, "message": message}})


class FakeFirebase:
    """Records requests; answers with ``reply`` (a dict -> 200 JSON, or an
    httpx.Response, or an exception to raise)."""
    def __init__(self, reply):
        self.reply, self.requests = reply, []

    def handler(self, request: httpx.Request):
        self.requests.append(request)
        if isinstance(self.reply, Exception):
            raise self.reply
        if isinstance(self.reply, httpx.Response):
            return self.reply
        return httpx.Response(200, json=self.reply)

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handler))

    def body(self, i=-1):
        return json.loads(self.requests[i].content)


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key, project", [("", ""), (API_KEY, ""), ("", PROJECT), ("  ", "  ")])
def test_missing_configuration(monkeypatch, key, project):
    monkeypatch.setenv("FIREBASE_WEB_API_KEY", key)
    monkeypatch.setenv("FIREBASE_PROJECT_ID", project)
    assert not firebase_auth.configured()
    with pytest.raises(FirebaseConfigError):
        firebase_auth.load_config()
    with pytest.raises(FirebaseConfigError):
        firebase_auth.sign_in_with_password(EMAIL, PASSWORD, client=FakeFirebase({}).client())


@pytest.mark.parametrize("project", ["Bad_Project", "x", "has space", "-leading", "a" * 40])
def test_invalid_project_id_is_rejected(monkeypatch, project):
    monkeypatch.setenv("FIREBASE_WEB_API_KEY", API_KEY)
    monkeypatch.setenv("FIREBASE_PROJECT_ID", project)
    assert not firebase_auth.configured()


def test_configuration_from_environment(monkeypatch):
    monkeypatch.setenv("FIREBASE_WEB_API_KEY", f"  {API_KEY} ")
    monkeypatch.setenv("FIREBASE_PROJECT_ID", PROJECT)
    cfg = firebase_auth.load_config()
    assert cfg.api_key == API_KEY and cfg.project_id == PROJECT
    assert API_KEY not in repr(cfg) and API_KEY not in str(cfg)


# ---------------------------------------------------------------------------
# email + password
# ---------------------------------------------------------------------------

def test_password_sign_in_extracts_uid_email_and_provider():
    fb = FakeFirebase(ok_password_answer())
    user = firebase_auth.sign_in_with_password(f"  {EMAIL} ", PASSWORD, config=CONFIG, client=fb.client())
    assert user.uid == UID
    assert user.email == EMAIL.lower()
    assert user.provider == "password"
    assert user.expires_at > time.time()
    req = fb.requests[0]
    assert req.url.path == "/v1/accounts:signInWithPassword" and req.url.host == "identitytoolkit.googleapis.com"
    assert req.url.scheme == "https" and req.headers["x-goog-api-key"] == API_KEY
    assert API_KEY not in str(req.url)                       # never in a URL that could be logged
    assert fb.body() == {"email": EMAIL, "password": PASSWORD, "returnSecureToken": True}


def test_email_falls_back_to_the_token_claims():
    fb = FakeFirebase(ok_password_answer(email=None))
    assert firebase_auth.sign_in_with_password(EMAIL, PASSWORD, config=CONFIG, client=fb.client()).email == EMAIL.lower()


def test_session_keeps_no_token_or_password():
    fb = FakeFirebase(ok_password_answer())
    session = firebase_auth.sign_in_with_password(EMAIL, PASSWORD, config=CONFIG, client=fb.client()).as_session()
    assert set(session) == {"uid", "email", "provider", "email_verified", "expires_at"}
    dump = json.dumps(session)
    for secret in (PASSWORD, API_KEY, "refresh-token-xyz", ok_password_answer()["idToken"].split(".")[1]):
        assert secret not in dump


@pytest.mark.parametrize("email, password", [("", PASSWORD), ("   ", PASSWORD), (EMAIL, ""), (None, None)])
def test_missing_fields_never_call_firebase(email, password):
    fb = FakeFirebase(ok_password_answer())
    with pytest.raises(FirebaseAuthError) as e:
        firebase_auth.sign_in_with_password(email, password, config=CONFIG, client=fb.client())
    assert e.value.code == "MISSING_FIELDS" and fb.requests == []


@pytest.mark.parametrize("message, code, shown", [
    ("INVALID_LOGIN_CREDENTIALS", "INVALID_LOGIN_CREDENTIALS", "Email or password is incorrect."),
    ("EMAIL_NOT_FOUND", "EMAIL_NOT_FOUND", "Email or password is incorrect."),
    ("INVALID_PASSWORD", "INVALID_PASSWORD", "Email or password is incorrect."),
    ("USER_DISABLED", "USER_DISABLED", "This account has been disabled."),
    ("TOO_MANY_ATTEMPTS_TRY_LATER : Access to this account has been temporarily disabled",
     "TOO_MANY_ATTEMPTS_TRY_LATER", "Too many attempts — try again later."),
    ("OPERATION_NOT_ALLOWED", "OPERATION_NOT_ALLOWED", "This sign-in method isn't enabled for the Firebase project."),
    ("API key not valid. Please pass a valid API key.", "API_KEY_INVALID", "Firebase sign-in isn't configured correctly."),
    ("SOMETHING_NEW", "SOMETHING_NEW", "Sign-in failed — try again."),
])
def test_authentication_failures(message, code, shown):
    fb = FakeFirebase(error_answer(message))
    with pytest.raises(FirebaseAuthError) as e:
        firebase_auth.sign_in_with_password(EMAIL, PASSWORD, config=CONFIG, client=fb.client())
    assert e.value.code == code and str(e.value) == shown


@pytest.mark.parametrize("reply", [
    httpx.Response(500, text="<html>Server Error</html>"),
    httpx.Response(400, json={"unexpected": True}),
    httpx.Response(403, json={"error": "string-not-object"}),
])
def test_unreadable_error_answers(reply):
    with pytest.raises(FirebaseAuthError) as e:
        firebase_auth.sign_in_with_password(EMAIL, PASSWORD, config=CONFIG, client=FakeFirebase(reply).client())
    assert str(e.value) == "Sign-in failed — try again."


def _expired():
    return ok_password_answer(idToken=make_id_token(exp=time.time() - 5))


@pytest.mark.parametrize("reply", [
    httpx.Response(200, text="not json"),
    httpx.Response(200, json=["a", "list"]),
    httpx.Response(200, json={}),
    httpx.Response(200, json=ok_password_answer(localId=None)),
    httpx.Response(200, json=ok_password_answer(localId="")),
    httpx.Response(200, json=ok_password_answer(localId="has spaces/slash")),
    httpx.Response(200, json=ok_password_answer(localId=12345)),
    httpx.Response(200, json=ok_password_answer(idToken=None)),
    httpx.Response(200, json=ok_password_answer(idToken="not-a-jwt")),
    httpx.Response(200, json=ok_password_answer(idToken="a.!!!notbase64!!!.c")),
    httpx.Response(200, json=ok_password_answer(idToken=f"a.{_b64(['list'])}.c")),
    httpx.Response(200, json=ok_password_answer(idToken=make_id_token(exp="soon"))),
    httpx.Response(200, json=ok_password_answer(email=["x"])),
    httpx.Response(200, json=ok_password_answer(idToken=make_id_token(provider="google.com"))),
])
def test_malformed_answers_are_rejected(reply):
    with pytest.raises(FirebaseAuthError) as e:
        firebase_auth.sign_in_with_password(EMAIL, PASSWORD, config=CONFIG, client=FakeFirebase(reply).client())
    assert e.value.code == "MALFORMED"


def test_expired_token_is_rejected():
    with pytest.raises(FirebaseAuthError):
        firebase_auth.sign_in_with_password(EMAIL, PASSWORD, config=CONFIG, client=FakeFirebase(_expired()).client())


@pytest.mark.parametrize("token", [
    make_id_token(project=OTHER_PROJECT),                        # key of another Firebase project
    make_id_token(uid="someone-else"),                           # token for a different user
    make_id_token(user_id="someone-else"),
])
def test_answer_for_another_project_or_user_is_rejected(token):
    fb = FakeFirebase(ok_password_answer(idToken=token))
    with pytest.raises(FirebaseAuthError) as e:
        firebase_auth.sign_in_with_password(EMAIL, PASSWORD, config=CONFIG, client=fb.client())
    assert e.value.code == "PROJECT_MISMATCH"


# ---------------------------------------------------------------------------
# Google (ID token from st.login, exchanged with signInWithIdp)
# ---------------------------------------------------------------------------

def test_google_sign_in_extracts_uid_email_and_provider():
    fb = FakeFirebase(ok_google_answer())
    user = firebase_auth.sign_in_with_google_id_token(GOOGLE_ID_TOKEN, "https://app.example/oauth2callback",
                                                      config=CONFIG, client=fb.client())
    assert (user.uid, user.email, user.provider, user.email_verified) == (UID, EMAIL.lower(), "google.com", True)
    assert fb.requests[0].url.path == "/v1/accounts:signInWithIdp"
    body = fb.body()
    assert body["postBody"] == f"id_token={GOOGLE_ID_TOKEN}&providerId=google.com"
    assert body["requestUri"] == "https://app.example/oauth2callback" and body["returnSecureToken"] is True
    assert "oauthIdToken" not in json.dumps(user.as_session()) and GOOGLE_ID_TOKEN not in json.dumps(user.as_session())


@pytest.mark.parametrize("token", [None, "", 123])
def test_google_without_a_token_never_calls_firebase(token):
    fb = FakeFirebase(ok_google_answer())
    with pytest.raises(FirebaseAuthError) as e:
        firebase_auth.sign_in_with_google_id_token(token, "https://x", config=CONFIG, client=fb.client())
    assert e.value.code == "NO_GOOGLE_TOKEN" and fb.requests == []


def test_google_answer_must_be_a_google_sign_in():
    fb = FakeFirebase(ok_google_answer(providerId=None, idToken=make_id_token(provider="password")))
    with pytest.raises(FirebaseAuthError):
        firebase_auth.sign_in_with_google_id_token(GOOGLE_ID_TOKEN, "https://x", config=CONFIG, client=fb.client())


def test_google_failure_is_reported():
    fb = FakeFirebase(error_answer("INVALID_IDP_RESPONSE : Invalid Idp Response"))
    with pytest.raises(FirebaseAuthError) as e:
        firebase_auth.sign_in_with_google_id_token(GOOGLE_ID_TOKEN, "https://x", config=CONFIG, client=fb.client())
    assert str(e.value) == "Google sign-in couldn't be verified."


# ---------------------------------------------------------------------------
# credential leakage
# ---------------------------------------------------------------------------

def test_network_errors_never_carry_the_key(caplog):
    url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={API_KEY}"
    fb = FakeFirebase(httpx.ConnectError(f"connection failed for {url} with password {PASSWORD}"))
    caplog.set_level(logging.DEBUG)
    with pytest.raises(FirebaseAuthError) as e:
        firebase_auth.sign_in_with_password(EMAIL, PASSWORD, config=CONFIG, client=fb.client())
    assert e.value.__cause__ is None and e.value.__suppress_context__      # original text not chained
    for secret in (API_KEY, PASSWORD):
        assert secret not in str(e.value) and secret not in repr(e.value) and secret not in caplog.text
    assert e.value.code == "NETWORK"


def test_failures_never_log_credentials(caplog):
    caplog.set_level(logging.DEBUG)
    for reply in (error_answer("INVALID_LOGIN_CREDENTIALS"), ok_password_answer(idToken=make_id_token(project=OTHER_PROJECT))):
        with pytest.raises(FirebaseAuthError):
            firebase_auth.sign_in_with_password(EMAIL, PASSWORD, config=CONFIG, client=FakeFirebase(reply).client())
    for secret in (API_KEY, PASSWORD, EMAIL, EMAIL.lower()):
        assert secret not in caplog.text


# ---------------------------------------------------------------------------
# session state
# ---------------------------------------------------------------------------

def test_session_user():
    now = time.time()
    good = {"uid": UID, "email": EMAIL, "provider": "password", "email_verified": None, "expires_at": now + 60}
    key = firebase_auth.SESSION_KEY
    assert firebase_auth.session_user({}) is None                                  # signed out
    assert firebase_auth.session_user({key: good}) == good
    assert firebase_auth.session_user({key: {**good, "expires_at": now - 1}}) is None
    for bad in (None, "uid", [], {**good, "uid": ""}, {**good, "uid": None}, {**good, "expires_at": "x"}):
        assert firebase_auth.session_user({key: bad}) is None
    assert firebase_auth.session_user(None) is None


# ---------------------------------------------------------------------------
# Configuration from Streamlit secrets (the preview's hidden-card bug)
# ---------------------------------------------------------------------------
# Only TOP-LEVEL secrets become environment variables. Firebase lines written
# below the [auth] section belong to it, so reading only the environment
# silently hid the whole feature.

GOOGLE_CLIENT = {"client_id": "placeholder-client.apps.example", "client_secret": "placeholder-client-value",
                 "server_metadata_url": "https://accounts.google.com/.well-known/openid-configuration"}
AUTH_BASE = {"redirect_uri": "https://job-tracker-bot.streamlit.app/oauth2callback",
             "cookie_secret": "placeholder-cookie-value", "expose_tokens": "id"}
FB_SECRETS = {"FIREBASE_WEB_API_KEY": API_KEY, "FIREBASE_PROJECT_ID": PROJECT}


def _without(d, key):
    return {k: v for k, v in d.items() if k != key}


@pytest.fixture
def no_env(monkeypatch):
    monkeypatch.delenv("FIREBASE_WEB_API_KEY", raising=False)
    monkeypatch.delenv("FIREBASE_PROJECT_ID", raising=False)
    monkeypatch.delenv("JT_ADMIN_EMAILS", raising=False)
    yield
    os.environ.pop("JT_ADMIN_EMAILS", None)     # the app may promote it from st.secrets


@pytest.mark.parametrize("secrets", [
    dict(FB_SECRETS),                                                       # top level
    {"auth": {**AUTH_BASE, **FB_SECRETS}},                                  # lines below [auth]
    {"firebase": {"web_api_key": API_KEY, "project_id": PROJECT}},          # [firebase] section
    {"firebase": {"api_key": API_KEY, "FIREBASE_PROJECT_ID": PROJECT}},
])
def test_config_is_found_in_streamlit_secrets(no_env, secrets):
    cfg = firebase_auth.load_config(secrets)
    assert cfg.api_key == API_KEY and cfg.project_id == PROJECT
    assert firebase_auth.configured(secrets) and firebase_auth.any_setting(secrets)


def test_environment_wins_over_secrets(monkeypatch):
    monkeypatch.setenv("FIREBASE_WEB_API_KEY", API_KEY)
    monkeypatch.setenv("FIREBASE_PROJECT_ID", PROJECT)
    cfg = firebase_auth.load_config({"FIREBASE_WEB_API_KEY": "other-placeholder", "FIREBASE_PROJECT_ID": "other-project"})
    assert (cfg.api_key, cfg.project_id) == (API_KEY, PROJECT)


@pytest.mark.parametrize("secrets, expected", [
    ({}, "isn't configured"),
    ({"FIREBASE_WEB_API_KEY": API_KEY}, "FIREBASE_PROJECT_ID is missing"),
    ({"auth": {"FIREBASE_PROJECT_ID": PROJECT}}, "FIREBASE_WEB_API_KEY is missing"),
    ({"FIREBASE_WEB_API_KEY": API_KEY, "FIREBASE_PROJECT_ID": "Bad Project!"}, "isn't a valid Firebase project ID"),
    ({"FIREBASE_WEB_API_KEY": "  ", "FIREBASE_PROJECT_ID": 123}, "isn't configured"),
    (None, "isn't configured"),
    ("not-a-mapping", "isn't configured"),
])
def test_config_problems_name_settings_never_values(no_env, secrets, expected):
    config, problem = firebase_auth.config_status(secrets)
    assert config is None and expected in problem
    assert API_KEY not in problem and PROJECT not in problem and "Bad Project" not in problem


def test_any_setting_detects_partial_setup(no_env):
    assert not firebase_auth.any_setting({}) and not firebase_auth.any_setting({"auth": AUTH_BASE})
    assert firebase_auth.any_setting({"auth": {"FIREBASE_PROJECT_ID": PROJECT}})


@pytest.mark.parametrize("token, expired", [
    (make_id_token(exp=time.time() + 3600), False),
    (make_id_token(exp=time.time() + 30), True),          # inside the safety margin
    (make_id_token(exp=time.time() - 10), True),
    ("not-a-jwt", True), (None, True), ("", True), ("a." + _b64({"exp": "soon"}) + ".c", True),
])
def test_google_token_expiry(token, expired):
    assert firebase_auth.token_expired(token) is expired


class FakeUser:
    """Stand-in for st.user: the identity Streamlit's st.login verified."""
    def __init__(self, logged_in=True, token=None, email=EMAIL.lower(), verified=True, name="Person Example"):
        self.is_logged_in = logged_in
        self.tokens = {"id": token} if token else {}
        self._claims = {"email": email, "email_verified": verified, "name": name} if logged_in else {}

    def get(self, key, default=None):
        return self._claims.get(key, default)


@pytest.fixture
def google_env(monkeypatch, no_env):
    """st.login / st.logout / st.user replaced by fakes, Firebase by a
    FakeFirebase. Nothing reaches Google or Firebase."""
    import streamlit
    calls = {"login": [], "logout": 0}
    monkeypatch.setattr(streamlit, "login", lambda provider=None: calls["login"].append(provider))

    def fake_logout():
        calls["logout"] += 1
        monkeypatch.setattr(streamlit, "user", FakeUser(logged_in=False))
    monkeypatch.setattr(streamlit, "logout", fake_logout)
    monkeypatch.setattr(streamlit, "user", FakeUser(logged_in=False))

    def set_user(user):
        monkeypatch.setattr(streamlit, "user", user)

    def use_firebase(reply):
        fb = FakeFirebase(reply)
        monkeypatch.setattr(firebase_auth, "_new_client", fb.client)
        return fb
    return {"calls": calls, "set_user": set_user, "firebase": use_firebase}


AUTH_GOOGLE = {**AUTH_BASE, **GOOGLE_CLIENT}
GATED = {**FB_SECRETS, "auth": AUTH_GOOGLE}


def _fresh_google_token(**extra):
    return make_id_token(project="google-issued", uid="google-sub", iat=time.time() - 5, **extra)


@pytest.mark.parametrize("age, expected", [(5, 5), (600, 600)])
def test_token_age(age, expected):
    assert firebase_auth.token_age(make_id_token(iat=time.time() - age)) == pytest.approx(expected, abs=2)


@pytest.mark.parametrize("token", [make_id_token(), "not-a-jwt", None, "a." + _b64({"iat": "x"}) + ".c"])
def test_token_age_unknown(token):
    assert firebase_auth.token_age(token) is None


# ---------------------------------------------------------------------------
# One login: the gate in front of the whole app
# ---------------------------------------------------------------------------

def _buttons(at):
    return {b.key for b in at.button}


def _dump(at) -> str:
    return json.dumps(at.session_state.to_dict(), default=str)


def _login_page(at) -> bool:
    return "Sign in to your <em>opportunities</em>" in _html(at)


def test_without_firebase_the_app_is_unchanged(app, no_env):
    at = app([NEW_RECORD])
    assert not _login_page(at) and "Graduate Software Engineer" in _html(at)
    _nav(at, "settings")
    assert "Owner access" in _html(at) and "set_account" not in str(at)


def test_visitor_sees_only_the_login_page(app, google_env):
    at = app([NEW_RECORD], secrets=GATED, owner=False)
    assert not at.exception and _login_page(at)
    html = _html(at)
    assert {"btn_login_google", "btn_login_email"} <= _buttons(at)
    assert "login_email" in {t.key for t in at.text_input} and "login_pw" in {t.key for t in at.text_input}
    # nothing of the app is drawn or loaded for a visitor
    assert "Graduate Software Engineer" not in html and "nav_home" not in _buttons(at)
    assert "Owner access" not in html and "owner_pw" not in {t.key for t in at.text_input}
    assert API_KEY not in html


def test_partial_setup_keeps_the_app_open_and_says_why(app, no_env):
    at = app([], page="settings", secrets={"FIREBASE_WEB_API_KEY": API_KEY})
    assert not _login_page(at)
    html = _html(at)
    assert "Sign-in isn’t available yet" in html and "FIREBASE_PROJECT_ID is missing" in html
    assert "Owner access" in html and API_KEY not in html


@pytest.mark.parametrize("auth, provider", [
    (AUTH_GOOGLE, None),                                      # flat [auth]
    ({**AUTH_BASE, "google": dict(GOOGLE_CLIENT)}, "google"),  # [auth.google]
])
def test_continue_with_google_starts_streamlit_login(app, google_env, auth, provider):
    at = app([], secrets={**FB_SECRETS, "auth": auth}, owner=False)
    at.button(key="btn_login_google").click().run()
    assert google_env["calls"]["login"] == [provider]


def test_incomplete_google_setup_hides_the_button_and_explains(app, google_env):
    at = app([], secrets={**FB_SECRETS, "auth": {**_without(AUTH_BASE, "expose_tokens"), **GOOGLE_CLIENT}}, owner=False)
    assert _login_page(at) and "btn_login_google" not in _buttons(at)
    assert "expose_tokens" in _html(at) and "btn_login_email" in _buttons(at)


# --- Google ------------------------------------------------------------------

def test_google_sign_in_opens_the_app_with_a_persistent_account_chip(app, google_env):
    fb = google_env["firebase"](ok_google_answer())
    google_env["set_user"](FakeUser(token=_fresh_google_token()))
    at = app([NEW_RECORD], secrets=GATED, owner=False)          # back from Google, on Home
    assert not at.exception and not _login_page(at) and at.session_state.page == "home"
    html = _html(at)
    assert "Graduate Software Engineer" in html                  # the app itself
    assert 'class="side-acct"' in html and "Person Example" in html and EMAIL.lower() in html
    assert "btn_account_sign_out" in _buttons(at)
    assert 'class="jt-toast"' not in html                                 # no fading success message
    assert len(fb.requests) == 1 and at.session_state[firebase_auth.SESSION_KEY]["uid"] == UID
    for _ in range(2):                                            # it stays
        at.run()
        assert 'class="side-acct"' in _html(at)
    assert len(fb.requests) == 1
    _nav(at, "settings")
    html = _html(at)
    assert "Signed in with Google" in html and UID in html and "Member — read-only" in html


def test_reload_after_an_hour_stays_signed_in_without_google_again(app, google_env):
    """Streamlit's sign-in cookie lasts 30 days; Google's ID token 1 hour.
    A new session after the token expired is still signed in — no repeated
    Google sign-in — and nothing stale is sent to Firebase."""
    fb = google_env["firebase"](ok_google_answer())
    google_env["set_user"](FakeUser(token=make_id_token(project="google-issued", exp=time.time() - 600)))
    at = app([NEW_RECORD], secrets=GATED, owner=False)
    assert not _login_page(at) and 'class="side-acct"' in _html(at)
    assert fb.requests == [] and google_env["calls"]["login"] == []
    assert "expired" not in _html(at).lower()
    _nav(at, "settings")
    assert "Linked at your next Google sign-in" in _html(at)


@pytest.mark.parametrize("message, code, hint", [
    ("INVALID_IDP_RESPONSE : Invalid Idp Response", "INVALID_IDP_RESPONSE", "isn't allowed by the Firebase project"),
    ("OPERATION_NOT_ALLOWED", "OPERATION_NOT_ALLOWED", "Google provider isn't enabled"),
])
def test_firebase_link_failure_never_blocks_and_is_explained(app, google_env, message, code, hint):
    fb = google_env["firebase"](error_answer(message))
    google_env["set_user"](FakeUser(token=_fresh_google_token()))
    at = app([], page="settings", secrets=GATED, owner=False)
    assert not _login_page(at) and 'class="side-acct"' in _html(at)       # still signed in
    html = unescape(_html(at))
    assert f"({code})" in html and hint in html and 'class="jt-toast"' not in _html(at)
    at.run()
    assert len(fb.requests) == 1                                          # once per session


def test_google_sign_out(app, google_env):
    google_env["firebase"](ok_google_answer())
    google_env["set_user"](FakeUser(token=_fresh_google_token()))
    at = app([], secrets=GATED, owner=False)
    at.button(key="btn_account_sign_out").click().run()
    assert google_env["calls"]["logout"] == 1
    at.run()
    assert _login_page(at)
    assert ACCOUNT_KEY not in at.session_state and firebase_auth.SESSION_KEY not in at.session_state
    at.button(key="btn_login_google").click().run()                       # signing in again is a click
    assert google_env["calls"]["login"] == [None]


def test_sign_out_does_not_bounce_back_while_the_cookie_is_cleared(app, google_env, monkeypatch):
    import streamlit
    google_env["firebase"](ok_google_answer())
    google_env["set_user"](FakeUser(token=_fresh_google_token()))
    monkeypatch.setattr(streamlit, "logout", lambda: None)                 # cookie not cleared (yet)
    at = app([], secrets=GATED, owner=False)
    at.button(key="btn_account_sign_out").click().run()
    at.run()
    assert _login_page(at)


# --- email + password ----------------------------------------------------------

def _login(at, email=EMAIL, password=PASSWORD):
    at.text_input(key="login_email").set_value(email)
    at.text_input(key="login_pw").set_value(password)
    at.button(key="btn_login_email").click().run()


def test_email_sign_in_opens_the_app(app, google_env):
    fb = google_env["firebase"](ok_password_answer())
    at = app([NEW_RECORD], secrets=GATED, owner=False)
    _login(at)
    assert not _login_page(at) and "Graduate Software Engineer" in _html(at)
    assert 'class="side-acct"' in _html(at) and EMAIL.lower() in _html(at)
    assert fb.requests[0].headers["x-goog-api-key"] == API_KEY
    for secret in (PASSWORD, API_KEY, "refresh-token-xyz", ok_password_answer()["idToken"]):
        assert secret not in _dump(at) and secret not in _html(at)


def test_wrong_password_stays_on_the_login_page_with_a_lasting_message(app, google_env):
    google_env["firebase"](error_answer("INVALID_LOGIN_CREDENTIALS"))
    at = app([], secrets=GATED, owner=False)
    _login(at, password="wrong-password-123")
    for _ in range(2):
        assert _login_page(at) and "Email or password is incorrect." in _html(at)
        assert "wrong-password-123" not in _dump(at)
        at.run()


def test_password_sign_ins_are_rate_limited(app, google_env):
    fb = google_env["firebase"](error_answer("INVALID_LOGIN_CREDENTIALS"))
    at = app([], secrets=GATED, owner=False)
    for _ in range(11):
        _login(at, password="nope-nope-nope")
    assert len(fb.requests) == 10 and "Too many failed attempts" in _html(at)


def test_email_sign_out_and_session_expiry(app, google_env):
    google_env["firebase"](ok_password_answer())
    at = app([], secrets=GATED, owner=False)
    _login(at)
    at.button(key="btn_account_sign_out").click().run()
    assert _login_page(at) and google_env["calls"]["logout"] == 0
    _login(at)
    at.session_state[firebase_auth.SESSION_KEY] = {**at.session_state[firebase_auth.SESSION_KEY],
                                                   "expires_at": time.time() - 1}
    at.run()
    assert _login_page(at)


# --- authorization: one identity, roles from it --------------------------------

@pytest.fixture
def admins(monkeypatch):
    def set_admins(value):
        monkeypatch.setenv("JT_ADMIN_EMAILS", value)
    return set_admins


def test_admin_comes_from_the_signed_in_identity(app, google_env, admins):
    admins(f"someone@else.example, {EMAIL.upper()}")
    google_env["firebase"](ok_google_answer())
    google_env["set_user"](FakeUser(token=_fresh_google_token()))
    at = app([NEW_RECORD], secrets=GATED, owner=False)
    assert 'class="role admin"' in _html(at)
    _nav(at, "jobs")
    at.button(key=_key("dismiss", NEW_RECORD)).click().run()
    assert _seen(at)[0]["dismissed"] is True                       # no second password needed
    _nav(at, "settings")
    html = _html(at)
    assert "Admin — can manage" in html
    assert "owner_pw" not in {t.key for t in at.text_input} and "Owner access" not in html


def test_admin_list_works_wherever_it_sits_in_the_secrets(app, google_env):
    google_env["firebase"](ok_google_answer())
    google_env["set_user"](FakeUser(token=_fresh_google_token()))
    at = app([NEW_RECORD], secrets={**FB_SECRETS, "auth": {**AUTH_GOOGLE, "JT_ADMIN_EMAILS": EMAIL}}, owner=False)
    assert 'class="role admin"' in _html(at)


def test_members_are_read_only(app, google_env, admins, monkeypatch):
    admins("boss@example.org")
    google_env["firebase"](ok_google_answer())
    google_env["set_user"](FakeUser(token=_fresh_google_token()))
    sent = []
    monkeypatch.setattr(notifier, "test_mail", lambda to: sent.append(to))
    at = app([NEW_RECORD], secrets=GATED)           # even a leftover owner-password flag doesn't count
    _nav(at, "jobs")
    at.button(key=_key("dismiss", NEW_RECORD)).click().run()
    assert not _seen(at)[0].get("dismissed") and "Only admins can dismiss jobs." in _html(at)
    _nav(at, "email")
    at.button(key="btn_test").click().run()
    assert sent == [] and "Only admins can send test emails." in _html(at)


def test_an_unverified_email_is_never_admin(app, google_env, admins):
    """A fresh email/password account merely claims its address."""
    admins(EMAIL)
    google_env["firebase"](ok_password_answer())              # no emailVerified in the answer
    at = app([NEW_RECORD], secrets=GATED, owner=False)
    _login(at)
    assert 'class="role admin"' not in _html(at)
    _nav(at, "jobs")
    at.button(key=_key("dismiss", NEW_RECORD)).click().run()
    assert not _seen(at)[0].get("dismissed")


def test_google_identity_without_verified_email_is_not_admin(app, google_env, admins):
    admins(EMAIL)
    google_env["firebase"](ok_google_answer(emailVerified=False))
    google_env["set_user"](FakeUser(token=_fresh_google_token(), verified=False))
    at = app([], secrets=GATED, owner=False)
    assert 'class="role admin"' not in _html(at)


def test_until_admins_are_set_the_owner_password_is_a_tucked_away_unlock(app, google_env):
    google_env["firebase"](ok_google_answer())
    google_env["set_user"](FakeUser(token=_fresh_google_token()))
    at = app([NEW_RECORD], page="settings", secrets=GATED, owner=False)
    assert not _login_page(at) and "Owner access" not in _html(at)           # never a second login screen
    assert "Member — read-only (admins are set with the JT_ADMIN_EMAILS secret)" in _html(at)
    assert "owner_pw" in {t.key for t in at.text_input}
    at.text_input(key="owner_pw").set_value(OWNER_PASSWORD)
    at.button(key="btn_sign_in").click().run()
    assert "owner_pw" not in at.session_state or not at.session_state["owner_pw"]
    assert OWNER_PASSWORD not in _dump(at)
    _nav(at, "jobs")
    at.button(key=_key("dismiss", NEW_RECORD)).click().run()
    assert _seen(at)[0]["dismissed"] is True


def test_signing_out_also_drops_unlocked_admin_access(app, google_env):
    google_env["firebase"](ok_password_answer())
    at = app([], secrets=GATED)                    # the session holds an owner-password sign-in
    assert not _login_page(at) and "Owner" in _html(at)
    at.button(key="btn_account_sign_out").click().run()
    assert _login_page(at)
    assert "_owner_until" not in at.session_state and ACCOUNT_KEY not in at.session_state


def test_owner_password_break_glass_on_the_login_page(app, google_env):
    """Until admins come from sign-in, the owner can always get in — even if
    Google/Firebase misbehave — via a collapsed option, never a second login."""
    at = app([NEW_RECORD], secrets=GATED, owner=False)
    assert _login_page(at) and "login_owner_pw" in {t.key for t in at.text_input}
    at.text_input(key="login_owner_pw").set_value("not the owner password")
    at.button(key="btn_login_owner").click().run()
    assert _login_page(at) and "That password isn" in _html(at)
    at.text_input(key="login_owner_pw").set_value(OWNER_PASSWORD)
    at.button(key="btn_login_owner").click().run()
    assert not _login_page(at) and 'class="role admin"' in _html(at)
    assert OWNER_PASSWORD not in _dump(at) and "not the owner password" not in _dump(at)
    _nav(at, "jobs")
    at.button(key=_key("dismiss", NEW_RECORD)).click().run()
    assert _seen(at)[0]["dismissed"] is True


def test_break_glass_disappears_once_admins_are_set(app, google_env, admins):
    admins(EMAIL)
    at = app([], secrets=GATED)                    # even a leftover owner flag doesn't open the gate
    assert _login_page(at) and "login_owner_pw" not in {t.key for t in at.text_input}


# --- the test-email operation checks the role itself -----------------------------

def test_send_test_email_checks_the_admin_role(monkeypatch):
    import access
    monkeypatch.setenv("JT_OWNER_PASSWORD", "a-long-owner-password-123")
    monkeypatch.setenv("JT_ADMIN_EMAILS", EMAIL)
    monkeypatch.setenv("ALERT_RECIPIENT", "alerts@example.org")
    sent = []
    monkeypatch.setattr(notifier, "_send", lambda to, *a: sent.append(to))
    admin = {ACCOUNT_KEY: {"email": EMAIL.lower(), "email_verified": True}}
    member = {ACCOUNT_KEY: {"email": "other@example.org", "email_verified": True}}
    unverified = {ACCOUNT_KEY: {"email": EMAIL.lower(), "email_verified": False}}
    legacy_flag = {access.OWNER_SESSION_KEY: time.time() + 3600}       # ignored once admins are set
    for session in (member, unverified, legacy_flag, {}):
        with pytest.raises(access.OwnerRequired):
            access.send_test_email(session, {})
    assert access.send_test_email(admin, {}) == "alerts@example.org" and sent == ["alerts@example.org"]


# ---------------------------------------------------------------------------
# A failed Google code exchange must be explained, not silently "start over"
# (production evidence: Google answered "invalid_client: The provided client
# secret is invalid", Streamlit cleared the cookies and redirected to the
# login page with nothing said).
# ---------------------------------------------------------------------------

AUTH_ROUTES_LOGGER = "streamlit.web.server.starlette.starlette_auth_routes"


@pytest.fixture
def auth_capture():
    """Reset the process-wide sign-in record before and after each test."""
    def reset():
        for h in logging.getLogger(AUTH_ROUTES_LOGGER).handlers:
            store = getattr(h, "_jt_auth_capture", None)
            if store is not None:
                store["events"].clear()
                store["exchange"] = {"at": None, "code": ""}
    reset()
    yield
    reset()


class OAuthError(Exception):
    """Shaped like authlib's: str() is "<error>: <description>"."""


def _streamlit_reports_failed_exchange(message):
    """Exactly what Streamlit's callback logs when the code exchange fails."""
    try:
        raise OAuthError(message)
    except OAuthError:
        logging.getLogger(AUTH_ROUTES_LOGGER).warning(
            "OAuth token exchange failed for provider '%s'. Clearing auth cookies.", "default", exc_info=True)


def test_invalid_client_is_explained_on_the_login_page(app, google_env, auth_capture):
    at = app([], secrets=GATED, owner=False)                 # the app installs its capture
    assert _login_page(at) and "invalid_client" not in _html(at)
    _streamlit_reports_failed_exchange("invalid_client: The provided client secret is invalid.")
    for _ in range(2):                                       # a lasting message, not a toast
        at.run()
        html = unescape(_html(at))
        assert _login_page(at)
        assert "Google rejected this app's sign-in credentials (invalid_client)" in html
        assert "update the Google OAuth client secret" in html
        assert 'class="jt-toast"' not in _html(at)


def test_invalid_grant_asks_to_try_again(app, google_env, auth_capture):
    at = app([], secrets=GATED, owner=False)
    _streamlit_reports_failed_exchange("invalid_grant: Bad Request")
    at.run()
    assert "had expired or was already used" in unescape(_html(at))


def test_unknown_exchange_errors_name_the_code_only(app, google_env, auth_capture):
    at = app([], secrets=GATED, owner=False)
    _streamlit_reports_failed_exchange("server_error: upstream person@example.com eyJhbGciOiJSUzI1NiJ9.e30.sig")
    at.run()
    html = unescape(_html(at))
    assert "Google sign-in couldn't be completed (server_error)" in html
    assert "person@example.com" not in html and "eyJ" not in html


def test_the_notice_expires(app, google_env, auth_capture, monkeypatch):
    at = app([], secrets=GATED, owner=False)
    _streamlit_reports_failed_exchange("invalid_client: The provided client secret is invalid.")
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 301)
    at.run()
    assert "invalid_client" not in _html(at)


def test_no_notice_without_a_failure(app, google_env, auth_capture):
    at = app([], secrets=GATED, owner=False)
    at.run()
    assert 'class="login-err"' not in _html(at)


def test_diag_page_lists_the_redacted_failure(app, google_env, auth_capture):
    at = app([], secrets=GATED, owner=False)
    _streamlit_reports_failed_exchange("invalid_client: The provided client secret is invalid.")
    diag = app([], query={"diag": "auth"}, secrets=GATED, owner=False)
    html = _html(diag)
    assert "Sign-in diagnostics" in html and "token exchange failed" in html
    assert "OAuthError: invalid_client: The provided client secret is invalid." in unescape(html)
    assert API_KEY not in html


# ---------------------------------------------------------------------------
# ?diag=auth reports where JT_ADMIN_EMAILS is — names/booleans/counts only
# ---------------------------------------------------------------------------

def _admin_rows(at) -> dict:
    import re as _re
    html = _html(at)
    table = html[html.index("id='admin-diag'"):]
    return {unescape(a): unescape(b) for a, b in _re.findall(r"<tr><td>(.*?)</td><td><code>(.*?)</code></td></tr>", table)}


@pytest.mark.parametrize("secrets, found, top", [
    ({"JT_ADMIN_EMAILS": EMAIL, **FB_SECRETS}, "<top level>", "True"),
    ({**FB_SECRETS, "auth": {**AUTH_GOOGLE, "JT_ADMIN_EMAILS": EMAIL}}, "[auth]", "False"),
    ({**FB_SECRETS, "firebase": {"JT_ADMIN_EMAILS": EMAIL}}, "[firebase]", "False"),
])
def test_admin_diag_locates_the_key(app, no_env, secrets, found, top):
    at = app([], query={"diag": "auth"}, secrets=secrets, owner=False)
    rows = _admin_rows(at)
    assert rows["JT_ADMIN_EMAILS found in"] == found
    assert rows["JT_ADMIN_EMAILS_PRESENT_TOP_LEVEL"] == top
    assert rows["VALUE_HAS_AT_SIGN"] == "True"
    assert EMAIL not in _html(at) and EMAIL.lower() not in _html(at) and API_KEY not in _html(at)


def test_admin_diag_counts_entries_only_where_the_app_looks(app, no_env):
    at = app([], query={"diag": "auth"}, secrets={"JT_ADMIN_EMAILS": f"{EMAIL}, other@example.org", **FB_SECRETS}, owner=False)
    assert _admin_rows(at)["ADMIN_ENTRY_COUNT (in secrets)"] == "2"
    at = app([], query={"diag": "auth"}, secrets={**FB_SECRETS, "firebase": {"JT_ADMIN_EMAILS": EMAIL}}, owner=False)
    assert _admin_rows(at)["ADMIN_ENTRY_COUNT (in secrets)"] == "0"      # not a place the app reads


@pytest.mark.parametrize("secrets, row, expected", [
    ({"JT_ADMIN_EMAIL": EMAIL, **FB_SECRETS}, "similar key names (names only)", "JT_ADMIN_EMAIL"),
    ({"JT_ADMIN_EMAILS": "no-at-sign-here", **FB_SECRETS}, "VALUE_HAS_AT_SIGN", "False"),
    ({"JT_ADMIN_EMAILS": f" {EMAIL} ", **FB_SECRETS}, "value has leading/trailing spaces", "True"),
    ({"JT_ADMIN_EMAILS": f"{EMAIL}​", **FB_SECRETS}, "value has non-ASCII / invisible characters", "True"),
    ({"JT_ADMIN_EMAILS": [EMAIL], **FB_SECRETS}, "value type", "list"),
    ({**FB_SECRETS}, "JT_ADMIN_EMAILS found in", "nowhere"),
])
def test_admin_diag_flags_common_mistakes(app, no_env, secrets, row, expected):
    at = app([], query={"diag": "auth"}, secrets=secrets, owner=False)
    assert _admin_rows(at)[row] == expected
    assert EMAIL not in _html(at) and EMAIL.lower() not in _html(at)


def test_admin_diag_signed_in_checks(app, google_env):
    google_env["set_user"](FakeUser(token=_fresh_google_token()))
    at = app([], query={"diag": "auth"}, secrets={"JT_ADMIN_EMAILS": EMAIL, **GATED}, owner=False)
    rows = _admin_rows(at)
    assert rows["SIGNED_IN_EMAIL_VERIFIED"] == "True" and rows["SIGNED_IN_EMAIL_ON_LIST"] == "True"
    google_env["set_user"](FakeUser(token=_fresh_google_token(), email="someone@else.example", verified=False))
    at = app([], query={"diag": "auth"}, secrets={"JT_ADMIN_EMAILS": EMAIL, **GATED}, owner=False)
    rows = _admin_rows(at)
    assert rows["SIGNED_IN_EMAIL_VERIFIED"] == "False" and rows["SIGNED_IN_EMAIL_ON_LIST"] == "False"
    assert "someone@else.example" not in _html(at) and EMAIL.lower() not in _html(at)
