"""Phase 2 security: the sign-in gate fails closed, roles are enforced on
the server for every protected operation, visitors and members never see
internal details, and one person's session never carries into another's.
Google and Firebase are never contacted (see test_firebase_auth's fakes)."""
import ast
import json
import time
from html import unescape

import pytest

import access
import config_store
import firebase_auth
import notifier
from access import ACCOUNT_KEY
from test_firebase_auth import (  # noqa: F401  (fixture re-export)
    API_KEY, AUTH_GOOGLE, EMAIL, FB_SECRETS, GATED, PASSWORD, PROJECT, UID, FakeUser, _buttons, _dump,
    _fresh_google_token, _login, _login_page, admins, error_answer, google_env, make_id_token, no_env,
    ok_google_answer, ok_password_answer,
)
from test_security import _fake_github
from test_streamlit_app import FRESHER_RECORD, NEW_RECORD, OWNER_PASSWORD, REPO, _companies, _html, _key, _nav, _seen, app  # noqa: F401

ADMIN = EMAIL.lower()
BOSS = "boss@example.org"                 # an admin who is NOT the signed-in test user
RECIPIENT = "alerts.private@example.org"
DISMISSED = {**FRESHER_RECORD, "dismissed": True}
ADMIN_BUTTONS = {"btn_test", "btn_clear_all", "btn_run_check", "btn_open_add", "btn_remove_company"}
# internal names a visitor or member must never be shown
INTERNALS = ("FIREBASE_", "JT_ADMIN_EMAILS", "JT_OWNER_PASSWORD", "ALERT_RECIPIENT", "expose_tokens", "redirect_uri",
             "client_secret", "cookie_secret", "[auth]", API_KEY, PROJECT, "admin-diag", "auth-events")


def _password_answer(uid, email, verified=None):
    extra = {} if verified is None else {"emailVerified": verified}
    return ok_password_answer(localId=uid, email=email, idToken=make_id_token(uid=uid, email=email), **extra)


def _no_internals(html: str):
    for name in INTERNALS:
        assert name not in html, name


# ---------------------------------------------------------------------------
# Unauthenticated visitors: the login page and nothing else
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("query", [
    {}, {"page": "jobs"}, {"page": "companies"}, {"page": "monitoring"}, {"page": "email"}, {"page": "settings"},
    {"page": "companies", "company": "sanofi"}, {"page": "companies", "view": "add"}, {"diag": "auth"},
])
def test_visitors_get_nothing_but_the_login_page(app, google_env, admins, monkeypatch, query):
    admins(ADMIN)
    monkeypatch.setenv("ALERT_RECIPIENT", RECIPIENT)
    at = app([NEW_RECORD, DISMISSED], query=query, secrets=GATED, owner=False)
    assert not at.exception and _login_page(at)
    html = _html(at)
    for data in ("Graduate Software Engineer", FRESHER_RECORD["title"], "Sanofi", "MetLife", RECIPIENT, access.mask_email(RECIPIENT),
                 "Monitoring", "Sign-in diagnostics"):
        assert data not in html, data
    assert not _buttons(at) & (ADMIN_BUTTONS | {"nav_home", "nav_settings", "btn_account_sign_out"})
    _no_internals(html)
    assert ACCOUNT_KEY not in at.session_state


@pytest.mark.parametrize("secrets", [
    {"FIREBASE_WEB_API_KEY": API_KEY},                                       # project ID missing
    {"FIREBASE_PROJECT_ID": PROJECT},                                        # key missing
    {"FIREBASE_WEB_API_KEY": API_KEY, "FIREBASE_PROJECT_ID": "Job Tracker"},  # a typo
    {"auth": {"redirect_uri": "https://x.example/oauth2callback"}},          # half a Google setup
    {"JT_ADMIN_EMAILS": ADMIN},                                              # only the admin list
])
def test_any_partial_sign_in_setup_fails_closed(app, no_env, secrets):
    """Once any part of sign-in is configured, a mistake makes sign-in
    unavailable — it never opens the app to everyone."""
    at = app([NEW_RECORD], secrets=secrets, owner=False)
    assert not at.exception and _login_page(at)
    html = _html(at)
    assert "Graduate Software Engineer" not in html and "nav_home" not in _buttons(at)
    _no_internals(html)


