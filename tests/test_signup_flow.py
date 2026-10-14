"""Email/password sign-up (2026-10-15 production bug).

"Sign up" on the login page called _login_google, so it opened Google's
account picker instead of a registration form. These tests pin the two
flows apart: "Sign up" shows a name / email / password / confirm form that
creates a Firebase email/password account and has Firebase send its own
verification email — and never starts Google; "Continue with Google" is the
only control that calls st.login. A new account is not signed in and gets
nothing until Firebase reports the address verified. Google and Firebase
are never contacted (test_firebase_auth's fakes)."""
import json
from html import unescape

import httpx
import pytest

import email_policy
import firebase_auth
from access import ACCOUNT_KEY
from firebase_auth import FirebaseConfig
from test_firebase_auth import (  # noqa: F401  (fixture re-export)
    API_KEY, GATED, PROJECT, _buttons, _dump, _login, _login_page, admins, error_answer, google_env,
    make_id_token, no_env,
)
from test_security_audit_2026_10_13 import _verified_password_answer, firebase_replies  # noqa: F401
from test_streamlit_app import NEW_RECORD, REPO, _html, app  # noqa: F401

CONFIG = FirebaseConfig(API_KEY, PROJECT)
NAME = "Asha  Verma "
NEW_PW = "fresh-Signup-pw-2026"
NEW_EMAIL = "new.person@example.org"
NEW_UID = "NewUid0000000000000000000001"


def sign_up_answer(uid=NEW_UID, email=NEW_EMAIL):
    """What accounts:signUp returns: a token for the new, unverified account."""
    return {"kind": "identitytoolkit#SignupNewUserResponse", "localId": uid, "email": email,
            "idToken": make_id_token(uid=uid, email=email, email_verified=False),
            "refreshToken": "refresh-token-xyz", "expiresIn": "3600"}


def ok_replies(**over):
    return {"signUp": sign_up_answer(), "update": {"localId": NEW_UID, "displayName": "Asha Verma"},
            "sendOobCode": {"email": NEW_EMAIL}, **over}


def _text(at) -> str:
    return unescape(_html(at))


def _inputs(at) -> dict:
    return {t.key: t for t in at.text_input}


def _open_sign_up(at):
    at.button(key="btn_login_signup").click().run()


def _sign_up(at, name=NAME, email=NEW_EMAIL, password=NEW_PW, confirm=None):
    if "btn_signup_submit" not in _buttons(at):
        _open_sign_up(at)
    at.text_input(key="signup_name").set_value(name)
    at.text_input(key="signup_email").set_value(email)
    at.text_input(key="signup_pw").set_value(password)
    at.text_input(key="signup_pw2").set_value(password if confirm is None else confirm)
    at.button(key="btn_signup_submit").click().run()


# ---------------------------------------------------------------------------
# firebase_auth.sign_up_with_password
# ---------------------------------------------------------------------------

def test_sign_up_creates_names_and_sends_the_verification_email(firebase_replies):
    fake = firebase_replies(**ok_replies())
    asked = []
    user, sent = firebase_auth.sign_up_with_password(f" {NEW_EMAIL} ", NEW_PW, "Asha Verma", config=CONFIG,
                                                     may_send_verification=lambda a: asked.append(a) or True)
    assert fake.methods() == ["signUp", "update", "sendOobCode"]
    assert fake.calls[0][1] == {"email": NEW_EMAIL, "password": NEW_PW, "returnSecureToken": True}
    token = fake.by_method["signUp"]["idToken"]                                    # the new account's token
    assert fake.calls[1][1] == {"idToken": token, "displayName": "Asha Verma", "returnSecureToken": False}
    assert fake.calls[2][1] == {"requestType": "VERIFY_EMAIL", "idToken": token}
    assert sent and asked == [NEW_EMAIL]
    assert (user.uid, user.email, user.provider, user.email_verified, user.name) == (
        NEW_UID, NEW_EMAIL, "password", False, "Asha Verma")


@pytest.mark.parametrize("code, shown", [
    ("EMAIL_EXISTS", "An account with this email already exists. Sign in instead"),
    ("INVALID_EMAIL", "Enter a valid email address."),
    ("WEAK_PASSWORD : Password should be at least 6 characters", "Choose a stronger password."),
    ("PASSWORD_DOES_NOT_MEET_REQUIREMENTS : Missing password requirements: [Password must contain a numeric "
     "character]", "Choose a stronger password"),
    ("ADMIN_ONLY_OPERATION", "New email accounts can't be created right now."),
    ("TOO_MANY_ATTEMPTS_TRY_LATER", "Too many attempts"),
])
def test_sign_up_refusals_are_explained_and_nothing_else_is_asked(firebase_replies, code, shown):
    fake = firebase_replies(signUp=error_answer(code))
    with pytest.raises(firebase_auth.FirebaseAuthError) as e:
        firebase_auth.sign_up_with_password(NEW_EMAIL, NEW_PW, "Asha", config=CONFIG, may_send_verification=lambda a: True)
    assert str(e.value).startswith(shown) and e.value.code == code.split(" ", 1)[0]
    assert fake.methods() == ["signUp"] and NEW_PW not in str(e.value)


