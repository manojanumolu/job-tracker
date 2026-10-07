"""Activation of personal data while the server keeps running.

Production evidence (2026-10-07): after FIREBASE_SERVICE_ACCOUNT was added to
the Streamlit secrets, Settings showed no "Personal data" row — the app still
believed no service account existed. Cause: _user_store() was a no-argument
@st.cache_resource, so its first answer ("not configured") was kept for the
life of the server process; Streamlit reloads st.secrets when they change but
does not clear st.cache_resource.

Here the real user_store.configured() reads the real (fake-valued) secret;
only user_store.configure() — the part that would contact Firebase — is
replaced. No real credential is ever used."""
import json

import pytest

import user_store
from user_store import MemoryBackend, UserStore, UserStoreError
from test_firebase_auth import GATED, _login, _login_page, admins, google_env, no_env  # noqa: F401
from test_phase3_user_data import A_MAIL, A_UID, B_MAIL, B_UID, FakeIdentity, _password
from test_streamlit_app import NEW_RECORD, _html, _nav, app  # noqa: F401

SA = {"type": "service_account", "project_id": "job-tracker-test", "private_key_id": "key-1",
      "private_key": "VALID", "client_email": "svc@job-tracker-test.iam.gserviceaccount.com"}


class FakeConfigure:
    """configure() stand-in: a working store for a "VALID" key, the real
    refusal text otherwise; counts how often a store had to be built."""
    def __init__(self):
        self.calls, self.stores = 0, []

    def __call__(self, secrets, project_id=""):
        self.calls += 1
        info = user_store._service_account(secrets)
        if info is None:
            return None, ""
        if info.get("private_key") != "VALID":
            return None, "the Firebase service account secret is incomplete"
        store = UserStore(MemoryBackend(), FakeIdentity())
        self.stores.append(store)
        return store, ""


@pytest.fixture
def fake(monkeypatch):
    f = FakeConfigure()
    monkeypatch.setattr(user_store, "configure", f)
    return f


def _admin_session(app, google_env, admins, secrets):
    admins(A_MAIL)
    google_env["firebase"](_password(A_UID, A_MAIL))
    at = app([NEW_RECORD], secrets=secrets, owner=False)
    _login(at, email=A_MAIL)
    assert not _login_page(at)
    if "btn_onboarding_skip" in {b.key for b in at.button}:
        at.button(key="btn_onboarding_skip").click().run()
    _nav(at, "settings")
    return at


def _personal_row(at) -> str:
    if at.session_state.page == "onboarding":           # the store just came on: first-sign-in setup
        at.button(key="btn_onboarding_skip").click().run()
        _nav(at, "settings")
    html = _html(at)
    if "<dt>Personal data</dt>" not in html:
        return ""
    return html.split("<dt>Personal data</dt><dd>", 1)[1].split("</dd>", 1)[0]


def test_a_secret_added_while_the_server_runs_is_used_without_a_reboot(app, google_env, admins, fake):
    at = _admin_session(app, google_env, admins, GATED)                    # started without the secret
    assert _personal_row(at).startswith("Not switched on")
    at.secrets["FIREBASE_SERVICE_ACCOUNT"] = "\n" + json.dumps(SA, indent=2) + "\n"   # added later, TOML-style
    at.run()
    assert _personal_row(at) == "Saved to your own account"                   # was stuck before the fix


def test_a_broken_secret_that_gets_fixed_is_used_without_a_reboot(app, google_env, admins, fake):
    at = _admin_session(app, google_env, admins, {**GATED, "FIREBASE_SERVICE_ACCOUNT": json.dumps({**SA, "private_key": "BROKEN"})})
    row = _personal_row(at)
    assert row.startswith(user_store.UNAVAILABLE) and "the Firebase service account secret is incomplete" in row
    at.secrets["FIREBASE_SERVICE_ACCOUNT"] = json.dumps(SA)                  # fixed, same key ID
    at.run()
    assert _personal_row(at) == "Saved to your own account"                   # the failure was not cached


def test_removing_the_secret_switches_personal_data_off_at_once(app, google_env, admins, fake):
    at = _admin_session(app, google_env, admins, {**GATED, "FIREBASE_SERVICE_ACCOUNT": json.dumps(SA)})
    assert _personal_row(at) == "Saved to your own account"
    del at.secrets["FIREBASE_SERVICE_ACCOUNT"]
    at.run()
    assert _personal_row(at).startswith("Not switched on")