def test_a_leftover_owner_flag_never_opens_a_gated_app_with_admins(app, no_env):
    at = app([NEW_RECORD], secrets={"JT_ADMIN_EMAILS": ADMIN, **FB_SECRETS})       # owner=True: stale flag
    assert _login_page(at) and "login_owner_pw" not in {t.key for t in at.text_input}


def test_without_any_sign_in_setup_the_legacy_app_is_unchanged(app, no_env):
    """A deployment with no sign-in configuration at all (local runs, the
    tests) keeps the legacy behaviour: browse, owner password for changes."""
    at = app([NEW_RECORD], owner=False)
    assert not _login_page(at) and "Graduate Software Engineer" in _html(at)


# ---------------------------------------------------------------------------
# Google sign-in keeps working without Firebase (identity from st.login)
# ---------------------------------------------------------------------------

def test_google_admin_without_firebase_config(app, google_env, admins):
    admins(ADMIN)
    fb = google_env["firebase"](ok_google_answer())
    google_env["set_user"](FakeUser(token=_fresh_google_token()))
    at = app([NEW_RECORD], secrets={"auth": AUTH_GOOGLE}, owner=False)
    assert not _login_page(at) and 'class="role admin"' in _html(at)
    assert fb.requests == []                                       # nothing to link to
    _nav(at, "jobs")
    at.button(key=_key("dismiss", NEW_RECORD)).click().run()
    assert _seen(at)[0]["dismissed"] is True
    _nav(at, "settings")
    assert "<dd>Not linked</dd>" in _html(at)


def test_login_page_without_firebase_offers_google_only(app, google_env):
    at = app([], secrets={"auth": AUTH_GOOGLE}, owner=False)
    assert _login_page(at) and "btn_login_google" in _buttons(at) and "btn_login_email" not in _buttons(at)


# ---------------------------------------------------------------------------
# Members: read-only, enforced on the server for every operation
# ---------------------------------------------------------------------------

def _member(app, google_env, admins, seen, **kw):
    admins(BOSS)
    google_env["firebase"](ok_google_answer())
    google_env["set_user"](FakeUser(token=_fresh_google_token()))
    return app(seen, secrets=GATED, owner=False, **kw)


@pytest.mark.parametrize("action", [
    "row_dismiss", "row_restore", "detail_dismiss", "detail_restore", "dismiss_all",
    "add_company", "remove_company", "test_email", "run_check",
])
def test_members_cannot_change_anything(app, google_env, admins, monkeypatch, action):
    sent, dispatched = [], []
    monkeypatch.setattr(notifier, "test_mail", lambda to: sent.append(to))
    monkeypatch.setenv("ALERT_RECIPIENT", RECIPIENT)
    _fake_github(monkeypatch, dispatched)
    if action == "run_check":
        monkeypatch.setenv("GITHUB_TOKEN", "test-token")
        monkeypatch.setattr(config_store, "fetch_remote_json", lambda *a, **k: {})
    at = _member(app, google_env, admins, [NEW_RECORD, DISMISSED],
                 **({"query": {"page": "companies", "company": "sanofi"}} if action == "remove_company" else {}))
    assert 'class="role admin"' not in _html(at)
    seen, companies = _seen(at), _companies(at)
    if action == "row_dismiss":
        _nav(at, "jobs")
        at.button(key=_key("dismiss", NEW_RECORD)).click().run()
    elif action == "row_restore":
        _nav(at, "jobs")
        at.pills(key="jobs_tab").set_value("dismissed").run()
        at.button(key=_key("restore", DISMISSED)).click().run()
    elif action in ("detail_dismiss", "detail_restore"):
        rec = NEW_RECORD if action == "detail_dismiss" else DISMISSED
        _nav(at, "jobs")
        if rec is DISMISSED:
            at.pills(key="jobs_tab").set_value("dismissed").run()
        at.button(key=_key("crit", rec)).click().run()
        at.button(key=action).click().run()
    elif action == "dismiss_all":
        _nav(at, "settings")
        at.button(key="btn_clear_all").click().run()
        assert "btn_clear_all_confirm" not in _buttons(at)
    elif action == "add_company":
        _nav(at, "companies")
        at.button(key="btn_open_add").click().run()
        at.text_input(key="new_name").set_value("Evil Corp").run()
        at.text_input(key="new_url").set_value("https://evil.example/jobs").run()
        at.button(key="btn_add").click().run()
    elif action == "remove_company":
        at.button(key="btn_remove_company").click().run()
        assert "btn_remove" not in _buttons(at)
    elif action == "test_email":
        _nav(at, "email")
        at.button(key="btn_test").click().run()
    elif action == "run_check":
        _nav(at, "monitoring")
        at.button(key="btn_run_check").click().run()
    assert not at.exception
    assert "Only admins can" in _html(at)
    assert _seen(at) == seen and _companies(at) == companies
    assert sent == [] and dispatched == []