def test_sign_up_network_failure_is_a_plain_message():
    def boom(request):
        raise httpx.ConnectError("down")
    client = httpx.Client(transport=httpx.MockTransport(boom))
    with pytest.raises(firebase_auth.FirebaseAuthError) as e:
        firebase_auth.sign_up_with_password(NEW_EMAIL, NEW_PW, "Asha", config=CONFIG, client=client)
    assert e.value.code == "NETWORK" and "Couldn't reach Firebase" in str(e.value)


def test_verification_email_failure_still_reports_the_account(firebase_replies):
    fake = firebase_replies(**ok_replies(sendOobCode=error_answer("TOO_MANY_ATTEMPTS_TRY_LATER")))
    user, sent = firebase_auth.sign_up_with_password(NEW_EMAIL, NEW_PW, "Asha", config=CONFIG,
                                                     may_send_verification=lambda a: True)
    assert user.uid == NEW_UID and not sent and fake.methods() == ["signUp", "update", "sendOobCode"]
    fake = firebase_replies(**ok_replies())
    _, sent = firebase_auth.sign_up_with_password(NEW_EMAIL, NEW_PW, "Asha", config=CONFIG,
                                                  may_send_verification=lambda a: False)      # throttled
    assert not sent and fake.methods() == ["signUp", "update"]


def test_a_failed_name_update_does_not_fail_the_sign_up(firebase_replies):
    fake = firebase_replies(**ok_replies(update=error_answer("INVALID_ID_TOKEN")))
    user, sent = firebase_auth.sign_up_with_password(NEW_EMAIL, NEW_PW, "Asha", config=CONFIG,
                                                     may_send_verification=lambda a: True)
    assert sent and user.name == "" and fake.methods() == ["signUp", "update", "sendOobCode"]


@pytest.mark.parametrize("email, password", [("", NEW_PW), ("  ", NEW_PW), (NEW_EMAIL, ""), (None, None)])
def test_sign_up_without_email_or_password_never_calls_firebase(firebase_replies, email, password):
    fake = firebase_replies(**ok_replies())
    with pytest.raises(firebase_auth.FirebaseAuthError):
        firebase_auth.sign_up_with_password(email, password, "Asha", config=CONFIG)
    assert fake.calls == []


def test_a_sign_up_answer_claiming_a_verified_address_is_rejected(firebase_replies):
    firebase_replies(**ok_replies(signUp={**sign_up_answer(), "idToken": make_id_token(
        uid=NEW_UID, email=NEW_EMAIL, email_verified=True)}))
    with pytest.raises(firebase_auth.FirebaseAuthError) as e:
        firebase_auth.sign_up_with_password(NEW_EMAIL, NEW_PW, "Asha", config=CONFIG)
    assert e.value.code == "MALFORMED"


def test_sign_in_carries_the_firebase_profile_name(firebase_replies):
    firebase_replies(signInWithPassword=_verified_password_answer(email=NEW_EMAIL, uid=NEW_UID) | {
        "displayName": "Asha Verma"})
    user = firebase_auth.sign_in_with_password(NEW_EMAIL, NEW_PW, config=CONFIG, require_verified=True)
    assert user.name == "Asha Verma" and user.as_session()["name"] == "Asha Verma"


# ---------------------------------------------------------------------------
# The login page: Sign up and Continue with Google are separate flows
# ---------------------------------------------------------------------------

def test_sign_up_opens_the_registration_form_not_google(app, google_env, firebase_replies):
    fake = firebase_replies(**ok_replies())
    at = app([NEW_RECORD], secrets=GATED, owner=False)
    _open_sign_up(at)
    assert google_env["calls"]["login"] == [] and fake.calls == []          # Google never started, nothing sent
    assert _login_page(at) and not at.exception
    fields = _inputs(at)
    assert [(k, fields[k].label) for k in ("signup_name", "signup_email", "signup_pw", "signup_pw2")] == [
        ("signup_name", "Full name"), ("signup_email", "Email address"), ("signup_pw", "Password"),
        ("signup_pw2", "Confirm password")]
    assert fields["signup_pw"].proto.type == 1 and fields["signup_pw2"].proto.type == 1     # password fields
    assert "login_email" not in fields and "login_pw" not in fields                         # the sign-in form is gone
    submit = at.button(key="btn_signup_submit")
    assert submit.label == "Create account" and submit.proto.type == "primary"
    assert "Create your account" in _text(at)
    buttons = _buttons(at)
    assert "btn_login_signin" in buttons and "btn_login_google" in buttons                  # back link + Google stay
    assert "btn_login_email" not in buttons and "btn_login_signup" not in buttons and "btn_login_owner" not in buttons