def test_a_working_store_is_built_once_and_shared(app, google_env, admins, fake):
    secrets = {**GATED, "FIREBASE_SERVICE_ACCOUNT": json.dumps(SA)}
    first = _admin_session(app, google_env, admins, secrets)
    google_env["firebase"](_password(B_UID, B_MAIL))
    second = app([NEW_RECORD], secrets=secrets, owner=False)
    _login(second, email=B_MAIL)
    for _ in range(3):
        first.run()
        second.run()
    assert fake.calls == 1 and len(fake.stores) == 1                          # one Firestore client per credential
    assert set(fake.stores[0].backend.docs) == {f"users/{A_UID}", f"users/{B_UID}"}


def test_a_new_key_gets_its_own_store(app, google_env, admins, fake):
    at = _admin_session(app, google_env, admins, {**GATED, "FIREBASE_SERVICE_ACCOUNT": json.dumps(SA)})
    at.secrets["FIREBASE_SERVICE_ACCOUNT"] = json.dumps({**SA, "private_key_id": "key-2"})   # a rotated key
    at.run()
    assert fake.calls == 2 and _personal_row(at) == "Saved to your own account"


def test_members_never_see_configuration_details(app, google_env, admins, fake):
    admins("boss@example.org")
    google_env["firebase"](_password(B_UID, B_MAIL))
    at = app([NEW_RECORD], secrets=GATED, owner=False)                      # no secret: no row for a member
    _login(at, email=B_MAIL)
    _nav(at, "settings")
    assert _personal_row(at) == ""
    at.secrets["FIREBASE_SERVICE_ACCOUNT"] = json.dumps({**SA, "private_key": "BROKEN"})
    at.run()
    assert _personal_row(at) == user_store.UNAVAILABLE                        # plain message only
    assert "incomplete" not in _html(at) and "service account" not in _html(at).lower()


def test_an_admin_sees_the_error_category_not_the_error(app, google_env, admins, monkeypatch):
    class Denied(Exception):
        """Shaped like google.api_core.exceptions.PermissionDenied."""
    class DeniedBackend(MemoryBackend):
        def create(self, path, data):
            raise Denied(f"403 Missing or insufficient permissions for {path} as svc@job-tracker-test")
    store = UserStore(DeniedBackend(), FakeIdentity())
    monkeypatch.setattr(user_store, "configure", lambda secrets, project_id="": (store, ""))
    at = _admin_session(app, google_env, admins, {**GATED, "FIREBASE_SERVICE_ACCOUNT": json.dumps(SA)})
    row = _personal_row(at)
    assert row == f'{user_store.UNAVAILABLE}<span class="kv-note">Denied</span>'
    assert A_UID not in _html(at).split("Firebase account", 1)[1].split("Personal data")[1]
    assert "svc@" not in _html(at) and "insufficient" not in _html(at)


# --- units ------------------------------------------------------------------------

def test_the_fingerprint_never_depends_on_the_private_key():
    def fp(**over):
        return user_store.credential_fingerprint({"FIREBASE_SERVICE_ACCOUNT": json.dumps({**SA, **over})}, "p")
    assert fp(private_key="A") == fp(private_key="B")                        # the key isn't part of it
    assert fp() != fp(private_key_id="key-2") and fp() != fp(client_email="other@x")
    assert user_store.credential_fingerprint({"FIREBASE_SERVICE_ACCOUNT": json.dumps(SA)}, "p") != \
        user_store.credential_fingerprint({"FIREBASE_SERVICE_ACCOUNT": json.dumps(SA)}, "q")
    assert "VALID" not in fp() and len(fp()) == 64


def test_storage_errors_carry_a_category_and_no_detail():
    class PermissionDenied(Exception):
        pass
    class Backend(MemoryBackend):
        def get(self, path):
            raise PermissionDenied(f"denied reading {path}")
    with pytest.raises(UserStoreError) as e:
        user_store.UserData(Backend(), A_UID).profile()
    assert str(e.value) == user_store.UNAVAILABLE and e.value.kind == "PermissionDenied"
    assert A_UID not in str(e.value) and A_UID not in e.value.kind