def test_members_see_no_internal_details(app, google_env, admins, monkeypatch):
    monkeypatch.setenv("ALERT_RECIPIENT", RECIPIENT)
    at = _member(app, google_env, admins, [NEW_RECORD])
    for page in ("home", "jobs", "companies", "monitoring", "email", "settings"):
        _nav(at, page)
        html = _html(at)
        _no_internals(html)
        assert RECIPIENT not in html and UID not in html and "owner_pw" not in {t.key for t in at.text_input}
        if page == "email":
            assert access.mask_email(RECIPIENT) in html and "Managed by an admin" in html


def test_forged_session_state_never_grants_a_role(app, google_env, admins):
    """The gate rebuilds the account from its source on every run; whatever
    sits in session state is overwritten, never trusted."""
    admins(ADMIN)
    forged = {"email": ADMIN, "email_verified": True, "name": "", "uid": UID, "provider": "google.com"}
    at = app([NEW_RECORD], secrets=GATED, owner=False)                    # a visitor
    at.session_state[ACCOUNT_KEY] = forged
    at.run()
    assert _login_page(at) and ACCOUNT_KEY not in at.session_state
    # a member (someone else) holding an admin record
    google_env["firebase"](ok_google_answer())
    google_env["set_user"](FakeUser(token=_fresh_google_token(), email="member@example.org"))
    at = app([NEW_RECORD], secrets=GATED, owner=False)
    at.session_state[ACCOUNT_KEY] = forged
    _nav(at, "jobs")
    at.button(key=_key("dismiss", NEW_RECORD)).click().run()
    assert not _seen(at)[0].get("dismissed") and at.session_state[ACCOUNT_KEY]["email"] == "member@example.org"


def test_firebase_uid_is_kept_only_for_the_same_identity(app, google_env, admins):
    """If Firebase answers for a different email than the Google identity,
    that UID is never attached to this account; the role still comes from
    Google's verified identity alone."""
    admins(ADMIN)
    google_env["firebase"](ok_google_answer(email="someone.else@example.org"))
    google_env["set_user"](FakeUser(token=_fresh_google_token()))
    at = app([], secrets=GATED, owner=False)
    account = at.session_state[ACCOUNT_KEY]
    assert account["email"] == ADMIN and account["uid"] == "" and 'class="role admin"' in _html(at)
    google_env["firebase"](ok_google_answer())
    at = app([], secrets=GATED, owner=False)
    assert at.session_state[ACCOUNT_KEY]["uid"] == UID


# ---------------------------------------------------------------------------
# Admins: exactly the operations intended for them
# ---------------------------------------------------------------------------

def _admin(app, google_env, admins, seen, **kw):
    admins(f"{BOSS}, {ADMIN}")
    google_env["firebase"](ok_google_answer())
    google_env["set_user"](FakeUser(token=_fresh_google_token()))
    return app(seen, secrets=GATED, owner=False, **kw)


def test_admin_operations(app, google_env, admins, monkeypatch):
    sent = []
    monkeypatch.setattr(notifier, "test_mail", lambda to: sent.append(to))
    monkeypatch.setenv("ALERT_RECIPIENT", RECIPIENT)
    at = _admin(app, google_env, admins, [NEW_RECORD, DISMISSED])
    assert 'class="role admin"' in _html(at)
    _nav(at, "jobs")
    at.button(key=_key("dismiss", NEW_RECORD)).click().run()
    assert all(r.get("dismissed") for r in _seen(at))
    at.pills(key="jobs_tab").set_value("dismissed").run()
    at.button(key=_key("restore", DISMISSED)).click().run()
    assert not {r["title"]: r for r in _seen(at)}[FRESHER_RECORD["title"]].get("dismissed")
    _nav(at, "email")
    assert RECIPIENT in _html(at)                                  # admins see the full address
    at.button(key="btn_test").click().run()
    assert sent == [RECIPIENT]                                     # only ever the configured recipient
    _nav(at, "companies")
    at.button(key="btn_open_add").click().run()
    at.text_input(key="new_name").set_value("Infosys").run()
    at.text_input(key="new_url").set_value("https://careers.infosys.com").run()
    at.button(key="btn_add").click().run()
    assert _companies(at)[-1]["name"] == "Infosys"
    _nav(at, "settings")
    at.button(key="btn_clear_all").click().run()
    at.button(key="btn_clear_all_confirm").click().run()
    assert all(r.get("dismissed") for r in _seen(at))


