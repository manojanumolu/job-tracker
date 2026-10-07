"""The redesigned login page: presentation only. Every control must reach
the EXISTING sign-in handlers (Firebase email/password via _login_submit,
Google via st.login), nothing about any account or the tracker's data may
show before sign-in, and the artwork is static. Google/Firebase are never
contacted (test_firebase_auth's fakes)."""
import json
import re

import pytest

from test_firebase_auth import (  # noqa: F401  (fixture re-export)
    AUTH_GOOGLE, EMAIL, FB_SECRETS, GATED, PASSWORD, _buttons, _login, _login_page, error_answer, google_env, no_env,
    ok_password_answer,
)
from test_streamlit_app import NEW_RECORD, REPO, _html, app  # noqa: F401


def _labels(at) -> dict:
    return {t.key: (t.label, t.placeholder) for t in at.text_input}


def test_the_card_matches_the_design(app, google_env):
    at = app([NEW_RECORD], secrets=GATED, owner=False)
    html = _html(at)
    assert _login_page(at) and not at.exception
    assert "Welcome back" in html and "Sign in to continue discovering fresher opportunities." in html
    assert _labels(at)["login_email"] == ("Email address", "you@example.com")
    assert _labels(at)["login_pw"] == ("Password", "Enter your password")
    assert next(t for t in at.text_input if t.key == "login_pw").proto.type == 1          # a password field (show/hide)
    sign_in = at.button(key="btn_login_email")
    assert sign_in.label == "Sign in" and sign_in.proto.type == "primary"
    assert at.button(key="btn_login_google").label == "Continue with Google"
    assert '<div class="lp-or">OR</div>' in html
    assert at.button(key="btn_login_signup").label == "Don't have an account? **Sign up**"


def test_the_left_side_is_generic_artwork(app, google_env):
    at = app([NEW_RECORD], secrets=GATED, owner=False)
    html = _html(at)
    assert "Discover your next<br><em>opportunity.</em>" in html
    for line in ("Fresh opportunities", "From top companies", "Stay ahead", "Get notified early",
                 "Track what matters", "Follow your preferred companies"):
        assert line in html
    stage = html.split('<div class="lp-stage" aria-hidden="true">', 1)[1]
    assert stage.count('class="lp-card ') == 3                                            # decorative, hidden from readers


def test_no_statistics_or_tracker_data_before_sign_in(app, google_env):
    """The reference's stats strip is deliberately absent; the cards are
    fixed artwork, never the tracker's companies or jobs."""
    at = app([NEW_RECORD], secrets=GATED, owner=False)
    html = _html(at)
    for word in ("Active fresher jobs", "Companies monitored", "Last scan", "Next scheduled", "jobs found",
                 "Graduate Software Engineer", "Alerts on", "Admin", "Member"):
        assert word not in html, word
    catalogue = [c["name"] for c in json.loads((REPO / "companies.json").read_text("utf-8"))]
    assert not [name for name in catalogue if name in html]


def test_no_inline_svg(app, google_env):
    """Inline <svg> doesn't paint in this app's hosting: the Google mark is
    a CSS background image."""
    at = app([], secrets=GATED, owner=False)
    html = _html(at)
    assert "<svg" not in html.replace("data:image/svg+xml", "")
    assert re.search(r"\.st-key-btn_login_google .*::before \{[^}]*url\(\"data:image/svg\+xml;base64,", html)


def test_sign_in_uses_the_existing_firebase_handler(app, google_env):
    fb = google_env["firebase"](ok_password_answer())
    at = app([NEW_RECORD], secrets=GATED, owner=False)
    _login(at)
    assert not _login_page(at) and len(fb.requests) == 1
    assert fb.requests[0].url.path.endswith("accounts:signInWithPassword")
    assert json.loads(fb.requests[0].content)["email"] == EMAIL                         # what was typed, unchanged


def test_failed_sign_in_shows_the_existing_generic_message(app, google_env):
    google_env["firebase"](error_answer("INVALID_LOGIN_CREDENTIALS"))
    at = app([], secrets=GATED, owner=False)
    _login(at, password="a-wrong-one-123")
    assert _login_page(at) and "Email or password is incorrect." in _html(at)


@pytest.mark.parametrize("button", ["btn_login_google", "btn_login_signup"])
def test_google_and_sign_up_both_start_the_existing_google_sign_in(app, google_env, button):
    """New accounts are created by the first Google sign-in, so "Sign up"
    is that same, existing flow — no separate sign-up exists."""
    at = app([], secrets=GATED, owner=False)
    at.button(key=button).click().run()
    assert google_env["calls"]["login"] == [None]


def test_forgot_password_is_help_text_not_an_auth_action(app, google_env):
    """There is no password-reset flow in this app; the link explains who
    can reset one and triggers nothing."""
    at = app([], secrets=GATED, owner=False)
    html = _html(at)
    assert "Email-and-password accounts are managed by the workspace admin" in html
    assert "continue with Google instead" in html
    # nothing new to click: the existing sign-in buttons, plus the existing owner
    # break-glass (shown here because these tests set no admin list)
    assert {k for k in _buttons(at) if k and k.startswith("btn_login")} == {
        "btn_login_email", "btn_login_google", "btn_login_signup", "btn_login_owner"}


def test_without_google_there_is_no_sign_up_link(app, no_env):
    at = app([], secrets=FB_SECRETS, owner=False)
    assert _login_page(at)
    assert "btn_login_google" not in _buttons(at) and "btn_login_signup" not in _buttons(at)
    assert "btn_login_email" in _buttons(at) and "managed by the workspace admin" in _html(at)
    assert "continue with Google instead" not in _html(at) and '<div class="lp-or">' not in _html(at)


def test_without_firebase_only_google_and_sign_up(app, google_env):
    at = app([], secrets={"auth": AUTH_GOOGLE}, owner=False)
    assert "btn_login_email" not in _buttons(at) and "login_email" not in _labels(at)
    assert {"btn_login_google", "btn_login_signup"} <= _buttons(at)
    assert "managed by the workspace admin" not in _html(at) and '<div class="lp-or">' not in _html(at)


def test_motion_is_slow_and_respects_reduced_motion(app, google_env):
    at = app([], secrets=GATED, owner=False)
    css = _html(at)
    durations = [float(d) for d in re.findall(r"jt-float(?:-s)? (\d+(?:\.\d+)?)s", css.split(".lp-card.c1", 1)[1][:2000])]
    assert durations and min(durations) >= 8                                             # slow drift, no bouncing
    reduced = css.split("@media (prefers-reduced-motion: reduce) {", 2)[-1]
    assert ".lp-card, .lp-chip, .lp-glow, .lp-dot::after, .st-key-login_card { animation: none !important; }" in reduced
