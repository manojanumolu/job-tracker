"""Firebase Authentication spike. Firebase is never contacted: every REST
call goes through an httpx.MockTransport that answers like Identity Toolkit."""
import base64
import json
import logging
import time

import httpx
import pytest

import firebase_auth
from firebase_auth import FirebaseAuthError, FirebaseConfig, FirebaseConfigError
from test_streamlit_app import NEW_RECORD, _html, _key, _nav, _seen, app  # noqa: F401  (fixture re-export)

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
# The dashboard (AppTest)
# ---------------------------------------------------------------------------

@pytest.fixture
def firebase_env(monkeypatch):
    monkeypatch.setenv("FIREBASE_WEB_API_KEY", API_KEY)
    monkeypatch.setenv("FIREBASE_PROJECT_ID", PROJECT)

    def use(reply):
        fb = FakeFirebase(reply)
        monkeypatch.setattr(firebase_auth, "_new_client", fb.client)
        return fb
    return use


def _fb_sign_in(at, email=EMAIL, password=PASSWORD):
    at.text_input(key="fb_email").set_value(email)
    at.text_input(key="fb_pw").set_value(password)
    at.button(key="btn_fb_sign_in").click().run()


def _session_dump(at) -> str:
    return json.dumps(at.session_state.to_dict(), default=str)


def test_panel_is_hidden_without_firebase_configuration(app):
    at = app([], page="settings")
    assert not at.exception
    assert "Firebase sign-in" not in _html(at)
    assert not [t for t in at.text_input if t.key in ("fb_email", "fb_pw")]
    assert "Owner access" in _html(at)                         # the existing page is unchanged


def test_signed_out_panel(app, firebase_env):
    fb = firebase_env(ok_password_answer())
    at = app([], page="settings")
    html = _html(at)
    assert "Firebase sign-in" in html and "grant any access" in html
    assert {"btn_fb_sign_in"} <= {b.key for b in at.button}
    assert "btn_fb_google" not in {b.key for b in at.button}   # no [auth] secrets -> no Google button
    assert fb.requests == [] and API_KEY not in html


def test_successful_sign_in_shows_the_uid(app, firebase_env):
    fb = firebase_env(ok_password_answer())
    at = app([], page="settings")
    _fb_sign_in(at)
    assert not at.exception and len(fb.requests) == 1
    user = at.session_state[firebase_auth.SESSION_KEY]
    assert user["uid"] == UID and user["email"] == EMAIL.lower() and user["provider"] == "password"
    html = _html(at)
    assert "Signed in with Firebase" in html and UID in html and EMAIL.lower() in html
    assert "Email &amp; password" in html
    # nothing secret is kept or shown
    dump = _session_dump(at)
    for secret in (PASSWORD, API_KEY, "refresh-token-xyz"):
        assert secret not in dump and secret not in html
    assert all(PASSWORD not in str(v) and API_KEY not in str(v) for v in at.query_params.values())


def test_failed_sign_in_shows_a_safe_message(app, firebase_env):
    firebase_env(error_answer("INVALID_LOGIN_CREDENTIALS"))
    at = app([], page="settings")
    _fb_sign_in(at)
    html = _html(at)
    assert "Email or password is incorrect." in html
    assert firebase_auth.SESSION_KEY not in at.session_state
    assert PASSWORD not in html and API_KEY not in html and PASSWORD not in _session_dump(at)


def test_malformed_answer_does_not_sign_in(app, firebase_env):
    firebase_env(ok_password_answer(idToken=make_id_token(project=OTHER_PROJECT)))
    at = app([], page="settings")
    _fb_sign_in(at)
    assert firebase_auth.SESSION_KEY not in at.session_state
    assert "project mismatch" in _html(at)


def test_repeated_wrong_passwords_are_rate_limited(app, firebase_env):
    fb = firebase_env(error_answer("INVALID_LOGIN_CREDENTIALS"))
    at = app([], page="settings")
    for _ in range(10):
        _fb_sign_in(at)
    _fb_sign_in(at)
    assert len(fb.requests) == 10 and "Too many failed attempts" in _html(at)


def test_firebase_sign_in_grants_no_owner_access(app, firebase_env):
    """The spike only identifies the person; changes still need owner access."""
    firebase_env(ok_password_answer())
    at = app([NEW_RECORD], page="settings", owner=False)
    _fb_sign_in(at)
    assert at.session_state[firebase_auth.SESSION_KEY]["uid"] == UID
    _nav(at, "jobs")
    at.button(key=_key("dismiss", NEW_RECORD)).click().run()
    assert not _seen(at)[0].get("dismissed") and "Only the owner can" in _html(at)


def test_sign_out_and_expiry(app, firebase_env):
    firebase_env(ok_password_answer())
    at = app([], page="settings")
    _fb_sign_in(at)
    at.button(key="btn_fb_sign_out").click().run()
    assert firebase_auth.SESSION_KEY not in at.session_state
    assert "btn_fb_sign_in" in {b.key for b in at.button}
    _fb_sign_in(at)
    at.session_state[firebase_auth.SESSION_KEY] = {**at.session_state[firebase_auth.SESSION_KEY],
                                                   "expires_at": time.time() - 1}
    at.run()
    assert "btn_fb_sign_in" in {b.key for b in at.button}       # expired -> signed out again