def test_admin_can_open_diagnostics(app, google_env, admins):
    at = _admin(app, google_env, admins, [], query={"diag": "auth"})
    html = _html(at)
    assert "Sign-in diagnostics" in html and ADMIN not in html and BOSS not in html and API_KEY not in html


def test_admin_test_email_rechecks_the_role_at_send_time(app, google_env, admins, monkeypatch):
    """The button checks, and the operation itself checks again: a role
    lost between the two sends nothing."""
    sent = []
    monkeypatch.setattr(notifier, "test_mail", lambda to: sent.append(to))
    monkeypatch.setenv("ALERT_RECIPIENT", RECIPIENT)
    at = _admin(app, google_env, admins, [], page="email")
    monkeypatch.setenv("JT_ADMIN_EMAILS", BOSS)                    # removed from the list meanwhile
    at.button(key="btn_test").click().run()
    assert sent == [] and "Only admins can send test emails." in _html(at)


# ---------------------------------------------------------------------------
# Verified vs unverified identity; email + password admins
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("verified, is_admin", [(True, True), (False, False), (None, False)])
def test_password_accounts_are_admin_only_with_a_verified_email(app, google_env, admins, verified, is_admin):
    admins(ADMIN)
    google_env["firebase"](_password_answer(UID, EMAIL, verified))
    at = app([NEW_RECORD], secrets=GATED, owner=False)
    _login(at)
    assert not _login_page(at) and ('class="role admin"' in _html(at)) is is_admin
    _nav(at, "jobs")
    at.button(key=_key("dismiss", NEW_RECORD)).click().run()
    assert bool(_seen(at)[0].get("dismissed")) is is_admin


def test_expired_password_sign_in_loses_admin_rights(app, google_env, admins):
    admins(ADMIN)
    google_env["firebase"](_password_answer(UID, EMAIL, True))
    at = app([NEW_RECORD], secrets=GATED, owner=False)
    _login(at)
    assert 'class="role admin"' in _html(at)
    at.session_state[firebase_auth.SESSION_KEY] = {**at.session_state[firebase_auth.SESSION_KEY],
                                                   "expires_at": time.time() - 1}
    _nav(at, "jobs")
    assert _login_page(at) and not _seen(at)[0].get("dismissed")


# ---------------------------------------------------------------------------
# Login failures: a lasting, human message; nothing retained
# ---------------------------------------------------------------------------

def _email_form(at):
    # AppTest lists an expander that has an icon as a "status" element
    (form,) = [e.proto for e in [*at.expander, *at.get("status")] if e.proto.label == "Sign in with email and password"]
    return form


@pytest.mark.parametrize("reply, shown", [
    (error_answer("USER_DISABLED"), "This account has been disabled."),
    (error_answer("TOO_MANY_ATTEMPTS_TRY_LATER : Access disabled"), "Too many attempts"),
    (OSError("connection refused to identitytoolkit"), "Couldn't reach Firebase"),
    (error_answer("INVALID_LOGIN_CREDENTIALS"), "Email or password is incorrect."),
])
def test_login_failures(app, google_env, reply, shown):
    google_env["firebase"](reply)
    at = app([NEW_RECORD], secrets=GATED, owner=False)
    _login(at, password="a-wrong-password-1")
    html = unescape(_html(at))
    assert _login_page(at) and shown in html and "Graduate Software Engineer" not in html
    assert "a-wrong-password-1" not in _dump(at) and "identitytoolkit" not in html
    assert ACCOUNT_KEY not in at.session_state and firebase_auth.SESSION_KEY not in at.session_state
    assert _email_form(at).expanded


def test_email_form_is_tucked_away_behind_google(app, google_env):
    at = app([], secrets=GATED, owner=False)
    assert not _email_form(at).expanded and "btn_login_google" in _buttons(at)


# ---------------------------------------------------------------------------
# Cross-user and session isolation
# ---------------------------------------------------------------------------