@pytest.mark.parametrize("from_sign_up", [False, True])
def test_continue_with_google_starts_google_from_either_card(app, google_env, firebase_replies, from_sign_up):
    fake = firebase_replies(**ok_replies())
    at = app([], secrets=GATED, owner=False)
    if from_sign_up:
        _open_sign_up(at)
    at.button(key="btn_login_google").click().run()
    assert google_env["calls"]["login"] == [None] and fake.calls == []


def test_switching_between_sign_in_and_sign_up_never_starts_google(app, google_env, firebase_replies):
    fake = firebase_replies(**ok_replies())
    at = app([], secrets=GATED, owner=False)
    for _ in range(2):
        _open_sign_up(at)
        assert "btn_signup_submit" in _buttons(at)
        at.button(key="btn_login_signin").click().run()
        assert "btn_login_email" in _buttons(at) and "Welcome back" in _text(at)
    assert google_env["calls"]["login"] == [] and fake.calls == []


def test_successful_sign_up_sends_verification_and_signs_nobody_in(app, google_env, firebase_replies):
    fake = firebase_replies(**ok_replies())
    at = app([NEW_RECORD], secrets=GATED, owner=False)
    _sign_up(at)
    assert fake.methods() == ["signUp", "update", "sendOobCode"]
    assert fake.calls[0][1] == {"email": NEW_EMAIL, "password": NEW_PW, "returnSecureToken": True}
    assert fake.calls[1][1]["displayName"] == "Asha Verma"                                   # tidied full name
    assert fake.calls[2][1]["requestType"] == "VERIFY_EMAIL"
    text = _text(at)
    assert _login_page(at) and "Graduate Software Engineer" not in text
    assert f"Account created. We've sent a verification link to {NEW_EMAIL}" in text
    assert ACCOUNT_KEY not in at.session_state and firebase_auth.SESSION_KEY not in at.session_state
    assert "btn_login_email" in _buttons(at) and at.text_input(key="login_email").value == NEW_EMAIL
    assert google_env["calls"]["login"] == []
    assert NEW_PW not in _dump(at) and NEW_PW not in text


def test_new_account_gets_in_only_after_firebase_says_verified(app, google_env, firebase_replies):
    fake = firebase_replies(**ok_replies(signInWithPassword=_verified_password_answer(
        uid=NEW_UID, email=NEW_EMAIL, verified=False)))
    at = app([NEW_RECORD], secrets=GATED, owner=False)
    _sign_up(at)
    _login(at, email=NEW_EMAIL, password=NEW_PW)                       # before opening the link
    text = _text(at)
    assert _login_page(at) and "Verify your email address first" in text
    assert ACCOUNT_KEY not in at.session_state and firebase_auth.SESSION_KEY not in at.session_state
    # the sign-up just sent a link: no second email within 15 minutes
    assert fake.methods() == ["signUp", "update", "sendOobCode", "signInWithPassword"]
    fake.by_method["signInWithPassword"] = _verified_password_answer(uid=NEW_UID, email=NEW_EMAIL) | {
        "displayName": "Asha Verma"}
    _login(at, email=NEW_EMAIL, password=NEW_PW)                       # after Firebase marked it verified
    assert not _login_page(at) and "Graduate Software Engineer" in _html(at)
    account = at.session_state[ACCOUNT_KEY]
    assert (account["uid"], account["email"], account["provider"], account["email_verified"], account["name"]) == (
        NEW_UID, NEW_EMAIL, "password", True, "Asha Verma")
    assert 'class="role admin"' not in _html(at)                       # a new member, not an admin


