"""The browser-side session probe (src/session_probe.py), 28 Oct 2026.

Production crashed at the sign-in gate with "RuntimeError: module is None.
This should never happen." from Streamlit's declare_component: the probe
declared its component on every call, declare_component looks the caller's
module up in sys.modules, and the app (streamlit_app._drop_moved_local_modules)
and Streamlit's file watcher both remove modules from sys.modules. The
component is now declared once, at import.

AppTest has no browser, so the app normally skips the probe in tests; the
restoration tests below make a browser count as connected and let the probe
report, the way it does on Streamlit Community Cloud (where the server never
sees the cookie). Firestore and Firebase are fakes; no browser is started."""
import importlib
import sys
from pathlib import Path

import pytest
import streamlit
import streamlit.components.v1 as components

from access import ACCOUNT_KEY
from test_firebase_auth import GATED, _login_page, google_env, no_env  # noqa: F401
from test_session_persistence_delete_account import _ready, jar, store  # noqa: F401
from test_phase3_user_data import A_MAIL, A_UID  # noqa: F401
from test_streamlit_app import _html, app  # noqa: F401

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def fresh_probe():
    """session_probe imported anew (as the app does after a deploy), and
    sys.modules put back afterwards."""
    saved = sys.modules.pop("session_probe", None)
    mod = importlib.import_module("session_probe")
    yield mod
    sys.modules.pop("session_probe", None)
    if saved is not None:
        sys.modules["session_probe"] = saved


def test_the_probe_survives_its_module_leaving_sys_modules(fresh_probe):
    """The production crash: the module is removed from sys.modules (a deploy,
    Streamlit's file watcher) while the app still calls into it."""
    sys.modules.pop("session_probe")
    fresh_probe.probe(key="jt_session_probe", default=None)          # raised "module is None" before the fix


def test_the_component_is_declared_once_at_import_under_the_same_name(fresh_probe, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("declare_component called at render time")
    monkeypatch.setattr(components, "declare_component", refuse)
    fresh_probe.probe(key="jt_session_probe", default=None)
    assert fresh_probe.COMPONENT.name == "session_probe.jt_session_probe"     # the name the browser side knows
    assert Path(fresh_probe.COMPONENT.path) == REPO / "assets" / "session_probe"
    assert (REPO / "assets" / "session_probe" / "index.html").is_file()


# ---------------------------------------------------------------------------
# the browser-session restoration path, through the probe
# ---------------------------------------------------------------------------

@pytest.fixture
def browser(monkeypatch):
    """A connected browser as the app sees one (AppTest's client is a Mock),
    and every component declared under the run recorded."""
    import streamlit.runtime as runtime
    real = runtime.get_instance

    class Connected:
        def __init__(self, inner):
            self._inner = inner

        def get_client(self, session_id):
            return object()

        def __getattr__(self, name):
            return getattr(self._inner, name)
    monkeypatch.setattr(runtime, "get_instance", lambda: Connected(real()))
    registered = []
    import streamlit.components.v1.component_registry as reg
    real_registry = reg.get_instance

    class Recording:
        def __init__(self, inner):
            self._inner = inner

        @property
        def component_registry(self):
            outer = self

            class R:
                def register_component(self, component):
                    registered.append(component.name)
                    outer._inner.component_registry.register_component(component)

                def __getattr__(self, name):
                    return getattr(outer._inner.component_registry, name)
            return R()

        def __getattr__(self, name):
            return getattr(self._inner, name)
    monkeypatch.setattr(reg, "get_instance", lambda: Recording(real_registry()))
    sys.modules.pop("session_probe", None)               # imported by the app, inside its run
    yield registered
    sys.modules.pop("session_probe", None)


def test_a_browser_session_is_restored_through_the_probe(app, google_env, store, jar, browser):
    u = _ready(store, A_UID, A_MAIL)
    cookie, _ = u.start_session(A_MAIL, "", True)
    at = app([], secrets=GATED, owner=False)               # a new connection; the server can't see the cookie
    assert not at.exception, at.exception
    assert "session_probe.jt_session_probe" in browser      # declared and registered inside the app's run
    assert ACCOUNT_KEY not in at.session_state              # nobody signed in until the probe reports
    at.session_state["jt_session_probe"] = cookie           # the probe reports what the browser holds
    at.run()
    assert not at.exception, at.exception
    assert not _login_page(at), _html(at)[-300:]
    assert at.session_state[ACCOUNT_KEY]["uid"] == A_UID


def test_the_restoration_survives_the_module_being_dropped_between_runs(app, google_env, store, jar, browser):
    """A deploy (or the file watcher) removes session_probe between two runs
    of the same sign-in gate: the next run re-imports it and still restores."""
    u = _ready(store, A_UID, A_MAIL)
    cookie, _ = u.start_session(A_MAIL, "", True)
    at = app([], secrets=GATED, owner=False)
    assert not at.exception, at.exception
    sys.modules.pop("session_probe", None)
    at.session_state["jt_session_probe"] = cookie
    at.run()
    assert not at.exception, at.exception
    assert at.session_state[ACCOUNT_KEY]["uid"] == A_UID


def test_without_a_session_the_login_page_follows_the_probes_report(app, google_env, store, jar, browser):
    at = app([], secrets=GATED, owner=False)
    assert not at.exception, at.exception
    at.session_state["jt_session_probe"] = ""                # the browser holds no jt_session
    at.run()
    assert not at.exception and _login_page(at)