def test_two_people_at_once_never_see_each_other(app, google_env, admins):
    """Two browser sessions on one server process: each sees only its own
    account and role."""
    admins(ADMIN)
    member_email, member_uid = "member@example.org", "MemberUid000000000000000001"
    google_env["firebase"](_password_answer(UID, EMAIL, True))
    a = app([NEW_RECORD], secrets=GATED, owner=False)
    _login(a)
    google_env["firebase"](_password_answer(member_uid, member_email))
    b = app([NEW_RECORD], secrets=GATED, owner=False)
    _login(b, email=member_email)
    a.run()
    assert 'class="role admin"' in _html(a) and member_email not in _html(a) and member_uid not in _dump(a)
    assert 'class="role admin"' not in _html(b) and ADMIN not in _html(b) and UID not in _dump(b)
    _nav(b, "jobs")
    b.button(key=_key("dismiss", NEW_RECORD)).click().run()
    assert not _seen(b)[0].get("dismissed")
    c = app([NEW_RECORD], secrets=GATED, owner=False)                   # a third, anonymous tab
    assert _login_page(c) and ADMIN not in _html(c) and member_email not in _html(c)


def test_sign_out_leaves_nothing_for_the_next_person_in_the_tab(app, google_env, admins, monkeypatch):
    admins(ADMIN)
    monkeypatch.setenv("ALERT_RECIPIENT", RECIPIENT)
    monkeypatch.setattr(notifier, "test_mail", lambda to: None)
    google_env["firebase"](_password_answer(UID, EMAIL, True))
    at = app([NEW_RECORD], secrets=GATED, owner=False)
    _login(at)
    _nav(at, "email")
    at.button(key="btn_test").click().run()
    assert "Sent at" in _html(at)
    at.button(key="btn_account_sign_out").click().run()
    assert _login_page(at)
    left = at.session_state.to_dict()
    # the theme, the sign-out marker, routing read back from the URL and the
    # (empty) login form — nothing that belonged to the account
    allowed = {"dark_mode", "_signed_out", "page", "job_id", "company_id", "company_view", "confirm_remove",
               "login_email", "login_pw", "login_owner_pw", "btn_login_email", "btn_login_google", "btn_login_owner"}
    assert {k for k in left if not k.startswith("$$")} <= allowed, left
    assert not left.get("login_email") and not left.get("login_pw")
    member = "member@example.org"
    google_env["firebase"](_password_answer("MemberUid000000000000000001", member))
    _login(at, email=member)
    html = _html(at)
    assert not _login_page(at) and 'class="role admin"' not in html and ADMIN not in html
    _nav(at, "email")
    html = _html(at)
    assert "Not sent this session" in html and RECIPIENT not in html and UID not in _dump(at)


def test_shared_caches_hold_no_per_user_data():
    """Streamlit caches are shared by every session of the process. Only
    these may exist, none takes an argument (so nothing can be keyed by a
    user), and none is about accounts. A new cache must be reviewed here."""
    tree = ast.parse((REPO / "streamlit_app.py").read_text("utf-8"))
    cached = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for d in node.decorator_list:
                src = ast.unparse(d)
                if "cache_data" in src or "cache_resource" in src:
                    cached[node.name] = [a.arg for a in node.args.args]
    # _source_label takes a company record (shared data), never anything per-user
    # _user_store_for is the Firestore client for one credential (Phase 3): it
    # holds no one's data; its key is a non-secret credential fingerprint and
    # the project ID; every per-user read goes through a UserData bound to the
    # session's own uid. ("Not configured" and failures are never cached.)
    assert cached == {"_logo_data_uri": [], "_source_label": ["c"], "_guards": [], "_remote_snapshot": [],
                      "_user_store_for": ["fingerprint", "project_id"]}, cached


# ---------------------------------------------------------------------------
# access.py units: JT_ADMIN_EMAILS, owner password, identity re-check
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("A@Example.com", {"a@example.com"}),
    (" a@x.org , b@y.org;c@z.org\n d@w.org ", {"a@x.org", "b@y.org", "c@z.org", "d@w.org"}),
    ("not-an-email, ", set()),
    ("", set()),
])
def test_admin_list_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv("JT_ADMIN_EMAILS", raw)
    assert access.admin_emails() == expected and access.admins_configured() is bool(expected)