@pytest.mark.parametrize("field, value, message", [
    ("confirm", "something-else-2026", "The passwords don't match."),
    ("email", "not-an-email", "Enter a valid email address."),
    ("email", "two@@example.org", "Enter a valid email address."),
    ("email", "", "Enter your email address."),
    ("name", "   ", "Enter your full name."),
    ("name", "Asha‮Verma", "Full name: The name contains characters that can't be shown."),
    ("password", "short1", "Use at least 8 characters for your password."),
    ("email", "me@mailinator.com", email_policy.DISPOSABLE_MESSAGE),
])
def test_bad_sign_up_input_is_refused_before_firebase(app, google_env, firebase_replies, field, value, message):
    fake = firebase_replies(**ok_replies())
    at = app([], secrets=GATED, owner=False)
    kwargs = {"name": NAME, "email": NEW_EMAIL, "password": NEW_PW, "confirm": None, field: value}
    if field == "password":
        kwargs["confirm"] = value
    _sign_up(at, **kwargs)
    assert message in _text(at) and fake.calls == [] and google_env["calls"]["login"] == []
    assert "btn_signup_submit" in _buttons(at)                          # still on the form
    fields = _inputs(at)
    assert fields["signup_pw"].value == "" and fields["signup_pw2"].value == ""     # passwords wiped
    if field != "name":
        assert fields["signup_name"].value == NAME                                  # the rest stays
    assert ACCOUNT_KEY not in at.session_state


def test_already_registered_email_is_explained(app, google_env, firebase_replies):
    fake = firebase_replies(signUp=error_answer("EMAIL_EXISTS"))
    at = app([], secrets=GATED, owner=False)
    _sign_up(at)
    assert "An account with this email already exists. Sign in instead" in _text(at)
    assert fake.methods() == ["signUp"] and "btn_signup_submit" in _buttons(at)
    assert ACCOUNT_KEY not in at.session_state and firebase_auth.SESSION_KEY not in at.session_state


def test_firebase_unreachable_during_sign_up(app, google_env, monkeypatch):
    def boom(request):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(firebase_auth, "_new_client", lambda: httpx.Client(transport=httpx.MockTransport(boom)))
    at = app([], secrets=GATED, owner=False)
    _sign_up(at)
    assert "Couldn't reach Firebase — try again in a minute." in _text(at) and not at.exception


def test_verification_email_not_sent_is_said_and_sign_in_asks_again(app, google_env, firebase_replies):
    fake = firebase_replies(**ok_replies(sendOobCode=error_answer("TOO_MANY_ATTEMPTS_TRY_LATER"),
                                         signInWithPassword=_verified_password_answer(
                                             uid=NEW_UID, email=NEW_EMAIL, verified=False)))
    at = app([], secrets=GATED, owner=False)
    _sign_up(at)
    assert "Your account was created, but the verification email couldn't be sent just now." in _text(at)
    fake.by_method["sendOobCode"] = {}
    _login(at, email=NEW_EMAIL, password=NEW_PW)
    assert fake.methods()[-2:] == ["signInWithPassword", "sendOobCode"]      # a fresh link on sign-in
    assert _login_page(at) and ACCOUNT_KEY not in at.session_state


def test_signing_up_with_an_admin_address_grants_nothing(app, google_env, firebase_replies, admins):
    """Admin rights come from JT_ADMIN_EMAILS AND a Firebase-verified
    address — never from what was typed into the form."""
    admins(NEW_EMAIL)
    firebase_replies(**ok_replies(signInWithPassword=_verified_password_answer(
        uid=NEW_UID, email=NEW_EMAIL, verified=False)))
    at = app([NEW_RECORD], secrets=GATED, owner=False)
    _sign_up(at, name="Admin")
    assert ACCOUNT_KEY not in at.session_state and firebase_auth.SESSION_KEY not in at.session_state
    _login(at, email=NEW_EMAIL, password=NEW_PW)
    assert _login_page(at) and ACCOUNT_KEY not in at.session_state and 'class="role admin"' not in _html(at)


def test_sign_ups_are_rate_limited_per_address(app, google_env, firebase_replies):
    fake = firebase_replies(signUp=error_answer("EMAIL_EXISTS"))
    at = app([], secrets=GATED, owner=False)
    for _ in range(4):
        _sign_up(at)
    assert fake.methods() == ["signUp"] * 3 and "Too many sign-up attempts" in _text(at)


def test_without_firebase_there_is_no_sign_up_and_google_stays(app, google_env):
    at = app([], secrets={"auth": GATED["auth"]}, owner=False)
    assert "btn_login_signup" not in _buttons(at) and "btn_login_google" in _buttons(at)
    at.session_state["_login_mode"] = "signup"                        # even if forced, no form without Firebase
    at.run()
    assert "signup_email" not in _inputs(at)


def test_sign_up_page_shows_no_tracker_data(app, google_env):
    at = app([NEW_RECORD], secrets=GATED, owner=False)
    _open_sign_up(at)
    text = _text(at)
    assert "Graduate Software Engineer" not in text and "Admin" not in text
    catalogue = [c["name"] for c in json.loads((REPO / "data" / "companies.json").read_text("utf-8"))]
    assert not [n for n in catalogue if f">{n}<" in text]