@pytest.mark.parametrize("account, is_admin", [
    ({"email": "A@x.org", "email_verified": True}, True),
    ({"email": "a@x.org", "email_verified": True, "expires_at": time.time() + 10 ** 8}, True),   # far future: params are built at collection time
    ({"email": "a@x.org", "email_verified": True, "expires_at": time.time() - 1}, False),
    ({"email": "a@x.org", "email_verified": True, "expires_at": "soon"}, False),
    ({"email": "a@x.org", "email_verified": "true"}, False),        # only a real boolean True counts
    ({"email": "a@x.org"}, False),
    ({"email": "other@x.org", "email_verified": True}, False),
    ({"email": None, "email_verified": True}, False),
    (None, False), ("a@x.org", False),
])
def test_account_is_admin(monkeypatch, account, is_admin):
    monkeypatch.setenv("JT_ADMIN_EMAILS", "a@x.org")
    assert access.account_is_admin(account) is is_admin


def test_owner_password_is_a_break_glass_only_until_admins_exist(monkeypatch):
    monkeypatch.setenv("JT_OWNER_PASSWORD", OWNER_PASSWORD)
    flag = {access.OWNER_SESSION_KEY: time.time() + 3600}
    monkeypatch.delenv("JT_ADMIN_EMAILS", raising=False)
    assert access.owner_password_enabled() and access.owner_session_valid(flag)
    monkeypatch.setenv("JT_ADMIN_EMAILS", ADMIN)
    assert not access.owner_password_enabled() and not access.owner_session_valid(flag)
    monkeypatch.setenv("JT_OWNER_PASSWORD", "short")
    monkeypatch.delenv("JT_ADMIN_EMAILS")
    assert not access.owner_password_enabled() and not access.owner_session_valid(flag)


def test_owner_password_cannot_sign_in_once_admins_exist(app, no_env, monkeypatch):
    """Even the legacy Settings form (no sign-in configured) refuses the
    password while an admin list exists — it would grant nothing anyway."""
    at = app([NEW_RECORD], page="settings", owner=False)
    monkeypatch.setenv("JT_ADMIN_EMAILS", ADMIN)          # set after the page was drawn
    at.text_input(key="owner_pw").set_value(OWNER_PASSWORD)
    at.button(key="btn_sign_in").click().run()
    assert access.OWNER_SESSION_KEY not in at.session_state and OWNER_PASSWORD not in _dump(at)


GOOGLE_ACCT = {"email": "a@x.org", "email_verified": True, "uid": "U1", "provider": "google.com"}
PASSWORD_ACCT = {"email": "a@x.org", "email_verified": True, "uid": "U1", "provider": "password"}
FB = {"uid": "U1", "email": "a@x.org", "provider": "password"}


@pytest.mark.parametrize("account, google, fb_user, ok", [
    (GOOGLE_ACCT, {"email": "a@x.org", "email_verified": True}, None, True),
    (GOOGLE_ACCT, None, None, False),                                        # signed out of Google
    (GOOGLE_ACCT, {"email": "b@x.org", "email_verified": True}, None, False),  # another Google account
    (GOOGLE_ACCT, {"email": "", "email_verified": True}, None, False),
    (PASSWORD_ACCT, None, FB, True),
    (PASSWORD_ACCT, None, None, False),                                      # sign-in expired
    (PASSWORD_ACCT, None, {**FB, "uid": "U2"}, False),                        # another Firebase account
    (PASSWORD_ACCT, None, {**FB, "email": "b@x.org"}, False),
    (PASSWORD_ACCT, None, {**FB, "provider": "google.com"}, False),
    (PASSWORD_ACCT, {"email": "a@x.org", "email_verified": True}, None, False),  # Google doesn't stand in
    ({**GOOGLE_ACCT, "provider": "made-up"}, {"email": "a@x.org"}, FB, False),
    (None, {"email": "a@x.org"}, FB, False),
])
def test_account_must_still_match_its_source(account, google, fb_user, ok):
    assert access.account_matches_source(account, google, fb_user) is ok


def test_owner_account_matches_only_while_the_break_glass_is_enabled(monkeypatch):
    owner = {"provider": "owner"}
    monkeypatch.setenv("JT_OWNER_PASSWORD", OWNER_PASSWORD)
    monkeypatch.delenv("JT_ADMIN_EMAILS", raising=False)
    assert access.account_matches_source(owner, None, None)
    monkeypatch.setenv("JT_ADMIN_EMAILS", ADMIN)
    assert not access.account_matches_source(owner, None, None)
