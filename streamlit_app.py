import base64
import hashlib
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from urllib.parse import urlparse

import streamlit as st

# The app's modules live in src/ (this file stays at the repository root,
# where Streamlit Community Cloud runs it); data files are in data/.
_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if _SRC not in __import__("sys").path:
    __import__("sys").path.insert(0, _SRC)


def _drop_moved_local_modules() -> None:
    """Streamlit Cloud keeps imported modules across a deploy. A module whose
    file no longer exists (it moved — e.g. into src/ in Oct 2026) is dropped,
    so the import below loads the current copy instead of running stale code
    with stale paths. Every module of the app is dropped together, so none
    keeps a reference to an old one. Tests import the same names from the
    repository (files that exist), so nothing of theirs is touched."""
    import sys

    names = [f[:-3] for f in os.listdir(_SRC) if f.endswith(".py")] if os.path.isdir(_SRC) else []
    gone = any(getattr(sys.modules.get(n), "__file__", None) and not os.path.exists(sys.modules[n].__file__) for n in names)
    if gone:
        for n in names:
            sys.modules.pop(n, None)


_drop_moved_local_modules()


def _reload_changed_local_modules() -> None:
    """Streamlit (notably on Community Cloud) re-runs this script after a
    deploy but keeps the app's own helper modules from the previous version
    in memory. A changed module then stays stale until a reboot — an
    ImportError for a new name, or a silently failing call. Reload, in
    dependency order, every local module that is older than its file (and,
    once per process, any not yet stamped, since it may predate this check)."""
    import importlib
    import sys

    base = _SRC
    for name in _LOCAL_MODULES:
        mod = sys.modules.get(name)
        path = getattr(mod, "__file__", None) if mod else None
        if not path or os.path.dirname(os.path.abspath(path)) != base:
            continue                     # not loaded yet, or not this app's copy (tests)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        stamp = getattr(mod, "_jt_loaded_mtime", None)
        if stamp is None or mtime > stamp:
            try:
                mod = importlib.reload(mod)
            except Exception as e:      # keep the old module rather than break the page
                logging.getLogger("streamlit_app").warning("reload of %s failed: %s", name, type(e).__name__)
                mod._jt_reload_failed = True          # never stamped as current: retried on the next run
                continue
            mod._jt_loaded_mtime = mtime


def _stamp_fresh_local_modules() -> None:
    """End of a run: every local module still without a stamp was imported
    during THIS run (the check above stamps or reloads all the others at the
    start), so it is current. Stamp it — otherwise the next click reloaded
    it for nothing: about ten modules on a new server process's second run,
    and scraper.py (0.35 s) again right after the first Monitoring visit."""
    import sys

    base = _SRC
    for name in _LOCAL_MODULES:
        mod = sys.modules.get(name)
        path = getattr(mod, "__file__", None) if mod else None
        if (not path or os.path.dirname(os.path.abspath(path)) != base
                or getattr(mod, "_jt_loaded_mtime", None) is not None or getattr(mod, "_jt_reload_failed", False)):
            continue
        try:
            mod._jt_loaded_mtime = os.path.getmtime(path)
        except OSError:
            pass


_LOCAL_MODULES = ["scan_schedule", "identity", "locations", "job_classifier", "sources", "scraper", "config_store", "repo_sync",
                  "access", "email_policy", "firebase_auth", "job_filters", "user_store", "snapshot_cache", "notifier", "alerts"]
_reload_changed_local_modules()

from config_store import (
    alert_pending,
    dismiss_jobs,
    fetch_remote_json,
    friendly_github_error,
    job_key,
    update_json,
    visible_jobs,
)
from notifier import category_label, friendly_reason, safe_url
from identity import ats_job_id
import brand_logos
import company_logos
import email_policy
import firebase_auth
import job_filters
import scan_schedule
import snapshot_cache
import user_store
from access import (
    ACCOUNT_KEY,
    OWNER_SESSION_KEY,
    OWNER_SESSION_S,
    AccountAttemptLimiter,
    AttemptLimiter,
    Cooldown,
    OwnerRequired,
    account_is_admin,
    account_matches_source,
    admin_emails,
    admins_configured,
    alert_recipient,
    mask_email,
    owner_configured,
    owner_password_enabled,
    owner_session_valid,
    password_matches,
    send_test_email,
)

log = logging.getLogger("streamlit_app")

BASE = Path(__file__).parent
GITHUB_REPO = "manojanumolu/job-tracker"
ACTIONS_URL = f"https://github.com/{GITHUB_REPO}/actions/workflows/check_jobs.yml"
LOGO_PATH = BASE / "assets" / "logo.png"

# ── routing ───────────────────────────────────────────────────────────────────
# One script, several views. The current view lives in session state and is
# mirrored to the URL (?page=companies&company=sanofi) so links and browser
# reloads land on the same screen.
PAGES = {
    "home": ("Home", ":material/home:"),
    "jobs": ("Jobs", ":material/work:"),
    "companies": ("Companies", ":material/apartment:"),
    "monitoring": ("Monitoring", ":material/monitor_heart:"),
    "email": ("Email & Notifications", ":material/mail:"),
    "settings": ("Settings", ":material/settings:"),
    # personal pages (every signed-in person)
    "alerts": ("My alerts", ":material/notifications:"),
    "preferences": ("Job preferences", ":material/tune:"),
    "onboarding": ("Welcome", ":material/waving_hand:"),
}
NAV_GROUPS = [("Discover", ["home", "jobs", "companies", "monitoring"]),
              ("Management", ["email", "settings"])]

# Read the view from the URL on a new session, and also whenever the URL no
# longer matches what this app last wrote: that means the browser's Back /
# Forward changed it (Streamlit 1.65+ keeps the session on Back instead of
# starting a new one, so the URL must win over the remembered view).
if "page" not in st.session_state or (
        "_url_written" in st.session_state and dict(st.query_params) != st.session_state._url_written):
    qp = st.query_params
    st.session_state.page = qp.get("page") if qp.get("page") in PAGES else "home"
    st.session_state.job_id = qp.get("job") or None
    st.session_state.company_id = qp.get("company") or None
    st.session_state.company_view = qp.get("view") if qp.get("view") in ("add", "mine") else None
    st.session_state.confirm_remove = None
    if "dark_mode" in st.session_state:
        st.session_state.dark_mode = qp.get("theme") == "dark"
    st.session_state.pop("nav_collapsed", None)       # re-read from ?menu= once signed in (below)
if "dark_mode" not in st.session_state:
    # the theme lives in the URL (?theme=dark) so it survives reloads/bookmarks
    st.session_state.dark_mode = st.query_params.get("theme") == "dark"

st.set_page_config(
    page_title=f"{PAGES[st.session_state.page][0]} · Fresher Job Tracker",
    page_icon=str(LOGO_PATH) if LOGO_PATH.exists() else "💼",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── sign-in event log (read-only, in memory) ─────────────────────────────────
# Streamlit reports OAuth failures (e.g. a failed Google code exchange) only
# to the server log, then quietly redirects to "/". Keep the last few such
# records — level, logger, message and exception TYPE + first line, with
# emails and long token-like strings redacted — so ?diag=auth can show why a
# sign-in didn't stick. Never a value from a cookie, secret or token.
_REDACT_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+|[A-Za-z0-9_\-]{24,}(?:\.[A-Za-z0-9_\-]+)*")


def _redact(text: object) -> str:
    return _REDACT_RE.sub("<redacted>", str(text))[:300]


_AUTH_LOGGERS = ("streamlit.web.server.starlette.starlette_auth_routes",
                 "streamlit.web.server.starlette.starlette_websocket",
                 "streamlit.web.server.oauth_authlib_routes", "streamlit.web.server.browser_websocket_handler",
                 "streamlit.auth_util", "authlib")
_OAUTH_CODE_RE = re.compile(r"^\s*([a-z_]{3,40})\s*:")


def _auth_capture() -> dict:
    """The process-wide sign-in record: {"events": deque of redacted lines,
    "exchange": {"at": time or None, "code": str}} for the last failed Google
    code exchange. One handler per process, attached to Streamlit's own
    auth loggers (they don't propagate)."""
    import collections

    first = logging.getLogger(_AUTH_LOGGERS[0])
    for h in first.handlers:
        if getattr(h, "_jt_auth_capture", None) is not None:
            return h._jt_auth_capture
    store = {"events": collections.deque(maxlen=40), "exchange": {"at": None, "code": ""}}

    class _Capture(logging.Handler):
        def emit(self, record):
            try:
                msg = record.getMessage()
                exc = ""
                if record.exc_info and record.exc_info[1] is not None:
                    e = record.exc_info[1]
                    first_line = str(e).splitlines()[0] if str(e) else ""
                    exc = f" | {type(e).__name__}: {_redact(first_line)}"
                    if "token exchange failed" in msg:
                        m = _OAUTH_CODE_RE.match(first_line)
                        store["exchange"] = {"at": record.created, "code": m.group(1) if m else type(e).__name__}
                store["events"].append(f"{time.strftime('%H:%M:%S', time.gmtime(record.created))}Z {record.levelname} "
                                       f"{record.name.rsplit('.', 1)[-1]}: {_redact(msg)}{exc}")
            except Exception:
                pass

    handler = _Capture(level=logging.WARNING)
    handler._jt_auth_capture = store
    for name in _AUTH_LOGGERS:
        lg = logging.getLogger(name)
        lg.addHandler(handler)
        if lg.level == logging.NOTSET or lg.level > logging.WARNING:
            lg.setLevel(logging.WARNING)
    return store


def _auth_events():
    return _auth_capture()["events"]


def auth_event(message: str) -> None:
    """Record an app-level sign-in decision (booleans/codes only)."""
    _auth_events().append(f"{time.strftime('%H:%M:%S', time.gmtime())}Z APP gate: {_redact(message)}")


# what a failed Google code exchange means, for the login page — said to
# visitors, so the error code at most, never how the app is configured
_EXCHANGE_HINTS = {
    "invalid_client": "Google sign-in isn't working right now (invalid_client). Please try again later.",
    "invalid_grant": "That Google sign-in had expired or was already used. Please continue with Google again.",
}
_EXCHANGE_NOTICE_S = 300


def recent_google_failure(now: float | None = None) -> str:
    """A safe, human message if a Google sign-in failed at the code exchange
    in the last few minutes (process-wide), else ""."""
    ex = _auth_capture()["exchange"]
    if not ex.get("at") or (time.time() if now is None else now) - ex["at"] > _EXCHANGE_NOTICE_S:
        return ""
    code = ex.get("code") or "unknown"
    return _EXCHANGE_HINTS.get(code, f"Google sign-in couldn't be completed ({code}). Please try again.")


_auth_capture()

# ── sign-in diagnostics (read-only, admins only) ─────────────────────────────
# ?diag=auth shows what THIS session's server process can see of the sign-in
# plumbing — counts, cookie NAMES and booleans only, never a value, token or
# address — so a deployment can be checked without its logs. It runs only
# after the sign-in gate, for an admin (visitors get the login page, members
# the normal app), and stops before any data is loaded.
def render_auth_diag() -> None:
    def _xsrf_effective():
        try:
            from streamlit.web.server.server_util import is_xsrf_enabled
            return is_xsrf_enabled()
        except Exception as e:
            return f"n/a ({type(e).__name__})"

    def _auth_diag() -> list[tuple[str, str]]:
        try:
            names = sorted(st.context.cookies.keys())
        except Exception as e:
            names = [f"<unreadable: {type(e).__name__}>"]
        try:
            logged_in = bool(st.user.is_logged_in)
        except Exception as e:
            logged_in = f"error {type(e).__name__}"
        try:
            has_token = "id" in st.user.tokens
        except Exception:
            has_token = False
        try:
            embedded = bool(st.context.is_embedded)
        except Exception:
            embedded = "n/a"
        try:
            secrets = st.secrets.to_dict()
        except Exception:
            secrets = {}
        auth = secrets.get("auth") if isinstance(secrets.get("auth"), dict) else {}
        return [
            ("cookies seen by the server (count)", str(len(names))),
            ("cookie names seen", ", ".join(n for n in names if n.startswith("_streamlit") or n.startswith("jt_")) or "none"),
            ("st.user.is_logged_in", str(logged_in)),
            ("Google ID token exposed", str(has_token)),
            ("app embedded in a frame", str(embedded)),
            ("[auth] configured", str(bool(auth))),
            ("server.enableXsrfProtection (option)", str(st.get_option("server.enableXsrfProtection"))),
            ("XSRF effective (option or [auth])", str(_xsrf_effective())),
            ("streamlit version", st.__version__),
        ]

    def _admin_diag() -> list[tuple[str, str]]:
        """Where JT_ADMIN_EMAILS is (or isn't) in what this process received —
        key/section NAMES, booleans and counts only; a value is inspected for
        its shape but never shown."""
        import access as _access

        try:
            secrets = st.secrets.to_dict()
            readable = True
        except Exception:
            secrets, readable = {}, False
        key = "JT_ADMIN_EMAILS"
        found_in, similar, found_vals = [], [], []

        def walk(node, path):
            for k, v in (node.items() if isinstance(node, dict) else []):
                name = f"{path}.{k}" if path else str(k)
                if k == key:
                    found_in.append(path or "<top level>")
                    found_vals.append(v)
                elif "ADMIN" in str(k).upper() or str(k).strip().upper() == key:
                    similar.append(name)
                if isinstance(v, dict):
                    walk(v, f"{path}.{k}" if path else f"[{k}]")
        walk(secrets, "")
        value = secrets.get(key)
        if value is None:
            for sec in ("auth", "admin"):
                if isinstance(secrets.get(sec), dict) and key in secrets[sec]:
                    value = secrets[sec][key]
                    break
        usable = ",".join(map(str, value)) if isinstance(value, (list, tuple)) else value
        entries = [e for e in re.split(r"[,\s;]+", usable) if "@" in e] if isinstance(usable, str) else []
        # the shape checks describe the value wherever it was found (even a
        # place the app doesn't read), so a misplaced value can be diagnosed
        shape = value if value is not None else (found_vals[0] if found_vals else None)
        text = ",".join(map(str, shape)) if isinstance(shape, (list, tuple)) else shape
        env = os.environ.get(key, "")
        rows = [
            ("secrets readable by the app", str(readable)),
            ("top-level secret keys (count)", str(sum(1 for v in secrets.values() if not isinstance(v, dict)))),
            ("secret sections", ", ".join(f"[{k}]" for k, v in secrets.items() if isinstance(v, dict)) or "none"),
            ("JT_ADMIN_EMAILS_PRESENT_TOP_LEVEL", str(key in secrets)),
            ("JT_ADMIN_EMAILS found in", ", ".join(found_in) or "nowhere"),
            ("similar key names (names only)", ", ".join(similar) or "none"),
            ("value type", type(shape).__name__ if shape is not None else "n/a"),
            ("VALUE_HAS_AT_SIGN", str(isinstance(text, str) and "@" in text)),
            ("value has leading/trailing spaces", str(isinstance(text, str) and text != text.strip())),
            ("value has non-ASCII / invisible characters", str(isinstance(text, str) and any(ord(c) > 126 or ord(c) < 32 for c in text))),
            ("value wrapped in extra quotes", str(isinstance(text, str) and len(text) > 1 and text.strip()[:1] in "'\"“”")),
            ("ADMIN_ENTRY_COUNT (in secrets)", str(len(entries))),
            ("JT_ADMIN_EMAILS_ENV_SET", str(bool(env.strip()))),
            ("env entries with @ (count)", str(len([e for e in re.split(r"[,\s;]+", env) if "@" in e]))),
            ("ADMINS_CONFIGURED", str(_access.admins_configured())),
            ("OWNER_CONFIGURED", str(_access.owner_configured())),
        ]
        try:
            if st.user.is_logged_in:
                email = (st.user.get("email") or "").strip().lower()
                on_list = email in {e.strip().lower() for e in entries} or email in _access.admin_emails()
                rows += [("SIGNED_IN_EMAIL_VERIFIED", str(st.user.get("email_verified") is True)),
                         ("email_verified claim type", type(st.user.get("email_verified")).__name__),
                         ("SIGNED_IN_EMAIL_ON_LIST", str(on_list))]
            else:
                rows.append(("signed-in checks", "n/a (this browser isn't signed in)"))
        except Exception as e:
            rows.append(("signed-in checks", f"error {type(e).__name__}"))
        return rows

    st.html("<h3>Sign-in diagnostics</h3><table>" + "".join(
        f"<tr><td>{a}</td><td><code>{b}</code></td></tr>" for a, b in _auth_diag()) + "</table>"
        + "<h4>Admin list (names, booleans and counts only)</h4><table id='admin-diag'>" + "".join(
        f"<tr><td>{escape(a)}</td><td><code>{escape(b)}</code></td></tr>" for a, b in _admin_diag()) + "</table>"
        + "<h4>Recent sign-in events (this server process)</h4><pre id='auth-events'>"
        + (escape("\n".join(_auth_events())) or "none recorded") + "</pre>")
    print("[auth-diag] " + " | ".join(f"{a}={b}" for a, b in _auth_diag()), flush=True)


# ── theme ─────────────────────────────────────────────────────────────────────
# Design tokens. Two hand-tuned palettes (dark is designed, not inverted):
# an indigo-violet primary for intent/actions and a teal "signal" for the
# product's core promise (fresher roles found, portals healthy).


LIGHT = {
    "bg": "#f4f5fb", "surface": "#ffffff", "surface-2": "#f7f8fc", "raised": "#ffffff", "hover": "#f0f1f8",
    "border": "#e5e7f0", "border-strong": "#d3d6e3",
    "text": "#0d1024", "text-2": "#383e57", "muted": "#646a82",
    "accent": "#5b4cf5", "accent-hover": "#4a3be3", "accent-text": "#4636d4", "accent-soft": "#efedff",
    "accent-2": "#0d9488", "on-accent": "#ffffff",
    "green": "#0b7d55", "green-soft": "#e5f6ee", "amber": "#a85a06", "amber-soft": "#fff3e2",
    "red": "#c2303a", "red-soft": "#fdebed", "gray": "#596075", "gray-soft": "#eef0f6",
    "glow-1": "rgba(91,76,245,.13)", "glow-2": "rgba(13,148,136,.10)", "grid": "rgba(13,16,36,.045)",
    "hi": "inset 0 1px 0 rgba(255,255,255,.9)",
    "shadow": "0 1px 2px rgba(16,24,40,.04), 0 2px 6px -2px rgba(16,24,40,.06)",
    "shadow-lg": "0 22px 44px -22px rgba(36,30,110,.30), 0 2px 6px -2px rgba(16,24,40,.06)",
    "av-sat": "70%", "av-bg": "94%", "av-fg": "34%", "av-bd": "86%",
    # glossy primary / raised secondary controls
    "btn-grad": "linear-gradient(180deg, #6c5eff 0%, #5646f2 50%, #4130d6 100%)", "btn-border": "#3f2fcf",
    "btn-hi": "inset 0 1px 0 rgba(255,255,255,.45), inset 0 -2px 0 rgba(20,10,90,.28)",
    "btn-shadow": "0 1px 2px rgba(40,28,140,.25), 0 8px 18px -10px rgba(91,76,245,.75)",
    "btn-shadow-hover": "0 2px 4px rgba(40,28,140,.22), 0 14px 26px -12px rgba(91,76,245,.85)",
    "btn-pressed": "inset 0 2px 4px rgba(20,10,90,.28), 0 1px 2px rgba(40,28,140,.2)",
    "btn2-grad": "linear-gradient(180deg, #ffffff, #f6f7fc)", "btn2-shadow": "0 1px 2px rgba(16,24,40,.06)",
    "btn2-shadow-hover": "0 6px 14px -8px rgba(36,30,110,.28)", "btn2-pressed": "inset 0 1px 3px rgba(16,24,40,.12)",
    "badge-hi": "inset 0 1px 0 rgba(255,255,255,.75)",
    # hero opportunity cards
    "opp-glow": "1", "oc-bg": "rgba(255,255,255,.82)", "oc-border": "rgba(91,76,245,.14)",
    "oc-shadow": "0 18px 40px -18px rgba(36,30,110,.32), 0 2px 6px -2px rgba(16,24,40,.06)",
    "oc-bar": "#e9eaf3", "oc-bar-strong": "#d6d8e8",
    "oc-gloss": "linear-gradient(180deg, rgba(255,255,255,.75), rgba(255,255,255,0) 48%)",
    "oc-shadow-hover": "0 30px 56px -22px rgba(36,30,110,.42), 0 4px 12px -4px rgba(16,24,40,.10)",
    # the account avatar: a domed tile, a gap ring and a thin accent ring
    "pill-gloss": "rgba(255,255,255,.7)", "av-ring": "rgba(91,76,245,.38)",
    "av-gloss": "inset 0 1px 1px rgba(255,255,255,.65), inset 0 -3px 6px rgba(15,23,42,.07)",
    "av-drop": "0 6px 12px -6px rgba(36,30,110,.45)",
    # interactive surfaces: a lit hover wash, and the busy bar
    "hover-wash": "linear-gradient(180deg, rgba(255,255,255,.9), rgba(239,237,255,.75))",
    # the loading square: a purple→blue frame around a briefcase
    # search fields: a lavender→ice-blue glass with a gradient edge
    "search-gloss": "linear-gradient(180deg, rgba(255,255,255,.88) 0%, rgba(255,255,255,.34) 44%, rgba(255,255,255,0) 54%)",
    "search-tint": "linear-gradient(115deg, #f4f1ff 0%, #ecefff 50%, #e6f1ff 100%)",
    "search-tint-hover": "linear-gradient(115deg, #efebff 0%, #e6e9ff 50%, #dfedff 100%)",
    "search-edge": "linear-gradient(115deg, rgba(124,108,255,.55), rgba(59,130,246,.42))",
    "search-edge-hover": "linear-gradient(115deg, rgba(124,108,255,.78), rgba(59,130,246,.66))",
    "search-edge-focus": "linear-gradient(115deg, #6c5cf7, #3b82f6)",
    "search-shadow": "inset 0 1px 0 rgba(255,255,255,.95), inset 0 -1px 0 rgba(91,76,245,.10), 0 1px 2px rgba(36,30,110,.06), 0 12px 26px -16px rgba(91,76,245,.55)",
    "search-shadow-hover": "inset 0 1px 0 rgba(255,255,255,.95), inset 0 -1px 0 rgba(91,76,245,.12), 0 2px 4px rgba(36,30,110,.07), 0 16px 32px -16px rgba(91,76,245,.62)",
    "search-chip": "linear-gradient(135deg, #ebe7ff, #dfeaff)", "search-chip-focus": "linear-gradient(135deg, #6c5cf7, #3b82f6)",
    "search-icon": "#5446e8", "search-placeholder": "#646887",
}
DARK = {
    "bg": "#0b1020", "surface": "#121833", "surface-2": "#0f1530", "raised": "#171e3d", "hover": "#1b2347",
    "border": "rgba(255,255,255,.075)", "border-strong": "rgba(255,255,255,.15)",
    "text": "#eef0fb", "text-2": "#c4c9df", "muted": "#8f96b3",
    "accent": "#7c6cff", "accent-hover": "#9184ff", "accent-text": "#b8afff", "accent-soft": "rgba(124,108,255,.16)",
    "accent-2": "#2dd4bf", "on-accent": "#ffffff",
    "green": "#3ddc9a", "green-soft": "rgba(61,220,154,.12)", "amber": "#f6c04e", "amber-soft": "rgba(246,192,78,.12)",
    "red": "#ff7a85", "red-soft": "rgba(255,122,133,.12)", "gray": "#9aa3bf", "gray-soft": "rgba(154,163,191,.13)",
    "glow-1": "rgba(124,108,255,.20)", "glow-2": "rgba(45,212,191,.10)", "grid": "rgba(255,255,255,.035)",
    "hi": "inset 0 1px 0 rgba(255,255,255,.05)",
    "shadow": "0 1px 2px rgba(0,0,0,.35), 0 4px 14px -6px rgba(0,0,0,.45)",
    "shadow-lg": "0 26px 50px -24px rgba(0,0,0,.85), 0 0 0 1px rgba(124,108,255,.10)",
    "av-sat": "55%", "av-bg": "22%", "av-fg": "80%", "av-bd": "34%",
    "btn-grad": "linear-gradient(180deg, #8a7dff 0%, #7464fb 50%, #5d4be6 100%)", "btn-border": "rgba(160,150,255,.55)",
    "btn-hi": "inset 0 1px 0 rgba(255,255,255,.34), inset 0 -2px 0 rgba(10,5,50,.38)",
    "btn-shadow": "0 1px 2px rgba(0,0,0,.4), 0 8px 20px -10px rgba(124,108,255,.65)",
    "btn-shadow-hover": "0 2px 4px rgba(0,0,0,.4), 0 14px 28px -12px rgba(124,108,255,.8)",
    "btn-pressed": "inset 0 2px 5px rgba(10,5,50,.45)",
    "btn2-grad": "linear-gradient(180deg, #1a2148, #131936)", "btn2-shadow": "0 1px 2px rgba(0,0,0,.35)",
    "btn2-shadow-hover": "0 8px 18px -10px rgba(0,0,0,.7), 0 0 0 1px rgba(124,108,255,.12)", "btn2-pressed": "inset 0 1px 4px rgba(0,0,0,.45)",
    "badge-hi": "inset 0 1px 0 rgba(255,255,255,.06)",
    "opp-glow": ".85", "oc-bg": "rgba(26,32,66,.78)", "oc-border": "rgba(255,255,255,.10)",
    "oc-shadow": "0 20px 44px -18px rgba(0,0,0,.75), 0 0 0 1px rgba(124,108,255,.08)",
    "oc-bar": "rgba(255,255,255,.08)", "oc-bar-strong": "rgba(255,255,255,.16)",
    "oc-gloss": "linear-gradient(180deg, rgba(255,255,255,.09), rgba(255,255,255,0) 48%)",
    "oc-shadow-hover": "0 32px 60px -24px rgba(0,0,0,.9), 0 0 0 1px rgba(124,108,255,.22)",
    "pill-gloss": "rgba(255,255,255,.06)", "av-ring": "rgba(160,150,255,.55)",
    "av-gloss": "inset 0 1px 1px rgba(255,255,255,.45), inset 0 -3px 6px rgba(0,0,0,.14)",
    "av-drop": "0 6px 14px -6px rgba(0,0,0,.7)",
    "hover-wash": "linear-gradient(180deg, rgba(124,108,255,.16), rgba(124,108,255,.08))",
    "search-gloss": "linear-gradient(180deg, rgba(255,255,255,.10) 0%, rgba(255,255,255,.03) 46%, rgba(255,255,255,0) 54%)",
    "search-tint": "linear-gradient(115deg, #1f2552 0%, #1a214b 50%, #18264d 100%)",
    "search-tint-hover": "linear-gradient(115deg, #242b5c 0%, #1f2655 50%, #1c2c58 100%)",
    "search-edge": "linear-gradient(115deg, rgba(150,138,255,.55), rgba(96,165,250,.45))",
    "search-edge-hover": "linear-gradient(115deg, rgba(150,138,255,.78), rgba(96,165,250,.66))",
    "search-edge-focus": "linear-gradient(115deg, #8d80ff, #60a5fa)",
    "search-shadow": "inset 0 1px 0 rgba(255,255,255,.08), inset 0 -1px 0 rgba(0,0,0,.25), 0 1px 2px rgba(0,0,0,.35), 0 12px 28px -16px rgba(124,108,255,.55)",
    "search-shadow-hover": "inset 0 1px 0 rgba(255,255,255,.10), inset 0 -1px 0 rgba(0,0,0,.25), 0 2px 4px rgba(0,0,0,.35), 0 16px 34px -16px rgba(124,108,255,.65)",
    "search-chip": "linear-gradient(135deg, rgba(124,108,255,.28), rgba(96,165,250,.20))", "search-chip-focus": "linear-gradient(135deg, #8d80ff, #4f8ff7)",
    "search-icon": "#cbc4ff", "search-placeholder": "#a6acc9",
}
TH = DARK if st.session_state.dark_mode else LIGHT
_root_vars = ":root {" + "".join(f"--{k}:{v};" for k, v in TH.items()) + "}"

# Design system. Plus Jakarta Sans for everything (small labels are the same
# family in tracked-out caps); numbers use tabular figures. Icons are Material
# Symbols ligatures (one family everywhere) — inline <svg> doesn't paint in
# this app's hosting environment, so the hero cards are pure CSS. Widgets
# that need styling are wrapped in st.container(key=...) and targeted via
# .st-key-*; raw HTML tags are never opened in one st.* call and closed in
# another. Motion uses transform/opacity only and honours reduced motion;
# top-level entrances are opacity-only so they never trap the fixed toast.
_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap');
@import url('https://fonts.googleapis.com/css2?family=Material+Symbols+Rounded:opsz,wght,FILL,GRAD@20..48,400,0..1,0&display=block');

:root {
  --font: 'Plus Jakarta Sans', ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif;
  --label: var(--font);   /* small uppercase labels: same family, tracked out */
  --sidebar-w: 248px;
  --r-sm: 8px; --r: 12px; --r-lg: 16px; --r-xl: 20px;
  --fast: 150ms; --normal: 240ms; --slow: 480ms; --ease: cubic-bezier(.2,.7,.2,1);
  --ring: 0 0 0 3px color-mix(in srgb, var(--accent) 24%, transparent);
}

/* ── Streamlit chrome ── */
#MainMenu, footer:not(.app-foot), header[data-testid="stHeader"] { display: none !important; }
.stDeployButton, [data-testid="stToolbar"], [data-testid="stDecoration"] { display: none !important; }
[data-testid="stSidebarHeader"], [data-testid="stSidebarCollapseButton"], [data-testid="stSidebarCollapsedControl"],
[data-testid="stExpandSidebarButton"], [data-testid="stSidebarResizeHandle"] { display: none !important; }
/* The handle's own wrapper (8 px, right: -6px) still stuck 5 px out of the
   fixed-width sidebar; that overflow showed as a white bar at the sidebar's
   edge while pages redrew. The width is fixed, so there is nothing to resize. */
section[data-testid="stSidebar"] div:has(> [data-testid="stSidebarResizeHandle"]) { display: none !important; }
/* visual depth: two soft lights and a faint dot grid that fades out down the page */
.stApp, [data-testid="stAppViewContainer"] {
  background:
    radial-gradient(900px 520px at 78% -8%, var(--glow-1), transparent 70%),
    radial-gradient(700px 480px at 8% 4%, var(--glow-2), transparent 70%),
    var(--bg) !important;
  background-attachment: fixed !important;
}
.stMain { background: transparent !important; }
.stMain::before {
  content: ""; position: fixed; inset: 0; pointer-events: none; z-index: 0;
  background-image: radial-gradient(var(--grid) 1px, transparent 1.2px); background-size: 22px 22px;
  -webkit-mask-image: linear-gradient(180deg, #000 0, transparent 520px); mask-image: linear-gradient(180deg, #000 0, transparent 520px);
}
.stApp, .stApp p, .stApp label, .stApp input, .stApp button, .stApp textarea, .stApp li, .stApp h1, .stApp h2, .stApp h3, .stApp h4,
[data-baseweb="popover"] * { font-family: var(--font) !important; }
.stApp { color: var(--text); -webkit-font-smoothing: antialiased; word-spacing: .03em; }
.stApp input, .stApp textarea, [data-baseweb="select"] * { word-spacing: .03em; }
.stApp h1, .stApp h2, .stApp h3, .stApp h4 { padding: 0 !important; margin: 0; letter-spacing: -0.015em; word-spacing: .06em; color: var(--text); }
.stApp a { text-decoration: none !important; }
.num { font-variant-numeric: tabular-nums; }
[data-testid="stMainBlockContainer"], .block-container {
  max-width: 1200px !important; padding: 36px 44px 56px !important; margin: 0 !important; position: relative; z-index: 1;
}
[data-testid="stMainBlockContainer"] > div > [data-testid="stVerticalBlock"] { gap: 22px; }
::selection { background: color-mix(in srgb, var(--accent) 22%, transparent); }

/* ── motion ── */
@keyframes jt-fade { from { opacity: 0; } to { opacity: 1; } }
@keyframes jt-rise { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }
@keyframes jt-pulse { 0% { transform: scale(1); opacity: .55; } 100% { transform: scale(2.6); opacity: 0; } }
@keyframes jt-ring { 0% { transform: scale(1); opacity: .45; } 100% { transform: scale(1.32); opacity: 0; } }
/* opportunity cards: a few pixels of float, one card softly brightening */
@keyframes jt-float { 0%, 100% { transform: translate3d(0, 0, 0); } 50% { transform: translate3d(0, -6px, 0); } }
@keyframes jt-float-s { 0%, 100% { transform: translate3d(0, 0, 0) rotate(var(--tilt, 0deg)); } 50% { transform: translate3d(2px, -4px, 0) rotate(var(--tilt, 0deg)); } }
@keyframes jt-brighten { 0%, 100% { opacity: 0; } 50% { opacity: 1; } }
@keyframes jt-dim { 0%, 100% { opacity: 1; } 50% { opacity: .82; } }
@keyframes jt-glow-drift { 0% { transform: translate3d(-3%, 2%, 0) scale(1); } 100% { transform: translate3d(3%, -2%, 0) scale(1.05); } }
@keyframes jt-pop { from { opacity: 0; transform: scale(.92); } to { opacity: 1; transform: none; } }
@keyframes jt-grow { from { opacity: 0; transform: scaleY(.3); } to { opacity: 1; transform: none; } }
@keyframes jt-shimmer { from { background-position: -320px 0; } to { background-position: 320px 0; } }
@keyframes jt-toast-in { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: none; } }
[data-testid="stMainBlockContainer"] > div > [data-testid="stVerticalBlock"] > * { animation: jt-fade var(--normal) var(--ease) backwards; }
/* cards rise in, staggered; fill-mode backwards so :hover transforms win afterwards */
.stat, [class*="st-key-jr_"], [class*="st-key-cr_"], .mon tbody tr, .step { animation: jt-rise var(--slow) var(--ease) backwards; }
.stat:nth-child(2), .mon tbody tr:nth-child(2), .step:nth-child(2), [data-testid="stLayoutWrapper"]:nth-child(2) > :is([class*="st-key-jr_"], [class*="st-key-cr_"]) { animation-delay: 40ms; }
.stat:nth-child(3), .mon tbody tr:nth-child(3), .step:nth-child(3), [data-testid="stLayoutWrapper"]:nth-child(3) > :is([class*="st-key-jr_"], [class*="st-key-cr_"]) { animation-delay: 80ms; }
.stat:nth-child(4), .mon tbody tr:nth-child(4), .step:nth-child(4), [data-testid="stLayoutWrapper"]:nth-child(4) > :is([class*="st-key-jr_"], [class*="st-key-cr_"]) { animation-delay: 120ms; }
.mon tbody tr:nth-child(n+5), .step:nth-child(n+5), [data-testid="stLayoutWrapper"]:nth-child(n+5) > :is([class*="st-key-jr_"], [class*="st-key-cr_"]) { animation-delay: 160ms; }

/* ── icons ── */
.ms {
  font-family: 'Material Symbols Rounded' !important; font-weight: normal; font-style: normal; line-height: 1;
  letter-spacing: normal; text-transform: none; white-space: nowrap; direction: ltr; font-feature-settings: 'liga';
  -webkit-font-smoothing: antialiased; display: inline-block; overflow: hidden; flex-shrink: 0; vertical-align: middle;
  font-size: 18px; width: 1em; height: 1em; font-variation-settings: 'opsz' 20, 'wght' 450;
}
.ms.s14 { font-size: 14px; } .ms.s16 { font-size: 16px; } .ms.s20 { font-size: 20px; } .ms.s24 { font-size: 24px; } .ms.s28 { font-size: 28px; }
.ms.fill { font-variation-settings: 'FILL' 1, 'opsz' 20, 'wght' 450; }
[data-testid="stIconMaterial"] { font-family: 'Material Symbols Rounded' !important; }

/* ── type ── */
.eyebrow { font-family: var(--label) !important; font-size: 11px; line-height: 16px; font-weight: 600; letter-spacing: .12em; text-transform: uppercase; color: var(--muted); display: inline-flex; align-items: center; gap: 8px; }
.eyebrow.bar::before { content: ""; width: 14px; height: 3px; border-radius: 3px; background: linear-gradient(90deg, var(--accent), var(--accent-2)); }
.page-title { font-size: 30px; line-height: 38px; font-weight: 700; letter-spacing: -0.022em; color: var(--text); margin: 6px 0 0; }
.page-sub { font-size: 14px; line-height: 21px; color: var(--muted); margin: 6px 0 0; display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; }
.section-title { font-size: 16px; line-height: 24px; font-weight: 700; color: var(--text); margin: 0; letter-spacing: -0.015em; display: flex; align-items: center; gap: 8px; }
.section-title .ms { color: var(--accent-text); }
.section-sub { font-size: 13px; line-height: 19px; color: var(--muted); margin: 2px 0 0; }
.muted { color: var(--muted); } .text-2 { color: var(--text-2); } .accent { color: var(--accent-text); }
.sep { color: var(--border-strong); }
.sr-only { position: absolute !important; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); clip-path: inset(50%); white-space: nowrap; }

/* ── pills & badges: one system everywhere (status is never colour-only: dot/icon + word) ── */
.pill { display: inline-flex; align-items: center; gap: 6px; padding: 3px 10px 3px 9px; border-radius: 999px; font-size: 12px; line-height: 18px; font-weight: 600; white-space: nowrap; border: 1px solid transparent; }
.pill i { width: 6px; height: 6px; border-radius: 999px; background: currentColor; display: inline-block; box-shadow: 0 0 0 3px color-mix(in srgb, currentColor 18%, transparent); }
.pill .ms { font-size: 15px; margin-left: -2px; }
.pill.healthy, .pill.fresher, .pill.on { color: var(--green); background: var(--green-soft); border-color: color-mix(in srgb, var(--green) 22%, transparent); }
.pill.delayed, .pill.pending-mail { color: var(--amber); background: var(--amber-soft); border-color: color-mix(in srgb, var(--amber) 22%, transparent); }
.pill.failing, .pill.off { color: var(--red); background: var(--red-soft); border-color: color-mix(in srgb, var(--red) 22%, transparent); }
.pill.pending, .pill.legacy, .pill.neutral { color: var(--gray); background: var(--gray-soft); border-color: color-mix(in srgb, var(--gray) 18%, transparent); }
.pill.entry, .pill.checking, .pill.new { color: var(--accent-text); background: var(--accent-soft); border-color: color-mix(in srgb, var(--accent) 22%, transparent); }
/* category badges: slightly dimensional; Fresher carries the spark (same meaning, same words) */
.pill.fresher, .pill.entry { position: relative; padding-right: 11px; box-shadow: var(--badge-hi), 0 1px 2px -1px color-mix(in srgb, currentColor 30%, transparent);
  animation: jt-pop var(--normal) var(--ease) backwards; }
.pill.fresher { background: linear-gradient(180deg, color-mix(in srgb, var(--green-soft) 70%, var(--surface)), var(--green-soft));
  border-color: color-mix(in srgb, var(--green) 30%, transparent); }
.pill.entry { background: linear-gradient(180deg, color-mix(in srgb, var(--accent-soft) 70%, var(--surface)), var(--accent-soft)); }
.pill.fresher::after { content: ""; position: absolute; top: -2px; right: -2px; width: 6px; height: 6px; border-radius: 50%;
  background: radial-gradient(circle at 35% 35%, #fff 0 22%, var(--accent-2) 70%);
  box-shadow: 0 0 0 2px var(--surface), 0 0 8px color-mix(in srgb, var(--accent-2) 70%, transparent); }
.pill.healthy i, .pill.on i, .pill.checking i { position: relative; }
.pill.healthy i::after, .pill.on i::after, .pill.checking i::after { content: ""; position: absolute; inset: 0; border-radius: inherit; background: currentColor; animation: jt-pulse 2.4s ease-out infinite; }
.chip { display: inline-flex; align-items: center; gap: 6px; padding: 5px 11px; border-radius: 999px; font-size: 13px; line-height: 18px; font-weight: 500; color: var(--text-2); white-space: nowrap;
  background: linear-gradient(180deg, color-mix(in srgb, var(--surface) 92%, transparent), color-mix(in srgb, var(--surface-2) 85%, transparent)); border: 1px solid var(--border);
  box-shadow: var(--badge-hi), 0 1px 2px rgba(16,24,40,.05); backdrop-filter: blur(6px); -webkit-backdrop-filter: blur(6px); transition: border-color var(--fast), box-shadow var(--normal) var(--ease); }
.chip .ms { font-size: 16px; color: var(--muted); }
.dot { width: 8px; height: 8px; border-radius: 999px; display: inline-block; flex-shrink: 0; position: relative; }
.dot.healthy { background: var(--green); } .dot.delayed { background: var(--amber); } .dot.failing { background: var(--red); }
.dot.pending { background: var(--gray); } .dot.checking { background: var(--accent); }
.dot.healthy::after, .dot.checking::after { content: ""; position: absolute; inset: 0; border-radius: inherit; background: inherit; animation: jt-pulse 2.4s ease-out infinite; }
.tag { font-family: var(--label) !important; font-size: 10.5px; line-height: 16px; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); border: 1px solid var(--border-strong); border-radius: 6px; padding: 0 6px; font-weight: 500; }

/* ── surfaces ── */
.panel, [class*="st-key-filters_"], .st-key-add_form, .st-key-email_form, .st-key-test_panel, [class*="st-key-set_"],
.st-key-job_main, .st-key-job_side, .st-key-co_side, .st-key-empty, .st-key-hero, .st-key-job_hero, .st-key-co_hero, .st-key-mail_status,
:is(.st-key-danger_confirm, .st-key-mh_clear_ask, .st-key-delete_confirm), .mon tbody tr, [class*="st-key-jr_"], [class*="st-key-cr_"] {
  background: var(--surface) !important; border: 1px solid var(--border) !important; border-radius: var(--r-lg) !important;
  box-shadow: var(--hi), var(--shadow) !important;
}
.st-key-add_form, .st-key-email_form, .st-key-test_panel, [class*="st-key-set_"], .st-key-job_main, .st-key-job_side,
.st-key-co_side, .st-key-empty, .st-key-mail_status { padding: 22px 24px !important; gap: 16px !important; }
.st-key-job_main, .st-key-job_side, .st-key-co_side { gap: 22px !important; }
.divider { border-top: 1px solid var(--border); margin: 0; }

/* ── page header ── */
.st-key-page_head > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"],
.st-key-page_head > [data-testid="stHorizontalBlock"] { align-items: flex-end !important; gap: 16px !important; flex-wrap: wrap !important; }
.st-key-page_head [data-testid="stColumn"] { flex: 0 0 auto !important; width: auto !important; min-width: 0 !important; }
.st-key-page_head [data-testid="stColumn"]:first-child { flex: 1 1 320px !important; }
:is(.st-key-page_actions, .st-key-hero_actions) > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"],
:is(.st-key-page_actions, .st-key-hero_actions) > [data-testid="stHorizontalBlock"] { gap: 8px !important; flex-wrap: nowrap !important; }
:is(.st-key-page_actions, .st-key-hero_actions) [data-testid="stColumn"] { flex: 0 0 auto !important; width: auto !important; min-width: 0 !important; }

/* ── Home hero: the live discovery console ── */
.st-key-hero { position: relative; overflow: hidden; padding: 30px 32px 26px !important; gap: 18px !important; isolation: isolate;
  background:
    radial-gradient(460px 260px at 0% 100%, var(--glow-2), transparent 70%),
    linear-gradient(180deg, var(--surface) 40%, color-mix(in srgb, var(--surface) 92%, var(--accent)) 100%) !important; }
.st-key-hero::after { content: ""; position: absolute; inset: 0 0 auto 0; height: 1px; z-index: -1;
  background: linear-gradient(90deg, transparent, color-mix(in srgb, var(--accent) 55%, transparent), color-mix(in srgb, var(--accent-2) 45%, transparent), transparent); }
/* faint layered edge: a soft tint rising from the bottom and fading at the sides */
.st-key-hero::before { content: ""; position: absolute; inset: auto 0 0 0; height: 46%; z-index: -1; pointer-events: none;
  background: radial-gradient(70% 100% at 70% 100%, color-mix(in srgb, var(--accent) 7%, transparent), transparent 70%); }
.hero { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 38%); gap: 24px; align-items: center; }
.hero .page-title { font-size: 42px; line-height: 50px; font-weight: 800; letter-spacing: -0.028em; word-spacing: .08em; text-wrap: balance; max-width: 620px; margin-top: 10px; }
.hero .page-title em { font-style: normal; background: linear-gradient(92deg, var(--accent), var(--accent-2)); -webkit-background-clip: text; background-clip: text; color: transparent; }
.hero-sub { font-size: 16px; line-height: 25px; color: var(--text-2); margin: 10px 0 0; max-width: 560px; }
.hero-status { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 18px; }
.hero-status .live { color: var(--text); font-weight: 600; }
.hero-status .live.healthy { border-color: color-mix(in srgb, var(--green) 30%, var(--border)); }
.hero-status .live.failing { border-color: color-mix(in srgb, var(--red) 40%, var(--border)); }
.hero-status .live.delayed { border-color: color-mix(in srgb, var(--amber) 40%, var(--border)); }

/* the Opportunity Spark: the product's one recurring mark — a small point of
   light where something was found. Used sparingly: hero, discovery headings,
   the Fresher badge. */
.spark { display: inline-block; width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; position: relative;
  background: radial-gradient(circle at 35% 35%, #fff 0 18%, var(--accent-2) 46%, var(--accent) 100%);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent-2) 14%, transparent), 0 0 12px color-mix(in srgb, var(--accent-2) 55%, transparent); }
.eyebrow.sparked::before { content: ""; width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0;
  background: radial-gradient(circle at 35% 35%, #fff 0 18%, var(--accent-2) 46%, var(--accent) 100%);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent-2) 14%, transparent), 0 0 12px color-mix(in srgb, var(--accent-2) 55%, transparent); }

/* The discovery feed: the product's job in one calm loop (10 s). A search is
   typed, three roles arrive one after another, each gets its check (read
   from its own posting), the fresher seal settles, then the feed clears and
   starts again. Decorative (aria-hidden), abstract (bars, icons — no names
   or fake listings), compositor-only motion, still under reduced motion. */
.hero { position: relative; }
.opps-wrap { position: relative; align-self: stretch; min-height: 270px; }
.opps { position: absolute; inset: 0; container-type: inline-size; }
.opps > .glow-bg { position: absolute; inset: -30px -40px -60px -10%; z-index: -1; pointer-events: none; opacity: var(--opp-glow);
  background: radial-gradient(48% 52% at 58% 44%, color-mix(in srgb, var(--accent) 30%, transparent), transparent 72%),
    radial-gradient(36% 40% at 80% 70%, color-mix(in srgb, var(--accent-2) 26%, transparent), transparent 72%),
    radial-gradient(30% 34% at 30% 76%, color-mix(in srgb, #3b82f6 22%, transparent), transparent 72%);
  animation: jt-glow-drift 12s ease-in-out infinite alternate; }
.feed { position: absolute; left: 4%; right: 7%; top: 50%; translate: 0 -50%; display: flex; flex-direction: column; gap: 8px;
  padding: 14px; border-radius: 18px; background: var(--oc-bg); border: 1px solid var(--oc-border); box-shadow: var(--hi), var(--oc-shadow);
  backdrop-filter: blur(10px) saturate(1.2); -webkit-backdrop-filter: blur(10px) saturate(1.2);
  transition: border-color var(--normal) var(--ease), box-shadow var(--normal) var(--ease); }
.feed:hover { border-color: color-mix(in srgb, var(--accent) 28%, var(--oc-border)); }
.feed-q { display: flex; align-items: center; gap: 10px; height: 40px; padding: 0 12px; border-radius: 12px; color: var(--accent-text);
  background: var(--surface-2); border: 1px solid var(--border); margin-bottom: 2px; }
.feed-q .qbar { flex: 1; min-width: 0; display: flex; }
.feed-q .qbar i { display: block; width: 64%; height: 8px; border-radius: 6px; background: var(--oc-bar-strong); transform-origin: left center;
  animation: jt-feed-type 10s var(--ease) infinite both; }
.feed-row { display: flex; align-items: center; gap: 12px; padding: 10px 12px; border-radius: 12px; border: 1px solid transparent;
  background: color-mix(in srgb, var(--surface) 62%, transparent);
  transition: background var(--fast), border-color var(--fast), box-shadow var(--normal) var(--ease);
  animation: jt-feed-row 10s var(--ease) infinite both; }
.feed-row.r1 { animation-delay: .9s; } .feed-row.r2 { animation-delay: 1.6s; } .feed-row.r3 { animation-delay: 2.3s; }
.feed-row:hover { background: var(--surface); border-color: color-mix(in srgb, var(--accent) 30%, transparent);
  box-shadow: 0 12px 26px -18px color-mix(in srgb, var(--accent) 80%, transparent); }
.feed-row .ic, .feed-seal .ic { width: 36px; height: 36px; border-radius: 11px; display: flex; align-items: center; justify-content: center; flex-shrink: 0;
  color: var(--accent-text); border: 1px solid color-mix(in srgb, var(--accent) 18%, transparent); box-shadow: var(--badge-hi);
  background: linear-gradient(180deg, color-mix(in srgb, var(--accent-soft) 60%, var(--surface)), var(--accent-soft));
  transition: transform var(--normal) var(--ease); }
.feed-row:hover .ic { transform: scale(1.06); }
.feed-row .ic.teal { color: var(--accent-2); border-color: color-mix(in srgb, var(--accent-2) 22%, transparent);
  background: linear-gradient(180deg, color-mix(in srgb, var(--accent-2) 8%, var(--surface)), color-mix(in srgb, var(--accent-2) 16%, transparent)); }
.feed-row .ic.blue { color: #3b82f6; border-color: color-mix(in srgb, #3b82f6 22%, transparent);
  background: linear-gradient(180deg, color-mix(in srgb, #3b82f6 8%, var(--surface)), color-mix(in srgb, #3b82f6 15%, transparent)); }
.feed-row .tx { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 7px; }
.feed-row .tx i { display: block; height: 7px; border-radius: 6px; background: var(--oc-bar); }
.feed-row .tx i.l1 { width: 78%; height: 8px; background: var(--oc-bar-strong); } .feed-row .tx i.l2 { width: 48%; }
.feed-row.r2 .tx i.l1 { width: 66%; } .feed-row.r3 .tx i.l1 { width: 72%; } .feed-row.r3 .tx i.l2 { width: 36%; }
.feed-row .ok { width: 24px; height: 24px; border-radius: 50%; display: flex; align-items: center; justify-content: center; flex-shrink: 0;
  color: var(--green); background: var(--green-soft); border: 1px solid color-mix(in srgb, var(--green) 30%, transparent); box-shadow: var(--badge-hi);
  animation: jt-feed-ok 10s var(--ease) infinite both; }
.feed-row .ok .ms { font-size: 15px; }
.feed-row.r1 .ok { animation-delay: .9s; } .feed-row.r2 .ok { animation-delay: 1.6s; } .feed-row.r3 .ok { animation-delay: 2.3s; }
.feed-seal { position: absolute; right: -30px; bottom: -30px; z-index: 2; padding: 10px; border-radius: 16px; background: var(--oc-bg);
  border: 1px solid var(--oc-border); box-shadow: var(--hi), var(--oc-shadow); backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px);
  rotate: 6deg; animation: jt-feed-seal 10s var(--ease) infinite both; }
.feed-seal .ic { width: 40px; height: 40px; border-radius: 12px; color: var(--green); border-color: color-mix(in srgb, var(--green) 25%, transparent);
  background: linear-gradient(180deg, color-mix(in srgb, var(--green-soft) 50%, var(--surface)), var(--green-soft)); }
.feed-seal .spark-dot { position: absolute; top: 7px; right: 7px; width: 7px; height: 7px; border-radius: 50%;
  background: radial-gradient(circle at 35% 35%, #fff 0 20%, var(--accent-2) 70%); box-shadow: 0 0 8px color-mix(in srgb, var(--accent-2) 70%, transparent); }
@keyframes jt-feed-type { 0% { opacity: 0; transform: scaleX(0); } 3% { opacity: 1; } 10%, 88% { opacity: 1; transform: scaleX(1); } 96%, 100% { opacity: 0; transform: scaleX(1); } }
@keyframes jt-feed-row { 0% { opacity: 0; transform: translate3d(0, 10px, 0); } 5%, 80% { opacity: 1; transform: translate3d(0, 0, 0); } 88%, 100% { opacity: 0; transform: translate3d(0, -6px, 0); } }
@keyframes jt-feed-ok { 0%, 7% { opacity: 0; transform: scale(.4); } 11% { opacity: 1; transform: scale(1.14); } 14%, 80% { opacity: 1; transform: scale(1); } 88%, 100% { opacity: 0; transform: scale(.8); } }
@keyframes jt-feed-seal { 0%, 36% { opacity: 0; transform: translate3d(0, 8px, 0) scale(.86); } 42% { opacity: 1; transform: translate3d(0, 0, 0) scale(1.05); } 46%, 84% { opacity: 1; transform: translate3d(0, 0, 0) scale(1); } 92%, 100% { opacity: 0; transform: translate3d(0, 4px, 0) scale(.92); } }

/* ── metric modules ── */
.stats { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; }
.stat { position: relative; padding: 18px 18px 16px; min-width: 0; border: 1px solid var(--border); border-radius: var(--r-lg);
  background: radial-gradient(160px 90px at 0% 0%, color-mix(in srgb, var(--tint, var(--accent)) 6%, transparent), transparent 70%),
    linear-gradient(180deg, var(--surface), color-mix(in srgb, var(--surface) 70%, var(--surface-2)));
  box-shadow: var(--hi), var(--shadow); transition: transform var(--normal) var(--ease), box-shadow var(--normal) var(--ease), border-color var(--normal) var(--ease); overflow: hidden; }
.stat::before { content: ""; position: absolute; left: 16px; right: 40%; top: 0; height: 2px; border-radius: 0 0 2px 2px;
  background: linear-gradient(90deg, var(--tint, var(--accent)), transparent); opacity: .65; transition: opacity var(--normal) var(--ease); }
.stat .ic { width: 36px; height: 36px; border-radius: 11px; display: flex; align-items: center; justify-content: center; margin-bottom: 14px;
  color: var(--tint, var(--accent)); border: 1px solid color-mix(in srgb, var(--tint, var(--accent)) 18%, transparent);
  background: linear-gradient(180deg, color-mix(in srgb, var(--tint, var(--accent)) 9%, transparent), color-mix(in srgb, var(--tint, var(--accent)) 16%, transparent));
  box-shadow: var(--badge-hi); transition: transform var(--normal) var(--ease), box-shadow var(--normal) var(--ease); }
.stat.t-green { --tint: var(--green); } .stat.t-teal { --tint: var(--accent-2); } .stat.t-amber { --tint: var(--amber); } .stat.t-red { --tint: var(--red); } .stat.t-gray { --tint: var(--gray); }
.stat .k { font-size: 13px; line-height: 18px; color: var(--muted); font-weight: 500; display: flex; align-items: center; gap: 6px; }
.stat .v { font-size: 30px; line-height: 36px; font-weight: 700; letter-spacing: -0.03em; color: var(--text); margin-top: 4px; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.stat .v.sm { font-size: 20px; line-height: 28px; letter-spacing: -0.02em; }
.stat .n { font-size: 12.5px; line-height: 18px; color: var(--muted); margin-top: 4px; overflow-wrap: anywhere; }
.st-key-stats { background: transparent !important; border: none !important; box-shadow: none !important; }

/* ── discovery controls (search + filters as one component) ── */
[class*="st-key-filters_"] { padding: 12px !important; border-radius: var(--r-lg) !important; }
[class*="st-key-filters_"] > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"],
[class*="st-key-filters_"] > [data-testid="stHorizontalBlock"] { gap: 8px !important; flex-wrap: wrap !important; align-items: center !important; }
[class*="st-key-filters_"] [data-testid="stColumn"] { flex: 1 1 140px !important; width: auto !important; min-width: 130px !important; max-width: 230px; }
[class*="st-key-filters_"] [data-testid="stColumn"]:first-child { flex: 1 1 100% !important; max-width: none; }
[class*="st-key-filters_"] [data-testid="stColumn"]:last-child { flex: 0 0 auto !important; min-width: 0 !important; margin-left: auto; }
/* the personal filters' second row: four equal fields that wrap on narrow screens */
[class*="st-key-filters_"] [class*="st-key-prow_"] [data-testid="stColumn"]:nth-child(n) { flex: 1 1 180px !important;
  min-width: 160px !important; max-width: none !important; margin-left: 0 !important; }
[class*="st-key-filters_"] [class*="st-key-prow_"] [data-testid="stHorizontalBlock"] { gap: 10px !important; align-items: flex-start !important; }
:is(.st-key-h_q, .st-key-j_q) :is([data-baseweb="input"], [data-testid="stTextInputRootElement"]) { min-height: 48px !important; border-radius: var(--r) !important; background: var(--surface-2) !important; }
:is(.st-key-h_q, .st-key-j_q) input { font-size: 15px !important; }
:is(.st-key-h_q, .st-key-j_q, .st-key-co_q, .stTextInput:has(input[aria-label^="Search"])) :is([data-baseweb="input"], [data-testid="stTextInputRootElement"])::before {
  content: "search"; font-family: 'Material Symbols Rounded' !important; font-feature-settings: 'liga'; font-weight: 400;
  font-size: 20px; width: 20px; overflow: hidden; white-space: nowrap; flex-shrink: 0;
  color: var(--muted); margin-left: 14px; align-self: center; line-height: 1; box-sizing: content-box; transition: color var(--fast);
}
:is(.st-key-h_q, .st-key-j_q, .st-key-co_q, .stTextInput:has(input[aria-label^="Search"])) :is([data-baseweb="input"], [data-testid="stTextInputRootElement"]):focus-within::before { color: var(--accent-text); }
[class*="st-key-filters_"] :is(.stSelectbox, .stMultiSelect) div[role="group"] { border-radius: var(--r) !important; min-height: 44px !important; background: var(--surface) !important; }
[class*="st-key-filters_"] .stSelectbox input { padding-left: 14px !important; }

/* ── section heads ── */
.sec-head { display: flex; align-items: flex-end; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.sec-head .section-title { font-size: 19px; line-height: 26px; letter-spacing: -0.012em; margin-top: 4px; }

/* ── opportunity cards ── */
[class*="st-key-joblist_"] { gap: 10px !important; container-type: inline-size; }
[class*="st-key-jr_"] { position: relative; padding: 18px 20px !important; gap: 0 !important; overflow: hidden;
  transition: transform var(--normal) var(--ease), box-shadow var(--normal) var(--ease), border-color var(--normal) var(--ease); }
[class*="st-key-jr_"]::before { content: ""; position: absolute; left: 0; top: 14px; bottom: 14px; width: 3px; border-radius: 0 3px 3px 0;
  background: linear-gradient(180deg, var(--accent), var(--accent-2)); opacity: 0; transform: scaleY(.4); transition: opacity var(--normal) var(--ease), transform var(--normal) var(--ease); }
[class*="st-key-jr_"]:hover, [class*="st-key-jr_"]:focus-within { transform: translateY(-2px); box-shadow: var(--hi), var(--shadow-lg) !important; border-color: color-mix(in srgb, var(--accent) 30%, var(--border)) !important; }
[class*="st-key-jr_"]:hover::before, [class*="st-key-jr_"]:focus-within::before { opacity: 1; transform: none; }
/* the card IS its vertical block (Streamlit 1.65): text and actions side by side */
[class*="st-key-jr_"] { flex-direction: row !important; align-items: center !important; flex-wrap: nowrap !important; column-gap: 16px !important; }
[class*="st-key-jr_"] > [data-testid="stElementContainer"] { flex: 1 1 auto !important; width: auto !important; min-width: 0 !important; }
[class*="st-key-jr_"] > [data-testid="stLayoutWrapper"] { flex: 0 0 auto !important; width: auto !important; min-width: 0 !important; }
[class*="st-key-ja_"] { flex-direction: row !important; gap: 8px !important; align-items: center !important; flex-wrap: nowrap !important; width: auto !important; }
[class*="st-key-ja_"] > [data-testid="stElementContainer"] { width: auto !important; flex: 0 0 auto !important; }
.job { display: flex; gap: 16px; min-width: 0; align-items: flex-start; }
.logo { --h: 245; width: 44px; height: 44px; border-radius: 13px; display: flex; align-items: center; justify-content: center; flex-shrink: 0;
  font-size: 17px; font-weight: 800; letter-spacing: -0.02em;
  color: hsl(var(--h) var(--av-sat) var(--av-fg)); background: linear-gradient(140deg, hsl(var(--h) var(--av-sat) var(--av-bg)), hsl(calc(var(--h) + 28) var(--av-sat) var(--av-bg)));
  box-shadow: inset 0 0 0 1px hsl(var(--h) var(--av-sat) var(--av-bd)), var(--hi); transition: transform var(--normal) var(--ease); }
.logo.lg { width: 64px; height: 64px; border-radius: 18px; font-size: 26px; }
.logo.sm { width: 34px; height: 34px; border-radius: 10px; font-size: 14px; }
.logo.ini2 { font-size: 15px; letter-spacing: -0.03em; }
.logo.lg.ini2 { font-size: 23px; } .logo.sm.ini2 { font-size: 12.5px; }
/* a company's own mark on its tile (local SVG from brand_logos.py: nothing is fetched) */
/* (its own class name: the sidebar's .brand rules once resized these tiles) */
.logo.co-mark { background: var(--tile, #fff); padding: 0; box-shadow: inset 0 0 0 1px rgba(15,23,42,.10), var(--hi); }
.logo.co-mark img { width: 62%; height: 62%; margin: 0; padding: 0; border: 0; border-radius: 0; box-shadow: none;
  object-fit: contain; display: block; pointer-events: none; user-select: none; }
/* Account → Profile avatar: glossy round tiles; the chosen one is ringed and ticked */
.av-pick-head { margin-top: 14px; } .av-pick-head b { display: block; font-size: 14px; line-height: 20px; font-weight: 700; color: var(--text); }
.av-pick-head span { display: block; font-size: 13px; line-height: 19px; color: var(--muted); margin-top: 2px; }
/* (.st-key-av_picker IS the vertical block in Streamlit 1.65: lay its children out as a wrapping row) */
.st-key-av_picker { flex-direction: row !important; flex-wrap: wrap !important; gap: 10px !important; margin-top: 6px; }
.st-key-av_picker > * { width: auto !important; flex: 0 0 auto !important; }
[class*="st-key-av_pick_"] button { position: relative; width: 92px !important; min-height: 98px !important; padding: 12px 6px 10px !important;
  flex-direction: column !important; justify-content: flex-start !important; gap: 8px !important; border-radius: 16px !important;
  background: var(--btn2-grad) !important; border: 1px solid var(--border) !important; box-shadow: var(--hi), var(--shadow) !important;
  transition: transform var(--fast) var(--ease), border-color var(--fast) var(--ease), box-shadow var(--fast) var(--ease) !important; }
[class*="st-key-av_pick_"] button::before { content: ""; width: 46px; height: 46px; border-radius: 50%; flex-shrink: 0; display: grid; place-items: center;
  font-family: var(--font); font-size: 18px; font-weight: 800; color: #fff;
  background: var(--av-img, none) center / 60% no-repeat, var(--av-tile, #fff);
  box-shadow: inset 0 0 0 1px rgba(15,23,42,.12), inset 0 1px 0 rgba(255,255,255,.4), 0 6px 14px -8px rgba(15,23,42,.5);
  transition: transform var(--fast) var(--ease); }
[class*="st-key-av_pick_"] button p { font-size: 12px !important; line-height: 16px !important; font-weight: 600 !important; color: var(--muted) !important; }
[class*="st-key-av_pick_"] button:hover { transform: translateY(-2px); border-color: color-mix(in srgb, var(--accent) 35%, var(--border)) !important;
  box-shadow: var(--hi), var(--btn2-shadow-hover), 0 14px 26px -18px var(--accent) !important; }
[class*="st-key-av_pick_"] button:hover::before { transform: scale(1.06); }
.name-msg { display: flex; gap: 8px; align-items: center; font-size: 13.5px; line-height: 1.45; margin-top: 4px;
  color: var(--green); } .name-msg.err { color: var(--red); }
[class*="st-key-jr_"]:hover .logo, [class*="st-key-cr_"]:hover .logo { transform: scale(1.05);
  box-shadow: inset 0 0 0 1px hsl(var(--h) var(--av-sat) var(--av-bd)), var(--hi), 0 6px 14px -8px hsl(var(--h) 60% 45%); }
/* a hovered card's primary action steps forward slightly */
:is([class*="st-key-jr_"]:hover, [class*="st-key-jr_"]:focus-within) .btn.primary { box-shadow: var(--btn-hi), 0 10px 22px -10px var(--accent); }
/* soft light in the card's top-left corner on hover */
[class*="st-key-jr_"], [class*="st-key-cr_"] { position: relative; isolation: isolate; }
[class*="st-key-jr_"]::after, [class*="st-key-cr_"]::after { content: ""; position: absolute; inset: 0; border-radius: inherit; pointer-events: none; opacity: 0; z-index: -1;
  background: radial-gradient(420px 160px at 0% 0%, color-mix(in srgb, var(--accent) 6%, transparent), transparent 70%); transition: opacity var(--normal) var(--ease); }
:is([class*="st-key-jr_"], [class*="st-key-cr_"]):is(:hover, :focus-within)::after { opacity: 1; }
.job-body { min-width: 0; flex: 1; }
.job-co { font-size: 13px; line-height: 18px; color: var(--text-2); font-weight: 600; display: flex; align-items: center; gap: 6px; }
.job-title { font-size: 17px; line-height: 24px; font-weight: 600; letter-spacing: -0.008em; color: var(--text); margin: 2px 0 0; overflow-wrap: anywhere; }
.job-meta { font-size: 13px; line-height: 20px; color: var(--muted); margin-top: 6px; display: flex; flex-wrap: wrap; align-items: center; gap: 4px 14px; }
.job-meta .mi { display: inline-flex; align-items: center; gap: 4px; min-width: 0; }
.job-meta .mi .ms { font-size: 16px; color: var(--muted); }
.job-meta .co { color: var(--text-2); font-weight: 600; }
.job-why { font-size: 13px; line-height: 18px; color: var(--text-2); margin-top: 10px; display: flex; align-items: center; gap: 8px; min-width: 0; flex-wrap: wrap; }
.job-why span.t { display: inline-flex; align-items: center; gap: 5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; max-width: 100%; }
.job-why span.t .ms { font-size: 16px; color: var(--accent-text); }
.list-foot { padding: 12px 20px; display: flex; align-items: center; justify-content: space-between; gap: 12px; font-size: 13px; color: var(--muted); }
.st-key-list_foot_h, .st-key-list_foot_j { padding: 6px 4px 0 !important; }
.st-key-list_foot_h > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"], .st-key-list_foot_h > [data-testid="stHorizontalBlock"],
.st-key-list_foot_j > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"], .st-key-list_foot_j > [data-testid="stHorizontalBlock"] { align-items: center !important; gap: 8px !important; flex-wrap: nowrap !important; }
.st-key-list_foot_h [data-testid="stColumn"], .st-key-list_foot_j [data-testid="stColumn"] { flex: 0 0 auto !important; width: auto !important; min-width: 0 !important; }
.st-key-list_foot_h [data-testid="stColumn"]:first-child, .st-key-list_foot_j [data-testid="stColumn"]:first-child { flex: 1 1 auto !important; }
/* a narrow list (company page, tablet, phone): actions move under the text */
@container (max-width: 780px) {
  [class*="st-key-jr_"] { flex-wrap: wrap !important; row-gap: 14px !important; }
  [class*="st-key-jr_"] > [data-testid="stElementContainer"] { flex: 1 1 100% !important; }
  [class*="st-key-jr_"] > [data-testid="stLayoutWrapper"] { flex: 1 1 100% !important; padding-left: 60px; }
}
@container (max-width: 420px) {
  [class*="st-key-jr_"] > [data-testid="stLayoutWrapper"] { padding-left: 0; }
  .job { gap: 12px; } .job .logo { width: 38px; height: 38px; font-size: 15px; border-radius: 11px; }
}

/* ── buttons ── */
.btn { display: inline-flex; align-items: center; justify-content: center; gap: 6px; height: 38px; padding: 0 16px; border-radius: 10px; font-size: 14px; font-weight: 700;
  white-space: nowrap; letter-spacing: -0.005em; border: 1px solid transparent;
  transition: transform var(--fast) var(--ease), box-shadow var(--fast) var(--ease), background var(--fast), border-color var(--fast); }
.btn .ms { transition: transform var(--fast) var(--ease); }
.btn:hover .ms { transform: translate(2px, -2px); }
/* Primary = one glossy surface everywhere (links styled as buttons and real
   Streamlit buttons): a lit top edge, a gradient body, a darker lip, a soft
   coloured shadow, and on hover a single specular sweep. Secondary shares the
   radius, height and motion but stays a quiet raised surface. */
.btn.primary, [data-testid="stBaseButton-primary"] { position: relative; overflow: hidden; isolation: isolate; }
.btn.primary { color: var(--on-accent) !important; background: var(--btn-grad); border-color: var(--btn-border); box-shadow: var(--btn-hi), var(--btn-shadow); }
.btn.primary:hover { transform: translateY(-2px); box-shadow: var(--btn-hi), var(--btn-shadow-hover); }
.btn.primary:active { transform: translateY(0) scale(.98); box-shadow: var(--btn-pressed); }
.btn.primary::before, [data-testid="stBaseButton-primary"]::before {   /* glossy reflection on the upper half */
  content: ""; position: absolute; left: 1px; right: 1px; top: 1px; height: 52%; z-index: -1; pointer-events: none; border-radius: 9px 9px 14px 14px / 9px 9px 6px 6px;
  background: linear-gradient(180deg, rgba(255,255,255,.42), rgba(255,255,255,.16) 78%, rgba(255,255,255,.04) 96%, rgba(255,255,255,0)); }
.btn.primary::after, [data-testid="stBaseButton-primary"]::after {   /* the specular sweep on hover */
  content: ""; position: absolute; top: 0; bottom: 0; left: 0; width: 55%; z-index: -1; pointer-events: none;
  background: linear-gradient(100deg, transparent 20%, rgba(255,255,255,.38) 50%, transparent 80%);
  transform: translateX(-130%) skewX(-14deg); transition: transform 0s; }
.btn.primary:hover::after, [data-testid="stBaseButton-primary"]:hover:not(:disabled)::after { transform: translateX(240%) skewX(-14deg); transition: transform 520ms var(--ease); }
.btn.ghost { background: var(--btn2-grad); color: var(--text) !important; border-color: var(--border-strong); box-shadow: var(--hi), var(--btn2-shadow); }
.btn.ghost:hover { border-color: color-mix(in srgb, var(--accent) 45%, var(--border-strong)); color: var(--accent-text) !important; transform: translateY(-1px); box-shadow: var(--hi), var(--btn2-shadow-hover); }
.btn.disabled { background: var(--hover); color: var(--muted) !important; }
.btn:focus-visible, .link:focus-visible, .stApp a:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 8px; }
.link { color: var(--accent-text) !important; font-weight: 600; display: inline-flex; align-items: center; gap: 4px; overflow-wrap: anywhere; }
.link:hover { text-decoration: underline !important; text-underline-offset: 3px; }

[data-testid^="stBaseButton"] {
  border-radius: 10px !important; min-height: 38px !important; padding: 0 15px !important;
  transition: transform var(--fast) var(--ease), background var(--fast), border-color var(--fast), color var(--fast), box-shadow var(--fast) !important;
}
[data-testid^="stBaseButton"]:active:not(:disabled) { transform: scale(.98); }
[data-testid^="stBaseButton"]:focus-visible { outline: 2px solid var(--accent) !important; outline-offset: 2px !important; box-shadow: var(--ring) !important; }
[data-testid^="stBaseButton"] [data-testid="stMarkdownContainer"] { display: flex !important; align-items: center; margin: 0 !important; padding: 0 !important; }
[data-testid^="stBaseButton"] p { margin: 0 !important; font-size: 14px !important; line-height: 20px !important; font-weight: 600 !important; white-space: nowrap; }
[data-testid^="stBaseButton"] [data-testid="stIconMaterial"] { font-size: 18px !important; transition: transform var(--fast) var(--ease); }
[data-testid="stBaseButton-secondary"], [data-testid="stBaseButton-secondaryFormSubmit"] { background: var(--btn2-grad) !important; color: var(--text) !important; border: 1px solid var(--border-strong) !important; box-shadow: var(--hi), var(--btn2-shadow) !important; }
[data-testid="stBaseButton-secondary"]:hover:not(:disabled), [data-testid="stBaseButton-secondaryFormSubmit"]:hover:not(:disabled) { border-color: color-mix(in srgb, var(--accent) 45%, var(--border-strong)) !important; color: var(--accent-text) !important; transform: translateY(-1px);
  background: var(--btn2-grad) !important; box-shadow: var(--hi), var(--btn2-shadow-hover) !important; }
[data-testid="stBaseButton-secondary"]:active:not(:disabled), [data-testid="stBaseButton-secondaryFormSubmit"]:active:not(:disabled) { transform: translateY(0) scale(.98); box-shadow: var(--btn2-pressed) !important; }
[data-testid="stBaseButton-primary"] { color: var(--on-accent) !important; border: 1px solid var(--btn-border) !important;
  background: var(--btn-grad) !important; box-shadow: var(--btn-hi), var(--btn-shadow) !important; text-shadow: 0 1px 0 rgba(20,10,80,.18); }
[data-testid="stBaseButton-primary"]:hover:not(:disabled) { transform: translateY(-2px); box-shadow: var(--btn-hi), var(--btn-shadow-hover) !important; }
[data-testid="stBaseButton-primary"]:active:not(:disabled) { transform: translateY(0) scale(.98); box-shadow: var(--btn-pressed) !important; }
[data-testid="stBaseButton-primary"] p { font-weight: 700 !important; }
[data-testid^="stBaseButton"]:disabled { opacity: .45 !important; transform: none !important; cursor: not-allowed; }
[data-testid="stBaseButton-tertiary"] { color: var(--muted) !important; background: transparent !important; border: none !important; padding: 0 10px !important; }
[data-testid="stBaseButton-tertiary"]:hover { color: var(--text) !important; background: var(--hover) !important; }
.st-key-btn_back [data-testid="stBaseButton-tertiary"]:hover [data-testid="stIconMaterial"] { transform: translateX(-3px); }
.st-key-btn_run_check [data-testid^="stBaseButton"]:hover [data-testid="stIconMaterial"] { transform: scale(1.15); }
.st-key-btn_refresh [data-testid^="stBaseButton"]:hover [data-testid="stIconMaterial"], .st-key-btn_reload [data-testid^="stBaseButton"]:hover [data-testid="stIconMaterial"] { transform: rotate(90deg); }
/* destructive: quiet until hovered, then unmistakably red */
:is(.st-key-danger, .st-key-btn_mh_clear, .st-key-btn_delete_open) [data-testid^="stBaseButton"] { color: var(--red) !important; }
:is(.st-key-danger, .st-key-btn_mh_clear, .st-key-btn_delete_open) [data-testid^="stBaseButton"]:hover:not(:disabled) { color: var(--red) !important; background: var(--red-soft) !important; border-color: color-mix(in srgb, var(--red) 45%, transparent) !important; }
:is(.st-key-danger_confirm, .st-key-mh_clear_ask, .st-key-delete_confirm) { padding: 16px 18px !important; gap: 12px !important; border-color: color-mix(in srgb, var(--red) 35%, var(--border)) !important;
  background: linear-gradient(180deg, var(--red-soft), transparent 140%), var(--surface) !important; }
:is(.st-key-danger_confirm, .st-key-mh_clear_ask, .st-key-delete_confirm) [data-testid="stBaseButton-secondary"] { color: var(--text) !important; }
:is(.st-key-danger_confirm, .st-key-mh_clear_ask, .st-key-delete_confirm) [data-testid="stBaseButton-primary"] { background: var(--red) !important; border-color: var(--red) !important; color: #fff !important; box-shadow: 0 8px 18px -10px var(--red) !important; }
[class*="st-key-dismiss_"] [data-testid^="stBaseButton"], [class*="st-key-restore_"] [data-testid^="stBaseButton"] { width: 38px !important; padding: 0 !important; color: var(--muted) !important; }
[class*="st-key-dismiss_"] [data-testid^="stBaseButton"]:hover { color: var(--red) !important; border-color: color-mix(in srgb, var(--red) 40%, transparent) !important; background: var(--red-soft) !important; }
[class*="st-key-restore_"] [data-testid^="stBaseButton"]:hover { color: var(--accent-text) !important; }

/* segmented tabs (st.pills) */
.st-key-jobs_tab_wrap [data-testid="stButtonGroup"] > div, .st-key-co_status_wrap [data-testid="stButtonGroup"] > div {
  gap: 2px !important; background: color-mix(in srgb, var(--surface) 60%, var(--hover)); border: 1px solid var(--border); padding: 4px; border-radius: 12px; display: inline-flex !important; flex-wrap: wrap;
}
.st-key-jobs_tab_wrap button[data-variant="pills"], .st-key-co_status_wrap button[data-variant="pills"],
.st-key-jobs_tab_wrap [data-testid^="stBaseButton-pills"], .st-key-co_status_wrap [data-testid^="stBaseButton-pills"] {
  border: 1px solid transparent !important; border-radius: 9px !important; background: transparent !important; color: var(--muted) !important; min-height: 32px !important; padding: 0 13px !important;
}
.st-key-jobs_tab_wrap button[data-variant="pills"]:hover, .st-key-co_status_wrap button[data-variant="pills"]:hover { color: var(--text) !important; }
.st-key-jobs_tab_wrap button[data-variant="pills"][aria-checked="true"], .st-key-co_status_wrap button[data-variant="pills"][aria-checked="true"],
.st-key-jobs_tab_wrap [data-testid="stBaseButton-pillsActive"], .st-key-co_status_wrap [data-testid="stBaseButton-pillsActive"] {
  background: var(--surface) !important; color: var(--text) !important; border-color: var(--border) !important; box-shadow: var(--hi), var(--shadow) !important;
}
.st-key-jobs_tab_wrap button[data-variant="pills"] p, .st-key-co_status_wrap button[data-variant="pills"] p { color: inherit !important; font-size: 13.5px !important; }

/* ── detail views ── */
.st-key-job_hero, .st-key-co_hero { position: relative; overflow: hidden; padding: 26px 28px !important; gap: 20px !important;
  background: radial-gradient(480px 240px at 100% 0%, var(--glow-1), transparent 70%), var(--surface) !important; }
.detail-head { display: flex; gap: 18px; align-items: flex-start; }
.detail-title { font-size: 28px; line-height: 35px; font-weight: 700; letter-spacing: -0.03em; margin: 4px 0 0; color: var(--text); overflow-wrap: anywhere; }
.detail-chips { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; align-items: center; }
.kv { display: grid; grid-template-columns: 150px minmax(0, 1fr); gap: 12px 18px; font-size: 14px; line-height: 20px; margin: 14px 0 0 !important; padding: 0 !important; }
.kv dt { color: var(--muted); margin: 0 !important; padding: 0 !important; } .kv dd { margin: 0 !important; padding: 0 !important; color: var(--text); min-width: 0; overflow-wrap: anywhere; font-weight: 500; }
.note { font-size: 14px; line-height: 21px; color: var(--muted); margin: 0; }
.kv-note { display: block; margin-top: 3px; font-size: 13px; line-height: 19px; color: var(--muted); font-weight: 400; }
.kv.tech { margin: 2px 0 4px !important; font-size: 13px; } .kv.tech dd { font-weight: 400; color: var(--text-2); }
.why { display: flex; gap: 12px; align-items: flex-start; font-size: 15px; line-height: 22px; color: var(--text); font-weight: 500; margin-top: 12px; }
.why > .ms { color: var(--green); margin-top: 1px; }
.quote { margin-top: 10px; padding: 10px 12px; border-left: 3px solid color-mix(in srgb, var(--accent) 55%, transparent); background: var(--surface-2); border-radius: 0 10px 10px 0; font-size: 13px; line-height: 19px; color: var(--text-2); font-weight: 400; }
.checks-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.checks-score { display: inline-flex; align-items: center; gap: 8px; font-size: 13px; font-weight: 700; color: var(--green); }
.meter { display: grid; grid-template-columns: repeat(var(--n, 8), 1fr); gap: 4px; margin-top: 14px; }
.meter span { height: 6px; border-radius: 6px; background: var(--green); opacity: .85; }
.meter span.off { background: var(--red); }
.checks { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin: 16px 0 0 !important; padding: 0 !important; list-style: none; }
.checks li { display: flex; align-items: center; gap: 10px; padding: 10px 12px; border-radius: 10px; background: var(--surface-2); border: 1px solid var(--border); font-size: 13.5px; line-height: 19px; color: var(--text-2); }
.checks li .ms { font-size: 18px; color: var(--green); }
.checks li.off .ms { color: var(--red); }
.st-key-job_cols > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"], .st-key-job_cols > [data-testid="stHorizontalBlock"],
.st-key-co_cols > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"], .st-key-co_cols > [data-testid="stHorizontalBlock"] { gap: 22px !important; align-items: flex-start !important; }
.st-key-job_actions > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"], .st-key-job_actions > [data-testid="stHorizontalBlock"],
.st-key-co_actions > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"], .st-key-co_actions > [data-testid="stHorizontalBlock"] { gap: 8px !important; flex-wrap: wrap !important; }
.st-key-job_actions [data-testid="stColumn"], .st-key-co_actions [data-testid="stColumn"] { flex: 0 0 auto !important; width: auto !important; min-width: 0 !important; }
[data-testid="stExpander"] details { border: 1px solid var(--border) !important; border-radius: var(--r) !important; background: var(--surface-2) !important; }
[data-testid="stExpander"] summary p { font-size: 14px !important; font-weight: 600 !important; color: var(--text) !important; }
[data-testid="stExpander"] summary:hover { color: var(--accent-text) !important; }

/* ── companies ── */
.st-key-company_list { display: grid !important; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 14px !important; }
.st-key-company_list > [data-testid="stElementContainer"]:first-child { display: none; }   /* table header: cards carry their own labels */
[class*="st-key-cr_"] { padding: 18px !important; gap: 0 !important; height: 100%;
  transition: transform var(--normal) var(--ease), box-shadow var(--normal) var(--ease), border-color var(--normal) var(--ease); }
[class*="st-key-cr_"]:hover, [class*="st-key-cr_"]:focus-within { transform: translateY(-2px); box-shadow: var(--hi), var(--shadow-lg) !important; border-color: color-mix(in srgb, var(--accent) 30%, var(--border)) !important; }
/* the card's text grows; its button sits at the bottom, full width */
[class*="st-key-cr_"] { row-gap: 14px !important; justify-content: space-between; }
[class*="st-key-cr_"] > [data-testid="stElementContainer"] { width: 100% !important; min-width: 0 !important; }
[class*="st-key-cr_"] > [data-testid="stElementContainer"]:first-child { flex: 1 1 auto !important; }
[class*="st-key-cr_"] [data-testid^="stBaseButton"] { width: 100%; }
.co-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; grid-template-areas: "name status" "stats stats" "site site"; gap: 14px 12px; align-items: start; min-width: 0; }
.co-row > :nth-child(1) { grid-area: name; } .co-row > :nth-child(2) { grid-area: site; } .co-row > :nth-child(3) { grid-area: status; }
.co-stats { grid-area: stats; display: grid; grid-template-columns: 1fr 1fr; gap: 0; border: 1px solid var(--border); border-radius: var(--r); background: var(--surface-2); }
.co-stats > div { padding: 10px 12px; min-width: 0; } .co-stats > div + div { border-left: 1px solid var(--border); }
.co-stats .k { font-family: var(--label) !important; font-size: 10px; letter-spacing: .12em; text-transform: uppercase; color: var(--muted); display: block; margin-bottom: 2px; }
.co-name { display: flex; gap: 12px; align-items: center; min-width: 0; }
.co-name .t { font-size: 16px; line-height: 22px; font-weight: 600; color: var(--text); display: flex; gap: 8px; align-items: center; flex-wrap: wrap; letter-spacing: -0.015em; }
.co-name .h { font-size: 12.5px; line-height: 18px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.co-cell { font-size: 14px; color: var(--text); font-weight: 600; font-variant-numeric: tabular-nums; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; display: flex; align-items: center; gap: 6px; }
.co-cell .ms { font-size: 16px; color: var(--muted); }
.co-cell .l { color: var(--muted); font-weight: 500; }
.co-cell.site { font-size: 13px; font-weight: 500; }
.list-head { display: grid; grid-template-columns: minmax(0, 1.5fr) minmax(0, 1fr) 110px 90px 100px 88px; gap: 16px; padding: 10px 20px; font-size: 13px; color: var(--muted); }
.st-key-co_toolbar > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"], .st-key-co_toolbar > [data-testid="stHorizontalBlock"] { gap: 12px !important; flex-wrap: wrap !important; }
.st-key-co_toolbar [data-testid="stColumn"]:first-child { flex: 1 1 280px !important; min-width: 0 !important; }
.st-key-co_toolbar [data-testid="stColumn"]:last-child { flex: 2 1 320px !important; min-width: 0 !important; }
.st-key-co_q :is([data-baseweb="input"], [data-testid="stTextInputRootElement"]) { min-height: 44px !important; border-radius: var(--r) !important; }
.add-steps { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
.add-steps div { padding: 12px 14px; border-radius: var(--r); background: var(--surface-2); border: 1px solid var(--border); font-size: 13px; line-height: 19px; color: var(--text-2); }
.add-steps b { display: flex; align-items: center; gap: 6px; color: var(--text); font-size: 13.5px; margin-bottom: 2px; }
.add-steps .ms { color: var(--accent-text); }

/* ── monitoring: one health card per portal (semantic table, card layout) ── */
.st-key-mon_table { background: transparent !important; border: none !important; box-shadow: none !important; }
.mon { width: 100%; border-collapse: separate; border-spacing: 0; font-size: 14px; line-height: 20px; }
.mon thead { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); clip-path: inset(50%); white-space: nowrap; }
.mon tbody { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 14px; }
.mon tbody tr { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px 12px; padding: 18px; position: relative; overflow: hidden;
  transition: transform var(--normal) var(--ease), box-shadow var(--normal) var(--ease), border-color var(--normal) var(--ease); }
.mon tbody tr::before { content: ""; position: absolute; inset: 0 0 auto; height: 3px; background: var(--green); opacity: .75; }
.mon tbody tr:has(.pill.failing) { border-color: color-mix(in srgb, var(--red) 40%, var(--border)) !important; }
.mon tbody tr:has(.pill.failing)::before { background: var(--red); }
.mon tbody tr:has(.pill.delayed)::before { background: var(--amber); }
.mon tbody tr:has(.pill.pending)::before, .mon tbody tr:has(.pill.checking)::before { background: var(--gray); }
.mon td { display: block; padding: 0; border: none; color: var(--text); min-width: 0; font-weight: 600; }
.mon td.c { grid-column: 1 / 3; font-weight: 700; font-size: 15.5px; letter-spacing: -0.015em; }
.mon td[data-l="Status"] { grid-column: 3; justify-self: end; }
.mon td[data-l="Notes"] { grid-column: 1 / -1; font-weight: 400; color: var(--text-2); font-size: 13px; line-height: 19px; padding: 10px 12px; border-radius: 10px; background: var(--surface-2); border: 1px solid var(--border); }
.mon td[data-l]:not([data-l="Status"])::before { content: attr(data-l); display: block; font-family: var(--label); font-size: 10px; line-height: 14px; letter-spacing: .12em; text-transform: uppercase; color: var(--muted); font-weight: 500; margin-bottom: 3px; }
.mon td[data-l="Scraper"] { font-weight: 500; font-size: 13px; color: var(--text-2); }
.mon-co { display: flex; align-items: center; gap: 12px; min-width: 0; }
.mon td .sub { color: var(--muted); font-size: 12.5px; font-weight: 400; margin-top: 1px; letter-spacing: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.mon td.empty { display: none; }
.mon td.num { font-variant-numeric: tabular-nums; }
.legend { display: flex; gap: 12px; align-items: flex-start; padding: 14px 16px; border-radius: var(--r); background: color-mix(in srgb, var(--surface) 70%, transparent); border: 1px dashed var(--border-strong); }
.legend > .ms { color: var(--accent-text); margin-top: 1px; }

/* ── email: notification centre ── */
.mail-status { display: flex; gap: 16px; align-items: center; }
.mail-status .big { width: 52px; height: 52px; border-radius: 16px; display: flex; align-items: center; justify-content: center; flex-shrink: 0; position: relative; }
.mail-status .big.on { color: var(--green); background: var(--green-soft); } .mail-status .big.off { color: var(--red); background: var(--red-soft); }
.mail-status .big.on::after { content: ""; position: absolute; inset: 0; border-radius: inherit; border: 2px solid var(--green); animation: jt-ring 2.8s ease-out infinite; }
.mail-status h2 { font-size: 20px; line-height: 27px; font-weight: 700; letter-spacing: -0.02em; }
.mail-facts { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 0; margin: 4px 0 0 !important; padding: 0 !important; border: 1px solid var(--border); border-radius: var(--r); background: var(--surface-2); overflow: hidden; }
.mail-facts > div { padding: 12px 16px; min-width: 0; } .mail-facts > div + div { border-left: 1px solid var(--border); }
.mail-facts dt { font-family: var(--label) !important; font-size: 10px; letter-spacing: .12em; text-transform: uppercase; color: var(--muted); margin: 0 0 3px !important; }
.mail-facts dd { margin: 0 !important; font-size: 14px; font-weight: 600; color: var(--text); overflow-wrap: anywhere; }
.timeline { list-style: none; margin: 4px 0 0 !important; padding: 0 !important; display: flex; flex-direction: column; gap: 0; }
.step { margin: 0 !important; }
.step { display: grid; grid-template-columns: 36px minmax(0, 1fr); gap: 14px; position: relative; padding-bottom: 16px; }
.step:last-child { padding-bottom: 0; }
.step::before { content: ""; position: absolute; left: 17px; top: 36px; bottom: 0; width: 2px; background: linear-gradient(180deg, color-mix(in srgb, var(--accent) 35%, transparent), var(--border)); }
.step:last-child::before { display: none; }
.step .node { width: 36px; height: 36px; border-radius: 12px; display: flex; align-items: center; justify-content: center; color: var(--accent-text); background: var(--accent-soft); border: 1px solid color-mix(in srgb, var(--accent) 22%, transparent); }
.step b { display: block; font-size: 14px; line-height: 20px; color: var(--text); font-weight: 700; margin-top: 1px; }
.step p { margin: 2px 0 0; font-size: 13.5px; line-height: 20px; color: var(--muted); }

/* ── settings ── */
[class*="st-key-set_"] { position: relative; }
.set-head { display: flex; gap: 14px; align-items: flex-start; }
.set-head .ic { width: 40px; height: 40px; border-radius: 12px; display: flex; align-items: center; justify-content: center; flex-shrink: 0; color: var(--accent-text); background: var(--accent-soft); }
.set-head .ic.red { color: var(--red); background: var(--red-soft); }
.st-key-set_maint { border-color: color-mix(in srgb, var(--red) 28%, var(--border)) !important; }
.st-key-set_maint::before { content: ""; position: absolute; inset: 0 0 auto; height: 3px; border-radius: var(--r-lg) var(--r-lg) 0 0; background: linear-gradient(90deg, var(--red), transparent 70%); opacity: .7; }

/* ── widgets ── */
.stTextInput label p, .stSelectbox label p, .stCheckbox label p { font-size: 13px !important; line-height: 18px !important; font-weight: 600 !important; color: var(--text-2) !important; }
.stTextInput [data-baseweb="input"], [data-testid="stTextInputRootElement"], [data-baseweb="select"] > div, .stSelectbox [role="group"] {
  background: var(--surface) !important; border: 1px solid var(--border-strong) !important; border-radius: var(--r) !important;
  min-height: 40px; transition: border-color var(--fast), box-shadow var(--normal) var(--ease), background var(--fast);
}
.stTextInput [data-baseweb="input"] *, [data-testid="stTextInputRootElement"] * { background-color: transparent !important; }
.stTextInput [data-baseweb="input"]:hover, [data-testid="stTextInputRootElement"]:hover, [data-baseweb="select"] > div:hover, .stSelectbox [role="group"]:hover { border-color: color-mix(in srgb, var(--accent) 35%, var(--border-strong)) !important; }
.stTextInput [data-baseweb="input"]:focus-within, [data-testid="stTextInputRootElement"]:focus-within, [data-baseweb="select"] > div:focus-within, .stSelectbox [role="group"]:focus-within {
  border-color: var(--accent) !important; box-shadow: var(--ring), 0 8px 24px -14px var(--accent) !important; background: var(--surface) !important;
}
.stTextInput input { font-size: 14px !important; color: var(--text) !important; -webkit-text-fill-color: var(--text); padding: 8px 12px !important; }
.stTextInput input::placeholder { color: var(--muted) !important; -webkit-text-fill-color: var(--muted); opacity: 1; }
/* search fields (every "Search…" text input: Home, Jobs, My jobs, Companies):
   a lavender→ice-blue glass — a top gloss band over the tint, a purple→blue
   gradient edge (the border-box layer under a 1.5px transparent border), an
   inner highlight and a soft shadow beneath; the magnifying glass sits in its
   own soft chip that fills with the accent gradient on focus. Hover deepens
   the tint and the edge; focus brings the accent ring and a gentle glow. The
   app font is set on the input AND its placeholder (Streamlit's input
   doesn't inherit it reliably). Only these inputs — other fields, dropdowns
   and buttons are unchanged. */
.stTextInput:has(input[aria-label^="Search"]) :is([data-baseweb="input"], [data-testid="stTextInputRootElement"]) {
  background: var(--search-gloss) padding-box, var(--search-tint) padding-box, var(--search-edge) border-box !important;
  border: 1.5px solid transparent !important; border-radius: 16px !important; min-height: 52px !important;
  box-shadow: var(--search-shadow) !important; transition: box-shadow var(--normal) var(--ease), background var(--fast) !important; }
.stTextInput:has(input[aria-label^="Search"]) :is([data-baseweb="input"], [data-testid="stTextInputRootElement"]):hover {
  background: var(--search-gloss) padding-box, var(--search-tint-hover) padding-box, var(--search-edge-hover) border-box !important;
  box-shadow: var(--search-shadow-hover) !important; }
.stTextInput:has(input[aria-label^="Search"]) :is([data-baseweb="input"], [data-testid="stTextInputRootElement"]):focus-within {
  background: var(--search-gloss) padding-box, var(--search-tint) padding-box, var(--search-edge-focus) border-box !important;
  box-shadow: var(--search-shadow), 0 0 0 4px color-mix(in srgb, var(--accent) 16%, transparent),
    0 14px 30px -14px color-mix(in srgb, var(--accent) 70%, transparent) !important; }
.stTextInput:has(input[aria-label^="Search"]) :is([data-baseweb="input"], [data-testid="stTextInputRootElement"])::before {
  content: "search"; font-family: 'Material Symbols Rounded' !important; font-feature-settings: 'liga'; font-size: 20px !important;
  width: 34px !important; height: 34px; line-height: 34px !important; text-align: center; border-radius: 11px; margin-left: 9px !important;
  align-self: center; flex-shrink: 0; overflow: hidden; white-space: nowrap; color: var(--search-icon) !important;
  background: var(--search-chip); box-shadow: inset 0 1px 0 rgba(255,255,255,.55), 0 1px 2px rgba(36,30,110,.08);
  transition: color var(--fast), background var(--fast), box-shadow var(--fast); }
.stTextInput:has(input[aria-label^="Search"]) :is([data-baseweb="input"], [data-testid="stTextInputRootElement"]):focus-within::before { color: #ffffff !important; background: var(--search-chip-focus);
  box-shadow: inset 0 1px 0 rgba(255,255,255,.35), 0 6px 14px -6px color-mix(in srgb, var(--accent) 80%, transparent); }
.stTextInput:has(input[aria-label^="Search"]) input { font-family: var(--font) !important; font-size: 15px !important; font-weight: 500 !important; line-height: 22px !important;
  letter-spacing: -.005em; padding: 0 14px 0 12px !important; color: var(--text) !important; -webkit-text-fill-color: var(--text);
  caret-color: var(--accent); }
.stTextInput:has(input[aria-label^="Search"]) input::placeholder { font-family: var(--font) !important; font-size: 15px !important; font-weight: 500 !important;
  letter-spacing: -.005em; color: var(--search-placeholder) !important; -webkit-text-fill-color: var(--search-placeholder); opacity: 1; }
.stTextInput:has(input[aria-label^="Search"]:disabled) :is([data-baseweb="input"], [data-testid="stTextInputRootElement"]) {
  background: var(--surface-2) !important; box-shadow: none !important; opacity: .7; cursor: not-allowed; }
.stSelectbox input, .stSelectbox [role="group"] * { font-size: 14px !important; color: var(--text) !important; -webkit-text-fill-color: var(--text); }
.stSelectbox svg { fill: var(--muted) !important; color: var(--muted) !important; }
[data-baseweb="select"] * { font-size: 14px !important; color: var(--text) !important; }
[data-baseweb="select"] svg { fill: var(--muted) !important; }
[data-baseweb="popover"] ul, [data-baseweb="popover"] [role="listbox"] { background: var(--raised) !important; border-radius: 12px !important; }
[data-baseweb="popover"] > div { border-radius: 12px !important; box-shadow: var(--shadow-lg) !important; border: 1px solid var(--border) !important; overflow: hidden; }
[data-baseweb="popover"] li { color: var(--text) !important; font-size: 14px !important; }
[data-baseweb="popover"] li:hover, [data-baseweb="popover"] li[aria-selected="true"] { background: var(--accent-soft) !important; }
[data-testid="InputInstructions"] { display: none !important; }
/* ── selection controls ──
   Streamlit 1.65 draws selectbox / multiselect as react-aria fields, and
   their menus as portals straight on the page body — outside .stApp, so none of the
   app's type or colour reached them (they fell back to Source Sans on a
   bare panel). Every rule below names those elements directly. */
body { font-family: var(--font); }
:is([data-testid="stSelectboxVirtualDropdown"], [data-testid="stMultiSelectDropdown"]),
:is([data-testid="stSelectboxVirtualDropdown"], [data-testid="stMultiSelectDropdown"]) *:not(.ms):not([data-testid="stIconMaterial"]) {
  font-family: var(--font) !important; word-spacing: .03em; }
.stMultiSelect label p { font-size: 13px !important; line-height: 18px !important; font-weight: 600 !important; color: var(--text-2) !important; }
:is(.stSelectbox, .stMultiSelect) div[role="group"] { min-height: 44px; border-radius: var(--r) !important; }
.stMultiSelect div[role="group"] {
  background: var(--surface) !important; border: 1px solid var(--border-strong) !important;
  transition: border-color var(--fast), box-shadow var(--normal) var(--ease), background var(--fast); height: auto !important; }
.stMultiSelect div[role="group"]:hover { border-color: color-mix(in srgb, var(--accent) 35%, var(--border-strong)) !important; }
.stMultiSelect div[role="group"]:focus-within { border-color: var(--accent) !important; box-shadow: var(--ring), 0 8px 24px -14px var(--accent) !important; }
:is(.stSelectbox, .stMultiSelect) input { font-family: var(--font) !important; font-size: 14px !important; color: var(--text) !important;
  -webkit-text-fill-color: var(--text); }
:is(.stSelectbox, .stMultiSelect) input::placeholder { color: var(--muted) !important; -webkit-text-fill-color: var(--muted); opacity: 1; font-weight: 400; }
/* the field's own buttons: clear and open */
:is(.stSelectbox, .stMultiSelect) :is(button[aria-label="Clear all"], button[aria-label="Open"]) { color: var(--muted) !important; border-radius: 8px;
  transition: color var(--fast), background var(--fast); }
:is(.stSelectbox, .stMultiSelect) :is(button[aria-label="Clear all"], button[aria-label="Open"]):hover { color: var(--accent-text) !important; background: var(--hover); }
:is(.stSelectbox, .stMultiSelect) :is(button[aria-label="Clear all"], button[aria-label="Open"]) svg { fill: currentColor !important; color: currentColor !important; }
/* chosen values: soft accent chips that wrap onto a new row (never scroll out of sight) */
[data-testid="stMultiSelectTagsContainer"] { flex-wrap: wrap !important; overflow: visible !important; gap: 6px !important; padding: 7px 0 7px 7px !important; }
[data-testid="stMultiSelectTagsContainer"] input { flex: 1 1 48px !important; min-width: 48px !important; width: auto !important; height: 28px !important; padding: 0 6px !important; }
/* with chips and no focus the (empty) text box steps out of the flow, so it never adds a blank row; focusing the field brings it back to type in */
.stMultiSelect div[role="group"]:not(:focus-within) [data-testid="stMultiSelectTagsContainer"]:has([aria-label="Selected values"] > span) { position: relative; }
.stMultiSelect div[role="group"]:not(:focus-within) [data-testid="stMultiSelectTagsContainer"]:has([aria-label="Selected values"] > span) input {
  position: absolute !important; right: 0; bottom: 7px; flex: none !important; width: 1px !important; min-width: 0 !important; padding: 0 !important; opacity: 0; }
.stMultiSelect [aria-label="Selected values"] > span { display: inline-flex !important; align-items: center; gap: 2px; height: 28px; max-width: 100%;
  padding: 0 3px 0 11px !important; margin: 0 !important; border-radius: 999px !important; background: var(--accent-soft) !important;
  border: 1px solid color-mix(in srgb, var(--accent) 24%, transparent); box-shadow: var(--badge-hi); }
.stMultiSelect [aria-label="Selected values"] > span > span { font-size: 13px !important; font-weight: 600 !important; color: var(--accent-text) !important;
  -webkit-text-fill-color: var(--accent-text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.stMultiSelect [aria-label="Selected values"] > span > button { width: 22px; height: 22px; margin: 0 !important; padding: 0 !important; border-radius: 50%;
  display: inline-flex; align-items: center; justify-content: center; color: var(--accent-text) !important; opacity: .7;
  transition: opacity var(--fast), background var(--fast); }
.stMultiSelect [aria-label="Selected values"] > span > button:hover { opacity: 1; background: color-mix(in srgb, var(--accent) 16%, transparent); }
.stMultiSelect [aria-label="Selected values"] > span > button svg { width: 9px; height: 9px; fill: currentColor !important; }
/* the menus */
:is([data-testid="stSelectboxVirtualDropdown"], [data-testid="stMultiSelectDropdown"]) { background: var(--raised) !important;
  border: 1px solid var(--border) !important; border-radius: var(--r) !important; box-shadow: var(--shadow-lg) !important; overflow: hidden; }
:is([data-testid="stSelectboxVirtualDropdown"], [data-testid="stMultiSelectDropdown"]) [role="listbox"] { background: transparent !important; }
:is([data-testid="stSelectboxVirtualDropdown"], [data-testid="stMultiSelectDropdown"]) [role="option"] { display: flex !important; align-items: center;
  margin: 0 6px; padding: 0 10px !important; border-radius: 8px !important; color: var(--text) !important; font-size: 14px !important; font-weight: 500;
  background: transparent !important; cursor: pointer; transition: background var(--fast), color var(--fast); }
:is([data-testid="stSelectboxVirtualDropdown"], [data-testid="stMultiSelectDropdown"]) [role="option"] * { color: inherit !important; font-size: inherit !important; background: transparent !important; box-shadow: none !important; }
:is([data-testid="stSelectboxVirtualDropdown"], [data-testid="stMultiSelectDropdown"]) [role="option"]:is(:hover, [data-focused]) { background: var(--hover) !important; }
:is([data-testid="stSelectboxVirtualDropdown"], [data-testid="stMultiSelectDropdown"]) [role="option"][aria-selected="true"] {
  color: var(--accent-text) !important; font-weight: 600; background: var(--accent-soft) !important; }
:is([data-testid="stSelectboxVirtualDropdown"], [data-testid="stMultiSelectDropdown"]) [role="option"][aria-selected="true"]::after {
  content: "check"; margin-left: auto; padding-left: 10px; font-family: 'Material Symbols Rounded' !important; font-feature-settings: 'liga';
  font-size: 18px; font-weight: 400; line-height: 1; color: var(--accent-text); }
/* loading: Streamlit's own placeholders, given a calm shimmer (they vanish when content arrives) */
[data-testid="stSkeleton"] { border-radius: 12px !important; background: linear-gradient(90deg, var(--hover) 0, color-mix(in srgb, var(--surface) 60%, var(--hover)) 40%, var(--hover) 80%) !important;
  background-size: 640px 100% !important; animation: jt-shimmer 1.4s linear infinite; }

/* ── sidebar: the product's spine ── */
section[data-testid="stSidebar"] {
  width: var(--sidebar-w) !important; min-width: var(--sidebar-w) !important; max-width: var(--sidebar-w) !important; transform: none !important;
  background: radial-gradient(260px 300px at 20% 0%, var(--glow-1), transparent 70%), radial-gradient(240px 260px at 100% 100%, var(--glow-2), transparent 70%), var(--surface) !important;
  border-right: 1px solid var(--border) !important;
}
section[data-testid="stSidebar"] > div, [data-testid="stSidebarContent"] { background: transparent !important; }
[data-testid="stSidebarContent"] { padding: 0 !important; }
[data-testid="stSidebarUserContent"] { padding: 22px 14px 16px !important; margin: 0 !important; width: 100% !important; min-height: 100vh; display: flex; flex-direction: column; }
[data-testid="stSidebarUserContent"] > div { flex: 1 1 auto; display: flex; flex-direction: column; }
[data-testid="stSidebarUserContent"] > div > [data-testid="stVerticalBlock"] { flex: 1 1 auto; }
[data-testid="stSidebarUserContent"] [data-testid="stVerticalBlock"] { gap: 3px !important; }
.brand { display: flex; align-items: center; gap: 12px; padding: 0 6px 22px; }
.brand .mark { position: relative; flex-shrink: 0; }
.brand img { width: 38px; height: 38px; border-radius: 12px; display: block; box-shadow: 0 8px 20px -8px var(--accent), 0 0 0 1px color-mix(in srgb, var(--accent) 30%, transparent); }
.brand .mark::after { content: ""; position: absolute; right: -2px; bottom: -2px; width: 10px; height: 10px; border-radius: 50%; background: var(--green); border: 2px solid var(--surface); }
.brand .mark.failing::after { background: var(--red); } .brand .mark.delayed::after { background: var(--amber); } .brand .mark.pending::after, .brand .mark.checking::after { background: var(--gray); }
.brand > div:last-child { min-width: 0; }
.brand .n { font-size: 15px; line-height: 20px; font-weight: 800; letter-spacing: -0.025em; color: var(--text); white-space: nowrap; }
.brand .s { font-family: var(--label) !important; font-size: 9.5px; line-height: 13px; font-weight: 600; letter-spacing: .1em; text-transform: uppercase; color: var(--muted); margin-top: 3px; white-space: nowrap; }
.nav-group { font-family: var(--label) !important; font-size: 10.5px; line-height: 16px; font-weight: 600; letter-spacing: .14em; text-transform: uppercase; color: var(--muted); padding: 18px 12px 8px; }
.nav-group.first { padding-top: 2px; }
[class*="st-key-nav_"] [data-testid^="stBaseButton"] {
  position: relative; width: 100% !important; justify-content: flex-start !important; min-height: 42px !important; padding: 0 12px !important;
  border-radius: 11px !important; color: var(--text-2) !important; background: transparent !important; border: 1px solid transparent !important; gap: 10px;
  transition: background var(--normal) var(--ease), border-color var(--normal) var(--ease), box-shadow var(--normal) var(--ease), color var(--fast) !important;
}
[class*="st-key-nav_"] [data-testid^="stBaseButton"] > div { justify-content: flex-start !important; gap: 12px !important; }
[class*="st-key-nav_"] [data-testid^="stBaseButton"]:hover { background: var(--hover) !important; color: var(--text) !important; transform: none; }
[class*="st-key-nav_"] [data-testid^="stBaseButton"]:hover [data-testid="stIconMaterial"] { transform: translateX(2px); color: var(--accent-text); }
[class*="st-key-nav_"] [data-testid="stIconMaterial"] { font-size: 21px !important; color: var(--muted); transition: transform var(--normal) var(--ease), color var(--fast); }
[class*="st-key-nav_"] p { font-size: 14px !important; font-weight: 600 !important; }
/* the ✕ / ↺ buttons on job rows are icon-only; their label is for screen readers */
[class*="st-key-dismiss_"] [data-testid="stMarkdownContainer"], [class*="st-key-restore_"] [data-testid="stMarkdownContainer"],
[class*="st-key-dismiss_"] [data-testid="stMarkdownContainer"] p, [class*="st-key-restore_"] [data-testid="stMarkdownContainer"] p { position: absolute !important; width: 1px !important; height: 1px !important; overflow: hidden !important; clip: rect(0 0 0 0) !important; clip-path: inset(50%) !important; white-space: nowrap !important; margin: -1px !important; padding: 0 !important; border: 0 !important; }


/* ── how-it-works strip (Home) ── */
.how { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; }
.how > div { display: flex; gap: 12px; align-items: flex-start; padding: 16px; border-radius: var(--r-lg); border: 1px dashed var(--border-strong); background: color-mix(in srgb, var(--surface) 55%, transparent); }
.how .ic { width: 34px; height: 34px; border-radius: 10px; display: flex; align-items: center; justify-content: center; flex-shrink: 0; color: var(--accent-text); background: var(--accent-soft); }
.how b { display: block; font-size: 14px; line-height: 20px; color: var(--text); }
.how p { margin: 2px 0 0; font-size: 13px; line-height: 19px; color: var(--muted); }
/* every icon tile shares one finish: lit top edge + hairline border */
.how .ic, .set-head .ic, .step .node, .empty .ic, .add-steps .ms { box-shadow: var(--badge-hi); }
.how .ic, .set-head .ic:not(.red) { border: 1px solid color-mix(in srgb, var(--accent) 18%, transparent);
  background: linear-gradient(180deg, color-mix(in srgb, var(--accent-soft) 60%, var(--surface)), var(--accent-soft)); }
.how > div { transition: border-color var(--normal) var(--ease), background var(--normal) var(--ease); }

/* ── footer ── */
.app-foot { font-size: 13px; color: var(--muted); display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; padding-top: 8px; }
.app-foot a { color: var(--muted) !important; } .app-foot a:hover { color: var(--accent-text) !important; }

/* ── empty states ── */
.st-key-empty { background: radial-gradient(360px 180px at 50% 0%, var(--glow-1), transparent 70%), var(--surface) !important; }
.empty { display: flex; flex-direction: column; align-items: center; text-align: center; gap: 8px; padding: 26px 8px 18px; }
.empty .ic { width: 56px; height: 56px; border-radius: 18px; background: var(--accent-soft); color: var(--accent-text); display: flex; align-items: center; justify-content: center; margin-bottom: 8px;
  box-shadow: 0 0 0 8px color-mix(in srgb, var(--accent) 7%, transparent), 0 0 0 16px color-mix(in srgb, var(--accent) 4%, transparent); }
.empty h3 { font-size: 17px; line-height: 24px; font-weight: 700; }
.empty p { font-size: 14px; line-height: 21px; color: var(--muted); max-width: 460px; margin: 0; }
.st-key-empty_actions > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"], .st-key-empty_actions > [data-testid="stHorizontalBlock"] { justify-content: center !important; gap: 8px !important; }
.st-key-empty_actions [data-testid="stColumn"] { flex: 0 0 auto !important; width: auto !important; min-width: 0 !important; }
.st-key-empty_actions { align-items: center !important; }
.st-key-empty_actions > [data-testid="stElementContainer"] { width: auto !important; }

/* ── toast ── */
.jt-toast { position: fixed; bottom: 24px; right: 24px; z-index: 9999; display: flex; align-items: center; gap: 12px; padding: 12px 16px 12px 14px; border-radius: 14px;
  background: var(--raised); border: 1px solid var(--border); box-shadow: var(--shadow-lg); max-width: 400px; overflow: hidden; }
.jt-toast::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 3px; background: var(--tone); }
.jt-toast .ic { display: flex; color: var(--tone); }
.jt-toast .msg { font-size: 14px; line-height: 20px; color: var(--text); font-weight: 500; }

/* ── responsive ── */
@media (max-width: 1279px) {
  [data-testid="stMainBlockContainer"], .block-container { padding: 30px 30px 44px !important; }
  .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  /* company page: the job list gets the full width, details follow */
  .st-key-co_cols > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"], .st-key-co_cols > [data-testid="stHorizontalBlock"] { flex-wrap: wrap !important; }
  .st-key-co_cols [data-testid="stColumn"]:nth-child(n) { flex: 1 1 100% !important; width: 100% !important; }
}
@media (max-width: 1100px) {
  .hero { grid-template-columns: minmax(0, 1fr) minmax(0, 34%); gap: 16px; }
  .opps-wrap { min-height: 240px; } .feed { left: 0; right: 4%; padding: 12px; gap: 6px; } .feed-row { gap: 10px; padding: 8px 10px; }
  .feed-row .ic { width: 32px; height: 32px; } .feed-row.r3 { display: none; } .feed-seal { padding: 8px; right: -20px; bottom: -24px; } .feed-seal .ic { width: 34px; height: 34px; }
  .hero .page-title { font-size: 34px; line-height: 41px; }
}
/* tablet: icon rail */
@media (max-width: 1023px) {
  :root { --sidebar-w: 76px; }
  .brand { justify-content: center; padding: 0 0 16px; } .brand > div:not(.mark) { display: none; }
  .nav-group { font-size: 0; padding: 10px 0 4px; border-top: 1px solid var(--border); margin: 8px 10px 0; }
  .nav-group.first { display: none; }
  [class*="st-key-nav_"] [data-testid^="stBaseButton"] { justify-content: center !important; padding: 0 !important; min-height: 46px !important; }
  [class*="st-key-nav_"] [data-testid^="stBaseButton"] > div { justify-content: center !important; }
  /* icon rail: labels are hidden visually but kept for screen readers */
  [class*="st-key-nav_"] p { position: absolute !important; width: 1px !important; height: 1px !important; overflow: hidden !important; clip: rect(0 0 0 0) !important; clip-path: inset(50%) !important; white-space: nowrap !important; margin: -1px !important; padding: 0 !important; border: 0 !important; }
  [data-testid="stSidebarUserContent"] { padding: 18px 10px !important; }
  .st-key-job_cols > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"], .st-key-job_cols > [data-testid="stHorizontalBlock"] { flex-wrap: wrap !important; }
  .st-key-job_cols [data-testid="stColumn"]:nth-child(n) { flex: 1 1 100% !important; width: 100% !important; }
  .how { grid-template-columns: minmax(0, 1fr); }
  .mail-facts { grid-template-columns: minmax(0, 1fr); } .mail-facts > div + div { border-left: none; border-top: 1px solid var(--border); }
}
/* phone: sidebar hidden; the menu button opens a drawer instead */
@media (max-width: 767px) {
  section[data-testid="stSidebar"] { display: none !important; }
  [data-testid="stMainBlockContainer"], .block-container { padding: 12px 16px 36px !important; }
  [data-testid="stMainBlockContainer"] > div > [data-testid="stVerticalBlock"] { gap: 18px; }
  .page-title, .detail-title { font-size: 24px; line-height: 31px; }
  .st-key-hero { padding: 22px 18px 20px !important; }
  /* phones: not a shrunk desktop — a compact strip (the search being typed
     and the check tile) above the eyebrow, so nothing sits over the heading */
  .hero { grid-template-columns: minmax(0, 1fr); gap: 0; }
  .opps-wrap { order: -1; min-height: 0; height: 58px; margin-bottom: 16px; }
  .opps > .glow-bg { inset: -22px -18px -20px -18px; }
  .feed { left: 0; right: 64px; top: 0; translate: none; padding: 0; border: none; background: none; box-shadow: none;
    backdrop-filter: none; -webkit-backdrop-filter: none; }
  .feed-row { display: none; }
  .feed-q { height: 46px; margin: 0; border-radius: 14px; background: var(--oc-bg); border-color: var(--oc-border); box-shadow: var(--hi), var(--oc-shadow); }
  .feed-seal { right: -64px; top: 2px; bottom: auto; padding: 8px; border-radius: 13px; }
  .feed-seal .ic { width: 34px; height: 34px; }
  .hero .page-title { font-size: 27px; line-height: 33px; }
  .hero-sub { font-size: 14.5px; line-height: 22px; }
  .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
  .stat { padding: 14px 14px 13px; } .stat .ic { width: 32px; height: 32px; margin-bottom: 10px; } .stat .v { font-size: 24px; line-height: 30px; } .stat .v.sm { font-size: 17px; line-height: 24px; }
  .stat .v, .stat .n { white-space: normal; }   /* wrap instead of cutting off key facts */
  [class*="st-key-jr_"] { padding: 16px !important; }
  .detail-head { gap: 14px; } .logo.lg { width: 52px; height: 52px; font-size: 21px; border-radius: 15px; }
  .st-key-job_hero, .st-key-co_hero { padding: 20px 18px !important; }
  .st-key-company_list { grid-template-columns: minmax(0, 1fr); }
  .mon tbody { grid-template-columns: minmax(0, 1fr); }
  .mon tbody tr { grid-template-columns: repeat(2, minmax(0, 1fr)); padding: 16px; }
  .mon td.c { grid-column: 1 / -1; } .mon td[data-l="Status"] { grid-column: 1 / -1; justify-self: start; grid-row: 2; }
  .mon td[data-l="Scraper"] { grid-column: 1 / -1; }
  .kv { grid-template-columns: minmax(0, 1fr); gap: 2px; } .kv dd { margin-bottom: 10px !important; }
  .checks { grid-template-columns: minmax(0, 1fr); }
  .add-steps { grid-template-columns: minmax(0, 1fr); }
  .st-key-add_form, .st-key-email_form, .st-key-test_panel, [class*="st-key-set_"], .st-key-job_main, .st-key-job_side, .st-key-co_side, .st-key-empty, .st-key-mail_status { padding: 18px 16px !important; }
  /* Streamlit Community Cloud draws its own floating badges at the bottom
     right of the browser window, outside this app's frame (the app can't
     move or hide them): toasts show at the top instead, and the page ends
     with room to scroll its last buttons clear of the badges */
  .jt-toast { left: 16px; right: 16px; top: 74px; bottom: auto; max-width: none; }
  [data-testid="stMainBlockContainer"], .block-container { padding-bottom: 104px !important; }
  /* the hero's three status chips: two share a line instead of one each,
     so Refresh / Run check sit higher on the first screen */
  .hero-status { gap: 6px; margin-top: 14px; }
  .hero-status .chip { font-size: 12.5px; padding: 4px 9px; gap: 5px; }
  .hero-status .chip .ms { font-size: 15px; }
}
@media (max-width: 360px) {
  .stat .v { font-size: 21px; line-height: 27px; }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation-duration: .001ms !important; animation-iteration-count: 1 !important; transition-duration: .001ms !important; scroll-behavior: auto !important; }
  /* the feed rests in its finished state: query typed, every role checked, seal in place */
  .feed-q .qbar i, .feed-row, .feed-row .ok, .feed-seal, .opps > .glow-bg { animation: none !important; opacity: 1 !important; transform: none !important; }
  .btn.primary::after, [data-testid="stBaseButton-primary"]::after { display: none; }
  .st-key-onboard { animation: none !important; }
}

/* ── account menu, top right ── */
.st-key-acct_bar { flex-direction: row !important; justify-content: flex-end; margin-bottom: -8px; position: relative; z-index: 6; }
.st-key-acct_bar > div { width: auto !important; flex: 0 0 auto !important; }
/* the menu button (laptops and desktops) opens / closes the left navigation:
   at the left of the top bar, an icon; its label stays for screen readers */
.st-key-acct_bar { align-items: center; }
.st-key-acct_bar > :first-child { margin-right: auto !important; }    /* the phone menu-button fragment */
.st-key-btn_nav_toggle [data-testid^="stBaseButton"] { width: 36px !important; height: 36px !important; min-height: 36px !important; padding: 0 !important;
  border-radius: 10px !important; justify-content: center !important; }
.st-key-side_toggle { align-items: center; }
.st-key-btn_nav_toggle [data-testid^="stBaseButton"] > div { justify-content: center !important; gap: 0 !important; }
.st-key-btn_nav_toggle [data-testid="stIconMaterial"] { font-size: 21px !important; margin: 0 !important; color: var(--text-2); }
.st-key-btn_nav_toggle [data-testid="stMarkdownContainer"], .st-key-btn_nav_toggle [data-testid="stMarkdownContainer"] p {
  position: absolute !important; width: 1px !important; height: 1px !important; overflow: hidden !important; clip: rect(0 0 0 0) !important;
  clip-path: inset(50%) !important; white-space: nowrap !important; margin: -1px !important; padding: 0 !important; border: 0 !important; }
/* phones: "Open menu" instead of the sidebar button, and the drawer it opens */
.st-key-btn_drawer_open, .st-key-drawer, .st-key-btn_drawer_backdrop { display: none !important; }
@keyframes jt-drawer-in { from { transform: translateX(-100%); } to { transform: none; } }
@keyframes jt-scrim-in { from { opacity: 0; } to { opacity: 1; } }
@media (max-width: 767px) {
  .st-key-btn_nav_toggle { display: none !important; }
  .st-key-btn_drawer_open { display: block !important; }
  .st-key-btn_drawer_open [data-testid^="stBaseButton"] { width: 46px !important; height: 46px !important; min-height: 46px !important;
    padding: 0 !important; border-radius: 13px !important; justify-content: center !important; }
  .st-key-btn_drawer_open [data-testid^="stBaseButton"] > div { justify-content: center !important; gap: 0 !important; }
  .st-key-btn_drawer_open [data-testid="stIconMaterial"] { font-size: 24px !important; margin: 0 !important; color: var(--text-2); }
  /* the dimmed page behind the drawer: one big "Close menu" button */
  .st-key-btn_drawer_backdrop { display: block !important; position: fixed !important; inset: 0; z-index: 1000002; margin: 0 !important; }
  .st-key-btn_drawer_backdrop [data-testid^="stBaseButton"] { width: 100vw !important; height: 100vh !important; height: 100dvh !important;
    border: none !important; border-radius: 0 !important; box-shadow: none !important; cursor: default;
    background: rgba(13, 16, 36, .42) !important; animation: jt-scrim-in 160ms ease-out; }
  /* the drawer: slides in from the left, scrolls on its own if needed */
  .st-key-drawer { display: flex !important; position: fixed !important; top: 0; left: 0; bottom: 0; z-index: 1000003;
    width: min(304px, 84vw) !important; padding: 16px 12px calc(20px + env(safe-area-inset-bottom)) !important; gap: 2px !important;
    overflow-y: auto; overscroll-behavior: contain; background: var(--surface); border-right: 1px solid var(--border);
    box-shadow: 18px 0 44px -18px rgba(13, 16, 36, .45); animation: jt-drawer-in 200ms cubic-bezier(.2, .7, .2, 1); }
  .st-key-drawer_head { flex-direction: row !important; align-items: center; justify-content: space-between; padding: 0 2px 10px; }
  .st-key-drawer_head > * { width: auto !important; flex: 0 0 auto !important; }
  /* the tablet icon rail hides the brand name and group labels: the drawer shows them */
  .st-key-drawer .brand { padding: 0; justify-content: flex-start; }
  .st-key-drawer .brand > div:not(.mark) { display: block !important; }
  .st-key-drawer .nav-group, .st-key-drawer .nav-group.first { display: block !important; font-size: 11px !important;
    margin: 14px 8px 4px !important; padding: 0 !important; border: none !important; }
  .st-key-drawer [class*="st-key-drw_"] [data-testid^="stBaseButton"] { justify-content: flex-start !important; min-height: 48px !important;
    padding: 0 14px !important; border-radius: 12px !important; color: var(--text) !important; }
  .st-key-drawer [class*="st-key-drw_"] [data-testid^="stBaseButton"] > div { justify-content: flex-start !important; gap: 12px !important; }
  .st-key-drawer [class*="st-key-drw_"] [data-testid="stIconMaterial"] { font-size: 21px !important; color: var(--muted); }
  .st-key-drawer [class*="st-key-drw_"] p { font-size: 15px !important; font-weight: 600 !important; }
  :is(.st-key-btn_drawer_close, .st-key-btn_drawer_backdrop) [data-testid="stMarkdownContainer"],
  :is(.st-key-btn_drawer_close, .st-key-btn_drawer_backdrop) [data-testid="stMarkdownContainer"] p {
    position: absolute !important; width: 1px !important; height: 1px !important; overflow: hidden !important; clip: rect(0 0 0 0) !important;
    clip-path: inset(50%) !important; white-space: nowrap !important; margin: -1px !important; padding: 0 !important; border: 0 !important; }
  .st-key-btn_drawer_close [data-testid^="stBaseButton"] { width: 44px !important; height: 44px !important; min-height: 44px !important;
    padding: 0 !important; border-radius: 12px !important; justify-content: center !important; }
  .st-key-btn_drawer_close [data-testid^="stBaseButton"] > div { justify-content: center !important; gap: 0 !important; }
}
.st-key-btn_drawer_open [data-testid="stMarkdownContainer"], .st-key-btn_drawer_open [data-testid="stMarkdownContainer"] p {
  position: absolute !important; width: 1px !important; height: 1px !important; overflow: hidden !important; clip: rect(0 0 0 0) !important;
  clip-path: inset(50%) !important; white-space: nowrap !important; margin: -1px !important; padding: 0 !important; border: 0 !important; }
/* the compact control: avatar · first name · ADMIN · chevron — one glossy pill.
   The avatar is its own circle: a hairline-edged tile, a gap in the pill's
   colour and a thin accent ring, clear of the pill's edge on every side. */
[class*="st-key-acct_menu"] [data-testid="stPopoverButton"] { min-height: 50px !important; padding: 7px 14px 7px 9px !important; border-radius: 999px !important;
  gap: 12px; border: 1px solid var(--border) !important;
  background: linear-gradient(180deg, var(--pill-gloss), transparent 55%), var(--btn2-grad) !important;
  box-shadow: var(--hi), var(--shadow), 0 10px 24px -18px var(--accent) !important;
  transition: border-color var(--fast) var(--ease), box-shadow var(--fast) var(--ease), transform var(--fast) var(--ease); }
[class*="st-key-acct_menu"] [data-testid="stPopoverButton"]:hover { border-color: color-mix(in srgb, var(--accent) 40%, var(--border)) !important;
  box-shadow: var(--hi), var(--btn2-shadow-hover), 0 16px 30px -18px var(--accent) !important; transform: translateY(-1px); }
[class*="st-key-acct_menu"] [data-testid="stPopoverButton"]:focus-visible { outline: 2px solid var(--accent) !important; outline-offset: 3px !important; }
[class*="st-key-acct_menu"] [data-testid="stPopoverButton"][aria-expanded="true"] { border-color: color-mix(in srgb, var(--accent) 45%, var(--border)) !important; }
[class*="st-key-acct_menu"] [data-testid="stPopoverButton"]::before { width: 34px; height: 34px; border-radius: 50%; display: grid; place-items: center;
  flex-shrink: 0; box-sizing: border-box; font-family: var(--font); font-weight: 800; font-size: 14.5px; line-height: 1; color: #fff;
  background: linear-gradient(135deg, var(--accent), var(--accent-2));
  box-shadow: var(--av-gloss), 0 0 0 2px var(--surface), 0 0 0 3.5px var(--av-ring), var(--av-drop); }
/* the label: name, then the ADMIN badge, then a separate chevron that turns when open */
[class*="st-key-acct_menu"] [data-testid="stPopoverButton"] > div { gap: 8px !important; align-items: center !important; }
[class*="st-key-acct_menu"] [data-testid="stPopoverButton"] div[aria-hidden="true"] { margin-left: 2px; display: flex; align-items: center; }
[class*="st-key-acct_menu"] [data-testid="stPopoverButton"] div[aria-hidden="true"] [data-testid="stIconMaterial"] { transition: transform var(--normal) var(--ease), color var(--fast); }
[class*="st-key-acct_menu"] [data-testid="stPopoverButton"][aria-expanded="true"] div[aria-hidden="true"] [data-testid="stIconMaterial"] { transform: rotate(180deg); color: var(--accent-text); }
[class*="st-key-acct_menu"] [data-testid="stPopoverButton"] p { font-family: var(--font) !important; font-size: 15px !important; font-weight: 700 !important;
  letter-spacing: -.01em; color: var(--text) !important; white-space: nowrap; display: flex; align-items: center; }
[class*="st-key-acct_menu"] [data-testid="stPopoverButton"] [data-testid="stIconMaterial"] { color: var(--muted); }
[class*="st-key-acct_menu"] strong, .acct-head .role { margin-left: 8px; padding: 3px 8px; border-radius: 999px; font-family: var(--font) !important;
  font-size: 10px; line-height: 13px; font-weight: 800 !important; letter-spacing: .1em; text-transform: uppercase; color: var(--accent-text);
  background: linear-gradient(135deg, var(--accent-soft), color-mix(in srgb, var(--accent-2) 12%, var(--accent-soft)));
  border: 1px solid color-mix(in srgb, var(--accent) 24%, transparent); box-shadow: var(--badge-hi); }
/* the menu itself: a portal outside .stApp, so it needs the app font set here */
[data-testid="stPopoverBody"], [data-testid="stPopoverBody"] *:not([data-testid="stIconMaterial"]):not(.ms) { font-family: var(--font) !important; }
[data-testid="stPopoverBody"]:has(.acct-head) { padding: 8px !important; min-width: 280px; border-radius: 18px !important;
  background: linear-gradient(180deg, color-mix(in srgb, var(--surface) 94%, transparent), color-mix(in srgb, var(--surface-2) 94%, transparent)) !important;
  -webkit-backdrop-filter: blur(18px) saturate(1.4); backdrop-filter: blur(18px) saturate(1.4);
  border: 1px solid var(--border) !important; box-shadow: var(--hi), var(--shadow-lg) !important; }
[data-testid="stPopoverBody"]:has(.acct-head) [data-testid="stVerticalBlock"] { gap: 2px !important; }
.acct-head { display: flex; align-items: center; gap: 11px; padding: 8px 8px 12px; margin-bottom: 6px; min-width: 250px;
  border-bottom: 1px solid var(--border); }
.acct-av { width: 40px; height: 40px; border-radius: 50%; display: grid; place-items: center; flex-shrink: 0; font-size: 16px; font-weight: 800;
  color: #fff; background: linear-gradient(135deg, var(--accent), var(--accent-2)); margin: 4px;
  box-shadow: var(--av-gloss), 0 0 0 2px var(--surface), 0 0 0 3.5px var(--av-ring), var(--av-drop); }
/* the person's picked avatar (a brand mark on its tile) and the Account page size */
.acct-av.av-mark { box-shadow: var(--av-gloss), inset 0 0 0 1px rgba(15,23,42,.10), 0 0 0 2px var(--surface), 0 0 0 3.5px var(--av-ring), var(--av-drop); }
.acct-av.av-mark img { width: 60%; height: 60%; margin: 0; padding: 0; border: 0; border-radius: 0; box-shadow: none;
  object-fit: contain; display: block; pointer-events: none; user-select: none; }
.acct-av-lg { width: 52px; height: 52px; font-size: 20px; }
.acct-head .who { min-width: 0; flex: 1; }
.acct-head .nm { font-size: 14.5px; line-height: 20px; font-weight: 700; letter-spacing: -.01em; color: var(--text); }
.acct-head .em { font-size: 12.5px; line-height: 18px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.acct-head .role { margin-left: 0; align-self: flex-start; margin-top: 2px; }
.acct-sep { height: 1px; background: var(--border); margin: 5px 6px; }
/* the menu's items: compact left-aligned rows; a soft glossy highlight follows the pointer */
:is(.st-key-acct_settings, .st-key-acct_preferences, .st-key-acct_companies, .st-key-acct_alerts, .st-key-btn_account_sign_out, .st-key-btn_account_delete)
  [data-testid^="stBaseButton"] { justify-content: flex-start !important; min-height: 38px !important; padding: 0 10px !important;
  border-radius: 11px !important; border: 1px solid transparent !important; color: var(--text-2) !important;
  transition: background-color var(--fast) var(--ease), color var(--fast) var(--ease), border-color var(--fast) var(--ease),
              box-shadow var(--fast) var(--ease), transform var(--fast) var(--ease); }
:is(.st-key-acct_settings, .st-key-acct_preferences, .st-key-acct_companies, .st-key-acct_alerts, .st-key-btn_account_sign_out, .st-key-btn_account_delete)
  [data-testid^="stBaseButton"] > div { justify-content: flex-start !important; gap: 11px !important; }
:is(.st-key-acct_settings, .st-key-acct_preferences, .st-key-acct_companies, .st-key-acct_alerts, .st-key-btn_account_sign_out, .st-key-btn_account_delete)
  [data-testid^="stBaseButton"] p { font-size: 14px !important; font-weight: 600 !important; color: inherit !important; }
:is(.st-key-acct_settings, .st-key-acct_preferences, .st-key-acct_companies, .st-key-acct_alerts, .st-key-btn_account_sign_out, .st-key-btn_account_delete)
  [data-testid="stIconMaterial"] { color: var(--muted) !important; font-size: 19px !important; transition: color var(--fast) var(--ease); }
:is(.st-key-acct_settings, .st-key-acct_preferences, .st-key-acct_companies, .st-key-acct_alerts)
  [data-testid^="stBaseButton"]:hover { background: linear-gradient(90deg, var(--accent-soft), color-mix(in srgb, var(--accent-soft) 40%, transparent)) !important;
  color: var(--accent-text) !important; border-color: color-mix(in srgb, var(--accent) 16%, transparent) !important;
  box-shadow: var(--badge-hi) !important; transform: translateX(2px); }
:is(.st-key-acct_settings, .st-key-acct_preferences, .st-key-acct_companies, .st-key-acct_alerts)
  [data-testid^="stBaseButton"]:hover [data-testid="stIconMaterial"] { color: var(--accent-text) !important; }
.st-key-btn_account_sign_out [data-testid^="stBaseButton"]:hover { background: var(--red-soft) !important; color: var(--red) !important;
  border-color: color-mix(in srgb, var(--red) 16%, transparent) !important; transform: translateX(2px); }
.st-key-btn_account_sign_out [data-testid^="stBaseButton"]:hover [data-testid="stIconMaterial"] { color: var(--red) !important; }
/* Delete account: destructive at rest too */
.st-key-btn_account_delete [data-testid^="stBaseButton"], .st-key-btn_account_delete [data-testid="stIconMaterial"] { color: var(--red) !important; }
.st-key-btn_account_delete [data-testid^="stBaseButton"]:hover { background: var(--red-soft) !important;
  border-color: color-mix(in srgb, var(--red) 16%, transparent) !important; transform: translateX(2px); }
.del-info p { margin: 0 0 6px; font-size: 14px; line-height: 1.55; color: var(--text); }
.del-info ul { margin: 0 0 8px; padding-left: 18px !important; font-size: 14px; line-height: 1.6; color: var(--text-2); }
@media (max-width: 767px) {
  .st-key-acct_bar { margin: 8px 0 -8px; }
  [class*="st-key-acct_menu"] [data-testid="stPopoverButton"] { min-height: 44px !important; }
  [class*="st-key-acct_menu"] [data-testid="stPopoverButton"] p { max-width: 170px; overflow: hidden; text-overflow: ellipsis; }
}
@media (prefers-reduced-motion: reduce) {
  :is(.st-key-acct_settings, .st-key-acct_preferences, .st-key-acct_companies, .st-key-acct_alerts, .st-key-btn_account_sign_out, .st-key-btn_account_delete)
    [data-testid^="stBaseButton"]:hover { transform: none; }
}

/* ── email tailoring card ── */
.tl-opt { margin-left: 4px; padding: 1px 7px; border-radius: 999px; font-size: 9.5px; letter-spacing: .1em; color: var(--accent-text);
  background: var(--accent-soft); border: 1px solid color-mix(in srgb, var(--accent) 20%, transparent); }
.tl-status { display: flex; gap: 12px; align-items: flex-start; padding: 13px 15px; border-radius: 14px; border: 1px solid var(--border);
  background: linear-gradient(135deg, var(--surface-2), color-mix(in srgb, var(--surface-2) 55%, transparent)); box-shadow: var(--hi); }
.tl-status > .ms { color: var(--muted); margin-top: 1px; }
.tl-status b { display: block; font-size: 14.5px; line-height: 20px; font-weight: 700; color: var(--text); }
.tl-status div > span { display: block; font-size: 13px; line-height: 19px; color: var(--muted); margin-top: 2px; }
.tl-status.on { border-color: color-mix(in srgb, var(--accent) 30%, var(--border));
  background: linear-gradient(135deg, var(--accent-soft), color-mix(in srgb, var(--accent-2) 9%, var(--surface))); }
.tl-status.on > .ms { color: var(--accent-text); }
.tl-q { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; margin-top: 6px; }
.tl-q .ms { color: var(--accent-text); }
.tl-q b { font-size: 14.5px; font-weight: 700; color: var(--text); }
.tl-q span:not(.ms) { font-size: 12.5px; color: var(--muted); }
:is([class*="st-key-tl_pick_"], [class*="st-key-pref_"], [class*="st-key-ob_"]) button[data-variant="pills"] { min-height: 36px; padding: 0 14px !important; border-radius: 999px !important;
  background: var(--btn2-grad) !important; border: 1px solid var(--border-strong) !important; color: var(--text-2) !important;
  box-shadow: var(--hi), var(--btn2-shadow) !important;
  transition: border-color var(--fast) var(--ease), box-shadow var(--fast) var(--ease), transform var(--fast) var(--ease), color var(--fast) var(--ease); }
:is([class*="st-key-tl_pick_"], [class*="st-key-pref_"], [class*="st-key-ob_"]) button[data-variant="pills"]:hover { border-color: color-mix(in srgb, var(--accent) 40%, var(--border-strong)) !important;
  color: var(--accent-text) !important; box-shadow: var(--hi), var(--btn2-shadow-hover) !important; transform: translateY(-1px); }
:is([class*="st-key-tl_pick_"], [class*="st-key-pref_"], [class*="st-key-ob_"]) button[data-variant="pills"]:is([aria-checked="true"], [aria-pressed="true"], [data-selected="true"]) { color: var(--accent-text) !important;
  background: linear-gradient(180deg, var(--accent-soft), color-mix(in srgb, var(--accent) 16%, var(--accent-soft))) !important;
  border-color: color-mix(in srgb, var(--accent) 45%, transparent) !important; box-shadow: var(--badge-hi), 0 6px 14px -10px var(--accent) !important; }
:is([class*="st-key-tl_pick_"], [class*="st-key-pref_"], [class*="st-key-ob_"]) button[data-variant="pills"] p { color: inherit !important; font-size: 13.5px !important; font-weight: 600 !important; }
.st-key-tl_actions { align-items: center; gap: 10px; flex-wrap: wrap; margin-top: 4px; }
.tl-dirty { display: inline-flex; align-items: center; gap: 6px; font-size: 12.5px; font-weight: 700; color: var(--amber); }
.tl-dirty::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: var(--amber); }
.tl-note { display: flex; gap: 8px; align-items: flex-start; font-size: 13px; line-height: 1.55; }
.tl-note .ms { flex-shrink: 0; margin-top: 2px; }

/* ── member home ── */
.st-key-mh_hero { position: relative; overflow: hidden; isolation: isolate; padding: 28px 30px !important; border-radius: var(--r-xl) !important;
  border: 1px solid var(--border) !important; box-shadow: var(--hi), var(--shadow) !important;
  background: radial-gradient(520px 220px at 92% -20%, var(--glow-1), transparent 70%), var(--surface) !important; }
.mh-hero .page-title { font-size: 34px; line-height: 1.15; font-weight: 800; letter-spacing: -.028em; margin-top: 10px; }
.mh-hero .page-title em { font-style: normal; background: linear-gradient(92deg, var(--accent), var(--accent-2));
  -webkit-background-clip: text; background-clip: text; color: transparent; }
/* the three summary cards: one row of equal-height cards. The keyed card sits
   in a stLayoutWrapper inside each column, so height: 100% on the card alone
   resolved against an auto-height parent and every card took its own content
   height. Each level now stretches to the row's height, and the button is
   pushed to the bottom so the three line up whatever the content. */
:is(.st-key-mh_cards, .st-key-mh_cards > [data-testid="stLayoutWrapper"]) > [data-testid="stHorizontalBlock"] { align-items: stretch !important; }
.st-key-mh_cards [data-testid="stColumn"] { display: flex !important; flex-direction: column !important; }
.st-key-mh_cards [data-testid="stColumn"] > [data-testid="stVerticalBlock"] { flex: 1 1 auto !important; }
.st-key-mh_cards [data-testid="stLayoutWrapper"]:has(> [class*="st-key-set_mh_"]) { flex: 1 1 auto !important; display: flex !important; flex-direction: column !important; }
[class*="st-key-set_mh_"] { flex: 1 1 auto !important; justify-content: flex-start; }
[class*="st-key-set_mh_"] > [data-testid="stElementContainer"]:last-child { margin-top: auto; }
/* the job list's section heading, with this member's own bulk-dismiss button beside it */
.st-key-mh_list_bar { flex-direction: row !important; align-items: flex-end !important; justify-content: space-between !important;
  flex-wrap: wrap !important; column-gap: 12px !important; row-gap: 10px !important; }
.st-key-mh_list_bar > [data-testid="stElementContainer"] { width: auto !important; min-width: 0 !important; flex: 0 0 auto !important; }
.st-key-mh_list_bar > [data-testid="stElementContainer"]:first-child { flex: 1 1 320px !important; }
.mh-card .h { display: flex; align-items: center; gap: 8px; color: var(--accent-text); }
.mh-card .h b { color: var(--text); font-size: 15px; }
.mh-card ul { list-style: none; padding: 0 !important; margin: 10px 0 0 !important; }
.mh-card li { font-size: 14px; line-height: 1.6; color: var(--text-2); margin: 0; }
.mh-card li.muted, .ob-summary li.muted { color: var(--muted); }
.mh-card li.pg, .ob-summary li.pg { margin-top: 10px; font-size: 11px; line-height: 1.5; font-weight: 700; letter-spacing: .07em;
  text-transform: uppercase; color: var(--muted); }
.mh-card li.pg:first-child, .ob-summary li.pg:first-child { margin-top: 0; }
.pref-group.later { margin-top: 14px; padding-top: 16px; border-top: 1px solid var(--border); }
.pref-group p { margin: 2px 0 0; font-size: 13px; line-height: 1.5; color: var(--muted); }

/* ── preferences, filters, companies ── */
.filter-note { margin: 2px 2px 0; font-size: 12.5px; line-height: 1.5; color: var(--muted); }
.pref-q { margin-top: 8px; }
.pref-q b { display: block; font-size: 15px; color: var(--text); }
.pref-q span { display: block; font-size: 13px; line-height: 1.5; color: var(--muted); margin-top: 2px; }
.co-mini { display: flex; align-items: center; gap: 12px; }
.co-mini b { display: block; font-size: 15px; color: var(--text); }
.co-mini span { font-size: 13px; color: var(--muted); }
[class*="st-key-fol_"], [class*="st-key-add_"] { padding: 8px 0; border-top: 1px solid var(--border); }

/* ── onboarding ── */
.st-key-onboard { max-width: 760px; margin: 3vh auto 0 !important; padding: 34px 36px 30px !important; gap: 18px !important;
  border: 1px solid var(--border) !important; border-radius: 28px !important; background: var(--surface) !important;
  box-shadow: var(--hi), var(--shadow-lg) !important; animation: jt-rise .5s var(--ease) both; }
.ob-dots { display: flex; gap: 6px; margin: 14px 0 4px; }
.ob-dots i { width: 30px; height: 5px; border-radius: 5px; background: var(--border-strong); transition: background var(--normal) var(--ease); }
.ob-dots i.on { background: var(--accent); }
.ob-dots i.now { background: linear-gradient(90deg, var(--accent), var(--accent-2)); }
.stApp .ob-title { margin: 12px 0 6px !important; font-size: 30px; line-height: 1.15; font-weight: 800; letter-spacing: -.025em; color: var(--text); }
.ob-sub { margin: 0; font-size: 15px; line-height: 1.6; color: var(--muted); }
.ob-done { width: 56px; height: 56px; border-radius: 18px; display: grid; place-items: center; margin-top: 16px;
  color: var(--green); background: var(--green-soft); }
.ob-summary { list-style: none; margin: 16px 0 0 !important; padding: 14px 18px !important; border-radius: 16px;
  background: var(--surface-2); border: 1px solid var(--border); }
.ob-summary li { font-size: 15px; line-height: 1.8; color: var(--text); margin: 0; }
.st-key-onboard button[data-variant="pills"] { min-height: 40px; padding: 0 16px !important; font-size: 14.5px !important; }
.st-key-ob_actions { align-items: center; gap: 10px; margin-top: 6px; }

/* ── Settings → Tracker status (moved here from the sidebar) ── */
.status-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin: 16px 0 0 !important; padding: 0 !important; }
.st-tile { min-width: 0; padding: 13px 14px; border-radius: 13px; border: 1px solid var(--border); box-shadow: var(--hi);
  background: linear-gradient(180deg, color-mix(in srgb, var(--surface-2) 60%, var(--surface)), var(--surface-2)); }
.st-tile dt { font-family: var(--label) !important; font-size: 10.5px; line-height: 15px; letter-spacing: .1em; text-transform: uppercase;
  color: var(--muted); font-weight: 600; margin: 0 0 7px !important; }
.st-tile dd { margin: 0 !important; display: flex; flex-direction: column; align-items: flex-start; gap: 5px; min-width: 0; }
.st-tile .v { font-size: 18px; line-height: 24px; font-weight: 700; letter-spacing: -.02em; color: var(--text); overflow-wrap: anywhere; }
.st-tile .n { font-size: 12.5px; line-height: 17px; color: var(--muted); overflow-wrap: anywhere; }
@media (max-width: 1100px) { .status-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 480px) { .status-grid { grid-template-columns: minmax(0, 1fr); } }
.st-key-set_status .st-key-btn_set_monitoring, .st-key-set_status [data-testid="stElementContainer"]:has(.st-key-btn_set_monitoring) { width: auto !important; }

/* ── loading: only for work that takes a moment ── */
/* While the app works, a thin purple→teal bar slides along the top of the
   window. It waits 450 ms first, so a quick page change never flashes it.
   (Restored from e4c241a on 2026-10-15: the full-screen veil, frame and
   centre icons that replaced it covered the page on every sign-in and
   every fresh page load.) */
@keyframes jt-busy-in { from { opacity: 0; } to { opacity: 1; } }
@keyframes jt-busy-slide { from { background-position: -45% 0; } to { background-position: 145% 0; } }
@keyframes jt-busy-pulse { 0%, 100% { opacity: 1; } 50% { opacity: .5; } }
@keyframes jt-spin { to { transform: rotate(360deg); } }
.stApp::after { content: ""; position: fixed; top: 0; left: 0; right: 0; height: 3px; z-index: 1000005; pointer-events: none; opacity: 0;
  background: linear-gradient(90deg, transparent, var(--accent) 30%, var(--accent-2) 70%, transparent) -45% 0 / 38% 100% no-repeat,
    color-mix(in srgb, var(--accent) 12%, transparent); }
.stApp[data-test-script-state="running"]::after { animation: jt-busy-in 220ms ease-out 450ms both, jt-busy-slide 1.15s linear 450ms infinite; }
.stApp[data-test-script-state="running"] { cursor: progress; }
/* the button that was just pressed shows that its work is under way:
   Refresh / Reload spin their icon at once, any other button's label
   breathes if its work takes more than a moment */
.stApp[data-test-script-state="running"] :is(.st-key-btn_refresh, .st-key-btn_reload) [data-testid^="stBaseButton"]:focus [data-testid="stIconMaterial"] {
  animation: jt-spin .8s linear infinite; }
.stApp[data-test-script-state="running"] [data-testid^="stBaseButton"]:focus > div,
.stApp[data-test-script-state="running"] [class*="st-key-av_pick_"] button:focus::before { animation: jt-busy-pulse 1s ease-in-out 350ms infinite; }
/* first load of a server process: a skeleton of the page while the shared data arrives */
.jt-skel { display: grid; gap: 14px; }
.jt-skel i { display: block; border-radius: var(--r-lg); border: 1px solid var(--border);
  background: linear-gradient(90deg, var(--surface) 0, color-mix(in srgb, var(--accent) 7%, var(--surface)) 40%, var(--surface) 80%) 0 0 / 640px 100%;
  animation: jt-shimmer 1.4s linear infinite; }
.jt-skel .hero { height: 180px; } .jt-skel .row { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; }
.jt-skel .row i { height: 112px; } .jt-skel .line { height: 92px; }
.jt-skel p { margin: 0; font-size: 13px; color: var(--muted); display: flex; align-items: center; gap: 8px; }
.jt-skel p .ms { animation: jt-spin 1.2s linear infinite; color: var(--accent-text); }
@media (max-width: 767px) { .jt-skel .row { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (prefers-reduced-motion: reduce) {
  /* the bar appears (still after the pause) but stays still: a full, soft line */
  .stApp[data-test-script-state="running"]::after { animation: jt-busy-in 1ms linear 450ms both !important;
    background: color-mix(in srgb, var(--accent) 55%, transparent) !important; }
  .stApp[data-test-script-state="running"] [data-testid^="stBaseButton"]:focus > div,
  .stApp[data-test-script-state="running"] [data-testid="stIconMaterial"],
  .stApp[data-test-script-state="running"] [class*="st-key-av_pick_"] button::before, .jt-skel i, .jt-skel p .ms { animation: none !important; }
}

/* ── interaction polish: a clear, consistent answer to the pointer ── */
/* sidebar: hovering lights the row (wash + hairline + soft glow); the current page keeps its own style */
[class*="st-key-nav_"] [data-testid^="stBaseButton"]:hover { background: var(--hover-wash) !important; color: var(--text) !important;
  border-color: color-mix(in srgb, var(--accent) 20%, var(--border)) !important; box-shadow: var(--hi), 0 8px 18px -14px var(--accent) !important; }
[class*="st-key-nav_"] [data-testid^="stBaseButton"]:active { transform: scale(.985); }
/* quiet (tertiary) buttons: an accent wash instead of an almost invisible grey */
[data-testid="stBaseButton-tertiary"]:hover:not(:disabled) { background: var(--accent-soft) !important; color: var(--accent-text) !important; }
/* secondary buttons: a lit top edge so they read as raised glass */
[data-testid="stBaseButton-secondary"], [data-testid="stBaseButton-secondaryFormSubmit"] {
  background: linear-gradient(180deg, var(--pill-gloss), transparent 60%), var(--btn2-grad) !important; }
[data-testid="stBaseButton-secondary"]:hover:not(:disabled), [data-testid="stBaseButton-secondaryFormSubmit"]:hover:not(:disabled) {
  background: linear-gradient(180deg, var(--pill-gloss), transparent 60%), var(--btn2-grad) !important;
  box-shadow: var(--hi), var(--btn2-shadow-hover), 0 0 0 3px color-mix(in srgb, var(--accent) 10%, transparent) !important; }
/* segmented tabs and filter pills: a visible hover surface */
.st-key-jobs_tab_wrap button[data-variant="pills"]:hover, .st-key-co_status_wrap button[data-variant="pills"]:hover {
  background: color-mix(in srgb, var(--surface) 75%, transparent) !important; }
/* avatar tiles: press feedback and a keyboard ring */
[class*="st-key-av_pick_"] button:active { transform: translateY(0) scale(.97); }
[class*="st-key-av_pick_"] button:focus-visible { outline: 2px solid var(--accent) !important; outline-offset: 3px !important; }
/* job and company cards: a brighter edge when they lift */
:is([class*="st-key-jr_"], [class*="st-key-cr_"]):hover { border-color: color-mix(in srgb, var(--accent) 38%, var(--border)) !important; }
/* settings cards (not clickable: no hover): a faint glossy sheen from the top edge */
[class*="st-key-set_"], .st-key-mail_status, .st-key-email_form, .st-key-test_panel {
  background: linear-gradient(180deg, color-mix(in srgb, var(--accent) 3%, var(--surface)), var(--surface) 160px) !important; }
/* the one-line "open at the top" script of a navigation run: takes no space */
:is(.st-key-jt_scroll_top, .st-key-jt_menu_guard, .st-key-jt_auth_cookie), [data-testid="stLayoutWrapper"]:has(> :is(.st-key-jt_scroll_top, .st-key-jt_menu_guard, .st-key-jt_auth_cookie)) { display: none !important; }
/* the session probe's frame: loaded and running, but taking no space */
.st-key-jt_auth_probe, [data-testid="stLayoutWrapper"]:has(> .st-key-jt_auth_probe) { position: absolute !important; width: 1px !important;
  height: 1px !important; overflow: hidden !important; opacity: 0 !important; pointer-events: none !important; }
/* Settings: the tracker section, drawn for admins only */
.admin-head { display: flex; gap: 14px; align-items: flex-start; margin-top: 10px; padding: 18px 4px 4px;
  border-top: 1px dashed var(--border-strong); }
.admin-head .ic { width: 40px; height: 40px; border-radius: 12px; display: flex; align-items: center; justify-content: center;
  flex-shrink: 0; color: var(--accent-text); border: 1px solid color-mix(in srgb, var(--accent) 18%, transparent);
  background: linear-gradient(180deg, color-mix(in srgb, var(--accent-soft) 60%, var(--surface)), var(--accent-soft)); box-shadow: var(--badge-hi); }
.admin-head .section-title { font-size: 19px; line-height: 26px; }
.st-key-scan_pick_row { flex-direction: row !important; flex-wrap: wrap !important; align-items: center !important; gap: 12px !important; }
.st-key-scan_pick_row > * { width: auto !important; flex: 0 1 auto !important; }
.st-key-scan_pick_row button[data-variant="pills"] { min-height: 38px; padding: 0 15px !important; border-radius: 999px !important;
  background: var(--btn2-grad) !important; border: 1px solid var(--border-strong) !important; color: var(--text-2) !important;
  transition: border-color var(--fast) var(--ease), color var(--fast) var(--ease), box-shadow var(--fast) var(--ease); }
.st-key-scan_pick_row button[data-variant="pills"]:hover { border-color: color-mix(in srgb, var(--accent) 45%, var(--border-strong)) !important;
  color: var(--accent-text) !important; box-shadow: var(--hi), var(--btn2-shadow-hover) !important; }
.st-key-scan_pick_row button[data-variant="pills"]:is([aria-checked="true"], [aria-pressed="true"], [data-selected="true"]) {
  color: var(--accent-text) !important; border-color: color-mix(in srgb, var(--accent) 50%, transparent) !important;
  background: linear-gradient(180deg, var(--accent-soft), color-mix(in srgb, var(--accent) 16%, var(--accent-soft))) !important;
  box-shadow: var(--badge-hi), 0 6px 14px -10px var(--accent) !important; }
.st-key-scan_pick_row button[data-variant="pills"] p { color: inherit !important; font-size: 13.5px !important; font-weight: 600 !important; }
/* menus: the account menu's items and select lists answer the pointer the same way */
[data-baseweb="popover"] li:hover { background: var(--accent-soft) !important; color: var(--accent-text) !important; }
"""

# @import rules only count at the very top of a stylesheet, so the design
# tokens go after the CSS (with them first, the web fonts never loaded)
st.html(f"<style>{_CSS}\n{_root_vars}</style>")

# Select menus close when they should. Streamlit keeps a multiselect's menu
# open after each pick and reruns the app; on a slow connection the stale
# menu then stayed open over the next control, and Tab out of the field
# selected whatever option was highlighted. This listener (installed once per
# page) closes an open menu as soon as the pointer goes down anywhere outside
# it and its own field, and on Tab closes it first so Tab only moves focus. A
# click anywhere on a field (not only on its text box) opens its menu.
_MENU_GUARD = """<script>(() => {
  if (window.__jtMenuGuard) return; window.__jtMenuGuard = true;
  const FIELD = '[data-testid="stMultiSelect"], [data-testid="stSelectbox"]';
  const openBox = () => document.querySelector('input[role="combobox"][aria-expanded="true"]');
  const close = (input) => input.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', code: 'Escape', keyCode: 27, bubbles: true}));
  document.addEventListener('pointerdown', (e) => {
    const input = openBox(); if (!input) return;
    const t = e.target;
    if (!(t instanceof Element) || t.closest('[role="listbox"]')) return;
    const field = input.closest(FIELD);
    if (field && field.contains(t)) return;
    close(input);
  }, true);
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Tab') return;
    const t = e.target;
    if (!(t instanceof Element) || !t.matches('input[role="combobox"][aria-expanded="true"]')) return;
    close(t);
    e.stopPropagation();          // the browser still moves focus; the menu just doesn't pick an option
  }, true);
  // a click anywhere on a field's surface (beside the chips, not on a button) opens its menu
  // (pointerdown, not click: focusing the field can reflow it under the pointer, and then no click fires)
  document.addEventListener('pointerdown', (e) => {
    const t = e.target;
    if (e.button !== 0 || !(t instanceof Element) || t.closest('button')) return;
    const group = t.closest('[data-testid="stMultiSelect"] div[role="group"], [data-testid="stSelectbox"] div[role="group"]');
    if (!group) return;
    setTimeout(() => {
      const input = group.querySelector('input[role="combobox"]');
      if (input && input.getAttribute('aria-expanded') !== 'true') {
        const open = group.querySelector('button[aria-label="Open"]');
        if (open) open.click();
      }
    }, 120);
  }, true);                       // capture: a chip's own handler stops the event before it bubbles
})();</script>"""
with st.container(key="jt_menu_guard"):
    st.html(_MENU_GUARD, unsafe_allow_javascript=True)


# ── helpers ───────────────────────────────────────────────────────────────────
def _load(path: Path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return default


def _host(url: str) -> str:
    try:
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return url


def _short_url(url: str, limit: int = 40) -> str:
    try:
        p = urlparse(url)
    except ValueError:
        return url
    s = p.netloc.replace("www.", "") + p.path.rstrip("/")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _parse_iso(iso: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(iso)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _ago(dt: datetime | None, now: datetime | None = None) -> str:
    if dt is None:
        return "never"
    diff = int(((now or datetime.now(timezone.utc)) - dt).total_seconds())
    if diff < 60:
        return "just now"
    if diff < 3600:
        return f"{diff // 60} min ago"
    if diff < 86400:
        return f"{diff // 3600}h ago"
    days = diff // 86400
    return "1 day ago" if days == 1 else f"{days} days ago"


def _found_at(date_str: str, now: datetime) -> datetime | None:
    """The scraper stamps jobs as "Jul 15, 13:45" (UTC, no year). Assume the
    most recent such date that isn't in the future."""
    try:
        dt = datetime.strptime(f"{now.year} {date_str}", "%Y %b %d, %H:%M").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return dt.replace(year=now.year - 1) if dt > now + timedelta(days=1) else dt


# The scan schedule is the admin's global scan interval (settings.json,
# scan_schedule.py): check_jobs.yml ticks hourly and scans only when a scan is
# due. Times are UTC on GitHub and shown in India time: Asia/Kolkata is a
# fixed +05:30 (India has no DST), so no tz database is needed.
IST = timezone(timedelta(hours=5, minutes=30), "IST")


def interval_label(hours: int) -> str:
    """'Every hour' / 'Every 3 hours'."""
    return "Every hour" if hours == 1 else f"Every {hours} hours"


def _ist(dt: datetime) -> str:
    """'11:30 PM IST'."""
    return dt.astimezone(IST).strftime("%I:%M %p").lstrip("0") + " IST"


def _ist_stamp(dt: datetime) -> str:
    """'Oct 05, 7:29 PM IST'."""
    return f"{dt.astimezone(IST):%b %d}, {_ist(dt)}"


def _ist_full(dt: datetime) -> str:
    """'13 Oct 2026, 11:08 AM IST'."""
    local = dt.astimezone(IST)
    return f"{local.day} {local:%b %Y}, {_ist(dt)}"


def _ms(name: str, size: str = "") -> str:
    return f'<span class="ms {size}" aria-hidden="true">{name}</span>'


def _initial(name: str) -> str:
    """Initials for a tile: "MS" for Morgan Stanley, "Me" for MetLife."""
    return escape(brand_logos.initials(name)) or "•"


# company logos found on each company's own website (logos.json, written by
# the Company Logos workflow): {lowercased company name: entry}, filled once
# the data has loaded. The logos a page draws are collected in _USED_LOGOS
# and sent as ONE small style block at the end of the page (each image once,
# however many cards show it).
_FOUND_LOGOS: dict[str, dict] = {}
_USED_LOGOS: dict[str, dict] = {}
_FRAGMENT_LOGOS: dict[str, dict] = {}       # ... of which a list fragment already styled these


def _avatar(name: str, size: str = "", person: bool = False) -> str:
    """Company identity: the company's own mark when brand_logos has one
    (a local SVG — nothing is fetched), otherwise its initials on a tint, so
    companies are told apart at a glance. ``person``: someone's account
    avatar — initials only, never a company's mark."""
    cls = "logo" + (" " + size if size else "")
    brand = None if person else brand_logos.brand_key(name)
    if brand:
        return (f'<div class="{cls} co-mark" style="--tile:{brand_logos.brand_tile(brand)}" aria-hidden="true">'
                f'<img src="{brand_logos.brand_uri(brand)}" alt=""></div>')
    found = None if person else _FOUND_LOGOS.get((name or "").strip().lower())
    if found:
        key = "co-l-" + hashlib.sha1(found["web"]["data"].encode("ascii")).hexdigest()[:12]
        _USED_LOGOS[key] = found
        return f'<div class="{cls} co-mark {key}" style="--tile:#ffffff" aria-hidden="true"></div>'
    hue = brand_logos.tile_hue(name, [c.get("name") for c in companies])
    text = _initial(name)
    return (f'<div class="{cls}{" ini2" if len(text) == 2 else ""}" style="--h:{hue}" aria-hidden="true">'
            f'{text}</div>')


def _plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


@st.cache_resource(show_spinner=False)
def _logo_data_uri() -> str:
    try:
        return "data:image/png;base64," + base64.b64encode(LOGO_PATH.read_bytes()).decode("ascii")
    except OSError:
        return ""


@st.cache_resource(show_spinner=False)
def _source_label(c: dict) -> str:
    """How the scraper reads this company ("Workday API", "Accenture job-search API", ...)."""
    try:
        from scraper import source_label
        return source_label(c)
    except Exception:
        return "Playwright (headless browser)"


_ACTIVE_RUN_STATES = {"queued", "in_progress", "waiting", "requested", "pending"}


def _active_check_run(workflow) -> str | None:
    """How long ago the newest Check Jobs run that is still queued or running
    started ("2 min ago"), or None when none is. One request (the newest page
    of runs). If GitHub can't tell us, None: Run check then works exactly as
    before (the workflow's concurrency group still never overlaps two runs)."""
    try:
        for i, run in enumerate(workflow.get_runs()):
            if i >= 10:
                break
            if getattr(run, "status", "") in _ACTIVE_RUN_STATES:
                started = getattr(run, "run_started_at", None) or getattr(run, "created_at", None)
                if isinstance(started, datetime):
                    started = started if started.tzinfo else started.replace(tzinfo=timezone.utc)
                    return _ago(started, datetime.now(timezone.utc))
                return "just now"
    except Exception as e:
        log.warning("could not list Check Jobs runs: %s", type(e).__name__)
    return None


def _trigger_scrape() -> tuple[bool, str]:
    """Ask GitHub Actions to run check_jobs.yml right now (workflow_dispatch).
    Owner only, and at most once per cooldown for the whole app."""
    if not is_owner():
        return False, "Only the owner can start a check."
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        return False, "Can't start a check — no GitHub token is configured for this app."
    try:
        import config_store
        # lazy: no request for the repository itself — only the workflow
        # lookup and the dispatch reach GitHub (two round trips, not three)
        repo = config_store._get_repo(token)
        workflow = repo.get_workflow("check_jobs.yml")
        running = _active_check_run(workflow)
        if running is not None:
            # a run is queued or in progress (a scan, or a scheduled check that
            # may start one): a second dispatch would scan everything again
            return False, (f"A check is already running on GitHub Actions (started {running}). Its jobs appear here "
                           "when it finishes — use Refresh then. No second check was started.")
        if workflow.create_dispatch(ref="main"):
            return True, ("Check started on GitHub Actions. A full scan takes about 5–8 minutes and keeps running "
                          "after this message — use Refresh afterwards to see new jobs.")
        return False, "GitHub declined to start a new check."
    except Exception as e:
        log.warning("workflow dispatch failed: %s", e)
        if getattr(e, "status", None) == 403:
            return False, "The app's GitHub token isn't allowed to start checks (needs Actions: write)."
        return False, f"Couldn't start a check. {friendly_github_error(e)}"


# ── owner access ──────────────────────────────────────────────────────────────
# Interim protection until real sign-in: browsing is open (the data is public
# in the repository anyway), every change needs the owner password. The
# sign-in is remembered server-side in this browser session only — never in
# the URL, a cookie or a widget — and expires after OWNER_SESSION_S.
_CHECK_COOLDOWN_S = 300
_REFRESH_COOLDOWN_S = 15
_TEST_EMAIL_COOLDOWN_S = 30


@st.cache_resource(show_spinner=False)
def _guards() -> dict:
    """Limits shared by every session of this server process."""
    return {"signin": AttemptLimiter(), "check": Cooldown(_CHECK_COOLDOWN_S),
            "refresh": Cooldown(_REFRESH_COOLDOWN_S),
            # one test email per 30 s for the whole app (a double click sends one)
            "test_email": Cooldown(_TEST_EMAIL_COOLDOWN_S),
            # email sign-in failures, per address (one address's failures
            # never lock anyone else out)
            "firebase_signin": AccountAttemptLimiter(),
            # Firebase verification emails: one per address per 15 min
            "verify_email": AccountAttemptLimiter(per_account=1, overall=30),
            # email sign-ups that reached Firebase, per address and overall
            "firebase_signup": AccountAttemptLimiter(per_account=3, overall=30)}


def is_owner() -> bool:
    """May this session make changes right now? Every protected operation
    calls this immediately before acting. Behind the sign-in gate the role
    stored by this run's gate must also still match its source: Streamlit's
    verified Google identity, or an unexpired Firebase password sign-in for
    the same account."""
    if not owner_session_valid(st.session_state):
        return False
    if not AUTH_GATE:
        return True                       # legacy owner password, no sign-in configured
    google = None if st.session_state.get("_signed_out") else _google_identity()
    return account_matches_source(st.session_state.get(ACCOUNT_KEY), google,
                                  firebase_auth.session_user(st.session_state))


def sign_in(password: str) -> tuple[bool, str]:
    limiter = _guards()["signin"]
    wait = limiter.locked_for()
    if wait:
        return False, f"Too many failed attempts — try again in {int(wait // 60) + 1} min."
    if not owner_configured():
        return False, "Owner access isn't set up for this app."
    if not owner_password_enabled():
        # admins come from sign-in now: the old password grants nothing
        return False, "Sign in with your account instead."
    if not password_matches(password):
        limiter.failed()
        log.warning("failed owner sign-in")
        return False, "That password isn't right."
    limiter.succeeded()
    st.session_state[OWNER_SESSION_KEY] = time.time() + OWNER_SESSION_S
    return True, "Signed in as the owner"


def _owner_submit() -> None:
    """Form callback: wipe the password from session state before checking
    it, so neither a right nor a wrong one stays on the server
    (clear_on_submit only clears the browser's copy)."""
    password = st.session_state.get("owner_pw", "")
    st.session_state.owner_pw = ""
    ok, msg = sign_in(password)
    toast(msg, "success" if ok else "error")


def sign_out() -> None:
    st.session_state.pop(OWNER_SESSION_KEY, None)


def owner_only(action: str) -> bool:
    """True for the owner; otherwise explain why ``action`` can't happen."""
    if is_owner():
        return True
    if AUTH_GATE:
        toast(f"Only admins can {action}." if admins_configured() or not owner_configured()
              else f"Only admins can {action}. Unlock admin access under Settings → Account.", "error")
    else:
        toast(f"Only the owner can {action}. Sign in under Settings → Owner access." if owner_configured()
              else f"Changes are turned off: owner access isn't set up, so you can't {action}.", "error")
    return False


def _refresh_data() -> None:
    """Reload from GitHub now (an explicit Refresh waits for it) — at most
    every few seconds for the whole app, so repeated clicks can't spend the
    token's API quota."""
    if _guards()["refresh"].try_start():
        _shared_data().refresh_now()


_DATA_FILES = ["data/companies.json", "data/settings.json", "data/seen_jobs.json", "data/logos.json"]


@st.cache_resource(show_spinner=False)
def _shared_data() -> snapshot_cache.SnapshotCache:
    """The shared files as last read from GitHub, for every session of this
    process (no one's personal data). A click is answered from the current
    copy at once; a copy older than 5 minutes is refreshed in the
    background (it took ~2.5 s, which used to freeze that click). A copy
    older than 15 minutes is never shown: the first page after an idle spell
    reads GitHub again first (it once showed a day-old scan as "delayed")."""
    return snapshot_cache.SnapshotCache(lambda: fetch_remote_json(_DATA_FILES), ttl_s=300, max_stale_s=900)


def _remote_snapshot() -> dict:
    """Latest data straight from GitHub — the local checkout can be hours old."""
    return _shared_data().get()


def _load_synced(name: str, default):
    """Prefer the GitHub copy (read-only here — saves go through
    _save_change); fall back to the local checkout."""
    remote = _remote_snapshot().get(name)
    if remote is not None and isinstance(remote, type(default)):
        return remote
    local = _load(BASE / name, default)
    # a corrupt or hand-edited file ("null", "[]" for settings) must not crash the app
    return local if isinstance(local, type(default)) else default


@st.cache_resource(show_spinner=False)
def _shared_writer() -> dict:
    """ONE background thread for the shared file's dismissal commits, for the
    whole server process: commits run one at a time, in click order (no two
    of this process ever race), and each one still reads GitHub's latest
    copy and retries on a conflict (update_json). A failure is kept for the
    session that asked and shown on its next run."""
    from concurrent.futures import ThreadPoolExecutor
    return {"pool": ThreadPoolExecutor(max_workers=1, thread_name_prefix="jt-shared-write"),
            "failed": {}, "lock": threading.Lock()}


def _save_change_soon(name: str, mutate, default, message: str) -> bool:
    """_save_change for an admin's dismissal when their own (Firestore) copy
    already changed: the owner check runs NOW, in this run; the GitHub
    commit (two round trips, ~1 s) runs on the shared writer, so the click
    doesn't wait for it. The admin's view already follows their own copy."""
    if not is_owner():
        log.warning("blocked a change to %s from a session without owner access", name)
        return False
    token = st.session_state.setdefault("_writer_token", os.urandom(16).hex())
    writer, cache = _shared_writer(), _shared_data()

    def commit():
        try:
            data, saved, err = update_json(BASE / name, name, mutate, default, message)
        except Exception as e:                       # never lost silently
            log.warning("background save of %s failed: %s", name, type(e).__name__)
            data, saved, err = None, False, "GitHub couldn't be reached — the change wasn't saved for everyone."
        if data is not None:
            cache.put(name, data)
        if not saved:
            with writer["lock"]:
                writer["failed"].setdefault(token, []).append(err)
    writer["pool"].submit(commit)
    return True


def _report_background_saves() -> None:
    """A dismissal whose shared commit failed after its click returned: say
    so on this session's next run (the person's own copy is unaffected)."""
    token = st.session_state.get("_writer_token")
    if not token:
        return
    writer = _shared_writer()
    with writer["lock"]:
        failed = writer["failed"].pop(token, None)
    if failed:
        toast(f"A dismissal was saved for you, but not in the shared list. {failed[-1]}", "error")


def _save_change(name: str, mutate, default, message: str):
    """Apply one change to the latest copy of ``name`` (merge-safe).
    Every write goes through here, so the owner check is repeated here: a
    button that forgot its own check still can't change anything."""
    if not is_owner():
        log.warning("blocked a change to %s from a session without owner access", name)
        return None, False, "Only the owner can make changes."
    data, saved, err = update_json(BASE / name, name, mutate, default, message)
    if data is not None:
        _shared_data().put(name, data)        # show the saved version at once
    return data, saved, err


def _widget_key(prefix: str, key: str) -> str:
    return f"{prefix}_{hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]}"


def _jid(job: dict) -> str:
    return hashlib.sha1(job_key(job).encode("utf-8")).hexdigest()[:12]


def _restore_jobs(keys: set[str]):
    """Undo a dismissal. The record keeps its ``notified`` flag, so restoring
    never causes a second email — and a job dismissed *before* it was emailed
    is marked as handled, so restoring it doesn't send it later either (the
    user has already seen it)."""
    def _mutate(seen: list[dict]) -> list[dict]:
        for j in seen:
            if isinstance(j, dict) and job_key(j) in keys:
                j.pop("dismissed", None)
                if not j.get("notified") and j.get("notify_state") not in ("claimed", "sent"):
                    j["notify_state"] = "skipped"
        return seen
    return _mutate


# ── sign-in gate ──────────────────────────────────────────────────────────────
# When Firebase is configured the whole app sits behind ONE login page:
# Continue with Google, or email + password. Nothing below this block (data,
# sidebar, pages) runs for a visitor. Who is signed in:
#   * email/password — Firebase checked the password (firebase_auth); kept in
#     server-side session state until INACTIVITY_LIMIT_S passes without use.
#   * Google — Streamlit's st.login verified Google's ID token and keeps the
#     identity in its signed 30-day cookie, so a reload or a new tab is
#     already signed in. The Firebase account is linked right after sign-in
#     (while Google's ID token is fresh) to obtain the Firebase UID.
# Admin rights come from the account (access.JT_ADMIN_EMAILS, verified email).
# No token, password or secret is ever kept in session state.
_OIDC_KEYS = ("client_id", "client_secret", "server_metadata_url")
# Signed out after 48 consecutive hours without using the app (an inactivity
# limit, not a fixed time from sign-in): see enforce_inactivity().
INACTIVITY_LIMIT_S = 48 * 3600
ACCOUNT_SESSION_S = INACTIVITY_LIMIT_S        # an email sign-in's first expiry; each use pushes it on
_FB_CREDENTIAL_ERRORS = {"EMAIL_NOT_FOUND", "INVALID_PASSWORD", "INVALID_LOGIN_CREDENTIALS", "INVALID_EMAIL"}
_GOOGLE_ERROR_HINTS = {
    "INVALID_IDP_RESPONSE": "Firebase didn't accept this Google sign-in — usually the Google OAuth client used by "
                            "this app isn't allowed by the Firebase project's Google provider.",
    "OPERATION_NOT_ALLOWED": "The Google provider isn't enabled in the Firebase project.",
    "PROJECT_MISMATCH": "The Firebase project settings don't match the project that answered.",
}


def _secrets_dict() -> dict:
    """All Streamlit secrets as plain dicts ({} when there is no secrets file)."""
    try:
        return st.secrets.to_dict()
    except Exception:
        return {}


def _promote_admin_setting() -> None:
    """JT_ADMIN_EMAILS works wherever it sits in the secrets (top level, or —
    a common TOML slip — below [auth]); access.py reads the environment."""
    if os.environ.get("JT_ADMIN_EMAILS", "").strip():
        return
    secrets = _secrets_dict()
    for section in (secrets, secrets.get("auth"), secrets.get("admin")):
        value = section.get("JT_ADMIN_EMAILS") if isinstance(section, dict) else None
        if isinstance(value, (list, tuple)):
            value = ",".join(str(v) for v in value)
        if isinstance(value, str) and value.strip():
            os.environ["JT_ADMIN_EMAILS"] = value
            return


def _firebase_config():
    """(config or None, problem, any Firebase setting present)."""
    secrets = _secrets_dict()
    config, problem = firebase_auth.config_status(secrets)
    return config, problem, firebase_auth.any_setting(secrets)


class _StoreUnavailable(Exception):
    """configure() refused (its safe message is the text). Raised rather
    than returned so Streamlit never caches the failure."""


def _user_store():
    """(store, problem): the per-user data store. (None, "") while no
    Firebase service account is configured — the app then works exactly as
    without personal data.

    Read on every call: "not configured" and failures are never cached, so
    a service account that is added, fixed or removed takes effect on the
    next page load, without a reboot. (Caching the whole answer once kept
    "not configured" for the life of the server process: Streamlit reloads
    st.secrets when they change but keeps st.cache_resource.)"""
    secrets = _secrets_dict()
    if not user_store.configured(secrets):
        return None, ""
    fb = firebase_auth.config_status(secrets)[0]
    project = fb.project_id if fb else ""
    try:
        return _user_store_for(user_store.credential_fingerprint(secrets, project), project), ""
    except _StoreUnavailable as e:
        return None, str(e)


@st.cache_resource(show_spinner=False)
def _user_store_for(fingerprint: str, project_id: str):
    """The working store for one credential, shared by every session of
    this process (a Firestore client — it holds no one's data). Keyed by a
    fingerprint of the credential's identity (never the key itself), so a
    different credential gets its own store. A failure raises, and Streamlit
    never caches an exception."""
    store, problem = user_store.configure(_secrets_dict(), project_id)
    if store is None:
        raise _StoreUnavailable(problem)
    return store


def _google_firebase_account(sub: str) -> dict | None:
    """The Firebase account linked to this Google account, looked up on the
    server (Admin SDK) by Google's stable subject ID — used when Google's
    1-hour token was too old to link at sign-in. Once per session; None if
    per-user data isn't set up or the lookup failed."""
    store = _user_store()[0]
    if store is None or not sub:
        return None
    cached = st.session_state.get("_fb_lookup")
    if isinstance(cached, dict) and cached.get("sub") == sub:
        return cached.get("account")
    try:
        found = store.google_account(sub)
    except user_store.UserStoreError:
        return None                      # not cached: retried on the next run
    st.session_state._fb_lookup = {"sub": sub, "account": found}
    return found


_ACCOUNT_CHECK_S = 300


def _firebase_account_state(uid: str) -> str:
    """Firebase's current state of the signed-in account, by UID:
    "ok", "disabled", "deleted" — or "unknown" when per-user data isn't set
    up or Firebase couldn't be asked (never treated as a problem by itself).
    Re-checked every few minutes, so disabling someone takes effect within
    minutes even for a session that is already open."""
    store = _user_store()[0]
    if store is None or not user_store.valid_uid(uid):
        return "unknown"
    cached = st.session_state.get("_acct_state")
    if isinstance(cached, dict) and cached.get("uid") == uid and time.time() - cached.get("at", 0) < _ACCOUNT_CHECK_S:
        return cached["state"]
    try:
        acct = store.account(uid)
    except user_store.UserStoreError:
        return "unknown"
    state = "deleted" if acct is None else "disabled" if acct.get("disabled") else "ok"
    st.session_state._acct_state = {"uid": uid, "state": state, "at": time.time()}
    return state


def _blocked_account() -> None:
    """A Firebase account the owner disabled: no access, whatever Google says."""
    st.session_state._login_error = "This account has been disabled."
    auth_event("google identity ok; firebase account disabled — refused")
    return None


_GOOGLE_UNVERIFIED = "Your Google account's email address isn't verified, so it can't be used to sign in here."


def _refused_identity(problem: str, provider: str) -> None:
    """An identity email_policy refuses (a disposable or unverified address):
    no account, no Firebase link, no personal data — and nothing is deleted.
    The login page says why; signing in again starts from Google's or the
    email form's own step (no automatic retry with the same identity).
    When the policy itself can't be evaluated (a broken blocklist) access
    is refused the same way, but the sign-in is kept: it is re-checked on
    every page load and goes through by itself once the list is fixed."""
    st.session_state.pop(ACCOUNT_KEY, None)
    if problem == email_policy.UNAVAILABLE_MESSAGE:
        st.session_state._login_error = problem
        auth_event(f"{provider} identity refused: disposable-address policy unavailable")
        return None
    st.session_state.pop(firebase_auth.SESSION_KEY, None)
    st.session_state._signed_out = True
    if provider == "google.com" and problem == email_policy.UNVERIFIED_MESSAGE:
        problem = _GOOGLE_UNVERIFIED
    st.session_state._login_error = problem
    reason = "disposable address" if problem == email_policy.DISPOSABLE_MESSAGE else "unverified or unusable address"
    auth_event(f"{provider} identity refused: {reason}")          # never the address itself
    return None


def _google_setup() -> tuple[dict | None, str]:
    """Streamlit's built-in Google sign-in (st.login) as configured in the
    [auth] secrets: ({"provider": None | name, "redirect_uri": ...}, "") when
    usable, else (None, why) — why is "" when Google sign-in simply isn't
    set up. Accepts the flat [auth] layout and [auth.<provider>]."""
    auth = _secrets_dict().get("auth")
    if not isinstance(auth, dict):
        return None, ""
    if all(auth.get(k) for k in _OIDC_KEYS):
        provider = None
    else:
        named = [n for n, v in auth.items() if isinstance(v, dict) and all(v.get(k) for k in _OIDC_KEYS)]
        if not named:
            return None, ""
        provider = "google" if "google" in named else named[0]
    missing = [k for k in ("redirect_uri", "cookie_secret") if not auth.get(k)]
    if missing:
        return None, f"Google sign-in needs {' and '.join(missing)} in the [auth] secrets."
    if not str(auth["redirect_uri"]).rstrip("/").endswith("/oauth2callback"):
        return None, "Google sign-in needs redirect_uri in the [auth] secrets to end with /oauth2callback."
    tokens = auth.get("expose_tokens")
    if not (tokens == "id" or (isinstance(tokens, (list, tuple)) and "id" in tokens)):
        return None, 'Google sign-in needs expose_tokens = "id" in the [auth] secrets.'
    return {"provider": provider, "redirect_uri": str(auth["redirect_uri"])}, ""


def _google_identity() -> dict | None:
    """The Google account Streamlit's st.login verified (from its signed
    cookie) — identity claims only, never a token."""
    try:
        if not st.user.is_logged_in:
            return None
        email = st.user.get("email") if hasattr(st.user, "get") else getattr(st.user, "email", None)
        verified = st.user.get("email_verified") if hasattr(st.user, "get") else getattr(st.user, "email_verified", None)
        name = st.user.get("name") if hasattr(st.user, "get") else getattr(st.user, "name", None)
        sub = st.user.get("sub") if hasattr(st.user, "get") else getattr(st.user, "sub", None)
    except Exception as e:
        auth_event(f"reading st.user failed: {type(e).__name__}")
        return None
    if not isinstance(email, str) or "@" not in email:
        auth_event(f"st.user logged in but no usable email (type {type(email).__name__})")
        return None
    return {"email": email.strip().lower(), "email_verified": verified is True,
            "name": name if isinstance(name, str) else "", "sub": sub if isinstance(sub, str) else ""}


def _google_id_token() -> str | None:
    try:
        token = st.user.tokens["id"]
    except Exception:
        return None
    return token if isinstance(token, str) and token else None


def _link_google_to_firebase(google: dict, config) -> None:
    """Once per session, right after Google sign-in (while Google's ID token
    is fresh): find/create the Firebase account and keep its UID. The
    outcome is kept for the Account card; it never blocks the app."""
    if st.session_state.get("_fb_link_tried"):
        return
    st.session_state._fb_link_tried = True
    if config is None:
        st.session_state._fb_link = {"state": "unavailable"}
        return
    token = _google_id_token()
    if token is None or firebase_auth.token_expired(token):
        st.session_state._fb_link = {"state": "stale"}
        print("[auth] google sign-in restored from cookie; firebase link skipped (google token not fresh)", flush=True)
        return
    try:
        user = firebase_auth.sign_in_with_google_id_token(token, google["redirect_uri"], config=config)
    except firebase_auth.FirebaseAuthError as e:
        st.session_state._fb_link = {"state": "failed", "code": e.code, "message": str(e)}
        print(f"[auth] google sign-in ok; firebase link failed code={e.code}", flush=True)
        auth_event(f"google identity ok; firebase link failed code={e.code}")
        return
    st.session_state._fb_link = {"state": "linked"}
    auth_event("google identity ok; firebase linked")
    st.session_state[firebase_auth.SESSION_KEY] = {**user.as_session(), "expires_at": time.time() + ACCOUNT_SESSION_S}
    st.session_state._last_active = time.time()           # a sign-in made just now, not a restored one
    print("[auth] google sign-in ok; firebase linked", flush=True)


def resolve_account(google: dict | None, config) -> dict | None:
    """The signed-in account for this run (also stored, without secrets,
    under ACCOUNT_KEY for the server-side admin check), or None."""
    account = None
    fb_user = firebase_auth.session_user(st.session_state)
    if fb_user is None and not st.session_state.get("_signed_out") and not (google and _google_identity()):
        fb_user = _restore_email_session()
    if not st.session_state.get("_signed_out"):
        g = _google_identity() if google else None
        if g:
            # before anything is linked or created in Firebase, and on every
            # run (a session restored from Streamlit's cookie is checked too)
            problem = email_policy.sign_in_problem(g["email"], g["email_verified"])
            if problem:
                return _refused_identity(problem, "google.com")
            if fb_user is None:
                _link_google_to_firebase(google, config)
                fb_user = firebase_auth.session_user(st.session_state)
            if (st.session_state.get("_fb_link") or {}).get("code") == "USER_DISABLED":
                st.session_state.pop(ACCOUNT_KEY, None)
                return _blocked_account()
            uid = fb_user.get("uid") if fb_user and fb_user.get("email") == g["email"] else ""
            if not uid:
                found = _google_firebase_account(g.get("sub", ""))
                if found and found.get("disabled"):
                    st.session_state.pop(ACCOUNT_KEY, None)
                    return _blocked_account()
                uid = found.get("uid", "") if found else ""
            account = {**g, "uid": uid, "provider": "google.com"}
        elif fb_user and fb_user.get("provider") == "password":
            account = {"email": fb_user.get("email") or "", "email_verified": fb_user.get("email_verified") is True,
                       "name": fb_user.get("name") or "", "uid": fb_user["uid"], "provider": "password"}
    elif fb_user and fb_user.get("provider") == "password":
        # signed in again with email/password after an explicit sign-out
        account = {"email": fb_user.get("email") or "", "email_verified": fb_user.get("email_verified") is True,
                   "name": fb_user.get("name") or "", "uid": fb_user["uid"], "provider": "password"}
    if account and account["provider"] == "password":
        # firebase_sign_in already refused these; checked again for every run
        problem = email_policy.sign_in_problem(account["email"], account["email_verified"])
        if problem:
            return _refused_identity(problem, "password")
    if account and account.get("uid"):
        state = _firebase_account_state(account["uid"])
        if state == "disabled":
            st.session_state.pop(ACCOUNT_KEY, None)
            st.session_state.pop(firebase_auth.SESSION_KEY, None)
            return _blocked_account()
        if state == "deleted":
            # the Firebase account behind this sign-in no longer exists: no
            # personal data is read or written for it (approved policy: deletion
            # is a reset — a fresh sign-in gets a new UID; see user_store docs)
            account = {**account, "uid": "", "account_deleted": True}
    if account is None and not admins_configured() and owner_session_valid(st.session_state):
        account = {"email": "", "email_verified": False, "name": "Owner", "uid": "", "provider": "owner"}
    if account:
        st.session_state[ACCOUNT_KEY] = account
        if not st.session_state.get("_auth_logged"):
            # once per sign-in, for the server log only (?diag=auth is admins-only,
            # so a broken admin list is diagnosed here): booleans and counts, no address
            st.session_state._auth_logged = True
            print(f"[auth] signed in: provider={account['provider']} email_verified={account['email_verified']} "
                  f"admin={account_is_admin(account)} admin_list_entries={len(admin_emails())}", flush=True)
    else:
        st.session_state.pop(ACCOUNT_KEY, None)
    return account


# ── an email sign-in survives a reload ────────────────────────────────────────
# Streamlit keeps session_state per browser connection, so a reload (or a
# new tab) starts with nothing: an email/password sign-in, kept only there,
# was lost on every refresh (Google sign-ins survive in Streamlit's own
# signed cookie). Now a successful email sign-in also gets a server-side
# session: a random token whose sha256 is stored with the person's own data
# (user_store.start_session), and the browser keeps "<uid>.<token>" in the
# jt_session cookie (written by _session_cookie_html, the one script that
# touches a cookie). On a new connection the gate accepts it only if:
#   * that token's hash is stored under that UID (the UID alone proves
#     nothing), the session isn't ended, idle for INACTIVITY_LIMIT_S or
#     older than user_store.SESSION_MAX_S;
#   * Firebase (Admin SDK, by UID) says the account still exists, isn't
#     disabled, is verified and has the same email address.
# Sign out ends the session on the server and clears the cookie. No
# password, Firebase token or secret is ever put in the cookie.
SESSION_COOKIE = "jt_session"
_COOKIE_VALUE_RE = re.compile(r"^[A-Za-z0-9_.-]{0,200}$")


def _session_cookie_html(value: str) -> str:
    """The script that writes (value) or clears ("") the jt_session cookie
    in this browser: one cookie, a fixed name and attributes, SameSite=Lax,
    Secure on https. ``value`` is server-made and checked against a strict
    pattern before it is placed in the page."""
    if not _COOKIE_VALUE_RE.match(value):
        raise ValueError("unexpected session cookie value")
    max_age = user_store.SESSION_MAX_S if value else 0
    return (f'<script data-c="{time.time_ns()}">(() => {{ document.cookie = "{SESSION_COOKIE}={value}; '
            f'Path=/; Max-Age={max_age}; SameSite=Lax" + (location.protocol === "https:" ? "; Secure" : ""); }})();</script>')


def _write_session_cookie(value: str) -> None:
    with st.container(key="jt_auth_cookie"):
        st.html(_session_cookie_html(value), unsafe_allow_javascript=True)


def _browser_session_cookie() -> str:
    """jt_session as the server received it. Streamlit Community Cloud's
    proxy keeps cookies an app sets from reaching the server, so there this
    is always "" (it works when self-hosted and locally)."""
    try:
        value = st.context.cookies.get(SESSION_COOKIE)
    except Exception:
        return ""
    return value if isinstance(value, str) else ""


def _browser_session_token() -> str | None:
    """This browser's jt_session: from the request when the server can see
    it, else from the session probe. None = the probe hasn't reported yet
    (it does within a moment of the page loading, which runs the app again)."""
    raw = _browser_session_cookie()
    if raw or not _browser_connected():          # (AppTest has no browser)
        return raw
    with st.container(key="jt_auth_probe"):
        import session_probe              # assets/session_probe: reads jt_session in the browser (see the module)
        value = session_probe.probe(key="jt_session_probe", default=None)
    return value if value is None or isinstance(value, str) else ""


def _known_session_token() -> str:
    """The jt_session this browser is known to hold, without asking again."""
    value = _browser_session_cookie() or st.session_state.get("jt_session_probe")
    return value if isinstance(value, str) else ""


def _restore_email_session() -> dict | None:
    """This browser's email sign-in, from its jt_session cookie (see above),
    or None. Tried once per connection and cookie value."""
    store = _user_store()[0]
    if store is None:                             # no per-user data: no browser sessions either
        return None
    raw = _browser_session_token()
    if raw is None:
        st.session_state._probe_pending = True    # the gate waits for the probe instead of the login page
        return None
    if not raw or st.session_state.get("_restore_tried") == raw:
        return None
    st.session_state._restore_tried = raw
    try:
        sess = store.resolve_session(raw, idle_limit_s=INACTIVITY_LIMIT_S)
        acct = store.account(sess["uid"]) if sess else None
    except user_store.UserStoreError:
        auth_event("email session not restored: per-user data unavailable")
        return None                                   # can't be validated: not restored
    if not sess:
        return None
    if (acct is None or acct.get("disabled") or acct.get("email_verified") is not True
            or acct.get("email") != sess["email"] or email_policy.sign_in_problem(sess["email"], True)):
        try:
            store.end_session(sess["uid"], sess["key"])
        except user_store.UserStoreError:
            pass
        auth_event("email session refused: the Firebase account changed")
        return None
    st.session_state[firebase_auth.SESSION_KEY] = {
        "uid": sess["uid"], "email": sess["email"], "email_verified": True, "provider": "password",
        "name": sess["name"], "expires_at": time.time() + INACTIVITY_LIMIT_S}
    st.session_state._session_key = sess["key"]
    st.session_state._last_active = sess["last_active"]          # the server's record, not "now"
    auth_event("email sign-in restored from this browser's session")
    return st.session_state[firebase_auth.SESSION_KEY]


def account_sign_out() -> bool:
    """End this sign-in. Returns True when Streamlit's Google sign-in is
    being ended too (the browser is then redirected).
    Everything this browser session held for the account is dropped — the
    sign-in itself, admin unlocks, and per-user leftovers such as the last
    test-email result or form contents — so whoever signs in next in this
    tab starts clean. Only the theme survives. An email sign-in's browser
    session is ended on the server and its cookie cleared, so a reload
    can't sign anyone back in."""
    google = _google_identity() is not None
    user, key = globals().get("USER"), st.session_state.get("_session_key")
    if user is not None and key:
        try:
            user.end_session(key)
        except user_store.UserStoreError:
            pass                       # the cookie is cleared below either way
    for k in list(st.session_state.keys()):
        if k != "dark_mode":
            del st.session_state[k]
    st.session_state._signed_out = True
    st.session_state._clear_session_cookie = True
    if google:
        st.logout()              # clears Streamlit's sign-in cookie, back to the login page
        return True
    return False


def _may_send_verification(email: str) -> bool:
    """At most one Firebase verification email per address per 15 minutes,
    for the whole app (counted by the "verify_email" limiter)."""
    limiter = _guards()["verify_email"]
    if limiter.locked_for(email):
        return False
    limiter.failed(email)
    return True


def firebase_sign_in(email: str, password: str) -> tuple[bool, str]:
    limiter = _guards()["firebase_signin"]
    wait = limiter.locked_for(email)
    if wait:
        return False, f"Too many failed attempts — try again in {int(wait // 60) + 1} min."
    if _AUTH_CONFIG is None:
        return False, "Email sign-in is unavailable right now."
    problem = email_policy.address_problem(email)            # a disposable address: Firebase isn't even asked
    if problem:
        return False, problem
    try:
        user = firebase_auth.sign_in_with_password(email, password, config=_AUTH_CONFIG, require_verified=True,
                                                   may_send_verification=_may_send_verification)
    except firebase_auth.FirebaseAuthError as e:
        if e.code in _FB_CREDENTIAL_ERRORS:
            limiter.failed(email)
        return False, str(e)
    # the address Firebase answered with is the one that counts
    problem = email_policy.sign_in_problem(user.email, user.email_verified)
    if problem:
        return False, problem
    limiter.succeeded(email)
    st.session_state[firebase_auth.SESSION_KEY] = {**user.as_session(), "expires_at": time.time() + ACCOUNT_SESSION_S}
    st.session_state.pop("_signed_out", None)
    st.session_state._last_active = time.time()
    st.session_state._start_session = True             # made once the gate has bound this account's data
    return True, ""


SIGN_UP_PASSWORD_MIN = 8
_EMAIL_SHAPE = re.compile(r"^[^@\s]+@[^@\s]+$")


def sign_up_problem(name: object, email: object, password: object, confirm: object) -> str:
    """Why this sign-up form can't be sent to Firebase ("" = it can). Firebase
    checks again (address format, existing accounts, its own password policy)."""
    if not isinstance(name, str) or not name.strip():
        return "Enter your full name."
    try:
        user_store.clean_display_name(name)
    except user_store.InvalidInput as e:
        return f"Full name: {e}"
    email = email.strip() if isinstance(email, str) else ""
    if not email:
        return "Enter your email address."
    if len(email) > 254 or not _EMAIL_SHAPE.match(email) or not email_policy.email_domain(email):
        return "Enter a valid email address."
    if not isinstance(password, str) or not password:
        return "Enter a password."
    if len(password) < SIGN_UP_PASSWORD_MIN:
        return f"Use at least {SIGN_UP_PASSWORD_MIN} characters for your password."
    if len(password) > 4096:
        return "That password is too long."
    if password != confirm:
        return "The passwords don't match."
    return email_policy.address_problem(email)         # a disposable address: Firebase isn't even asked


def firebase_sign_up(name: str, email: str, password: str, confirm: str) -> tuple[bool, str]:
    """Create an email/password account and have Firebase send its
    verification link. (ok, message). Never signs anyone in and grants
    nothing: the account gets in only through firebase_sign_in once
    Firebase reports the address verified, and admin rights come only from
    JT_ADMIN_EMAILS plus that verified address (access.account_is_admin)."""
    if _AUTH_CONFIG is None:
        return False, "Email sign-up is unavailable right now."
    problem = sign_up_problem(name, email, password, confirm)
    if problem:
        return False, problem
    email = email.strip()
    limiter = _guards()["firebase_signup"]
    wait = limiter.locked_for(email)
    if wait:
        return False, f"Too many sign-up attempts — try again in {int(wait // 60) + 1} min."
    limiter.failed(email)                               # every request that reaches Firebase counts
    try:
        user, sent = firebase_auth.sign_up_with_password(email, password, user_store.clean_display_name(name),
                                                         config=_AUTH_CONFIG,
                                                         may_send_verification=_may_send_verification)
    except firebase_auth.FirebaseAuthError as e:
        return False, str(e)
    if not sent:
        _guards()["verify_email"].succeeded(user.email)    # the next sign-in attempt asks for a link again
        print("[auth] email sign-up ok; verification email not sent", flush=True)
        return True, ("Your account was created, but the verification email couldn't be sent just now. "
                      "Sign in with your new password to have it sent again.")
    print("[auth] email sign-up ok; verification email sent", flush=True)
    return True, (f"Account created. We've sent a verification link to {user.email} — open it (check spam "
                  "too), then sign in here.")


def _login_mode(mode: str) -> None:
    """Switch the login card between "signin" and "signup". Only switches:
    nothing here signs in or starts Google."""
    st.session_state._login_mode = mode
    for k in ("_login_error", "_login_error_from", "_signup_error", "_login_notice"):
        st.session_state.pop(k, None)


def _signup_submit() -> None:
    """Sign-up form callback: both passwords are wiped from session state
    first; name and email stay on a mistake so they needn't be retyped."""
    name = st.session_state.get("signup_name", "")
    email = st.session_state.get("signup_email", "")
    password = st.session_state.get("signup_pw", "")
    confirm = st.session_state.get("signup_pw2", "")
    st.session_state.signup_pw = ""
    st.session_state.signup_pw2 = ""
    ok, msg = firebase_sign_up(name, email, password, confirm)
    if not ok:
        st.session_state._signup_error = msg
        return
    _login_mode("signin")
    st.session_state._login_notice = msg
    st.session_state.login_email = email.strip()          # ready to sign in once verified
    for k in ("signup_name", "signup_email"):
        st.session_state.pop(k, None)


def _login_submit() -> None:
    """Login form callback. It only checks that both fields are filled and
    marks an attempt pending. Firebase is asked later in this same run, by
    _run_login_attempt, AFTER the card has drawn "Signing in…" with the
    button disabled. A callback runs before anything can be drawn, so
    asking Firebase here left the person looking at an unchanged form."""
    for k in ("_login_error", "_login_error_from", "_login_notice"):
        st.session_state.pop(k, None)
    if not (st.session_state.get("login_email") or "").strip() or not st.session_state.get("login_pw"):
        st.session_state._login_error = "Enter your email address and password."
        st.session_state._login_error_from = "email"
        return
    st.session_state._login_pending = True


# a moment to read "Signed in successfully" before the dashboard replaces it
# (only with a real browser connected: AppTest needn't wait)
_SIGNED_IN_PAUSE_S = 0.6


def _browser_connected() -> bool:
    """A real browser is connected to this run (False under AppTest, whose
    runtime hands out a mock client). If Streamlit's internals change,
    assume a browser: that is what production needs."""
    try:
        from unittest.mock import Mock
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        ctx = get_script_run_ctx()
        client = st.runtime.get_instance().get_client(ctx.session_id) if ctx is not None else None
        return client is not None and not isinstance(client, Mock)
    except Exception:
        return True


def _run_login_attempt(status) -> None:
    """The sign-in itself, drawn into ``status`` while it runs: "Signing
    in…", then a green check once Firebase has confirmed the account, or the
    error. The email address stays in its field either way. The password
    lives in the widget's session state only for this one request and is
    wiped (before the field is drawn again) on the rerun that follows,
    success or not."""
    email = (st.session_state.get("login_email") or "").strip()
    password = st.session_state.get("login_pw") or ""
    status.html(f'<div class="login-busy" role="status" aria-live="polite">{_ms("progress_activity", "s20")}'
                '<div>Signing in…</div></div>')
    try:
        ok, msg = firebase_sign_in(email, password)
    finally:
        st.session_state._login_clear_pw = True
    if ok:
        status.html(f'<div class="login-ok" role="status" aria-live="polite">{_ms("check_circle", "s20")}'
                    '<div>Signed in successfully</div></div>')
        if _browser_connected():
            time.sleep(_SIGNED_IN_PAUSE_S)
    else:
        st.session_state._login_error = msg
        st.session_state._login_error_from = "email"     # keeps the email form open
    st.rerun()


def _login_owner_submit() -> None:
    """Break-glass owner password on the login page: wipe it first."""
    password = st.session_state.get("login_owner_pw", "")
    st.session_state.login_owner_pw = ""
    ok, msg = sign_in(password)
    if ok:
        st.session_state.pop("_signed_out", None)
        st.session_state.pop("_login_error", None)
    else:
        st.session_state._login_error = msg
        st.session_state._login_error_from = "owner"


def _login_google() -> None:
    for k in ("_signed_out", "_login_error", "_fb_link", "_fb_link_tried"):
        st.session_state.pop(k, None)
    if _GOOGLE["provider"]:
        st.login(_GOOGLE["provider"])
    else:
        st.login()


# Google's "G" mark for the Google button. Inline <svg> elements don't paint
# in this app's hosting (see the design-system note above), so it is drawn
# as a CSS background image instead.
_GOOGLE_G_SVG = brand_logos.GOOGLE_G_SVG
_GOOGLE_G_URI = "data:image/svg+xml;base64," + base64.b64encode(_GOOGLE_G_SVG.encode("utf-8")).decode("ascii")

_LOGIN_CSS = """
[data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"], .st-key-nav_toggle { display: none !important; }
[data-testid="stMainBlockContainer"], .block-container { max-width: 1520px !important; margin: 0 auto !important;
  padding: 24px clamp(24px, 4.5vw, 84px) !important; min-height: 100vh; display: flex; flex-direction: column; justify-content: center; }
/* the page's own block must not stretch, or there is nothing left to centre */
:is([data-testid="stMainBlockContainer"], .block-container) > [data-testid="stVerticalBlock"] { flex: 0 0 auto; }
.lp, .lp *:not(.ms), .st-key-login_card,
.st-key-login_card *:not([data-testid="stIconMaterial"]):not([data-testid="stExpanderIcon"]):not(.ms) { font-family: var(--font) !important; }

/* ── left: the product ── */
.lp-brand { display: flex; align-items: center; gap: 14px; animation: jt-rise .45s var(--ease) both; }
.lp-brand img { animation: jt-pop .5s var(--ease) both; }
.lp-brand img { width: 56px; height: 56px; border-radius: 16px; display: block;
  box-shadow: 0 12px 26px -12px var(--accent), 0 0 0 1px color-mix(in srgb, var(--accent) 28%, transparent); }
.lp-brand .n { font-size: 21px; line-height: 26px; font-weight: 800; letter-spacing: -.02em; color: var(--text); }
.lp-brand .s { font-size: 11.5px; line-height: 16px; font-weight: 600; letter-spacing: .16em; text-transform: uppercase;
  color: var(--muted); margin-top: 3px; }
.lp-hero { display: grid; grid-template-columns: minmax(0, 1fr) clamp(300px, 25vw, 400px); column-gap: clamp(20px, 2.4vw, 44px);
  grid-template-areas: "eyebrow eyebrow" "title title" "copy stage"; margin-top: 26px; }
.lp-hero > .eyebrow { grid-area: eyebrow; animation: jt-rise .45s var(--ease) .06s both; }
.stApp .lp-title { grid-area: title; margin: 10px 0 0 !important; font-size: clamp(46px, 3.9vw, 66px); line-height: 1.04; font-weight: 800;
  letter-spacing: -.035em; word-spacing: .02em; color: var(--text); animation: jt-rise .5s var(--ease) .12s both; }
.lp-title em { font-style: normal; background: linear-gradient(92deg, var(--accent), var(--accent-2));
  -webkit-background-clip: text; background-clip: text; color: transparent; }
.lp-copy { grid-area: copy; min-width: 0; animation: jt-rise .5s var(--ease) .2s both; }
.lp-sub { margin: 14px 0 0; max-width: 470px; font-size: clamp(16px, 1.05vw, 17.5px); line-height: 1.6; color: var(--text-2); }
.lp-benefits { list-style: none; margin: 20px 0 0 !important; padding: 0 !important; display: grid; gap: 12px; }
.lp-benefits li { display: flex; align-items: center; gap: 14px; margin: 0; }
.lp-benefits .ic { width: 42px; height: 42px; border-radius: 13px; display: grid; place-items: center; flex-shrink: 0;
  box-shadow: var(--hi); }
.lp-benefits .ic.violet { color: var(--accent-text); background: var(--accent-soft); }
.lp-benefits .ic.green { color: var(--green); background: var(--green-soft); }
.lp-benefits .ic.blue { color: color-mix(in srgb, var(--accent) 50%, var(--accent-2));
  background: color-mix(in srgb, var(--accent-soft) 55%, var(--green-soft)); }
.lp-benefits b { display: block; font-size: 16px; line-height: 22px; font-weight: 700; color: var(--text); }
.lp-benefits .t { display: block; font-size: 14px; line-height: 20px; color: var(--muted); }

/* the floating opportunity cards: decorative artwork, never data */
.lp-stage { grid-area: stage; position: relative; height: 352px; margin-top: 10px; }
@media (min-width: 1500px) { .lp-stage { height: 380px; } .lp-card.c2 { top: 126px !important; } .lp-card.c3 { top: 252px !important; } }
.lp-glow { position: absolute; inset: 0 -14% 4% -24%; border-radius: 50%; pointer-events: none;
  background: radial-gradient(closest-side, var(--glow-1), transparent 72%),
              radial-gradient(closest-side at 72% 78%, var(--glow-2), transparent 70%);
  animation: jt-glow-drift 16s ease-in-out infinite alternate; }
.lp-arc { position: absolute; width: 380px; height: 380px; top: -60px; left: -40px; border-radius: 50%; pointer-events: none;
  border: 1px solid color-mix(in srgb, var(--accent) 13%, transparent); }
.lp-card { position: absolute; width: clamp(300px, 24vw, 360px); }
.lp-card-in { position: relative; display: flex; align-items: center; gap: 12px; padding: 14px; overflow: hidden;
  border-radius: 18px; background: var(--oc-bg); border: 1px solid var(--oc-border); box-shadow: var(--oc-shadow);
  -webkit-backdrop-filter: blur(10px); backdrop-filter: blur(10px); will-change: transform;
  transition: transform .35s var(--ease), box-shadow .35s var(--ease), border-color .35s var(--ease); }
/* gloss: a soft top highlight, and a light sweep across the card on hover */
.lp-card-in::before { content: ""; position: absolute; inset: 0; border-radius: inherit; pointer-events: none;
  background: var(--oc-gloss); opacity: .7; transition: opacity .35s var(--ease); }
.lp-card-in::after { content: ""; position: absolute; top: 0; bottom: 0; left: -60%; width: 45%; pointer-events: none;
  background: linear-gradient(100deg, transparent, color-mix(in srgb, #fff 34%, transparent), transparent);
  transform: translateX(0) skewX(-14deg); opacity: 0; transition: transform .7s var(--ease), opacity .35s var(--ease); }
.lp-card:hover .lp-card-in::before { opacity: 1; }
.lp-card:hover .lp-card-in::after { opacity: .55; transform: translateX(420%) skewX(-14deg); }
.lp-card-in > * { position: relative; z-index: 1; }
/* four invisible quarters: the card leans 2–4 px toward the one under the cursor */
.lp-card .z { position: absolute; z-index: 2; width: 50%; height: 50%; }
.lp-card .z1 { top: 0; left: 0; } .lp-card .z2 { top: 0; right: 0; }
.lp-card .z3 { bottom: 0; left: 0; } .lp-card .z4 { bottom: 0; right: 0; }
.lp-card:hover { z-index: 3; }
.lp-card:hover .lp-card-in { transform: translate3d(0, -8px, 0) scale(1.03); box-shadow: var(--oc-shadow-hover);
  border-color: color-mix(in srgb, var(--accent) 38%, transparent); }
.lp-card .z1:hover ~ .lp-card-in { transform: translate3d(-3px, -9px, 0) scale(1.03); }
.lp-card .z2:hover ~ .lp-card-in { transform: translate3d(3px, -9px, 0) scale(1.03); }
.lp-card .z3:hover ~ .lp-card-in { transform: translate3d(-3px, -6px, 0) scale(1.03); }
.lp-card .z4:hover ~ .lp-card-in { transform: translate3d(3px, -6px, 0) scale(1.03); }
.lp-card .go { transition: transform .35s var(--ease), color .35s var(--ease); }
.lp-card:hover .go { transform: translateX(3px); color: var(--accent-text); }
/* company marks: drawn in CSS from local data (no image is fetched) */
.lp-card .bm { width: 42px; height: 42px; border-radius: 12px; flex-shrink: 0; display: grid; place-items: center; position: relative;
  background: linear-gradient(180deg, #ffffff, #f3f4f8); border: 1px solid rgba(15,23,42,.08);
  box-shadow: inset 0 1px 0 #fff, 0 6px 14px -8px rgba(15,23,42,.35); transition: transform .35s var(--ease), box-shadow .35s var(--ease); }
.lp-card:hover .bm { transform: scale(1.06); box-shadow: inset 0 1px 0 #fff, 0 10px 20px -10px var(--accent); }
.lp-card .bm img { width: 24px; height: 24px; display: block; object-fit: contain; }
.lp-card .bm.dark { border-color: rgba(255,255,255,.08);
  box-shadow: inset 0 1px 0 rgba(255,255,255,.12), 0 6px 14px -8px rgba(15,23,42,.6); }
.lp-card .tx { min-width: 0; flex: 1; }
.lp-card .t { font-size: 13.5px; line-height: 18px; font-weight: 700; color: var(--text); overflow: hidden;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
.lp-card .m { font-size: 12.5px; line-height: 17px; color: var(--muted); margin-top: 2px; }
.lp-card .tags { display: flex; gap: 6px; margin-top: 8px; }
.lp-card .tags .pill { font-size: 11px; line-height: 16px; padding: 1px 8px; }
.lp-card .go { color: var(--muted); flex-shrink: 0; }
.lp-card.c1 { top: 0; left: 14px; --tilt: -1.4deg;
  animation: jt-rise .8s var(--ease) .15s backwards, jt-float-s 9s ease-in-out 1s infinite; }
.lp-card.c2 { top: 116px; left: -28px;
  animation: jt-rise .8s var(--ease) .3s backwards, jt-float 10s ease-in-out 1.6s infinite; }
.lp-card.c3 { top: 232px; left: 0; --tilt: 1deg;
  animation: jt-rise .8s var(--ease) .45s backwards, jt-float-s 11s ease-in-out 2.2s infinite; }
.lp-chip { position: absolute; width: 56px; height: 56px; border-radius: 18px; display: grid; place-items: center;
  background: var(--oc-bg); border: 1px solid var(--oc-border); box-shadow: var(--oc-shadow); color: var(--accent-text);
  animation: jt-pop .7s var(--ease) .7s backwards, jt-float 8s ease-in-out 1.5s infinite; }
.lp-chip.k1 { top: -70px; right: 26px; }
.lp-chip.k2 { top: 160px; left: -78px; color: color-mix(in srgb, var(--accent) 50%, var(--accent-2)); animation-delay: .8s, 2.4s; }
.lp-chip.k3 { top: 336px; right: -14px; color: var(--accent-2); animation-delay: .9s, 3.1s; }
.lp-dot { position: absolute; width: 8px; height: 8px; border-radius: 50%; background: var(--accent); }
.lp-dot::after { content: ""; position: absolute; inset: 0; border-radius: inherit; background: inherit;
  animation: jt-pulse 3.6s ease-out infinite; }
.lp-dot.d1 { top: -50px; left: 96px; }
.lp-dot.d2 { top: 42px; right: -16px; background: var(--accent-2); }
.lp-dot.d2::after { animation-delay: 1.2s; }
.lp-dot.d3 { top: 216px; left: -36px; background: color-mix(in srgb, var(--accent) 50%, var(--accent-2)); }
.lp-dot.d3::after { animation-delay: 2.1s; }
.lp-dot.d4 { top: 362px; left: 92px; background: var(--accent-2); }
.lp-dot.d4::after { animation-delay: .6s; }

/* ── right: the sign-in card ── */
.st-key-login_card { max-width: 480px; margin: 0 0 0 auto !important; padding: 30px 34px 24px !important; gap: 12px !important;
  border: 1px solid var(--border) !important; border-radius: 28px !important; background: var(--surface) !important;
  box-shadow: var(--hi), var(--shadow-lg) !important; animation: jt-rise .5s var(--ease) .1s both; }
.lp-head img { width: 48px; height: 48px; border-radius: 14px; display: block;
  box-shadow: 0 12px 26px -12px var(--accent), 0 0 0 1px color-mix(in srgb, var(--accent) 28%, transparent); }
.stApp .lp-welcome { margin: 16px 0 6px !important; font-size: 32px; line-height: 1.1; font-weight: 800;
  letter-spacing: -.03em; color: var(--text); }
.lp-head p { margin: 0; max-width: 340px; font-size: 15px; line-height: 1.5; color: var(--muted); }
.st-key-login_card [data-testid="stWidgetLabel"] p { font-size: 14.5px !important; font-weight: 600 !important;
  color: var(--text) !important; }
.st-key-login_card :is([data-baseweb="input"], [data-testid="stTextInputRootElement"]) { min-height: 48px; border-radius: 14px !important;
  border: 1px solid var(--border-strong) !important; background: var(--surface-2) !important;
  transition: border-color var(--fast) var(--ease), box-shadow var(--fast) var(--ease), background var(--fast) var(--ease); }
.st-key-login_card :is([data-baseweb="input"], [data-testid="stTextInputRootElement"]):focus-within { border-color: var(--accent) !important; box-shadow: var(--ring) !important;
  background: var(--surface) !important; }
.st-key-login_card :is([data-baseweb="input"], [data-testid="stTextInputRootElement"]) > div, .st-key-login_card :is([data-baseweb="input"], [data-testid="stTextInputRootElement"]) input { background: transparent !important; }
.st-key-login_card input { font-size: 15px !important; }
:is(.st-key-login_email, .st-key-login_pw, .st-key-signup_name, .st-key-signup_email, .st-key-signup_pw, .st-key-signup_pw2) :is([data-baseweb="input"], [data-testid="stTextInputRootElement"])::before {
  font-family: 'Material Symbols Rounded' !important; font-feature-settings: 'liga'; font-weight: 400;
  font-size: 20px; width: 20px; overflow: hidden; white-space: nowrap; flex-shrink: 0;
  color: var(--muted); margin-left: 15px; align-self: center; line-height: 1; box-sizing: content-box; transition: color var(--fast); }
:is(.st-key-login_email, .st-key-login_pw, .st-key-signup_name, .st-key-signup_email, .st-key-signup_pw, .st-key-signup_pw2) :is([data-baseweb="input"], [data-testid="stTextInputRootElement"]):focus-within::before { color: var(--accent-text); }
.st-key-login_email :is([data-baseweb="input"], [data-testid="stTextInputRootElement"])::before { content: "mail"; }
.st-key-login_pw :is([data-baseweb="input"], [data-testid="stTextInputRootElement"])::before { content: "lock"; }
.st-key-signup_name :is([data-baseweb="input"], [data-testid="stTextInputRootElement"])::before { content: "person"; }
.st-key-signup_email :is([data-baseweb="input"], [data-testid="stTextInputRootElement"])::before { content: "mail"; }
:is(.st-key-signup_pw, .st-key-signup_pw2) :is([data-baseweb="input"], [data-testid="stTextInputRootElement"])::before { content: "lock"; }
.st-key-login_card [data-testid="stForm"] { gap: 10px; }
.st-key-btn_login_email [data-testid^="stBaseButton"], .st-key-btn_signup_submit [data-testid^="stBaseButton"] { min-height: 50px !important; border-radius: 14px !important; font-size: 16px !important;
  font-weight: 700 !important; margin-top: 6px; }
.st-key-btn_login_google [data-testid^="stBaseButton"] { min-height: 50px !important; border-radius: 14px !important; font-weight: 700 !important;
  background: var(--surface) !important; border: 1px solid var(--border-strong) !important; gap: 10px; }
.st-key-btn_login_email [data-testid^="stBaseButton"]:hover:not(:disabled),
.st-key-btn_signup_submit [data-testid^="stBaseButton"]:hover:not(:disabled) { transform: translateY(-1px); }
.st-key-btn_login_google [data-testid^="stBaseButton"]:hover { border-color: color-mix(in srgb, var(--accent) 35%, var(--border-strong)) !important;
  box-shadow: var(--btn2-shadow-hover) !important; transform: translateY(-1px); }
.st-key-btn_login_google [data-testid^="stBaseButton"]::before { content: ""; width: 20px; height: 20px; flex-shrink: 0;
  background: url("GOOGLE_G_URI") center / contain no-repeat; }
.lp-or { display: flex; align-items: center; gap: 14px; color: var(--muted); font-size: 12.5px; font-weight: 600;
  letter-spacing: .08em; margin: 2px 0; }
.lp-or::before, .lp-or::after { content: ""; flex: 1; height: 1px; background: var(--border); }
.st-key-lp_links { justify-content: space-between; align-items: center; flex-wrap: wrap; row-gap: 4px; margin-top: 4px; }
.st-key-btn_login_signup [data-testid^="stBaseButton"], .st-key-btn_login_signin [data-testid^="stBaseButton"],
.st-key-lp_forgot [data-testid^="stBaseButton"] {
  min-height: 0 !important; padding: 2px 0 !important; border: 0 !important; background: transparent !important; box-shadow: none !important; }
.st-key-btn_login_signup p, .st-key-btn_login_signin p, .st-key-lp_forgot p { font-size: 14px !important; font-weight: 500 !important; color: var(--muted) !important; }
.st-key-btn_login_signup strong, .st-key-btn_login_signin strong { color: var(--accent-text) !important; font-weight: 700 !important; }
.st-key-lp_forgot p { color: var(--accent-text) !important; font-weight: 600 !important; }
.st-key-lp_forgot [data-testid="stIconMaterial"] { display: none; }
.st-key-btn_login_signup [data-testid^="stBaseButton"]:hover strong,
.st-key-btn_login_signin [data-testid^="stBaseButton"]:hover strong, .st-key-lp_forgot [data-testid^="stBaseButton"]:hover p {
  text-decoration: underline; text-underline-offset: 3px; }
.lp-help { margin: 0; font-family: var(--font) !important; font-size: 14px; line-height: 1.55; color: var(--text-2);
  max-width: 280px; }   /* the popover renders outside the card, so it needs the font itself */
.st-key-login_card [data-testid="stExpander"] details { background: var(--surface-2) !important; }
.st-key-login_card [data-testid="stExpander"] summary p { font-size: 13.5px !important; color: var(--text-2) !important; }
.login-err { display: flex; gap: 9px; align-items: flex-start; padding: 11px 13px; border-radius: 12px; font-size: 13.5px; line-height: 1.5;
  color: var(--text); background: color-mix(in srgb, var(--red) 9%, transparent); border: 1px solid color-mix(in srgb, var(--red) 35%, var(--border)); }
.login-err .ms { color: var(--red); }
.login-ok { display: flex; gap: 9px; align-items: flex-start; padding: 11px 13px; border-radius: 12px; font-size: 13.5px; line-height: 1.5;
  color: var(--text); background: color-mix(in srgb, var(--green) 9%, transparent); border: 1px solid color-mix(in srgb, var(--green) 35%, var(--border)); }
.login-ok .ms { color: var(--green); }
.login-busy { display: flex; gap: 9px; align-items: center; padding: 11px 13px; border-radius: 12px; font-size: 13.5px; line-height: 1.5;
  color: var(--text); background: color-mix(in srgb, var(--accent) 7%, transparent); border: 1px solid color-mix(in srgb, var(--accent) 28%, var(--border)); }
.login-busy .ms { color: var(--accent-text); animation: jt-spin 1s linear infinite; }
@media (prefers-reduced-motion: reduce) { .login-busy .ms { animation: none; } }
.login-off { display: flex; gap: 9px; align-items: flex-start; color: var(--text-2); font-size: 13.5px; line-height: 1.5; margin: 0; }

/* tablet: a smaller stage */
@media (max-width: 1180px) {
  .lp-hero { grid-template-columns: minmax(0, 1fr) 256px; }
  .lp-card { width: 256px; } .lp-card .tags .pill:nth-child(2) { display: none; }
  .stApp .lp-title { font-size: 44px; }
  .lp-chip.k2, .lp-chip.k3, .lp-dot.d3 { display: none; }
  .lp-card.c1 { left: 6px; } .lp-card.c2 { left: -4px; } .lp-card.c3 { left: 4px; }
}
/* tablet: the hero (with its cards) above a full-size sign-in card, instead of a squeezed side column */
@media (max-width: 960px) {
  [data-testid="stHorizontalBlock"]:has(.st-key-login_card) { flex-direction: column !important; flex-wrap: nowrap !important; gap: 28px !important; }
  [data-testid="stHorizontalBlock"]:has(.st-key-login_card) > [data-testid="stColumn"] { width: 100% !important; flex: 1 1 auto !important; min-width: 0 !important; }
  [data-testid="stMainBlockContainer"], .block-container { justify-content: flex-start; padding-top: 32px !important; }
  .st-key-login_card { margin: 0 auto !important; width: 100%; }
  .lp { overflow-x: clip; }                  /* the decorative arc must never widen the page */
  .lp-brand, .lp-hero { max-width: 640px; margin-left: auto; margin-right: auto; }
  .lp-hero { grid-template-columns: minmax(0, 1fr) 256px; margin-top: 22px; }
  .lp-benefits { display: none; }
  .stApp .lp-title { font-size: 40px; }
}
/* phones: the form is what matters — the artwork steps aside */
@media (max-width: 640px) {
  .lp-hero { grid-template-columns: minmax(0, 1fr); grid-template-areas: "eyebrow" "title" "copy"; }
  .lp-stage { display: none; }
}
@media (max-width: 640px) {
  [data-testid="stMainBlockContainer"], .block-container { padding: 20px 16px 28px !important; min-height: 0; }
  .lp-hero { margin-top: 20px; } .lp-hero > .eyebrow, .lp-sub { display: none; }
  .stApp .lp-title { font-size: 30px; margin-top: 0 !important; }
  .st-key-login_card { margin: 8px auto 0 !important; padding: 26px 20px 22px !important; border-radius: 22px !important; }
  .lp-head img { display: none; } .stApp .lp-welcome { margin-top: 0 !important; font-size: 28px; }
}
@media (prefers-reduced-motion: reduce) {
  .lp-card, .lp-chip, .lp-glow, .lp-dot::after, .st-key-login_card, .lp-brand, .lp-brand img, .lp-hero > .eyebrow,
  .lp-title, .lp-copy { animation: none !important; }
  .lp-card-in, .lp-card .bm, .lp-card .go, .lp-card-in::before, .lp-card-in::after { transition: none !important; }
  .lp-card-in::after { display: none; }
  .lp-card:hover .lp-card-in, .lp-card .z:hover ~ .lp-card-in { transform: none !important; }
  .lp-card.c1 { transform: rotate(-1.4deg); } .lp-card.c3 { transform: rotate(1deg); }
}
""".replace("GOOGLE_G_URI", _GOOGLE_G_URI)

# decorative opportunity cards for the login artwork — fixed example content,
# not job data: nothing here is read from the tracker or any account
_LOGIN_CARDS = (
    ("c1", "Google", "Software Engineer Intern", "Bengaluru", (("on", "New"), ("entry", "Internship"))),
    ("c2", "Microsoft", "SDE – New Grad", "Hyderabad", (("fresher", "Fresher"), ("entry", "Full-time"))),
    ("c3", "Amazon", "Software Development Engineer", "Bengaluru", (("fresher", "Fresher"), ("entry", "Full-time"))),
)


def _login_mark(company: str) -> str:
    """An example card's company mark: the same local SVG as on job cards."""
    key = brand_logos.brand_key(company)
    tile = brand_logos.brand_tile(key)
    return (f'<span class="bm {key}{" dark" if tile.lower() != "#ffffff" else ""}" style="background:{tile}">'
            f'<img src="{brand_logos.brand_uri(key)}" alt=""></span>')


def _login_art() -> str:
    """The left side of the login page: brand, headline, benefits and the
    floating cards. Static and generic — the same for every visitor."""
    logo = _logo_data_uri()
    cards = "".join(
        f'<div class="lp-card {cls}"><span class="z z1"></span><span class="z z2"></span><span class="z z3"></span>'
        '<span class="z z4"></span>'
        f'<div class="lp-card-in">{_login_mark(company)}'
        f'<div class="tx"><div class="t">{escape(title)}</div><div class="m">{escape(company)} · {escape(city)}</div>'
        '<div class="tags">' + "".join(f'<span class="pill {k}">{"<i></i>" if k == "on" else ""}{escape(v)}</span>'
                                       for k, v in tags) + "</div></div>"
        f'<span class="go">{_ms("chevron_right", "s20")}</span></div></div>'
        for cls, company, title, city, tags in _LOGIN_CARDS)
    benefits = [("violet", "search", "Fresh opportunities", "From top companies"),
                ("green", "bolt", "Stay ahead", "Get notified early"),
                ("blue", "apartment", "Track what matters", "Follow your preferred companies")]
    return ('<div class="lp">'
            '<div class="lp-brand">' + (f'<img src="{logo}" alt="">' if logo else "")
            + '<div><div class="n">Fresher Job Tracker</div><div class="s">Fresher opportunities</div></div></div>'
            '<div class="lp-hero">'
            '<div class="eyebrow sparked">Discover jobs</div>'
            '<h1 class="lp-title">Discover your next<br><em>opportunity.</em></h1>'
            '<div class="lp-copy"><p class="lp-sub">Fresher and entry-level roles in India, found across the companies '
            'you track — each one parsed from its own posting before it reaches you.</p>'
            '<ul class="lp-benefits">' + "".join(
                f'<li><span class="ic {tone}">{_ms(icon, "s20")}</span><div><b>{escape(head)}</b>'
                f'<span class="t">{escape(sub)}</span></div></li>' for tone, icon, head, sub in benefits)
            + '</ul></div>'
            '<div class="lp-stage" aria-hidden="true"><span class="lp-glow"></span><span class="lp-arc"></span>'
            '<span class="lp-dot d1"></span><span class="lp-dot d2"></span><span class="lp-dot d3"></span>'
            '<span class="lp-dot d4"></span>'
            f'<span class="lp-chip k1">{_ms("school", "s24")}</span>'
            f'<span class="lp-chip k2">{_ms("apartment", "s24")}</span>'
            f'<span class="lp-chip k3">{_ms("trending_up", "s24")}</span>'
            + cards + '</div></div></div>')


def render_login_page() -> None:
    """The only thing a visitor ever sees: generic artwork on the left, the
    sign-in card on the right — or, after "Sign up", the email/password
    registration form. Each control calls its own handler (_login_submit,
    _signup_submit, _login_mode, _login_google, _login_owner_submit); only
    "Continue with Google" calls _login_google. Messages here are
    for people, not operators: what exactly is misconfigured goes to the
    server log."""
    st.html(f"<style>{_LOGIN_CSS}</style>")
    email_ok = _AUTH_CONFIG is not None
    # the password of a finished attempt is wiped before its field is drawn
    # again (a widget's value can't be changed once it is on the page)
    if st.session_state.pop("_login_clear_pw", False):
        st.session_state.login_pw = ""
    # popped at once: a run interrupted mid-attempt never leaves the form locked
    attempt = email_ok and bool(st.session_state.pop("_login_pending", False))
    # a session cookie that didn't sign anyone in (signed out, expired, ended)
    if st.session_state.pop("_clear_session_cookie", False) or _known_session_token():
        _write_session_cookie("")
    art, side = st.columns([1.6, 1], gap="large", vertical_alignment="center")
    with art:
        st.html(_login_art())
    with side, st.container(key="login_card"):
        logo = _logo_data_uri()
        # "Sign up" opens the email/password registration form; only the
        # "Continue with Google" button ever starts Google sign-in
        signup = email_ok and st.session_state.get("_login_mode") == "signup"
        head, sub = (("Create your account", "Sign up with your email to start discovering fresher opportunities.")
                     if signup else ("Welcome back", "Sign in to continue discovering fresher opportunities."))
        st.html('<div class="lp-head">' + (f'<img src="{logo}" alt="">' if logo else "")
                + f'<h2 class="lp-welcome">{head}</h2><p>{sub}</p></div>')
        notice = "" if signup else st.session_state.get("_login_notice")
        if notice:
            st.html(f'<div class="login-ok" role="status">{_ms("mark_email_read", "s20")}<div>{escape(notice)}</div></div>')
        err = "" if attempt else (st.session_state.get("_signup_error") if signup else
                                  st.session_state.get("_login_error") or (recent_google_failure() if _GOOGLE else ""))
        if err:
            st.html(f'<div class="login-err" role="alert">{_ms("error", "s20")}<div>{escape(err)}</div></div>')
        login_status = st.empty()           # "Signing in…", then the outcome
        if signup:
            with st.form("signup_form", clear_on_submit=False, border=False):
                st.text_input("Full name", key="signup_name", autocomplete="name", placeholder="Your full name",
                              max_chars=user_store.DISPLAY_NAME_MAX)
                st.text_input("Email address", key="signup_email", autocomplete="email", placeholder="you@example.com")
                st.text_input("Password", type="password", key="signup_pw", autocomplete="new-password",
                              placeholder=f"At least {SIGN_UP_PASSWORD_MIN} characters")
                st.text_input("Confirm password", type="password", key="signup_pw2", autocomplete="new-password",
                              placeholder="Re-enter your password")
                st.form_submit_button("Create account", key="btn_signup_submit", type="primary",
                                      icon=":material/arrow_forward:", icon_position="right",
                                      use_container_width=True, on_click=_signup_submit)
        elif email_ok:
            # the fields keep what was typed: the email survives a failed
            # attempt, and the password is wiped only once the attempt is over
            with st.form("login_form", clear_on_submit=False, border=False):
                st.text_input("Email address", key="login_email", autocomplete="email", placeholder="you@example.com")
                st.text_input("Password", type="password", key="login_pw", autocomplete="current-password",
                              placeholder="Enter your password")
                st.form_submit_button("Sign in", key="btn_login_email", type="primary", icon=":material/arrow_forward:",
                                      icon_position="right", use_container_width=True, on_click=_login_submit,
                                      disabled=attempt)
        if _GOOGLE:
            if email_ok:
                st.html('<div class="lp-or">OR</div>')
            st.button("Continue with Google", key="btn_login_google", type="secondary", use_container_width=True,
                      on_click=_login_google, disabled=attempt)
        elif _GOOGLE_PROBLEM:
            st.html(f'<p class="login-off">{_ms("info", "s20")}<span>Google sign-in is unavailable right now.</span></p>')
        if not email_ok and not _GOOGLE:
            st.html(f'<p class="login-off">{_ms("info", "s20")}<span>Sign-in is unavailable right now. '
                    "Please try again later.</span></p>")
        # break-glass, only until admins come from sign-in (JT_ADMIN_EMAILS):
        # the owner can get in even if Google or Firebase misbehaves
        if owner_password_enabled() and not signup:
            with st.expander("Trouble signing in? Use the owner password", icon=":material/key:",
                             expanded=st.session_state.get("_login_error_from") == "owner"):
                with st.form("login_owner_form", clear_on_submit=True, border=False):
                    st.text_input("Owner password", type="password", key="login_owner_pw",
                                  autocomplete="current-password")
                    st.form_submit_button("Sign in as owner", key="btn_login_owner", icon=":material/admin_panel_settings:",
                                          use_container_width=True, on_click=_login_owner_submit)
        if email_ok:
            with st.container(key="lp_links", horizontal=True):
                if signup:
                    st.button("Already have an account? **Sign in**", key="btn_login_signin", type="tertiary",
                              on_click=_login_mode, args=("signin",))
                else:
                    st.button("Don't have an account? **Sign up**", key="btn_login_signup", type="tertiary",
                              on_click=_login_mode, args=("signup",))
                    with st.popover("Forgot password?", key="lp_forgot", type="tertiary"):
                        st.html('<p class="lp-help">Email-and-password accounts are managed by the workspace admin — '
                                'ask them to reset your password'
                                + (", or continue with Google instead." if _GOOGLE else ".") + '</p>')
    if attempt:
        # everything above (the disabled button included) is already on the
        # screen: only now is Firebase asked
        _run_login_attempt(login_status)


_AUTH_CONFIG, _AUTH_PROBLEM, _AUTH_PRESENT = _firebase_config()
_promote_admin_setting()
# Fail closed: once ANY part of sign-in is set up — a Firebase setting,
# Google's [auth] section or the admin list — nothing of the app is shown
# without signing in. A typo in one secret makes a sign-in method
# unavailable; it never opens the app to everyone.
AUTH_GATE = (_AUTH_CONFIG is not None or _AUTH_PRESENT or isinstance(_secrets_dict().get("auth"), dict)
             or admins_configured())
_GOOGLE, _GOOGLE_PROBLEM = _google_setup() if AUTH_GATE else (None, "")
if AUTH_GATE and _AUTH_CONFIG is None:
    log.warning("email sign-in unavailable: %s", _AUTH_PROBLEM)       # names settings, never values
if _GOOGLE_PROBLEM:
    log.warning("Google sign-in unavailable: %s", _GOOGLE_PROBLEM)
ACCOUNT: dict | None = None
if AUTH_GATE:
    ACCOUNT = resolve_account(_GOOGLE, _AUTH_CONFIG)
    if ACCOUNT is None and st.session_state.pop("_probe_pending", False):
        # the browser is still reporting its session (a moment after a
        # reload): not the login page yet — the report runs the app again
        st.html(f'<div class="jt-skel" role="status" aria-live="polite"><p>{_ms("progress_activity", "s16")}'
                'Checking your sign-in…</p></div>')
        st.stop()
    if ACCOUNT is None:
        render_login_page()
        st.stop()
    for _k in ("login_pw", "_login_clear_pw", "_login_pending"):     # nothing of the sign-in form outlives it
        st.session_state.pop(_k, None)
else:
    st.session_state.pop(ACCOUNT_KEY, None)

if st.query_params.get("diag") == "auth" and is_owner():
    render_auth_diag()
    st.stop()

# ── personal data ─────────────────────────────────────────────────────────────
# USER is this viewer's own data, bound to the Firebase UID of the account
# the gate resolved on the server — never to anything from the URL, a form
# or the browser. None when per-user data isn't set up (the app then works
# as before) or unavailable (USER_PROBLEM says why; nothing falls back to
# shared data for a member).
USER: user_store.UserData | None = None
USER_PROBLEM = ""
USER_PROBLEM_DETAIL = ""          # for admins only: a fixed config message or an error class name
_STORE, _STORE_PROBLEM = _user_store() if AUTH_GATE else (None, "")
if _STORE_PROBLEM:
    log.warning("personal data unavailable: %s", _STORE_PROBLEM)
if ACCOUNT and ACCOUNT.get("provider") in ("google.com", "password") and (_STORE is not None or _STORE_PROBLEM):
    if _STORE is None:
        USER_PROBLEM, USER_PROBLEM_DETAIL = user_store.UNAVAILABLE, _STORE_PROBLEM
    elif ACCOUNT.get("account_deleted"):
        USER_PROBLEM = "Your account was removed, so personal data is off for this session."
    elif not user_store.valid_uid(ACCOUNT.get("uid")):
        USER_PROBLEM = ("Your personal data needs a linked Firebase account — sign out, then continue with "
                        "Google again.")
    else:
        try:
            USER = _STORE.for_uid(ACCOUNT["uid"])
            if st.session_state.get("_profile_uid") != USER.uid:      # once per sign-in
                _profile, _created = USER.ensure_profile(ACCOUNT.get("email") or "", ACCOUNT.get("name") or "",
                                                         ACCOUNT.get("email_verified") is True)
                st.session_state._profile_uid = USER.uid
                if _created:     # nothing to read back: a new profile follows and dismissed nothing
                    st.session_state._pcache = {"uid": USER.uid, "at": time.time(), "profile": _profile,
                                                "dismissed": set(), "watchlist": set()}
            # a fresh email sign-in: a browser session, so a reload keeps it
            if st.session_state.pop("_start_session", False) and ACCOUNT.get("provider") == "password":
                _cookie, st.session_state._session_key = USER.start_session(
                    ACCOUNT.get("email") or "", ACCOUNT.get("name") or "", ACCOUNT.get("email_verified") is True)
                _write_session_cookie(_cookie)
        except user_store.UserStoreError as e:
            USER, USER_PROBLEM, USER_PROBLEM_DETAIL = None, str(e), e.kind


# This viewer's own data, read once and kept in THIS browser session: clicks
# never wait for Firestore for data they already have. Once the copy is a
# minute old, a click still answers from it at once while a background read
# fetches the next one (adopted on a later click), so idling for a minute no
# longer freezes the next click behind three Firestore round trips. Every
# change made here refreshes it (that read waits: the change must show); a
# change made on another device shows up within about a minute.
_PERSONAL_TTL_S = 60
_PERSONAL_REFRESH_GIVEUP_S = 30          # a background read that never finished is retried


def _personal_snapshot() -> dict:
    cached = st.session_state.get("_pcache")
    if not (isinstance(cached, dict) and cached.get("uid") == USER.uid):
        st.session_state.pop("_pcache_next", None)
        snap = {"uid": USER.uid, "at": time.time(), **USER.snapshot()}       # nothing to show yet: wait once
        st.session_state._pcache = snap
        return snap
    nxt = st.session_state.get("_pcache_next")
    if isinstance(nxt, dict) and (nxt.get("done") or time.time() - nxt.get("started", 0) > _PERSONAL_REFRESH_GIVEUP_S):
        st.session_state.pop("_pcache_next", None)
        # only a read of this account that began after the current copy was read
        if (nxt.get("done") and isinstance(nxt.get("snap"), dict) and nxt.get("uid") == USER.uid
                and nxt.get("started", 0) >= cached.get("at", 0)):
            cached = {"uid": USER.uid, "at": nxt["started"], **nxt["snap"]}
            st.session_state._pcache = cached
        nxt = None
    if nxt is None and time.time() - cached.get("at", 0) >= _PERSONAL_TTL_S:
        holder = {"uid": USER.uid, "started": time.time(), "done": False}
        st.session_state._pcache_next = holder

        def _refresh(user=USER, holder=holder):     # no Streamlit calls in here: plain data only
            try:
                holder["snap"] = user.snapshot()
            except Exception:                         # keep the current copy; the next click retries
                holder["snap"] = None
            holder["done"] = True
        threading.Thread(target=_refresh, daemon=True).start()
    return cached


def personal_changed() -> None:
    """Call after any write to this viewer's data: the next read is fresh
    (and a background read begun before the write is never adopted)."""
    st.session_state.pop("_pcache", None)
    st.session_state.pop("_pcache_next", None)


def _personal_patched(profile: dict | None = None, dismissed_add=(), dismissed_remove=(),
                      watch_add=(), watch_remove=()) -> None:
    """After a write whose outcome is known exactly (one profile field, a
    dismissal, a follow): apply it to this session's copy instead of reading
    everything back — a full re-read is three Firestore round trips that the
    next click had to wait for. A background read begun before the write is
    dropped (it could bring back the old value); the copy keeps its age, so
    a change made on another device still arrives within a minute. Without
    a usable copy this falls back to a full re-read."""
    cached = st.session_state.get("_pcache")
    if not (USER is not None and isinstance(cached, dict) and cached.get("uid") == USER.uid
            and isinstance(cached.get("profile"), dict)):
        personal_changed()
        return
    st.session_state.pop("_pcache_next", None)
    st.session_state._pcache = {
        **cached,
        "profile": {**cached["profile"], **(profile or {})},
        "dismissed": (set(cached.get("dismissed") or ()) | set(dismissed_add)) - set(dismissed_remove),
        "watchlist": (set(cached.get("watchlist") or ()) | set(watch_add)) - set(watch_remove),
    }


_SKELETON = ('<div class="jt-skel" role="status" aria-live="polite"><p>' + _ms("progress_activity", "s16")
             + 'Loading the latest jobs…</p><i class="hero"></i><div class="row"><i></i><i></i><i></i><i></i></div>'
               '<i class="line"></i><i class="line"></i></div>')
# Only a cold start waits: the first read of the shared files in a new server
# process, or this sign-in's first read of its own data. A skeleton of the
# page shows meanwhile (every later click is answered from the copies at once,
# so it never appears for ordinary navigation).
_cold_load = None
if not _shared_data().has_data() or (USER is not None and (st.session_state.get("_pcache") or {}).get("uid") != USER.uid):
    _cold_load = st.empty()
    _cold_load.html(_SKELETON)

ME: dict | None = None
if USER is not None:
    try:
        ME = _personal_snapshot()
    except user_store.UserStoreError as _e:
        USER, USER_PROBLEM, USER_PROBLEM_DETAIL = None, str(_e), _e.kind


# ── 48-hour inactivity sign-out ───────────────────────────────────────────────
# Every run of a signed-in session is something the person did (a click, a
# page, a form: this app never reruns on a timer), so each one counts as
# activity. The last activity is kept on the SERVER: in this session's state
# and, for a new browser session, on the person's own profile
# (its profile field last_active_at, written with the server's clock at most every
# _ACTIVITY_SAVE_S). Nothing the browser sends is trusted for it. A sign-in
# with no use for INACTIVITY_LIMIT_S is ended exactly like Sign out
# (account_sign_out: the session is emptied and Google's sign-in cookie
# cleared), whichever way it was restored:
#   * a tab left open: this session's own _last_active;
#   * a new tab or browser with Google's 30-day cookie: the profile's
#     last_active_at (a sign-in Google issued later than that — the ID
#     token's iat, read on the server — counts as activity too);
#   * an email sign-in lives only in this session; its expiry moves on with
#     each use (firebase_auth.session_user enforces it).
# Without per-user data there is no profile to record into: a restored
# Google sign-in is then judged only by its own session.
_ACTIVITY_SAVE_S = 600
INACTIVE_NOTICE = "You were signed out after 2 days without activity. Sign in again to continue."


def _stamp_seconds(value) -> float | None:
    """Seconds since the epoch for a stored "2026-10-15T08:00:00Z" stamp."""
    if not isinstance(value, str) or not value:
        return None
    dt = _parse_iso(value.replace("Z", "+00:00"))
    return dt.timestamp() if dt else None


def _google_signed_in_at(now: float) -> float | None:
    """When Google issued this sign-in (its ID token's iat), or None."""
    age = firebase_auth.token_age(_google_id_token(), now=now)
    return now - age if isinstance(age, (int, float)) and age >= 0 else None


def last_activity(account: dict, profile: dict | None, now: float) -> float | None:
    """The latest moment this person is known to have used the app (or
    signed in), from server-side records only. None when there is no record
    of activity at all (no per-user data, or none recorded yet): inactivity
    can't be proven then, and a Google sign-in's age alone must never act
    as a fixed limit counted from sign-in."""
    records = [t for t in (st.session_state.get("_last_active"), _stamp_seconds((profile or {}).get("last_active_at")))
               if isinstance(t, (int, float)) and t <= now + 60]
    if not records:
        return None
    signed_in = _google_signed_in_at(now) if account.get("provider") == "google.com" else None
    return max(records + ([signed_in] if signed_in is not None and signed_in <= now + 60 else []))


def enforce_inactivity(account: dict, now: float | None = None) -> bool:
    """End the sign-in after INACTIVITY_LIMIT_S without use (True), or
    record this run as activity (False)."""
    now = time.time() if now is None else now
    last = last_activity(account, ME["profile"] if ME is not None else None, now)
    if last is not None and now - last >= INACTIVITY_LIMIT_S:
        auth_event(f"{account.get('provider')} sign-in ended after {INACTIVITY_LIMIT_S // 3600} h without activity")
        google = account_sign_out()
        st.session_state._login_notice = INACTIVE_NOTICE
        if not google:
            st.rerun()
        st.stop()
        return True
    st.session_state._last_active = now
    fb = st.session_state.get(firebase_auth.SESSION_KEY)
    if isinstance(fb, dict):                     # an email sign-in lasts while it is used
        st.session_state[firebase_auth.SESSION_KEY] = {**fb, "expires_at": now + INACTIVITY_LIMIT_S}
    if USER is not None and now - st.session_state.get("_activity_saved", 0) >= _ACTIVITY_SAVE_S:
        try:
            USER.record_activity(now)
            if st.session_state.get("_session_key"):
                USER.touch_session(st.session_state._session_key, now)
            st.session_state._activity_saved = now
        except user_store.UserStoreError:
            pass                                 # retried on the next run; never blocks the page
    return False


if ACCOUNT and ACCOUNT.get("provider") in ("google.com", "password"):
    enforce_inactivity(ACCOUNT)

# The name shown for this account: the one the person chose (saved in their
# own profile as preferred_name), else the sign-in provider's name
# as before. Display only: identity, role and data stay keyed by the UID.
CHOSEN_NAME = user_store.display_name_from(ME["profile"]) if ME is not None else ""
# ...and the profile avatar they picked ("" = their initial)
CHOSEN_AVATAR = user_store.avatar_from(ME["profile"]) if ME is not None else ""

# Roles, decided on the server once per run (the real checks still happen in
# every protected operation). A member never gets the admin's dashboard; the
# legacy no-sign-in mode keeps its old behaviour.
IS_ADMIN = is_owner()
MEMBER_VIEW = bool(AUTH_GATE and ACCOUNT and not IS_ADMIN)
ADMIN_PAGES = {"monitoring", "email"}
PERSONAL_PAGES = {"alerts", "preferences", "onboarding"}

# A member who opens an admin page by its address lands on Home: nothing of
# it is drawn (the operations themselves stay admin-checked regardless).
if MEMBER_VIEW and (st.session_state.page in ADMIN_PAGES or st.session_state.get("company_view") == "add"):
    st.session_state.page, st.session_state.company_view = "home", None
if st.session_state.page in PERSONAL_PAGES and USER is None:
    st.session_state.page = "home"
# first sign-in: a short setup before anything else
ONBOARDING = (ME is not None and (ME["profile"].get("onboarding") or {}).get("status") == user_store.ONBOARDING_PENDING)
if ONBOARDING:
    st.session_state.page = "onboarding"
elif st.session_state.page == "onboarding":
    st.session_state.page = "home"


def account_label(account: dict) -> str:
    return (account.get("name") or account.get("email") or "Signed in").strip()


def first_name(account: dict) -> str:
    """The compact header's name: "Manoj" for "Manoj Anumolu". An account
    without a name (email/password) gets the start of its address."""
    name = (account.get("name") or "").strip()
    if name:
        return name.split()[0]
    local = (account.get("email") or "").split("@")[0]
    word = next((w for w in re.split(r"[._+\-\d]+", local) if w), "")
    return word[:1].upper() + word[1:] if word else "Account"


# the signed-in account as it is SHOWN (with the chosen name); ACCOUNT itself
# — what sign-in and the role checks use — is never changed
SHOWN_ACCOUNT = {**ACCOUNT, "name": CHOSEN_NAME} if ACCOUNT and CHOSEN_NAME else ACCOUNT


def _me_initial() -> str:
    return next((ch for ch in first_name(SHOWN_ACCOUNT) if ch.isalnum()), "?").upper() if ACCOUNT else "?"


def _me_avatar(cls: str = "acct-av") -> str:
    """The signed-in person's avatar: the mark they picked on the Account
    page, else their initial. It is about the viewer — never the employer
    of a job on screen (those use _avatar)."""
    if CHOSEN_AVATAR:
        return (f'<span class="{cls} av-mark" style="background:{brand_logos.brand_tile(CHOSEN_AVATAR)}" '
                f'aria-hidden="true"><img src="{brand_logos.brand_uri(CHOSEN_AVATAR)}" alt=""></span>')
    return f'<span class="{cls}" aria-hidden="true">{escape(_me_initial())}</span>'


# ── data ──────────────────────────────────────────────────────────────────────
companies: list[dict] = []
for _i, _c in enumerate(c for c in _load_synced("data/companies.json", []) if isinstance(c, dict)):
    # every company needs a unique id for its widgets/links; a missing or
    # repeated id (hand-edited file) gets one for this page view only — the
    # file itself is never rewritten here
    _c = dict(_c)
    if not _c.get("id") or any(str(x.get("id")) == str(_c.get("id")) for x in companies):
        _c["id"] = f"_row{_i}"
    companies.append(_c)
settings: dict = _load_synced("data/settings.json", {})
SCAN_HOURS = scan_schedule.interval_from(settings)        # the admin's global interval (1, 3, 6 or 12 h)
_logo_entries = company_logos.entries(_load_synced("data/logos.json", {}))
_FOUND_LOGOS = {(c.get("name") or "").strip().lower(): _logo_entries[c["id"]]
                for c in companies if c.get("id") in _logo_entries and (c.get("name") or "").strip()}
# seen_jobs.json is also the scraper's dedup history: jobs are dismissed
# (hidden), never deleted, or the scraper would email them again
all_records: list[dict] = []
_seen_keys: set[str] = set()
for _j in _load_synced("data/seen_jobs.json", []):
    # a duplicated record (same id + URL) would give two widgets the same
    # key and crash the page — show each job once
    if isinstance(_j, dict) and job_key(_j) not in _seen_keys:
        _seen_keys.add(job_key(_j))
        all_records.append(_j)
if _cold_load is not None:
    _cold_load.empty()
# With personal data, "dismissed" means dismissed BY THIS VIEWER (an admin's
# view also includes the shared dismissals that today's alert emails follow);
# one person's dismissal never hides a job from anyone else.
_HIDDEN: set[str] | None = set(ME["dismissed"]) if ME is not None else None
if _HIDDEN is not None and IS_ADMIN:
    _HIDDEN |= {job_key(j) for j in all_records if j.get("dismissed")}


def is_dismissed(j: dict) -> bool:
    return job_key(j) in _HIDDEN if _HIDDEN is not None else bool(j.get("dismissed"))


_all_active_jobs: list[dict] = (visible_jobs(all_records) if _HIDDEN is None              # newest-first
                                else [j for j in reversed(all_records) if isinstance(j, dict) and not is_dismissed(j)])

# What this viewer asked to see. Saved preferences are the DEFAULTS of the
# job lists (the viewer can change them while browsing, see filter_jobs);
# the same model applies to admins and members.
# Company scope, the same as the alert emails (job_filters.for_person):
# "all companies" (watch_all) covers every company; otherwise only the
# companies this person follows — and a new account (its profile is created
# with watch_all False by user_store.ensure_profile) follows none, so its
# Home has nothing yet. Until 2026-10-15 an EMPTY follow list was read here
# as "every company", so every listing the shared scanner found landed on
# each new member's Home. The Jobs page stays the place to browse every
# shared listing: its company filter starts from the follows, and an empty
# selection there means any company.
_VIEW: dict | None = user_store.view_from(ME["profile"], ME["watchlist"]) if ME is not None else None


def my_defaults() -> dict | None:
    """The job-list filters this viewer saved: {"locations", "families",
    "experience", "companies"}: None = every company ("all companies"
    chosen), else the followed company IDs — [] when none is followed."""
    if _VIEW is None:
        return None
    follows = None if _VIEW["watch_all"] else sorted(_VIEW["watchlist"])
    return {"locations": _VIEW["prefs"]["locations"], "families": _VIEW["prefs"]["job_families"],
            "experience": _VIEW["prefs"]["experience"], "companies": follows}


def narrowed_for_me() -> bool:
    """Has this viewer chosen anything that narrows the job lists (a matched
    preference or followed companies)? If not, for_me() is every shared
    listing ("all companies", nothing else chosen) — and must not be called
    "your matches"."""
    d = my_defaults()
    return d is not None and (any(d[k] for k in ("locations", "families", "experience")) or d["companies"] is not None)


def follows_nothing() -> bool:
    """A personal viewer whose company scope is empty: not "all companies"
    and no followed company (every new account starts here)."""
    d = my_defaults()
    return d is not None and d["companies"] == []


def for_me(jobs: list[dict]) -> list[dict]:
    """The jobs for this viewer's Home: their company scope, narrowed by
    their saved preferences. Nothing when they follow no company."""
    d = my_defaults()
    if d is None:
        return jobs
    return [j for j in jobs if job_filters.matches_view(j, **d)]


active_jobs: list[dict] = _all_active_jobs
dismissed_jobs: list[dict] = [j for j in reversed(all_records) if is_dismissed(j)]  # newest-first

NOW = datetime.now(timezone.utc)
# cron asks for every 3 h, but GitHub starts scheduled runs late: measured
# gaps were median 5 h, 90th percentile 7.5 h, max 9 h — so a scan only
# counts as "delayed" once it is overdue by more than that
_STALE_H = max(10, SCAN_HOURS + 7)          # ... and a longer interval is not "delayed" either
SCHEDULE_NOTE = f"{interval_label(SCAN_HOURS).lower()} (GitHub may start scans later)"
HOME_LIMIT = 8
JOBS_PAGE = 12
_CATEGORY_PILL = {"FRESHER": "fresher", "ENTRY_LEVEL": "entry"}
LEGACY_LABEL = "Keyword match"  # records from before the classifier existed

st.session_state.pop("_app_rerun", None)
for _k, _v in (("toast", None), ("toast_kind", "success"), ("jobs_page", 0), ("test_status", None),
               ("confirm_remove", None),
               ("just_added", None), ("job_from", "jobs"), ("confirm_clear", False)):
    if _k not in st.session_state:
        st.session_state[_k] = _v
if st.session_state.pop("_clear_add_form", False):
    for _k in ("new_name", "new_url", "new_website"):
        st.session_state[_k] = ""


def change_dismissal(keys: set[str], dismiss: bool):
    """Dismiss or restore jobs for the signed-in viewer: (saved, error), or
    None when refused (the reason is already shown).
    With personal data, the viewer's OWN list changes — members included,
    never anyone else's — and an admin's change also goes to the shared
    record that today's alert emails follow. Without personal data only the
    shared record exists, and only admins may change it."""
    action = "dismiss jobs" if dismiss else "restore jobs"
    if USER is None:
        if USER_PROBLEM and not is_owner():
            toast(USER_PROBLEM, "error")
            return None
        if not owner_only(action):
            return None
    else:
        try:
            (USER.dismiss if dismiss else USER.restore)(keys)
        except user_store.UserStoreError as e:
            personal_changed()
            toast(str(e), "error")
            return None
        _personal_patched(dismissed_add=keys if dismiss else (), dismissed_remove=() if dismiss else keys)
        if not is_owner():
            return True, ""
        if dismiss:
            return (True, "") if _save_change_soon("data/seen_jobs.json", dismiss_jobs(keys), [],
                                                   f"chore: dismiss {len(keys)} alert(s)") else None
    _, saved, err = _save_change("data/seen_jobs.json", dismiss_jobs(keys) if dismiss else _restore_jobs(keys), [],
                                 f"chore: {'dismiss' if dismiss else 'restore'} {len(keys)} alert(s)")
    return saved, err


def toast(msg: str, kind: str = "success"):
    st.session_state.toast = msg
    st.session_state.toast_kind = kind


def go(page: str, **extra):
    if extra.get("job"):
        # remember the company page a job was opened from, so Back returns there
        st.session_state.job_from_company = (st.session_state.get("company_id")
                                             if extra.get("origin") == "companies" else None)
    st.session_state.page = page
    st.session_state.job_id = extra.get("job")
    st.session_state.company_id = extra.get("company")
    st.session_state.company_view = extra.get("view")
    st.session_state.confirm_remove = None
    if extra.get("job"):
        st.session_state.job_from = extra.get("origin", st.session_state.get("page_before", "jobs"))


# ── derived state ─────────────────────────────────────────────────────────────
_check_trigger = st.session_state.get("last_check_trigger", 0)
last_scan = max((d for d in (_parse_iso(c.get("last_checked", "")) for c in companies) if d), default=None)
scan_stale = last_scan is None or NOW - last_scan > timedelta(hours=_STALE_H)
NEXT_SCAN = scan_schedule.next_scan(NOW, SCAN_HOURS, scan_schedule.last_scan(companies))   # UTC; shown in IST
# a scan is already overdue (none since the latest interval boundary): it
# waits for the next plan tick (:17 / :47) — said plainly, never as a countdown
SCAN_DUE = scan_schedule.scan_due(NOW, SCAN_HOURS, scan_schedule.last_scan(companies))
# ... and long enough ago (a whole window's ticks missed) to say so up front:
# a scan GitHub never started is "overdue", never "healthy"
SCAN_OVERDUE = scan_schedule.scan_overdue(NOW, SCAN_HOURS, scan_schedule.last_scan(companies))
# GitHub couldn't be read for a while: the status below is from an old copy
DATA_STALE = _shared_data().stale()
NEXT_SCAN_NOTE = "Overdue — GitHub may start it later" if SCAN_DUE else "GitHub may start it later"


# scraper status -> label; all of them use the red "failing" style/counts
_UNHEALTHY = {"failing": "Failing", "broken": "Broken", "needs_config": "Needs configuration"}


def portal_status(c: dict) -> tuple[str, str]:
    """(css key, label) from the scraper's own status + last_checked."""
    checked = _parse_iso(c.get("last_checked", ""))
    if (_check_trigger and time.time() - _check_trigger < 1200 and c.get("status") not in _UNHEALTHY
            and (checked is None or checked.timestamp() < _check_trigger)):
        return "checking", "Checking"
    if c.get("status") in _UNHEALTHY:
        return "failing", _UNHEALTHY[c["status"]]
    if checked is None or c.get("status") not in ("active",):
        return "pending", "Pending"
    if NOW - checked > timedelta(hours=_STALE_H):
        return "delayed", "Delayed"
    return "healthy", "Healthy"


statuses = {c.get("id"): portal_status(c) for c in companies}
n_by_status = {k: sum(1 for s in statuses.values() if s[0] == k) for k in ("healthy", "delayed", "failing", "pending", "checking")}
if not companies:
    overall = ("pending", "No portals yet")
elif n_by_status["failing"] == len(companies):
    overall = ("failing", "All portals failing")
elif n_by_status["failing"]:
    overall = ("failing", f"{_plural(n_by_status['failing'], 'portal')} failing")
elif DATA_STALE:                    # never "healthy" on a copy GitHub couldn't refresh
    overall = ("delayed", "Status may be out of date")
elif last_scan is None:
    overall = ("pending", "Waiting for first scan")
elif scan_stale:
    overall = ("delayed", "Scans delayed")
elif SCAN_OVERDUE:
    overall = ("delayed", "Scan overdue")
else:
    overall = ("healthy", "All portals healthy")

found = {id(j): _found_at(j.get("date") or "", NOW) for j in all_records}
new_this_week = sum(1 for j in active_jobs if found[id(j)] and NOW - found[id(j)] <= timedelta(days=7))
# Records written since Sep 2026 carry the company's id (stable across a
# rename); older records only have the company name.
jobs_by_company: dict[str, list[dict]] = {}
jobs_by_company_id: dict[str, list[dict]] = {}
for j in reversed(all_records):
    if j.get("company_id"):
        jobs_by_company_id.setdefault(str(j["company_id"]), []).append(j)
    else:
        jobs_by_company.setdefault((j.get("company") or "").strip(), []).append(j)
# the ALERT_RECIPIENT secret, else the legacy (public) settings.json field;
# shown in full only to the owner
recipient, recipient_source = alert_recipient(settings)


def company_jobs(c: dict) -> list[dict]:
    """Every job found for this company (active and dismissed), newest first."""
    by_id = jobs_by_company_id.get(str(c.get("id")), [])
    by_name = jobs_by_company.get((c.get("name") or "").strip(), [])
    return sorted(by_id + by_name, key=lambda j: found[id(j)] or datetime.min.replace(tzinfo=timezone.utc),
                  reverse=True) if by_id and by_name else (by_id or by_name)


def company_active(c: dict) -> int:
    return sum(1 for j in company_jobs(c) if not is_dismissed(j))


def find_company(name: str, company_id: str | None = None) -> dict | None:
    if company_id:
        by_id = next((c for c in companies if str(c.get("id")) == str(company_id)), None)
        if by_id:
            return by_id
    name = (name or "").strip().lower()
    return next((c for c in companies if (c.get("name") or "").strip().lower() == name), None)


def job_category(j: dict) -> tuple[str, str]:
    cat = j.get("category")
    if cat in _CATEGORY_PILL:
        return _CATEGORY_PILL[cat], category_label(j)
    return "legacy", LEGACY_LABEL


def job_why(j: dict, long: bool = False) -> str:
    if j.get("reason") or j.get("category") in _CATEGORY_PILL:
        return friendly_reason(j)
    return ("Matched the tracker's earlier keyword filter — it was found before the classifier started recording reasons."
            if long else "Found by the earlier keyword filter")


def job_locations(j: dict) -> list[str]:
    return [p.strip() for p in re.split(r"\s*·\s*", j.get("location") or "") if p.strip()]


# ═══════════════════════════════════════════════════════════════════════════════
# SIDEBAR
# ═══════════════════════════════════════════════════════════════════════════════
current = st.session_state.page
# the selected destination: a soft accent wash, a glowing indicator bar and
# an accent icon (sidebar); an accent pill behind the icon (phone bar)
_nav_on = f".st-key-nav_{current} [data-testid^='stBaseButton']"
st.html(f"<style>{_nav_on}, {_nav_on}:hover {{"
        "background: linear-gradient(90deg, var(--accent-soft), color-mix(in srgb, var(--accent-soft) 30%, transparent)) !important;"
        "color: var(--text) !important; border-color: color-mix(in srgb, var(--accent) 22%, transparent) !important;"
        "box-shadow: var(--hi), 0 10px 22px -16px var(--accent) !important;}"
        f"{_nav_on}::before {{content: ''; position: absolute; left: -15px; top: 10px; bottom: 10px; width: 3px; border-radius: 0 3px 3px 0;"
        "background: linear-gradient(180deg, var(--accent), var(--accent-2)); box-shadow: 0 0 12px var(--accent);"
        "animation: jt-grow var(--normal) var(--ease) backwards;}"
        f"@media (max-width: 1023px) {{ {_nav_on}::before {{ left: -11px; }} }}"
        f".st-key-nav_{current} [data-testid='stIconMaterial'] {{color: var(--accent-text) !important; font-variation-settings: 'FILL' 1;"
        "text-shadow: 0 0 14px color-mix(in srgb, var(--accent) 45%, transparent);}"
        f".st-key-nav_{current} p {{font-weight: 700 !important;}}</style>")

def nav_groups() -> list[tuple[str, list[str]]]:
    """The destinations of this viewer: a member gets their own job search,
    an admin also the tracker's management pages."""
    if MEMBER_VIEW:
        return [("Discover", ["home", "jobs", "companies"]),
                ("Your account", (["alerts"] if USER is not None else []) + ["settings"])]
    return NAV_GROUPS


def nav_label(key: str) -> str:
    return "My companies" if key == "companies" and MEMBER_VIEW else PAGES[key][0]


def _leave_fragment_if_needed() -> None:
    """In a list fragment (a run of just the list): when its callback changed
    something outside the list — another page or job (Details, View all) or
    the data (Dismiss/Restore) — draw the whole app instead, once."""
    # compared with the destination the last full run drew (set before any
    # page is drawn), so a full run itself never asks for another one
    here = (st.session_state.page, st.session_state.get("job_id"), st.session_state.get("company_id"),
            st.session_state.get("company_view"))
    if st.session_state.pop("_app_rerun", False) or here != st.session_state.get("_dest"):
        st.rerun()


def _toggle_nav() -> None:
    """Open/close the desktop sidebar, and say so in the URL (?menu=hidden)
    at once — the toggle reruns only its fragment, which never reaches the
    full run's URL bookkeeping."""
    collapsed = not st.session_state.get("nav_collapsed", False)
    st.session_state.nav_collapsed = collapsed
    want = {k: v for k, v in (st.session_state.get("_url_written") or {}).items() if k != "menu"}
    if collapsed:
        want["menu"] = "hidden"
    st.query_params.from_dict(want)
    st.session_state._url_written = want


def _open_drawer() -> None:
    st.session_state.nav_drawer = True


def _close_drawer() -> None:
    st.session_state.nav_drawer = False


def _drawer_go(page: str) -> None:
    """A destination chosen in the phone drawer: close it and go there."""
    st.session_state.nav_drawer = False
    go(page)


@st.fragment
def nav_controls() -> None:
    """The menu button and the phone drawer, as a FRAGMENT: opening or closing
    the drawer, or the desktop sidebar, reruns only this small part — never
    the page. A destination chosen in the drawer changes the page, so that
    one click then draws the whole app (_leave_fragment_if_needed).

      laptops and desktops  the left sidebar is the navigation; the button
                            ("Hide/Show navigation") closes and opens it, and
                            the page takes the freed width (?menu=hidden)
      phones                the sidebar can't fit: the button ("Open menu")
                            slides a drawer in from the left with every
                            destination of this viewer — the admin pages for
                            admins only (members are also turned away from
                            them on the server, see ADMIN_PAGES) — closed by
                            its ✕, the dimmed page behind it, or a choice

    Real buttons throughout: keyboard reachable, each with a label that
    screen readers read (shown as icons where space is short)."""
    _leave_fragment_if_needed()
    with st.container(key="nav_toggle"):
        st.button("Open menu", key="btn_drawer_open", icon=":material/menu:", on_click=_open_drawer)
    if not st.session_state.get("nav_drawer"):
        return
    st.button("Close menu", key="btn_drawer_backdrop", on_click=_close_drawer)     # the dimmed page behind
    with st.container(key="drawer"):
        with st.container(key="drawer_head"):
            logo = _logo_data_uri()
            mark = f'<img src="{logo}" alt="">' if logo else ""
            st.html(f'<div class="brand"><div class="mark">{mark}</div>'
                    '<div><div class="n">Fresher Job Tracker</div><div class="s">Menu</div></div></div>')
            st.button("Close menu", key="btn_drawer_close", icon=":material/close:", on_click=_close_drawer)
        here = st.session_state.page
        for group, items in nav_groups():
            st.html(f'<div class="nav-group">{escape(group)}</div>')
            for key in items:
                st.button(nav_label(key), key=f"drw_{key}", icon=PAGES[key][1], type="tertiary",
                          use_container_width=True, on_click=_drawer_go, args=(key,))
        st.html(f"<style>.st-key-drw_{here} [data-testid^='stBaseButton'] {{ background: var(--accent-soft) !important;"
                " color: var(--accent-text) !important; }</style>")


# laptops and desktops: where the menu button sits. It is drawn INSIDE the
# sidebar (the main area is a lower layer than the sidebar, so a button drawn
# there could never sit on it). Each state's rule is drawn by the same
# fragment run, so the latest click always wins.
#   open    at the sidebar's upper right, level with the first group label
#           (narrow icon rail, 768-1023 px: at the top of the rail): a quiet
#           « icon that shows while the pointer is over the sidebar (or the
#           button has keyboard focus), as Streamlit's own collapse control
#           does; touch screens, which have no hover, always show it
#   closed  the sidebar folds to zero width (it is not removed, so the button
#           stays visible) and a small bordered » tile sits at the window's
#           left edge; the page keeps a left gutter so it never covers content
_NAV_OPEN_CSS = """@media (min-width: 1024px) {
  .st-key-side_toggle { position: fixed !important; top: 74px; left: calc(var(--sidebar-w) - 50px); width: auto !important; z-index: 5; }
}
@media (min-width: 768px) {
  .st-key-btn_nav_toggle [data-testid^="stBaseButton"] { background: transparent !important; border-color: transparent !important; box-shadow: none !important; }
  .st-key-btn_nav_toggle [data-testid^="stBaseButton"]:hover { background: var(--hover) !important; }
  .st-key-btn_nav_toggle [data-testid="stIconMaterial"] { color: var(--muted) !important; }
}
@media (min-width: 768px) and (hover: hover) {
  .st-key-side_toggle { transition: opacity .15s ease; }
  section[data-testid="stSidebar"]:not(:hover) .st-key-side_toggle:not(:has(:focus-visible)) { opacity: 0; }
}"""
_NAV_CLOSED_CSS = """@media (min-width: 768px) {
  .stApp section[data-testid="stSidebar"] { width: 0 !important; min-width: 0 !important; max-width: 0 !important;
    overflow: hidden !important; visibility: hidden; border: 0 !important; box-shadow: none !important; }
  .st-key-side_toggle { visibility: visible; position: fixed !important; top: 18px; left: 14px; width: auto !important; z-index: 5; }
  .st-key-btn_nav_toggle [data-testid^="stBaseButton"] { width: 40px !important; height: 40px !important; min-height: 40px !important;
    border-radius: 11px !important; background: var(--surface) !important; border: 1px solid var(--border-strong) !important;
    box-shadow: 0 8px 20px -14px rgba(13,16,36,.55) !important; }
  .st-key-btn_nav_toggle [data-testid^="stBaseButton"]:hover { background: var(--accent-soft) !important;
    border-color: color-mix(in srgb, var(--accent) 45%, var(--border)) !important; }
  .st-key-btn_nav_toggle [data-testid="stIconMaterial"] { color: var(--text) !important; font-size: 22px !important; }
  .stApp [data-testid="stMainBlockContainer"], .stApp .block-container { padding-left: 72px !important; }
}"""


@st.fragment
def sidebar_toggle() -> None:
    """The laptop/desktop menu button ("Hide/Show navigation"), drawn in the
    sidebar; a click reruns only this fragment. Phones never show the
    sidebar, so they never see it (they have the drawer)."""
    collapsed = st.session_state.get("nav_collapsed", False)
    label = "Show navigation" if collapsed else "Hide navigation"
    with st.container(key="side_toggle"):
        st.button(label, key="btn_nav_toggle", help=label, on_click=_toggle_nav,
                  icon=":material/keyboard_double_arrow_right:" if collapsed else ":material/keyboard_double_arrow_left:")
    st.html(f"<style>{_NAV_CLOSED_CSS if collapsed else _NAV_OPEN_CSS}</style>")


# the desktop navigation's open/closed state lives in the URL too (?menu=hidden);
# read only here, past the sign-in gate, so a signed-out tab holds none of it
if "nav_collapsed" not in st.session_state:
    st.session_state.nav_collapsed = st.query_params.get("menu") == "hidden"
# always drawn (laptops and desktops; CSS hides it on phones, and nav_controls
# hides it while closed) — so opening it again needs no page run
with st.sidebar:
    sidebar_toggle()
    logo = _logo_data_uri()
    st.html(f"""<div class="brand"><div class="mark {overall[0] if not MEMBER_VIEW else 'healthy'}">{f'<img src="{logo}" alt="">' if logo else ''}</div>
      <div><div class="n">Fresher Job Tracker</div><div class="s">Fresher opportunities</div></div></div>""")
    for gi, (group, items) in enumerate(nav_groups()):
        st.html(f'<div class="nav-group{" first" if gi == 0 else ""}">{group}</div>')
        for key in items:
            st.button(nav_label(key), key=f"nav_{key}", icon=PAGES[key][1], type="tertiary", use_container_width=True,
                      on_click=go, args=(key,))
    # (the scanner's health and last scan are on Home and, for admins, under
    # Settings → Tracker status — the rail holds navigation only)


# ── deleting one's own account ────────────────────────────────────────────────
# The same flow from the account menu and from Settings → Account
# management: a panel that says what is deleted and what isn't, a typed
# DELETE, and a fresh proof of identity (the current password, checked by
# Firebase; or a Google sign-in made in the last DELETE_GOOGLE_FRESH_S).
# Only the account the gate resolved on the server is ever deleted (its
# UID, bound to USER) — nothing typed, no address, no URL names it.
# Order: the person's own Firestore data first, then the Firebase account
# (Admin SDK), then a check with Firebase that it is gone; only then is
# "deleted" said. Shared listings, the company catalogue, settings and
# schedules live in GitHub and are never touched; admins can't delete their
# account here (they hold the app's administration).
DELETE_WORD = "DELETE"
DELETE_GOOGLE_FRESH_S = 600


def can_delete_account() -> bool:
    """A signed-in person with their own data (where the deletion runs)."""
    return (AUTH_GATE and ACCOUNT is not None and USER is not None
            and ACCOUNT.get("provider") in ("password", "google.com") and user_store.valid_uid(ACCOUNT.get("uid")))


def _open_delete_account(from_menu: bool = False) -> None:
    for k in ("_delete_error",):
        st.session_state.pop(k, None)
    st.session_state._delete_open = True
    if from_menu:
        _menu_go("settings")


def _close_delete_account() -> None:
    for k in ("_delete_open", "_delete_error"):
        st.session_state.pop(k, None)
    st.session_state._delete_clear = True


def _google_sign_in_age() -> float | None:
    return firebase_auth.token_age(_google_id_token())


def delete_own_account() -> tuple[bool, str]:
    """Delete the signed-in person's account (see above): (deleted, why not)."""
    if not can_delete_account():
        return False, "Your account can't be deleted here right now."
    uid, email = ACCOUNT["uid"], ACCOUNT.get("email") or ""
    if USER.uid != uid:                                           # never anyone else's
        return False, "Your account can't be deleted here right now."
    if is_owner():
        return False, "Admin accounts can't be deleted here."
    if (st.session_state.get("delete_confirm_text") or "").strip() != DELETE_WORD:
        return False, f"Type {DELETE_WORD} to confirm."
    if ACCOUNT["provider"] == "password":                         # prove it's still you
        password = st.session_state.get("delete_pw") or ""
        if not password:
            return False, "Enter your current password."
        limiter = _guards()["firebase_signin"]
        if limiter.locked_for(email):
            return False, "Too many attempts — try again in a few minutes."
        try:
            proof = firebase_auth.sign_in_with_password(email, password, config=_AUTH_CONFIG, require_verified=True,
                                                        may_send_verification=lambda _e: False)
        except firebase_auth.FirebaseAuthError as e:
            if e.code in _FB_CREDENTIAL_ERRORS:
                limiter.failed(email)
                return False, "That password isn't right."
            return False, str(e)
        if proof.uid != uid:
            return False, "That password belongs to a different account."
    else:
        age = _google_sign_in_age()
        if age is None or age > DELETE_GOOGLE_FRESH_S:
            return False, ("For your security, sign out, continue with Google again and then delete your account "
                           f"within {DELETE_GOOGLE_FRESH_S // 60} minutes.")
    try:
        USER.delete_personal_data()
    except user_store.UserStoreError:
        auth_event("account deletion stopped: personal data couldn't be deleted")
        return False, ("Your account was NOT deleted: your personal data couldn't be removed just now (some of it "
                       "may already be gone). Please try again.")
    try:
        _STORE.delete_account(uid)
        gone = _STORE.account(uid) is None
    except user_store.UserStoreError:
        gone = False
    if not gone:
        auth_event("account deletion incomplete: Firebase account still exists")
        return False, ("Your personal data was deleted, but your sign-in account couldn't be removed just now. "
                       "Please try again — nothing else was changed.")
    auth_event("account deleted by its owner")
    return True, ""


def account_management_card() -> None:
    """Settings → Account management: Delete account (see above)."""
    if not can_delete_account():
        return
    if st.session_state.pop("_delete_clear", False):              # before the fields are drawn
        for k in ("delete_confirm_text", "delete_pw"):
            st.session_state[k] = ""
    if st.session_state.pop("_delete_clear_pw", False):
        st.session_state.delete_pw = ""
    with st.container(key="set_delete"):
        st.html(f'<div class="set-head"><span class="ic red">{_ms("person_remove")}</span><div>'
                '<div class="eyebrow">Account management</div><h2 class="section-title">Delete account</h2>'
                '<p class="section-sub">Permanently delete your account and everything saved for it.</p></div></div>')
        if not st.session_state.get("_delete_open"):
            with st.container(key="delete_open_row"):
                st.button("Delete account…", key="btn_delete_open", icon=":material/delete_forever:",
                          on_click=_open_delete_account)
            return
        with st.container(key="delete_confirm"):
            st.html('<div class="del-info"><p><b>This permanently deletes your account.</b> It can’t be undone.</p>'
                    '<ul><li>Your sign-in account, so you can’t sign in with it again</li>'
                    '<li>Your job preferences, followed companies and dismissed jobs</li>'
                    '<li>Your alert-email settings and your profile (name and avatar)</li></ul>'
                    '<p>Not deleted: the shared job listings, the company list and the job scanner — other '
                    'members keep them, and nothing of theirs changes.</p></div>')
            if is_owner():
                st.html(f'<p class="note">{_ms("info", "s16")} Admin accounts can’t be deleted here, because they run '
                        'the tracker. Remove this address from the admin list first; it can then be deleted like any '
                        'other account.</p>')
                st.button("Cancel", key="btn_delete_cancel", on_click=_close_delete_account)
                return
            err = st.session_state.get("_delete_error")
            if err:
                st.html(f'<div class="login-err" role="alert">{_ms("error", "s20")}<div>{escape(err)}</div></div>')
            st.text_input(f"Type {DELETE_WORD} to confirm", key="delete_confirm_text", max_chars=12,
                          placeholder=DELETE_WORD, autocomplete="off")
            if ACCOUNT["provider"] == "password":
                st.text_input("Your current password", type="password", key="delete_pw",
                              autocomplete="current-password")
            else:
                age = _google_sign_in_age()
                if age is None or age > DELETE_GOOGLE_FRESH_S:
                    st.html(f'<p class="note">{_ms("info", "s16")} For your security, deleting a Google account '
                            f'needs a recent sign-in: sign out, continue with Google again, then delete your '
                            f'account within {DELETE_GOOGLE_FRESH_S // 60} minutes.</p>')
            y, n_ = st.columns([1.4, 3])
            with y:
                go_on = st.button("Delete my account", key="btn_delete_confirm", type="primary",
                                  icon=":material/delete_forever:", use_container_width=True)
            with n_:
                st.button("Cancel", key="btn_delete_cancel", on_click=_close_delete_account)
            if go_on:
                ok, why = delete_own_account()
                if not ok:
                    st.session_state._delete_error = why
                    st.session_state._delete_clear_pw = True
                    st.rerun()
                google = account_sign_out()
                st.session_state._login_notice = ("Your account and personal data were deleted. You can create a "
                                                  "new account at any time.")
                if not google:
                    st.rerun()
                st.stop()


# ── the account, top right ───────────────────────────────────────────────────
def _menu_go(page: str, **extra) -> None:
    """A destination chosen from the account menu: go there and close the menu."""
    go(page, **extra)
    st.session_state._acct_menu_n = st.session_state.get("_acct_menu_n", 0) + 1


def account_menu() -> None:
    """Who is signed in, and their own destinations: an initial avatar, the
    FIRST name and — for admins only — an ADMIN badge (the full name and
    email are inside the menu). Admin tools stay in the sidebar's
    Management group, never in this menu."""
    name = account_label(SHOWN_ACCOUNT)
    first = first_name(SHOWN_ACCOUNT)
    label = re.sub(r"([\\`*_{}\[\]()#+!|~<>-])", r"\\\1", first) + (" **ADMIN**" if IS_ADMIN else "")
    trigger = '[class*="st-key-acct_menu"] [data-testid="stPopoverButton"]::before'
    if CHOSEN_AVATAR:                 # the picked mark on its tile, as in the menu and on the Account page
        st.html(f'<style>{trigger} {{ content: ""; background: url("{brand_logos.brand_uri(CHOSEN_AVATAR)}") center / 58% '
                f'no-repeat, {brand_logos.brand_tile(CHOSEN_AVATAR)}; box-shadow: var(--av-gloss), '
                'inset 0 0 0 1px rgba(15,23,42,.10), 0 0 0 2px var(--surface), 0 0 0 3.5px var(--av-ring), var(--av-drop); }</style>')
    else:
        st.html(f'<style>{trigger} {{ content: "{_me_initial()}"; }}</style>')
    with st.container(key="acct_bar"):
        nav_controls()
        # a new key after an item is chosen: the next page draws the menu closed
        # (no extra round trip — the popover's own state isn't tracked)
        with st.popover(label, key=f"acct_menu_{st.session_state.get('_acct_menu_n', 0)}"):
            badge_html = '<span class="role admin">Admin</span>' if IS_ADMIN else ""
            st.html(f'<div class="acct-head">{_me_avatar()}'
                    f'<div class="who"><div class="nm">{escape(name)}</div>'
                    f'<div class="em">{escape(ACCOUNT.get("email") or "")}</div></div>{badge_html}</div>')
            items = [("settings", "Account", ":material/account_circle:", {})]
            if USER is not None:
                items += [("preferences", "Job preferences", ":material/tune:", {}),
                          ("companies", "My companies", ":material/apartment:", {"view": "mine"}),
                          ("alerts", "My alerts", ":material/notifications:", {})]
            for dest, text, icon, kw in items:
                st.button(text, key=f"acct_{dest}", icon=icon, type="tertiary", use_container_width=True,
                          on_click=_menu_go, args=(dest,), kwargs=kw)
            st.html('<div class="acct-sep"></div>')
            if st.button("Sign out", key="btn_account_sign_out", icon=":material/logout:", type="tertiary",
                         use_container_width=True):
                if not account_sign_out():
                    st.rerun()
                st.stop()
            if can_delete_account():
                st.html('<div class="acct-sep"></div>')
                st.button("Delete account", key="btn_account_delete", icon=":material/delete_forever:", type="tertiary",
                          use_container_width=True, on_click=_open_delete_account, args=(True,))


# A new destination opens at its top. Streamlit keeps the main area's scroll
# position across reruns, so a page reached from far down another page
# opened mid-way. Only a run whose destination (page, job, company, view)
# changed carries this one-line script (a fresh nonce, so it runs once);
# filters, pagination, dismissing or the account menu keep their place.
_dest = (st.session_state.page, st.session_state.get("job_id"), st.session_state.get("company_id"),
         st.session_state.get("company_view"))
if st.session_state.get("_dest") not in (None, _dest):
    st.session_state._dest_n = st.session_state.get("_dest_n", 0) + 1
    with st.container(key="jt_scroll_top"):
        st.html(f'<script data-nav="{st.session_state._dest_n}">(() => {{ const top = () => {{ '
                'const m = document.querySelector(\'[data-testid="stMain"]\'); if (m) m.scrollTop = 0; '
                'window.scrollTo(0, 0); }; top(); requestAnimationFrame(top); })();</script>',
                unsafe_allow_javascript=True)
st.session_state._dest = _dest

if ACCOUNT:
    account_menu()
else:
    with st.container(key="acct_bar"):          # no sign-in (legacy mode): the menu button alone
        nav_controls()



# ═══════════════════════════════════════════════════════════════════════════════
# SHARED PIECES
# ═══════════════════════════════════════════════════════════════════════════════
def _eyebrow(page: str) -> str:
    return next((g for g, items in nav_groups() if page in items),
                "Your account" if page in PERSONAL_PAGES else "Discover")


def page_header(title: str, sub_html: str = "", actions=None, eyebrow: str | None = None):
    with st.container(key="page_head"):
        c1, c2 = st.columns([3, 2], vertical_alignment="bottom")
        with c1:
            st.html(f'<div class="eyebrow bar">{escape(eyebrow or _eyebrow(st.session_state.page))}</div>'
                    f'<h1 class="page-title">{escape(title)}</h1>' + (f'<div class="page-sub">{sub_html}</div>' if sub_html else ""))
        with c2:
            if actions:
                with st.container(key="page_actions"):
                    actions()


def status_line() -> str:
    """The live system state as three chips: health · last scan · next slot."""
    return (f'<span class="chip live {overall[0]}"><span class="dot {overall[0]}"></span>{escape(overall[1])}</span>'
            f'<span class="chip num">{_ms("history", "s16")}Last scan {_ago(last_scan, NOW)}</span>'
            f'<span class="chip num">{_ms("schedule", "s16")}Next scheduled {_ist(NEXT_SCAN)}</span>')


# Actions run as on_click callbacks: Streamlit runs them BEFORE the page is
# drawn, so a click is one run of the script. (Handled inline, a click drew
# the page up to the button, acted, then st.rerun() drew it all again.)
def _refresh_click() -> None:
    _refresh_data()
    toast("Showing the latest data", "success")


def _run_check_click() -> None:
    cooldown = _guards()["check"]
    if not owner_only("start a check"):
        return
    if not cooldown.try_start():
        toast(f"A check was just started — try again in {int(cooldown.remaining() // 60) + 1} min", "error")
        return
    ok, msg = _trigger_scrape()
    if ok:
        st.session_state.last_check_trigger = time.time()
    else:
        cooldown.cancel()
    toast(msg, "success" if ok else "error")


def scan_actions():
    a, b = st.columns(2)
    with a:
        st.button("Refresh", key="btn_refresh", icon=":material/refresh:", help="Reload the latest jobs and portal status",
                  on_click=_refresh_click)
    with b:
        st.button("Run check", key="btn_run_check", icon=":material/travel_explore:", type="primary",
                  help="Ask GitHub Actions to scan every portal now (takes a few minutes)", on_click=_run_check_click)


def apply_link(j: dict, label: str = "Apply") -> str:
    url = safe_url(j.get("url", ""))
    if not url:
        return '<span class="btn disabled">No link</span>'
    title = (j.get("title") or "this job").strip() or "this job"
    return (f'<a class="btn primary" href="{escape(url, quote=True)}" target="_blank" rel="noopener noreferrer" '
            f'aria-label="{escape(label, quote=True)}: {escape(title, quote=True)} (opens the company site)">'
            f'{label} {_ms("arrow_outward", "s16")}</a>')


def _restore_widgets(keys) -> None:
    """Bring back filter values after a visit to another page: Streamlit
    forgets a widget's value on any run that doesn't draw it, so each value
    is also kept under a plain key (cleared at sign-out with everything else)."""
    for key in keys:
        if key not in st.session_state and f"_keep_{key}" in st.session_state:
            st.session_state[key] = st.session_state[f"_keep_{key}"]


def _remember_widgets(keys) -> None:
    for key in keys:
        if key in st.session_state:
            st.session_state[f"_keep_{key}"] = st.session_state[key]


def filter_jobs(prefix: str, jobs: list[dict]) -> list[dict]:
    """Search · location · category · company · sort · clear. With personal
    data, the viewer's saved preferences are the defaults instead."""
    if my_defaults() is not None:
        return _filter_jobs_personal(prefix, jobs, my_defaults())
    locs = sorted({loc for j in jobs for loc in job_locations(j)}, key=str.lower)
    comps = sorted({(j.get("company") or "").strip() for j in jobs if (j.get("company") or "").strip()}, key=str.lower)
    cats = {"all": "All categories", "FRESHER": "Fresher", "ENTRY_LEVEL": "Entry level", "legacy": LEGACY_LABEL}
    sorts = {"new": "Newest first", "old": "Oldest first", "company": "Company A–Z", "title": "Title A–Z"}
    keys = {k: f"{prefix}_{k}" for k in ("q", "loc", "cat", "co", "sort")}
    defaults = {"q": "", "loc": "All locations", "cat": "all", "co": "All companies", "sort": "new"}
    _restore_widgets(keys.values())
    for k, v in defaults.items():
        st.session_state.setdefault(keys[k], v)
    if st.session_state[keys["loc"]] not in ["All locations", *locs]:
        st.session_state[keys["loc"]] = "All locations"
    if st.session_state[keys["co"]] not in ["All companies", *comps]:
        st.session_state[keys["co"]] = "All companies"
    active = any(st.session_state[keys[k]] != v for k, v in defaults.items())

    def _clear():
        for k, v in defaults.items():
            st.session_state[keys[k]] = v

    with st.container(key=f"filters_{prefix}"):
        c = st.columns([2.4, 1.3, 1.3, 1.3, 1.3, 0.7], vertical_alignment="bottom")
        with c[0]:
            q = st.text_input("Search", key=keys["q"], placeholder="Search title, company or location",
                              label_visibility="collapsed").strip().lower()
        with c[1]:
            loc = st.selectbox("Location", ["All locations", *locs], key=keys["loc"], label_visibility="collapsed")
        with c[2]:
            cat = st.selectbox("Category", list(cats), key=keys["cat"], format_func=cats.get, label_visibility="collapsed")
        with c[3]:
            co = st.selectbox("Company", ["All companies", *comps], key=keys["co"], label_visibility="collapsed")
        with c[4]:
            sort = st.selectbox("Sort", list(sorts), key=keys["sort"], format_func=sorts.get, label_visibility="collapsed")
        with c[5]:
            st.button("Clear", key=f"{prefix}_clear", on_click=_clear, disabled=not active,
                      help="Clear all filters", use_container_width=True)

    out = []
    for j in jobs:
        if loc != "All locations" and loc not in job_locations(j):
            continue
        if cat == "legacy" and j.get("category") in _CATEGORY_PILL:
            continue
        if cat in _CATEGORY_PILL and j.get("category") != cat:
            continue
        if co != "All companies" and (j.get("company") or "").strip() != co:
            continue
        if q and q not in " ".join(str(j.get(k) or "") for k in ("title", "company", "location", "reason")).lower():
            continue
        out.append(j)
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    if sort == "old":
        out.sort(key=lambda j: found[id(j)] or epoch)
    elif sort == "company":
        out.sort(key=lambda j: ((j.get("company") or "").lower(), (j.get("title") or "").lower()))
    elif sort == "title":
        out.sort(key=lambda j: (j.get("title") or "").lower())
    else:
        out.sort(key=lambda j: found[id(j)] or epoch, reverse=True)
    st.session_state[f"{prefix}_filtered_sig"] = (q, loc, cat, co, sort)
    _remember_widgets(keys.values())
    return out, active, _clear


def _filter_jobs_personal(prefix: str, jobs: list[dict], saved: dict):
    """Location · job family · experience · company, starting from what the
    viewer saved. Changing them here is for this browse only — the saved
    preferences change on the Job preferences page."""
    names = {str(c.get("id")): (c.get("name") or "").strip() or "Unnamed" for c in companies
             if not str(c.get("id", "")).startswith("_row")}
    sorts = {"new": "Newest first", "old": "Oldest first", "company": "Company A–Z", "title": "Title A–Z"}
    k = {f: f"{prefix}_{f}" for f in ("q", "sort", "ploc", "pfam", "pexp", "pco")}
    _restore_widgets(k.values())
    base = {"q": "", "sort": "new", "ploc": list(saved["locations"]), "pfam": list(saved["families"]),
            "pexp": list(saved["experience"]), "pco": [c for c in (saved["companies"] or []) if c in names]}
    sig = json.dumps(base, sort_keys=True)
    if st.session_state.get(f"{prefix}_psig") != sig:      # first view, or the saved preferences changed
        for f, v in base.items():
            st.session_state[k[f]] = v
        st.session_state[f"{prefix}_psig"] = sig
    for f, v in base.items():       # Streamlit drops a widget's state on runs that don't draw it
        st.session_state.setdefault(k[f], v)
    for f in ("pco",):                                     # a company removed from the catalogue
        st.session_state[k[f]] = [c for c in st.session_state[k[f]] if c in names]
    active = any(st.session_state[k[f]] != v for f, v in base.items())

    def _reset():
        for f, v in base.items():
            st.session_state[k[f]] = v

    def _show_all():                 # "Clear filters": every job, whatever was saved
        for f in ("q", "ploc", "pfam", "pexp", "pco"):
            st.session_state[k[f]] = "" if f == "q" else []

    with st.container(key=f"filters_{prefix}"):
        r1 = st.columns([2.6, 1.1, 1.1], vertical_alignment="bottom")
        with r1[0]:
            q = st.text_input("Search", key=k["q"], placeholder="Search title, company or location",
                              label_visibility="collapsed").strip().lower()
        with r1[1]:
            sort = st.selectbox("Sort", list(sorts), key=k["sort"], format_func=sorts.get, label_visibility="collapsed")
        with r1[2]:
            st.button("My preferences", key=f"{prefix}_clear", on_click=_reset, disabled=not active,
                      icon=":material/restart_alt:", help="Back to your saved job preferences", use_container_width=True)
        with st.container(key=f"prow_{prefix}"):
            r2 = st.columns(4)
        with r2[0]:
            locs = st.multiselect("Location", list(job_filters.LOCATIONS), key=k["ploc"], placeholder="Any location", wrap=True, select_all=False)
        with r2[1]:
            fams = st.multiselect("Job family", list(job_filters.JOB_FAMILIES), key=k["pfam"], placeholder="Any family", wrap=True, select_all=False)
        with r2[2]:
            exp = st.multiselect("Experience", list(job_filters.EXPERIENCE), key=k["pexp"], placeholder="Any level",
                                 format_func=job_filters.EXPERIENCE_LABELS.get, wrap=True, select_all=False)
        with r2[3]:
            cos = st.multiselect("Company", list(names), key=k["pco"], placeholder="Any company", format_func=names.get, wrap=True, select_all=False)
        st.html('<p class="filter-note">Starts from your job preferences — changes here aren’t saved. Job type and work '
                'mode aren’t stated in the postings the tracker reads, so they can’t be filtered on yet.</p>')

    out = [j for j in jobs if job_filters.matches_view(j, locations=locs, families=fams, experience=exp,
                                                      companies=cos or None)
           and (not q or q in " ".join(str(j.get(f) or "") for f in ("title", "company", "location", "reason")).lower())]
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    if sort == "old":
        out.sort(key=lambda j: found[id(j)] or epoch)
    elif sort == "company":
        out.sort(key=lambda j: ((j.get("company") or "").lower(), (j.get("title") or "").lower()))
    elif sort == "title":
        out.sort(key=lambda j: (j.get("title") or "").lower())
    else:
        out.sort(key=lambda j: found[id(j)] or epoch, reverse=True)
    st.session_state[f"{prefix}_filtered_sig"] = (q, sort, tuple(locs), tuple(fams), tuple(exp), tuple(cos))
    _remember_widgets(k.values())
    return out, active, _show_all


def badge(cls: str, label: str) -> str:
    """The category badge: a check for jobs that passed the entry-level
    filter, a history mark for records from before the classifier."""
    icon = "history" if cls == "legacy" else "task_alt"
    return f'<span class="pill {cls}">{_ms(icon)}{escape(label)}</span>'


def job_row(j: dict, idx: str, origin: str):
    jkey = job_key(j)
    raw_title = (j.get("title") or "Untitled posting").strip() or "Untitled posting"
    company = (j.get("company") or "").strip()
    pill_cls, pill_label = job_category(j)
    meta = []
    if j.get("location"):
        meta.append(f'<span class="mi">{_ms("location_on")}<span>{escape(j["location"])}</span></span>')
    d = found[id(j)]
    if d or j.get("date"):
        meta.append(f'<span class="mi num" title="{escape(_ist_stamp(d) if d else (j.get("date") or "") + " UTC", quote=True)}">{_ms("schedule")}'
                    f'Found {_ago(d, NOW) if d else escape(j["date"])}</span>')
    # No st.columns here: the card's text and its actions sit side by side
    # through CSS. Each column is its own Streamlit element, so a page of
    # cards drew two extra elements per job on every click.
    with st.container(key=f"jr_{idx}"):
        st.html(f"""
            <div class="job">
              {_avatar(company)}
              <div class="job-body">
                {f'<div class="job-co"><span class="co">{escape(company)}</span></div>' if company else ''}
                <h3 class="job-title" title="{escape(raw_title, quote=True)}">{escape(raw_title)}</h3>
                <div class="job-meta">{"".join(meta) or '&nbsp;'}</div>
                <div class="job-why">{badge(pill_cls, pill_label)}<span class="t">{_ms("work_history" if pill_cls != "legacy" else "info")}{escape(job_why(j))}</span></div>
              </div>
            </div>""")
        with st.container(key=f"ja_{idx}"):
            st.html(apply_link(j))
            st.button("Details", key=_widget_key("crit", jkey), on_click=go, args=("jobs",),
                      kwargs={"job": _jid(j), "origin": origin})
            if is_dismissed(j):
                st.button("Restore", icon=":material/undo:", key=_widget_key("restore", jkey),
                          help="Restore to active jobs (it won't be emailed again)", on_click=_dismiss_click,
                          args=(jkey, False))
            else:
                st.button("Dismiss", icon=":material/close:", key=_widget_key("dismiss", jkey),
                          help="Dismiss — hide this job (it won't be emailed again)", on_click=_dismiss_click,
                          args=(jkey, True))


def _logo_styles(entries: dict) -> str:
    """One style block giving each found company logo its image."""
    return "<style>" + "".join(
        # 62% of the tile along the logo's longer side, as the drawn marks
        f'.logo.co-mark.{k} {{ background: url("{company_logos.web_uri(e)}") center / '
        f'{"62% auto" if (e["web"].get("w") or 1) >= (e["web"].get("h") or 1) else "auto 62%"} no-repeat, var(--tile, #fff); }}'
        for k, e in entries.items()) + "</style>"


def _fragment_logos(before: dict) -> None:
    """The logos a list fragment drew, styled inside the fragment (a list-only
    run doesn't reach the page's own style block at the end)."""
    mine = {k: e for k, e in _USED_LOGOS.items() if k not in before}
    if mine:
        st.html(_logo_styles(mine))
        _FRAGMENT_LOGOS.update(mine)


def _detail_dismiss_click(jkey: str, back: str, back_kwargs: dict) -> None:
    """Dismiss from a job's own page, then return to the list it came from."""
    res = change_dismissal({jkey}, dismiss=True)
    if not res:
        return
    toast("Removed 1 alert(s)" if res[0] else f"Removed here, but not saved permanently. {res[1]}",
          "success" if res[0] else "error")
    go(back, **back_kwargs)


def _dismiss_click(jkey: str, dismiss: bool) -> None:
    """Dismiss / Restore one job (a callback: the page is drawn once, after it)."""
    st.session_state._app_rerun = True          # counts and the hero change: the whole page, not just the list
    res = change_dismissal({jkey}, dismiss=dismiss)
    if res:
        if dismiss:
            toast("Removed 1 alert(s)" if res[0] else f"Removed here, but not saved permanently. {res[1]}",
                  "success" if res[0] else "error")
        else:
            toast("Job restored" if res[0] else f"Restored here, but not saved permanently. {res[1]}",
                  "success" if res[0] else "error")


def empty_state(icon: str, title: str, text: str, actions=None):
    with st.container(key="empty"):
        st.html(f'<div class="empty"><div class="ic">{_ms(icon, "s24")}</div><h3>{escape(title)}</h3><p>{text}</p></div>')
        if actions:
            with st.container(key="empty_actions"):
                actions()


# ═══════════════════════════════════════════════════════════════════════════════
# PAGES
# ═══════════════════════════════════════════════════════════════════════════════
def _stat(label: str, value, note: str, icon: str, tint: str = "", small: bool = False) -> str:
    """One metric module: icon, label, number, supporting line."""
    return (f'<div class="stat{" " + tint if tint else ""}"><div class="ic">{_ms(icon, "s20")}</div>'
            f'<div class="k">{label}</div><div class="v{" sm" if small else ""}">{value}</div><div class="n">{note}</div></div>')


def _opportunity_cards() -> str:
    """The hero's decorative discovery feed (aria-hidden; abstract, no names
    or listings): what the product does, in one loop — a search is typed,
    three roles arrive one after another, each is checked against its own
    posting, and the fresher seal settles."""
    def row(n: str, icon: str, tone: str) -> str:
        return (f'<div class="feed-row {n}"><span class="ic{tone}">{_ms(icon, "s20")}</span>'
                '<span class="tx"><i class="l1"></i><i class="l2"></i></span>'
                f'<span class="ok">{_ms("check")}</span></div>')
    return ('<div class="opps-wrap" aria-hidden="true"><div class="opps"><span class="glow-bg"></span>'
            f'<div class="feed"><div class="feed-q">{_ms("search", "s20")}<span class="qbar"><i></i></span>'
            '<span class="spark"></span></div>'
            + row("r1", "work", "") + row("r2", "school", " teal") + row("r3", "apartment", " blue")
            + f'<div class="feed-seal"><span class="ic">{_ms("task_alt", "s24")}</span><span class="spark-dot"></span></div>'
            '</div></div></div>')


def page_home():
    with st.container(key="hero"):
        st.html(f"""<div class="hero"><div>
          <div class="eyebrow sparked">Discover jobs</div>
          <h1 class="page-title">Discover your next <em>opportunity</em></h1>
          <p class="hero-sub">Fresher and entry-level roles in India, found across the companies you track — each one read from its own posting before it reaches you.</p>
          <div class="hero-status">{status_line()}</div>
        </div>{_opportunity_cards()}</div>""")
        with st.container(key="hero_actions"):
            scan_actions()

    healthy_note = " · ".join(f"{n_by_status[k]} {k}" for k in ("healthy", "delayed", "failing", "pending") if n_by_status[k]) or "—"
    st.html('<div class="stats">'
            + _stat("Active jobs", len(active_jobs),
                    _plural(len(dismissed_jobs), 'dismissed job') if dismissed_jobs else 'Fresher &amp; entry-level', "work")
            + _stat("New this week", new_this_week, "Found in the last 7 days", "trending_up", "t-teal")
            + _stat("Companies monitored", len(companies), healthy_note, "apartment",
                    "t-red" if n_by_status["failing"] else "t-green")
            + _stat("Last scan", _ago(last_scan, NOW), f"Next scheduled {_ist(NEXT_SCAN)}", "update",
                    "t-amber" if scan_stale or SCAN_OVERDUE else "", small=True)
            + "</div>")

    if not active_jobs:
        def _acts():
            a, b = st.columns(2)
            with a:
                if dismissed_jobs:
                    st.button(f"View {_plural(len(dismissed_jobs), 'dismissed job')}", key="btn_home_dismissed",
                              on_click=lambda: (go("jobs"), st.session_state.update(jobs_tab="dismissed")))
            with b:
                st.button("Open monitoring", key="btn_home_mon", on_click=go, args=("monitoring",))
        empty_state("travel_explore", "No alerts yet",
                    f"The tracker checks {_plural(len(companies), 'company', 'companies')} on a schedule of {interval_label(SCAN_HOURS).lower()} "
                    "(GitHub often starts it later). New fresher and "
                    "entry-level roles in India appear here — and in your inbox — as soon as a portal publishes them.",
                    _acts)
        return

    st.html('<div class="sec-head"><div><div class="eyebrow sparked">Latest opportunities</div><h2 class="section-title">Latest jobs</h2>'
            '<p class="section-sub">Newest fresher and entry-level roles found on the portals you track</p></div></div>')
    _home_list()
    st.html(f"""<div class="how">
      <div><span class="ic">{_ms("travel_explore")}</span><div><b>Tracks career portals</b><p>Each company's job list is scanned on a schedule — no manual searching.</p></div></div>
      <div><span class="ic">{_ms("article")}</span><div><b>Reads every posting</b><p>A job is judged from its own page, never from its title alone.</p></div></div>
      <div><span class="ic">{_ms("mark_email_read")}</span><div><b>Emails each role once</b><p>Only India fresher and entry-level jobs that pass every check are sent.</p></div></div>
    </div>""")


@st.fragment
def _home_list() -> None:
    """Home's filters and latest jobs. A fragment: changing a filter redraws
    only this part, not the sidebar, header, menu and hero."""
    _leave_fragment_if_needed()
    before = dict(_USED_LOGOS)
    shown, active, clear = filter_jobs("h", active_jobs)
    if not shown:
        empty_state("search_off", "No jobs match these filters",
                    "Try a different search or clear the filters to see every job.",
                    lambda: st.button("Clear filters", key="btn_reset_filters", on_click=clear))
        return
    with st.container(key="joblist_home"):
        for i, j in enumerate(shown[:HOME_LIMIT]):
            job_row(j, f"h{i}", "home")
        with st.container(key="list_foot_h"):
            f1, f2 = st.columns([3, 1], vertical_alignment="center")
            with f1:
                st.html(f'<span class="muted num" style="font-size:13px;">Showing {min(HOME_LIMIT, len(shown))} of {_plural(len(shown), "job")}</span>')
            with f2:
                st.button("View all jobs", key="btn_all_jobs", icon=":material/arrow_forward:", icon_position="right",
                          on_click=go, args=("jobs",))
    _fragment_logos(before)


def page_jobs():
    page_header("Jobs", f"{_plural(len(all_records), 'job')} found by the tracker · {len(active_jobs)} active · "
                        f"{len(dismissed_jobs)} dismissed")
    st.session_state.setdefault("jobs_tab", "active")
    _jobs_list()


@st.fragment
def _jobs_list() -> None:
    """Jobs' Active / Dismissed switch, filters, list and pagination. A
    fragment: switching tabs, searching, sorting, filtering and paging
    redraw only this part of the page (a dismissal still draws the whole
    page once, since the counts around the list change too)."""
    _leave_fragment_if_needed()
    with st.container(key="jobs_tab_wrap"):
        tab = st.pills("Show", ["active", "dismissed"], key="jobs_tab", label_visibility="collapsed",
                       format_func=lambda t: f"Active ({len(active_jobs)})" if t == "active" else f"Dismissed ({len(dismissed_jobs)})")
    tab = tab or "active"
    if not (active_jobs if tab == "active" else dismissed_jobs):
        empty_state("inbox", "No alerts yet" if tab == "active" else "Nothing dismissed",
                    "New jobs appear here after the next scan." if tab == "active"
                    else "Jobs you dismiss are kept here so they're never emailed twice — you can restore them anytime.")
        return
    before = dict(_USED_LOGOS)
    source = active_jobs if tab == "active" else dismissed_jobs
    shown, active, clear = filter_jobs("j", source)
    sig = (tab, st.session_state.get("j_filtered_sig"))
    if st.session_state.get("_jobs_sig") != sig:
        st.session_state._jobs_sig = sig
        st.session_state.jobs_page = 0
    if not shown:
        empty_state("search_off", "No jobs match these filters", "Try a different search or clear the filters.",
                    lambda: st.button("Clear filters", key="btn_reset_filters", on_click=clear))
        return
    total = len(shown)
    last_page = (total - 1) // JOBS_PAGE
    page = min(st.session_state.jobs_page, last_page)
    start, end = page * JOBS_PAGE, min(total, page * JOBS_PAGE + JOBS_PAGE)
    with st.container(key="joblist_jobs"):
        for i, j in enumerate(shown[start:end]):
            job_row(j, f"j{i}", "jobs")
        with st.container(key="list_foot_j"):
            f1, f2, f3 = st.columns([3, 1, 1], vertical_alignment="center")
            with f1:
                st.html(f'<span class="muted num" style="font-size:13px;">{start + 1}–{end} of {total}</span>')
            with f2:
                st.button("Previous", key="alerts_prev", icon=":material/chevron_left:", disabled=page == 0,
                          on_click=st.session_state.__setitem__, args=("jobs_page", page - 1))
            with f3:
                st.button("Next", key="alerts_next", icon=":material/chevron_right:", icon_position="right",
                          disabled=end >= total, on_click=st.session_state.__setitem__, args=("jobs_page", page + 1))
    _fragment_logos(before)


def _distinct(lines: list[str]) -> list[str]:
    """Each line once, in order — lines that differ only in spacing, case or
    punctuation ("Experience: 0 to 1 years" / "Experience :0 to 1 years") count
    as the same line."""
    seen, out = set(), []
    for line in lines:
        key = re.sub(r"[\W_]+", "", line).lower()
        if key and key not in seen:
            seen.add(key)
            out.append(line)
    return out


def page_job_detail(j: dict):
    back = st.session_state.get("job_from", "jobs")
    back_co = next((x for x in companies if back == "companies"
                    and str(x.get("id")) == str(st.session_state.get("job_from_company"))), None)
    back_label = (back_co.get("name") or "company").strip() if back_co else PAGES.get(back, PAGES["jobs"])[0]
    back_kwargs = {"company": back_co.get("id")} if back_co else {}
    st.button(f"Back to {back_label}", key="btn_back", icon=":material/arrow_back:",
              type="tertiary", on_click=go, args=(back,), kwargs=back_kwargs)
    jkey = job_key(j)
    title = (j.get("title") or "Untitled posting").strip() or "Untitled posting"
    company = (j.get("company") or "").strip()
    c = find_company(company, j.get("company_id"))
    pill_cls, pill_label = job_category(j)
    url = safe_url(j.get("url", ""))
    d = found[id(j)]
    ev = j.get("evidence") if isinstance(j.get("evidence"), dict) else {}
    # records carry evidence only when the scraper's safety gate ran; a
    # malformed/partial evidence field is treated as "no evidence"
    checks = ev.get("checks") if isinstance(ev.get("checks"), dict) else {}
    has_ev = bool(checks)
    raw_lines = ev.get("experience_lines") if isinstance(ev.get("experience_lines"), list) else []
    exp_lines = [x.strip(" ·•-*	") for x in raw_lines if isinstance(x, str)
                 and re.search(r"experience|\bexp\b|fresher|graduat|\d\s*(?:\+\s*)?(?:years?|yrs?|months?)\b", x, re.I)]
    exp_lines = _distinct(exp_lines)
    ev_exp = _distinct([str(x) for x in ev.get("experience") or [] if x] if isinstance(ev.get("experience"), list) else [])
    if ev_exp and ev_exp != ["no experience requirement stated"]:
        experience = escape(", ".join(ev_exp))
    elif exp_lines:
        experience = escape(exp_lines[0])
    elif has_ev:
        experience = "No requirement stated in the posting"
    elif re.search(r"(years|months) experience|No prior experience", friendly_reason(j)):
        experience = escape(friendly_reason(j))
    else:
        experience = "Not recorded for this job"

    with st.container(key="job_hero"):
        chips = []
        if j.get("location"):
            chips.append(f'<span class="chip">{_ms("location_on")}{escape(j["location"])}</span>')
        if pill_cls != "legacy":
            chips.append(f'<span class="chip">{_ms("work_history")}{escape(job_why(j))}</span>')
        if d:
            chips.append(f'<span class="chip num">{_ms("schedule")}Found {_ago(d, NOW)}</span>')
        st.html(f"""
        <div class="detail-head">
          {_avatar(company, "lg")}
          <div style="min-width:0;">
            <div class="eyebrow">{escape(company or 'Unknown company')}</div>
            <h1 class="detail-title">{escape(title)}</h1>
            <div class="detail-chips">{badge(pill_cls, pill_label)}
              {'<span class="pill neutral">Dismissed</span>' if is_dismissed(j) else ''}{"".join(chips)}</div>
          </div>
        </div>""")
        with st.container(key="job_actions"):
            a = st.columns(4)
            with a[0]:
                st.html(apply_link(j, "Apply on company site"))
            with a[1]:
                if is_dismissed(j):
                    st.button("Restore", key="detail_restore", icon=":material/undo:", on_click=_dismiss_click,
                              args=(jkey, False))
                else:
                    with st.container(key="danger"):
                        st.button("Dismiss", key="detail_dismiss", icon=":material/close:", on_click=_detail_dismiss_click,
                                  args=(jkey, back, back_kwargs))
            with a[2]:
                if c:
                    st.button("Company details", key="btn_job_company", icon=":material/apartment:",
                              on_click=go, args=("companies",), kwargs={"company": c.get("id")})

    with st.container(key="job_cols"):
        main, side = st.columns([3, 2])
    with main:
        with st.container(key="job_main"):
            reason = str(j.get("reason") or "").strip()
            classified = bool(reason) or j.get("category") in _CATEGORY_PILL
            st.html(f"""<div class="eyebrow bar">Why this qualifies</div><h2 class="section-title" style="margin-top:6px;">Why this matched</h2>
              <div class="why">{_ms("task_alt" if classified else "history", "s20")}<div>{escape(job_why(j, long=True))}
              {f'<div class="quote"><span class="muted">Classifier trace:</span> {escape(reason)}</div>' if reason else ''}
              {'' if classified else '<div class="note" style="margin-top:6px;">No classifier trace was recorded for this job.</div>'}
              </div></div>""")
            if has_ev:
                passed = sum(1 for v in checks.values() if v)
                labels = {"real_job_posting": "Real job posting", "india_location": "Located in India",
                          "detail_read": "Job's own posting was read", "detail_is_this_job": "Posting belongs to this job",
                          "no_conflicting_experience": "No conflicting experience requirement",
                          "not_programme_story_talent_recruiter": "Not a programme / story / recruiter page",
                          "evidence_not_staff_context": "Evidence is about the applicant",
                          "fresher_or_entry_evidence": "Fresher / entry-level evidence found"}
                items = "".join(f'<li class="{"" if ok else "off"}">{_ms("check_circle" if ok else "cancel")}'
                                f'<span><span class="sr-only">{"Passed: " if ok else "Failed: "}</span>{escape(labels.get(k, k))}</span></li>'
                                for k, ok in checks.items())
                meter = "".join(f'<span class="{"" if ok else "off"}"></span>' for ok in checks.values())
                quote_lines = "".join(f'<div class="quote">{escape(x)}</div>' for x in exp_lines[:3])
                st.html(f"""<div class="divider"></div>
                  <div class="checks-head"><div><div class="eyebrow">Evidence &amp; safety checks</div>
                    <h2 class="section-title" style="margin-top:4px;">{_ms("shield", "s20")}Checked before it was alerted</h2></div>
                    <span class="checks-score">{_ms("task_alt")}{passed} of {len(checks)} checks passed</span></div>
                  <div class="meter" style="--n:{len(checks)}" aria-hidden="true">{meter}</div>
                  <ul class="checks">{items}</ul>
                  <dl class="kv">
                    <dt>Job ID</dt><dd class="num">{escape(ev.get("job_id") or ats_job_id(j.get("url", "")) or "—")}</dd>
                    <dt>Detail matched by</dt><dd>{escape(ev.get("detail_match") or "—")}</dd>
                    <dt>Fresher evidence</dt><dd>{escape(ev.get("fresher_evidence") or "—")}</dd></dl>
                  {f'<div class="note" style="margin-top:10px;">From the posting:</div>{quote_lines}' if quote_lines else ''}""")
            rows = [("Company", escape(company or "—")),
                    ("Location", escape(j.get("location") or "Not captured for this posting")),
                    ("Category", escape(pill_label)),
                    ("Experience", experience),
                    ("Found", (f'<span class="num">{escape(_ist_stamp(d))}</span> <span class="muted">({_ago(d, NOW)})</span>' if d
                               else f'<span class="num">{escape(j.get("date") or "—")} UTC</span>')),
                    ("Status", "Dismissed" if is_dismissed(j) else "Active")]
            st.html('<div class="divider"></div><div class="eyebrow">Posting information</div>'
                    '<h2 class="section-title" style="margin-top:4px;">Job overview</h2><dl class="kv">'
                    + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows) + "</dl>")
            with st.expander("Description & requirements"):
                st.html('<p class="note">The scraper reads the full posting to classify it but only keeps the evidence '
                        'above, not the full description. Open the posting on the company site for the complete details.</p>')
    with side:
        with st.container(key="job_side"):
            src = [("Portal", escape(_host(url)) if url else "—"),
                   ("Platform", "Workday" if "myworkdayjobs.com" in url else "Company career site"),
                   ("Posting", f'<a class="link" href="{escape(url, quote=True)}" target="_blank" rel="noopener">Open posting {_ms("open_in_new", "s16")}</a>' if url else "No link"),
                   ("Email alert", "Sent" if j.get("notified")
                    else "Sending — delivery not yet confirmed" if j.get("notify_state") == "claimed"
                    else "Not sent (dismissed)" if j.get("dismissed")
                    else "Pending — goes out after the next check" if alert_pending(j)
                    else "Not emailed")]
            st.html(f'<h2 class="section-title">{_ms("link")}Source</h2><dl class="kv" style="grid-template-columns:110px minmax(0,1fr);">'
                    + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in src) + "</dl>")
            st.html('<div class="divider"></div>')
            if c:
                k, lbl = statuses.get(c.get("id"), ("pending", "Pending"))
                curl = safe_url(c.get("url", ""))
                co = [("Monitoring", f'<span class="pill {k}"><i></i>{lbl}</span>'),
                      ("Career portal", f'<a class="link" href="{escape(curl, quote=True)}" target="_blank" rel="noopener">{escape(_short_url(curl, 30))}</a>' if curl else "—"),
                      ("Last checked", f'<span class="num">{_ago(_parse_iso(c.get("last_checked", "")), NOW)}</span>'),
                      ("Active jobs", str(company_active(c)))]
                st.html(f'<h2 class="section-title">{_ms("apartment")}Company</h2><dl class="kv" style="grid-template-columns:110px minmax(0,1fr);">'
                        + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in co) + "</dl>")
            else:
                st.html(f'<h2 class="section-title">{_ms("apartment")}Company</h2><p class="note">This company is no longer tracked.</p>')


def page_companies():
    def _acts():
        st.button("Add company", key="btn_open_add", type="primary", icon=":material/add:",
                  on_click=go, args=("companies",), kwargs={"view": "add"})
    page_header("Companies", f"{_plural(len(companies), 'career portal')} monitored · checks {SCHEDULE_NOTE}", _acts)
    if not companies:
        empty_state("apartment", "No companies yet", "Add a company's career page and the tracker will start checking it on the next scan.")
        return
    with st.container(key="co_toolbar"):
        f1, f2 = st.columns([2, 3], vertical_alignment="center")
        with f1:
            q = st.text_input("Search companies", key="co_q", placeholder="Search companies or portals",
                              label_visibility="collapsed").strip().lower()
        with f2:
            with st.container(key="co_status_wrap"):
                opts = ["all", "healthy", "delayed", "failing", "pending"]
                sel = st.pills("Status", [o for o in opts if o == "all" or n_by_status[o]], key="co_status",
                               label_visibility="collapsed", default="all",
                               format_func=lambda o: f"All ({len(companies)})" if o == "all" else f"{o.title()} ({n_by_status[o]})")
    sel = sel or "all"
    rows = [c for c in companies
            if (not q or q in f"{c.get('name', '')} {c.get('url', '')}".lower())
            and (sel == "all" or statuses[c.get("id")][0] == sel)]
    if not rows:
        empty_state("search_off", "No companies match", "Try another name or status.")
        return
    with st.container(key="company_list"):
        st.html('<div class="list-head"><span>Company · career portal</span><span>Website</span><span>Status</span><span>Active jobs</span><span>Last checked</span><span></span></div>')
        for c in rows:
            name = (c.get("name") or "").strip() or "Unnamed"
            curl = safe_url(c.get("url", ""))
            site = safe_url(c.get("website", ""))
            k, lbl = statuses[c.get("id")]
            is_new = c.get("id") == st.session_state.just_added
            tags = ('<span class="tag" title="Built-in company">Core</span>' if c.get("locked") else "") + \
                   ('<span class="pill new">New</span>' if is_new else "")
            # the card's text, then its button (stacked by CSS; no st.columns)
            with st.container(key=f"cr_{_widget_key('c', str(c.get('id')))}"):
                st.html(f"""<div class="co-row">
                  <div class="co-name">{_avatar(name)}
                    <div style="min-width:0;"><div class="t">{escape(name)}{tags}</div>
                    <div class="h">{escape(_short_url(curl)) if curl else '<span style="color:var(--red)">Invalid career page URL</span>'}</div></div></div>
                  <div class="co-cell site">{_ms("language")}{f'<a class="link" href="{escape(site, quote=True)}" target="_blank" rel="noopener">{escape(_short_url(site, 28))}</a>' if site else '<span class="muted">Not set</span>'}</div>
                  <div><span class="pill {k}"><i></i>{lbl}</span></div>
                  <div class="co-stats">
                    <div><span class="k">Openings</span><div class="co-cell">{_ms("work")}{company_active(c)}<span class="l"> active jobs</span></div></div>
                    <div><span class="k">Last scan</span><div class="co-cell">{_ms("schedule")}{_ago(_parse_iso(c.get("last_checked", "")), NOW)}</div></div>
                  </div>
                </div>""")
                st.button("View company", key=f"view_{_widget_key('c', str(c.get('id')))}", icon=":material/arrow_forward:",
                          icon_position="right", on_click=go, args=("companies",), kwargs={"company": c.get("id")})


def page_add_company():
    st.button("Back to Companies", key="btn_back", icon=":material/arrow_back:", type="tertiary",
              on_click=go, args=("companies",))
    page_header("Add a company", "Start monitoring a company's career portal for fresher and entry-level roles")
    st.html(f"""<div class="add-steps">
      <div><b>{_ms("link")}1 · Paste the job search page</b>The page that lists individual openings — not the careers landing page.</div>
      <div><b>{_ms("travel_explore")}2 · It joins the next scan</b>Every posting found there is opened and read on the scheduled check.</div>
      <div><b>{_ms("mark_email_read")}3 · Fresher roles reach you</b>India fresher and entry-level jobs appear here and in your inbox.</div>
    </div>""")
    with st.container(key="add_form"):
        name = st.text_input("Company name", placeholder="e.g. Infosys", key="new_name")
        url = st.text_input("Career portal URL", placeholder="https://careers.example.com/jobs", key="new_url",
                            help="The page that lists open jobs — not the company's home page.")
        site = st.text_input("Company website (optional)", placeholder="https://www.example.com", key="new_website")
        st.html(f"""<div class="note" style="display:flex;gap:10px;align-items:flex-start;">
          {_ms("info", "s20")}<div>Use the page that lists individual job postings (its job search), not a careers
          landing page. Workday URLs (<span class="num">*.myworkdayjobs.com</span>) are read through Workday's API;
          other pages are opened with a headless browser (Playwright), each job link is followed, and roles in India
          that the classifier marks as fresher or entry level are kept. If the page shows no job postings the
          company is marked <b>Needs configuration</b> rather than healthy.</div></div>""")
        b1, b2 = st.columns([1, 4])
        with b1:
            submit = st.button("Start tracking", key="btn_add", type="primary", use_container_width=True)
        with b2:
            st.button("Cancel", key="btn_cancel_add", on_click=go, args=("companies",))
    if submit:
        n, u, w = name.strip(), url.strip(), site.strip()
        if not owner_only("add companies"):
            pass
        elif not n:
            toast("Company name is required", "error")
        elif not safe_url(u):
            toast("Enter the full career page URL, starting with https://", "error")
        elif w and not safe_url(w):
            toast("The website must be a full URL starting with https://", "error")
        elif find_company(n):
            toast(f"{n} is already tracked", "error")
        elif any((c.get("url") or "").rstrip("/") == u.rstrip("/") for c in companies):
            toast("That career page is already tracked", "error")
        else:
            new = {"id": f"c{int(time.time())}", "name": n, "url": u, "locked": False,
                   "status": "unknown", "last_job": "", "last_checked": ""}
            if w:
                new["website"] = w

            def _add(latest: list) -> list:
                if not any((c.get("name") or "").strip().lower() == n.lower() for c in latest):
                    latest.append(new)
                return latest

            _, saved, err = _save_change("data/companies.json", _add, [], f"chore: add {n}")
            toast(f"{n} added — it will be checked on the next scan" if saved
                  else f"{n} added, but not saved permanently. {err}", "success" if saved else "error")
            st.session_state.just_added = new["id"]
            st.session_state._clear_add_form = True
            go("companies")
        st.rerun()


def _catalogue_ids() -> set[str]:
    return {str(x.get("id")) for x in companies if not str(x.get("id", "")).startswith("_row")}


def set_following(cid: str, follow: bool, name: str = "") -> None:
    """Follow / unfollow in the viewer's own watchlist (only IDs from the
    shared catalogue; the catalogue itself never changes)."""
    try:
        USER.watch(cid, _catalogue_ids()) if follow else USER.unwatch(cid)
    except (user_store.UserStoreError, user_store.InvalidInput) as e:
        personal_changed()
        toast(str(e) if isinstance(e, user_store.UserStoreError) else "That company can't be followed.", "error")
        return
    _personal_patched(watch_add=(cid,) if follow else (), watch_remove=() if follow else (cid,))
    toast(f"Following {name}" if follow else f"Unfollowed {name}", "success")


def follow_button(c: dict) -> None:
    """Follow / Unfollow for the signed-in viewer's own watchlist."""
    cid = c.get("id")
    if ME is None or cid not in _catalogue_ids():
        return
    following = cid in ME["watchlist"]
    st.button("Following" if following else "Follow", key="btn_follow",
              icon=":material/check:" if following else ":material/add:", type="secondary" if following else "primary",
              help="Unfollow" if following else "Follow this company in your account",
              on_click=set_following, args=(cid, not following, c.get("name") or ""))


def _ask_remove_company(cid) -> None:
    if owner_only("remove companies"):
        st.session_state.confirm_remove = cid


def _remove_company(cid, name: str) -> None:
    """Stop tracking a company (the owner check is repeated in _save_change)."""
    if not owner_only("remove companies"):
        st.session_state.confirm_remove = None
        return
    _, saved, err = _save_change("data/companies.json", lambda latest: [x for x in latest if x.get("id") != cid],
                                 [], f"chore: remove {name}")
    toast(f"Removed {name}" if saved else f"Removed {name}, but not saved permanently. {err}",
          "success" if saved else "error")
    go("companies")


def page_company_detail(c: dict):
    st.button("Back to Companies", key="btn_back", icon=":material/arrow_back:", type="tertiary",
              on_click=go, args=("companies",))
    name = (c.get("name") or "").strip() or "Unnamed"
    curl = safe_url(c.get("url", ""))
    site = safe_url(c.get("website", ""))
    k, lbl = statuses[c.get("id")]
    jobs = company_jobs(c)
    hero = st.container(key="co_hero")
    hero.html(f"""<div class="detail-head">{_avatar(name, "lg")}<div style="min-width:0;">
      <div class="eyebrow">Tracked company</div>
      <h1 class="detail-title">{escape(name)}</h1>
      <div class="detail-chips">{"" if MEMBER_VIEW else f'<span class="pill {k}"><i></i>{lbl}</span>'}
        {'<span class="tag">Core</span>' if c.get('locked') else ''}
        <span class="chip">{_ms("work")}<span>{company_active(c)} active · {sum(1 for j in jobs if is_dismissed(j))} dismissed</span></span>
        <span class="chip num">{_ms("schedule")}Checked {_ago(_parse_iso(c.get("last_checked", "")), NOW)}</span></div></div></div>""")
    with hero, st.container(key="co_actions"):
        a = st.columns(4)
        with a[0]:
            if curl:
                st.html(f'<a class="btn primary" href="{escape(curl, quote=True)}" target="_blank" rel="noopener">Career portal {_ms("arrow_outward", "s16")}</a>')
        with a[1]:
            if site:
                st.html(f'<a class="btn ghost" href="{escape(site, quote=True)}" target="_blank" rel="noopener">Website {_ms("arrow_outward", "s16")}</a>')
        with a[3]:
            follow_button(c)
        with a[2]:
            with st.container(key="danger"):
                if not MEMBER_VIEW and st.session_state.confirm_remove != c.get("id"):
                    st.button("Stop tracking", key="btn_remove_company", icon=":material/delete:",
                              on_click=_ask_remove_company, args=(c.get("id"),))
    if st.session_state.confirm_remove == c.get("id"):
        with st.container(key="danger_confirm"):
            st.html(f'<p class="note" style="color:var(--text);">Stop tracking <b>{escape(name)}</b>? Jobs already found stay in your history.</p>')
            y, n_ = st.columns([1, 4])
            with y:
                st.button("Yes, stop tracking", key="btn_remove", type="primary", on_click=_remove_company,
                          args=(c.get("id"), name))
            with n_:
                st.button("Cancel", key="btn_cancel_remove", on_click=st.session_state.__setitem__,
                          args=("confirm_remove", None))

    with st.container(key="co_cols"):
        main, side = st.columns([3, 2])
    with side:
        with st.container(key="co_side"):
            checked = _parse_iso(c.get("last_checked", ""))
            health = [("Status", f'<span class="pill {k}"><i></i>{lbl}</span>'),
                      ("Last checked", f'<span class="num">{_ago(checked, NOW)}</span>' + (f' <span class="muted num">({_ist_stamp(checked)})</span>' if checked else "")),
                      ("Scraper", escape(_source_label(c))),
                      ("Latest scan", escape(c.get("last_job") or "No matching job on the last scan"))]
            if k == "failing":
                why = escape(c.get("status_reason") or "The last scan failed.")
                health.append(("Problem", f'{why} <a class="link" href="{ACTIONS_URL}" target="_blank" rel="noopener">See the Actions log</a>'))
            if not MEMBER_VIEW:
                st.html(f'<h2 class="section-title">{_ms("monitor_heart")}Portal health</h2><dl class="kv" style="grid-template-columns:110px minmax(0,1fr);">'
                        + "".join(f"<dt>{a}</dt><dd>{b}</dd>" for a, b in health) + "</dl>")
                st.html('<div class="divider"></div>')
            cats = {"FRESHER": 0, "ENTRY_LEVEL": 0, "legacy": 0}
            for j in jobs:
                cats[j.get("category") if j.get("category") in _CATEGORY_PILL else "legacy"] += 1
            info = [("Career portal", f'<a class="link" href="{escape(curl, quote=True)}" target="_blank" rel="noopener">{escape(_short_url(curl, 30))}</a>' if curl else "Invalid URL"),
                    ("Website", f'<a class="link" href="{escape(site, quote=True)}" target="_blank" rel="noopener">{escape(_short_url(site, 30))}</a>' if site else '<span class="muted">Not set</span>'),
                    ("Classified", f"{cats['FRESHER']} fresher · {cats['ENTRY_LEVEL']} entry level"
                                   + (f" · {cats['legacy']} keyword match" if cats["legacy"] else ""))]
            st.html(f'<h2 class="section-title">{_ms("info")}Details</h2><dl class="kv" style="grid-template-columns:110px minmax(0,1fr);">'
                    + "".join(f"<dt>{a}</dt><dd>{b}</dd>" for a, b in info) + "</dl>")
    with main:
        st.html(f'<div class="sec-head"><div><div class="eyebrow">Openings</div><h2 class="section-title">Recent jobs from {escape(name)}</h2>'
                f'<p class="section-sub">{_plural(len(jobs), "job")} found · {sum(1 for j in jobs if is_dismissed(j))} dismissed</p></div></div>')
        if jobs:
            with st.container(key="joblist_company"):
                for i, j in enumerate(jobs[:10]):
                    job_row(j, f"c{i}", "companies")
        else:
            empty_state("inbox", "No jobs found yet", "Matching fresher and entry-level roles from this portal will appear here.")


def page_monitoring():
    page_header("Monitoring", f"Every tracked portal is checked {SCHEDULE_NOTE}", scan_actions)
    tone = {"healthy": "t-green", "failing": "t-red", "delayed": "t-amber"}.get(overall[0], "t-gray")
    icon = {"healthy": "health_and_safety", "failing": "error", "delayed": "schedule"}.get(overall[0], "hourglass_empty")
    st.html('<div class="stats">'
            + _stat(f'<span class="dot {overall[0]}"></span>Overall', escape(overall[1]),
                    f"{_plural(len(companies), 'portal')} tracked", icon, tone, small=True)
            + _stat("Last scan", _ago(last_scan, NOW), _ist_stamp(last_scan) if last_scan else "No scan recorded",
                    "history", "t-amber" if scan_stale or SCAN_OVERDUE else "t-teal", small=True)
            + _stat("Next scheduled scan", _ist(NEXT_SCAN), NEXT_SCAN_NOTE, "schedule", "", small=True)
            + _stat("Portals", f'{n_by_status["healthy"]}<span class="muted" style="font-size:16px;font-weight:600;"> / {len(companies)} healthy</span>',
                    f"{n_by_status['delayed']} delayed · {n_by_status['failing']} failing · {n_by_status['pending'] + n_by_status['checking']} pending",
                    "lan", "t-red" if n_by_status["failing"] else "t-green")
            + "</div>")
    if not companies:
        empty_state("monitor_heart", "Nothing to monitor", "Add a company to start scanning its career portal.")
        return
    rows = []
    order = {"failing": 0, "delayed": 1, "checking": 2, "pending": 3, "healthy": 4}
    for c in sorted(companies, key=lambda c: (order[statuses[c.get("id")][0]], (c.get("name") or "").lower())):
        k, lbl = statuses[c.get("id")]
        curl = safe_url(c.get("url", ""))
        checked = _parse_iso(c.get("last_checked", ""))
        reason = escape(c.get("status_reason") or "")
        ok_at = _parse_iso(c.get("last_success", ""))
        since = f" · last succeeded {_ago(ok_at, NOW)}" if ok_at else ""
        note = {"failing": f'{reason or "Last scan failed"}{since} — <a class="link" href="{ACTIONS_URL}" target="_blank" rel="noopener">see log</a>',
                "delayed": f"No scan for over {_STALE_H} hours",
                "pending": "Not scanned yet — added since the last run",
                "checking": "Check requested"}.get(k, escape(c.get("scan_note") or "") or "—")
        rows.append(f"""<tr>
          <td class="c"><div class="mon-co">{_avatar(c.get('name') or '', "sm")}<div style="min-width:0;">{escape((c.get('name') or '').strip() or 'Unnamed')}
            <div class="sub">{f'<a class="link" href="{escape(curl, quote=True)}" target="_blank" rel="noopener">{escape(_short_url(curl, 34))}</a>' if curl else 'Invalid URL'}</div></div></div></td>
          <td data-l="Status"><span class="pill {k}"><i></i>{lbl}</span></td>
          <td class="num" data-l="Last checked">{_ago(checked, NOW)}</td>
          <td class="num" data-l="Active jobs">{company_active(c)}</td>
          <td data-l="Scraper">{escape(_source_label(c))}</td>
          <td data-l="Notes"{' class="empty"' if note == "—" else ""}>{note}</td></tr>""")
    if not (n_by_status["failing"] or n_by_status["delayed"]) and last_scan:
        st.html(f'<div class="legend" style="border-style:solid;">{_ms("task_alt", "s20")}<div class="note"><b style="color:var(--text);">No monitoring issues.</b> '
                f'Every portal was scanned successfully in the last {_STALE_H} hours.</div></div>')
    with st.container(key="mon_table"):
        st.html('<table class="mon"><thead><tr><th>Company</th><th>Status</th><th>Last checked</th><th>Active jobs</th>'
                '<th>Scraper</th><th>Notes</th></tr></thead><tbody>' + "".join(rows) + "</tbody></table>")
    email_tailoring_card()
    st.html(f'<div class="legend">{_ms("info", "s20")}<p class="note">Statuses come from the scraper: <b>Healthy</b> = scanned successfully in the last {_STALE_H} hours · '
            f'<b>Delayed</b> = no successful scan for {_STALE_H}+ hours · <b>Failing</b> = the last scan partly failed '
            f'(errors, rate limits or unreadable job pages) · <b>Broken</b> = the job list could not be read (blocked, HTTP error, '
            f'site changed) · <b>Needs configuration</b> = the URL shows no job postings · '
            f'<b>Pending</b> = not scanned yet. Full run logs: <a class="link" href="{ACTIONS_URL}" target="_blank" rel="noopener">GitHub Actions</a>.</p></div>')


def _save_my_alerts(prefs: dict) -> None:
    """The alert form's Save (a callback: the page is drawn once, after it).
    One read and one write; the session's copy is patched, not re-read."""
    enabled = st.session_state.get("my_alerts_on")
    scope = st.session_state.get("my_alerts_scope")
    try:
        # the mode is kept as saved: "Tailored" (job preferences) is an
        # older setting that stays until the person switches it off below
        fields = USER.save_alert_settings(bool(enabled) and bool(prefs["email"]), prefs["mode"], scope == "all")
    except (user_store.UserStoreError, ValueError) as e:
        personal_changed()
        toast(str(e) if isinstance(e, user_store.UserStoreError) else "Those settings couldn't be saved.", "error")
        return
    _personal_patched(profile=fields)
    toast("Alert settings saved", "success")


def my_alerts_card() -> None:
    """The viewer's own alert emails: on/off (to their verified sign-in
    email only) and which companies they cover. WHICH jobs is the separate,
    optional "Tailor my email alerts" card."""
    if USER is None or ME is None:
        return
    prefs = user_store.notification_settings_from(ME["profile"])
    followed = ME["watchlist"]
    with st.container(key="set_my_alerts"):
        dest = (f'<span class="num">{escape(prefs["email"])}</span> (your verified sign-in email)' if prefs["email"]
                else "your sign-in email isn't verified, so personal alerts can't be turned on")
        st.html(f'<div class="set-head"><span class="ic">{_ms("notifications")}</span><div><div class="eyebrow">Your account</div>'
                f'<h2 class="section-title">Alert emails</h2><p class="section-sub">Sent to {dest}. New qualifying jobs are '
                f'emailed at most once every {scan_schedule.MEMBER_ALERT_HOURS} hours, each job once — never the backlog '
                'from before you turned them on.</p></div></div>')
        with st.form("my_alerts_form", border=False):
            enabled = st.toggle("Email me new jobs", value=prefs["enabled"], key="my_alerts_on",
                                disabled=not prefs["email"])
            scope = st.radio("Companies", ["followed", "all"], index=0 if not _VIEW["watch_all"] else 1,
                             key="my_alerts_scope", horizontal=True,
                             format_func=lambda v: {"all": "Every tracked company",
                                                    "followed": f"Only companies I follow ({len(followed)})"}[v])
            st.form_submit_button("Save", key="btn_my_alerts", type="primary", icon=":material/save:",
                                  on_click=_save_my_alerts, args=(prefs,))
        if prefs["enabled"]:
            last = _parse_iso(user_store.alerts_last_sent_from(ME["profile"]).replace("Z", "+00:00"))
            nxt = last + timedelta(hours=scan_schedule.MEMBER_ALERT_HOURS) if last else None
            when = (f"your next email can go out after <b class=\"num\">{escape(_ist_stamp(nxt))}</b>"
                    if nxt and nxt > NOW else "your next email goes out after the next scan that finds new jobs for you")
            st.html(f'<p class="note">{_ms("schedule", "s16")} At most one email every '
                    f'{scan_schedule.MEMBER_ALERT_HOURS} hours, with every new job since — {when}.</p>')
        if scope == "followed" and not followed and prefs["mode"] != "tailored":
            st.html(f'<p class="note">{_ms("info", "s16")} You don’t follow any companies yet, so there is nothing to '
                    'email — follow some in My companies, or choose “Every tracked company”.</p>')
        if prefs["mode"] == "tailored":
            st.html(f'<p class="note">{_ms("info", "s16")} Your alerts also follow your saved job preferences '
                    '(an older setting). Use “Tailor my email alerts” instead to choose exactly what is emailed.</p>')
            st.button("Stop matching my job preferences", key="btn_alerts_legacy_off", icon=":material/tune:",
                      on_click=_alerts_mode_general)


def _alerts_mode_general() -> None:
    """Switch an older "Tailored" (job preferences) alert setting back to
    every qualifying job — on/off and everything else unchanged."""
    try:
        prefs = user_store.notification_settings_from(USER.profile() or {})
        USER.set_notifications(prefs["enabled"], "general")
        toast("Alerts no longer follow your job preferences", "success")
    except (user_store.UserStoreError, ValueError) as e:
        toast(str(e) if isinstance(e, user_store.UserStoreError) else "That couldn't be saved.", "error")
    finally:
        personal_changed()


# ── "Tailor my email alerts": an optional, personal narrowing of the EMAILS ──
# Stored in the person's own profile (notifications.tailoring), applied only when the
# notifier decides what to email this person (user_alerts.select_jobs, after
# the shared eligibility gate, their companies and their dismissals). It never
# touches the job lists, their browsing filters or anyone else.
TAILOR_FIELDS = (("locations", "Locations", "location_on", "Any of these places"),
                 ("job_families", "Job families", "work", "Any of these kinds of work, matched on each job’s title"))


def _tl_key(field: str) -> str:
    """The pickers' widget keys, per account: another sign-in in this same
    browser never starts from someone else's unsaved picks."""
    return _widget_key(f"tl_{field}", USER.uid)


def my_tailoring() -> dict:
    return _VIEW["tailoring"] if _VIEW is not None else {f: [] for f, *_ in TAILOR_FIELDS}


def tailoring_summary(t: dict) -> str:
    return " — ".join(" · ".join(t.get(f) or []) for f, *_ in TAILOR_FIELDS if t.get(f))


def alerts_summary(notif: dict) -> str:
    """"On · All qualifying jobs", "On · Tailored" or "Off"."""
    if not notif["enabled"]:
        return "Off"
    tailored = any(my_tailoring().values()) or notif["mode"] == "tailored"
    return "On · " + ("Tailored" if tailored else "All qualifying jobs")


def _tailoring_cached(saved: dict) -> None:
    """Show the saved tailoring at once without reading Firestore again:
    only this field changed, and it was written just now."""
    cached = st.session_state.get("_pcache")
    if isinstance(cached, dict) and cached.get("uid") == USER.uid and isinstance(cached.get("profile"), dict):
        notif = cached["profile"].get("notifications")
        cached["profile"]["notifications"] = {**(notif if isinstance(notif, dict) else {}), "tailoring": saved}
        cached["at"] = time.time()
        st.session_state.pop("_pcache_next", None)
    else:
        personal_changed()


def _tailor_save() -> None:
    picks = {f: list(st.session_state.get(_tl_key(f)) or []) for f, *_ in TAILOR_FIELDS}
    try:
        saved = USER.set_email_tailoring(picks["locations"], picks["job_families"])
    except user_store.UserStoreError as e:
        toast(str(e), "error")
        personal_changed()
        return
    except ValueError:
        toast("Those choices couldn't be saved.", "error")
        return
    _tailoring_cached(saved)
    toast("Your email alerts are tailored to your preferences." if any(saved.values())
          else "Saved — you'll get all qualifying jobs from your monitored companies.", "success")


def _tailor_clear() -> None:
    for f, *_ in TAILOR_FIELDS:
        st.session_state[_tl_key(f)] = []
    _tailor_save()


def email_tailoring_card() -> None:
    """The optional email tailoring, with its saved state. Nothing chosen =
    every qualifying job from the person's monitored companies."""
    if USER is None or ME is None or _VIEW is None:
        return
    saved = my_tailoring()
    notif = user_store.notification_settings_from(ME["profile"])
    for f, *_ in TAILOR_FIELDS:
        st.session_state.setdefault(_tl_key(f), list(saved[f]))
    tailored = any(saved.values())
    n_follow = len(ME["watchlist"])
    scope = ("every tracked company" if _VIEW["watch_all"]
             else f"the {_plural(n_follow, 'company', 'companies')} you follow" if n_follow
             else "the companies you follow (none yet)")
    with st.container(key="set_tailor"):
        st.html(f'<div class="set-head"><span class="ic">{_ms("tune")}</span><div>'
                '<div class="eyebrow">Email alerts <span class="tl-opt">Optional</span></div>'
                '<h2 class="section-title">Tailor my email alerts</h2>'
                '<p class="section-sub">Choose what you want to receive. Leave everything unselected to receive all '
                'qualifying jobs from your monitored companies.</p></div></div>')
        if tailored:
            st.html(f'<div class="tl-status on" role="status">{_ms("mark_email_read", "s20")}<div>'
                    '<b>Your email alerts are tailored to your preferences.</b>'
                    f'<span>{escape(tailoring_summary(saved))} · from {escape(scope)}</span></div></div>')
        else:
            st.html(f'<div class="tl-status" role="status">{_ms("all_inbox", "s20")}<div>'
                    '<b>All qualifying jobs from your monitored companies.</b>'
                    f'<span>No tailoring — every fresher and entry-level job from {escape(scope)}.</span></div></div>')
        for f, label, icon, hint in TAILOR_FIELDS:
            options = list(job_filters.LOCATIONS if f == "locations" else job_filters.JOB_FAMILIES)
            st.html(f'<div class="tl-q">{_ms(icon, "s16")}<b>{escape(label)}</b><span>{escape(hint)}</span></div>')
            with st.container(key=f"tl_pick_{f}"):
                st.pills(label, options, selection_mode="multi", key=_tl_key(f), label_visibility="collapsed")
        current = {f: list(st.session_state.get(_tl_key(f)) or []) for f, *_ in TAILOR_FIELDS}
        dirty = any(sorted(current[f]) != sorted(saved[f]) for f, *_ in TAILOR_FIELDS)
        with st.container(key="tl_actions", horizontal=True):
            st.button("Save email preferences", key="btn_tl_save", type="primary", icon=":material/check:",
                      on_click=_tailor_save, disabled=not dirty)
            if tailored or any(current.values()):
                st.button("Clear — email me everything", key="btn_tl_clear", icon=":material/restart_alt:",
                          on_click=_tailor_clear)
            if dirty:
                st.html('<span class="tl-dirty">Unsaved changes</span>')
        notes = ["Only changes which jobs are <b>emailed</b> to you. The filters on Home and Jobs are for browsing — "
                 "they aren’t saved and never change your emails."]
        if not notif["enabled"]:
            notes.append("Alert emails are off right now — turn them on in <b>My alerts</b>.")
        if IS_ADMIN:
            notes.append("This is your personal alert email; the tracker’s shared alert email "
                         "(Email &amp; Notifications) is unchanged.")
        st.html(f'<p class="note tl-note">{_ms("info", "s16")}<span>{" ".join(notes)}</span></p>')


def _send_test_click() -> None:
    """Send test email (a callback: one script run per click, not two).
    Owner only, and only ever to the configured recipient — never to an
    address typed in here, so the system mailbox can't be used to send mail
    to anyone else. At most one test email per cooldown for the whole app,
    so a double click (or a second tab) never sends two. It only sends one
    email: no scan, nothing else is triggered."""
    stamp = _ist(datetime.now(timezone.utc))
    if not owner_only("send test emails"):
        return
    recipient, _ = alert_recipient(settings)
    if not recipient:
        toast("No alert recipient is set — see Recipient above", "error")
        return
    cooldown = _guards()["test_email"]
    if not cooldown.try_start():
        toast(f"A test email was just sent — wait {int(cooldown.remaining()) + 1} s before sending another", "error")
        return
    try:
        # re-checks the owner sign-in itself and sends only to the configured recipient
        send_test_email(st.session_state, settings)
    except OwnerRequired:
        cooldown.cancel()
        owner_only("send test emails")
    except Exception as ex:
        cooldown.cancel()                   # nothing was sent: a retry may go at once
        log.warning("test mail failed: %s", type(ex).__name__)
        st.session_state.test_status = (False, stamp)
        toast("Email isn't configured for this app (Gmail address / app password missing)." if "GMAIL_" in str(ex)
              else "Couldn't send the test email. Check the Gmail app password and try again.", "error")
    else:
        st.session_state.test_status = (True, stamp)
        toast(f"Test email sent — check {recipient}", "success")


def page_email():
    notified = [j for j in all_records if j.get("notified")]
    # when the email actually went out (older records only know when the job was found)
    sent_times = [(_parse_iso(j.get("notified_at") or ""), found[id(j)]) for j in notified]
    last_mail, last_is_sent = max(((a or b, bool(a)) for a, b in sent_times if a or b), default=(None, False))
    pending = [j for j in all_records if alert_pending(j)]
    # the full address is personal: only the owner sees it
    shown = recipient if is_owner() else mask_email(recipient)
    page_header("Email & Notifications", (
        f'<span class="pill {"on" if recipient else "off"}"><i></i>{"Alerts on" if recipient else "Alerts off"}</span>'
        + (f'<span>Sending to {escape(shown)}</span>' if recipient else '<span>Add a recipient to turn alerts on</span>')))

    with st.container(key="mail_status"):
        if not recipient:
            headline, sub = "Alerts are off", "Set the alert recipient (see below) and new fresher jobs will be emailed after each check."
        elif pending:
            headline, sub = "Alerts are on", f"{_plural(len(pending), 'job')} waiting — sent after the next check."
        else:
            headline, sub = "Alerts are on", "Nothing waiting to send. New fresher jobs are emailed after each check."
        last_txt = (f'<span class="num">{"Sent" if last_is_sent else "Covered a job found"} {_ist_stamp(last_mail)}</span> <span class="muted">({_ago(last_mail, NOW)})</span>'
                    if last_mail else "No alert emails recorded yet")
        st.html(f"""<div class="mail-status"><div class="big {'on' if recipient else 'off'}">{_ms("notifications_active" if recipient else "notifications_off", "s28")}</div>
          <div style="min-width:0;"><h2>{headline}</h2><p class="section-sub">{sub}</p></div></div>
          <dl class="mail-facts">
            <div><dt>Last alert</dt><dd>{last_txt}</dd></div>
            <div><dt>Jobs emailed</dt><dd class="num">{len(notified)}</dd></div>
            <div><dt>Waiting to send</dt><dd class="num">{f"{len(pending)} — sent after the next check" if pending else "0"}</dd></div>
          </dl>""")

    # The recipient is read-only here: the repository is public, so the
    # address lives in the ALERT_RECIPIENT secret rather than in a file the
    # app could write. (Per-user alert addresses arrive with real sign-in.)
    with st.container(key="email_form"):
        st.html(f'<div><h2 class="section-title">{_ms("alternate_email")}Recipient</h2><p class="section-sub">Where new-job alerts are delivered.</p></div>'
                f'<div class="detail-chips"><span class="chip num">{_ms("mail", "s16")}<span>{escape(shown) or "No recipient set"}</span></span></div>')
        how = ("update the <span class=\"num\">ALERT_RECIPIENT</span> secret in the GitHub repository "
               "(Settings → Secrets → Actions) and in this app's Streamlit secrets")
        if not is_owner():
            # where the address is configured is admin business
            note = (f'{_ms("lock", "s16")} Managed by an admin. Only admins see the full address.' if recipient
                    else f'{_ms("info", "s16")} Alerts are off until an admin sets a recipient.')
        elif recipient_source == "secret":
            note = (f'{_ms("lock", "s16")} Kept in a private secret, so it never appears in the public repository. '
                    f"To change it, {how}.")
        else:
            note = f'{_ms("info", "s16")} No recipient is set. To turn alerts on, {how}.'
        st.html(f'<p class="note">{note}</p>')

    with st.container(key="test_panel"):
        ts = st.session_state.test_status
        status = ("Not sent this session" if ts is None else
                  f"Sent at {ts[1]}" if ts[0] else f"Failed at {ts[1]}")
        t1, t2 = st.columns([4, 1], vertical_alignment="center")
        with t1:
            st.html(f'<div><h2 class="section-title">{_ms("send")}Test delivery</h2><p class="section-sub">Send a sample alert to the recipient above to confirm emails arrive. '
                    f'<span class="num" style="color:var({"--green" if ts and ts[0] else "--red" if ts else "--muted"});">{status}</span></p></div>')
        with t2:
            st.button("Send test email", key="btn_test", icon=":material/send:", use_container_width=True,
                      on_click=_send_test_click)

    with st.container(key="set_behavior"):
        steps = [("travel_explore", "A check runs", f"Scheduled {interval_label(SCAN_HOURS).lower()} (GitHub often starts runs later). Every tracked portal is scanned."),
                 ("fact_check", "Only qualifying jobs are kept", "Fresher and entry-level jobs in India whose own posting was read — never a guess from a title."),
                 ("outgoing_mail", "New jobs go out in one email", "Each job is emailed at most once. Dismissed jobs are never emailed, and stay in the history so they're never sent again."),
                 ("replay", "Failures retry safely", "If sending fails, the jobs stay unsent and the next check retries. A job is recorded before its email goes out, so a failed save can't cause a duplicate.")]
        st.html(f'<div><h2 class="section-title">{_ms("route")}How alerts work</h2></div><ol class="timeline">'
                + "".join(f'<li class="step"><span class="node">{_ms(i)}</span><div><b>{t}</b><p>{d}</p></div></li>' for i, t, d in steps)
                + '</ol><p class="note" style="font-size:13px;">Mail is sent through the Gmail account configured in the repository\'s GitHub Actions secrets — credentials are never shown here.</p>')


# ── Settings → Account (when sign-in is on) ─────────────────────────────────
def _save_display_name() -> None:
    """Save the name typed into the Account page. USER is bound to the
    Firebase UID the server resolved at sign-in — the form carries no UID,
    email or address — and only the chosen name changes."""
    try:
        name = USER.set_display_name(st.session_state.get("display_name_input"))
    except user_store.InvalidInput as e:
        st.session_state._name_msg = ("err", str(e))
        return
    except user_store.UserStoreError:
        personal_changed()
        st.session_state._name_msg = ("err", "Couldn't save your name right now — try again in a moment.")
        return
    # one write, and only this field changed: no read-back of the whole profile
    _personal_patched(profile={"preferred_name": name})
    st.session_state.display_name_input = name               # show it as saved (trimmed)
    st.session_state._name_msg = ("ok", f"Saved — you'll appear as {name}.")
    toast("Display name saved", "success")


def display_name_editor() -> None:
    """Change the name shown in the header, the account menu and here."""
    current = CHOSEN_NAME or (ACCOUNT.get("name") or "").strip()
    st.session_state.setdefault("display_name_input", current)
    with st.form("display_name_form", clear_on_submit=False, border=False):
        st.text_input("Display name", key="display_name_input", max_chars=user_store.DISPLAY_NAME_MAX,
                      placeholder="Your name", autocomplete="name",
                      help="Shown in the app only. Your email and sign-in stay the same.")
        st.form_submit_button("Save name", key="btn_save_display_name", icon=":material/check:", type="primary",
                              on_click=_save_display_name)
    msg = st.session_state.pop("_name_msg", None)
    if msg:
        kind, text = msg
        st.html(f'<div class="name-msg {kind}" role="{"status" if kind == "ok" else "alert"}">'
                f'{_ms("check_circle" if kind == "ok" else "error", "s20")}<div>{escape(text)}</div></div>')


def _save_avatar(choice: str) -> None:
    """Save the avatar picked on the Account page — to the signed-in
    person's own profile (USER is bound to the server-side Firebase UID);
    nothing else changes."""
    try:
        saved = USER.set_avatar(choice)
    except user_store.InvalidInput as e:
        st.session_state._avatar_msg = ("err", str(e))
        return
    except user_store.UserStoreError:
        personal_changed()
        st.session_state._avatar_msg = ("err", "Couldn't save your avatar right now — try again in a moment.")
        return
    # one write, and only this field changed: no read-back of the whole profile
    _personal_patched(profile={"avatar": saved})
    shown = brand_logos.BRAND_NAMES.get(saved, "your initial")
    st.session_state._avatar_msg = ("ok", f"Avatar saved — you'll appear with {shown}.")
    toast("Profile avatar saved", "success")


def avatar_picker() -> None:
    """Pick a profile avatar, so several accounts (even with the same name)
    are told apart at a glance. Saved at once; the chosen tile is ringed."""
    st.html('<div class="av-pick-head"><b>Profile avatar</b><span>Tell your accounts apart at a glance — shown in the '
            'header, the account menu and here. Your sign-in and data stay the same.</span></div>')
    choices = ("",) + user_store.AVATAR_CHOICES
    rules = []
    for key in choices:
        sel = f".st-key-av_pick_{key or 'initials'} button"
        if key:
            rules.append(f'{sel} {{ --av-img: url("{brand_logos.brand_uri(key)}"); --av-tile: {brand_logos.brand_tile(key)}; }}')
        else:
            rules.append(f'{sel} {{ --av-tile: linear-gradient(135deg, var(--accent), var(--accent-2)); }}'
                         f'{sel}::before {{ content: "{_me_initial()}"; }}')
    on = f".st-key-av_pick_{CHOSEN_AVATAR or 'initials'} button"
    rules.append(f"{on} {{ border-color: var(--accent) !important; background: var(--accent-soft) !important;"
                 " box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 22%, transparent), var(--hi) !important; }"
                 f"{on} p {{ color: var(--accent-text) !important; font-weight: 700 !important; }}"
                 f'{on}::after {{ content: "✓"; position: absolute; top: 6px; right: 6px; width: 18px; height: 18px;'
                 " border-radius: 50%; display: grid; place-items: center; font-size: 11px; font-weight: 800; color: #fff;"
                 " background: var(--accent); box-shadow: 0 2px 6px -2px var(--accent); }")
    st.html(f"<style>{''.join(rules)}</style>")
    with st.container(key="av_picker"):
        for key in choices:
            st.button(brand_logos.BRAND_NAMES.get(key, "Initials"), key=f"av_pick_{key or 'initials'}",
                      on_click=_save_avatar, args=(key,))
    msg = st.session_state.pop("_avatar_msg", None)
    if msg:
        kind, text = msg
        st.html(f'<div class="name-msg {kind}" role="{"status" if kind == "ok" else "alert"}">'
                f'{_ms("check_circle" if kind == "ok" else "error", "s20")}<div>{escape(text)}</div></div>')


def account_card() -> None:
    admin = is_owner()
    method = {"google.com": "Google", "password": "email &amp; password",
              "owner": "the owner password"}.get(ACCOUNT.get("provider"), "Firebase")
    with st.container(key="set_account"):
        st.html(f'<div class="set-head">{_me_avatar("acct-av acct-av-lg")}<div style="min-width:0;">'
                '<div class="eyebrow">Account</div>'
                f'<h2 class="section-title">{escape(account_label(SHOWN_ACCOUNT))}</h2>'
                f'<p class="section-sub">Signed in with {method}'
                + (f' · {escape(ACCOUNT["email"])}' if ACCOUNT.get("email") else "") + '</p></div></div>')
        # Firebase IDs and the reasons a link failed are for admins; a member
        # sees only whether their account is linked
        link = st.session_state.get("_fb_link") or {}
        if ACCOUNT.get("provider") == "owner":
            fb = "— (owner password; sign in with Google to link your Firebase account)"
        elif ACCOUNT.get("uid"):
            fb = "Linked"                      # the UID itself is never shown, not even to admins
        elif link.get("state") == "failed" and admin:
            fb = (f'Not linked — Firebase refused the Google sign-in <span class="num">({escape(link.get("code") or "")})</span>'
                  + (f'<span class="kv-note">{escape(_GOOGLE_ERROR_HINTS[link["code"]])}</span>'
                     if link.get("code") in _GOOGLE_ERROR_HINTS else ""))
        elif link.get("state") in ("failed", "unavailable"):
            fb = "Not linked"
        else:
            fb = "Linked at your next Google sign-in"
        role = ("Admin — can manage companies, jobs, checks and email" if admin
                else "Member — your own job search, companies and alerts" if USER is not None
                else "Member — read-only")
        rows = [("Role", role), ("Email verified", "Yes" if ACCOUNT.get("email_verified") else "No")]
        if admin:                        # sign-in internals are the admin's business
            rows.append(("Firebase account", fb))
        if USER is not None:
            rows.append(("Personal data", "Saved to your own account"))
        elif USER_PROBLEM:
            rows.append(("Personal data", escape(USER_PROBLEM) + (
                f'<span class="kv-note">{escape(USER_PROBLEM_DETAIL)}</span>' if admin and USER_PROBLEM_DETAIL else "")))
        elif admin and ACCOUNT.get("provider") in ("google.com", "password"):
            # only admins: members simply have the app as it was
            rows.append(("Personal data", "Not switched on — no Firebase service account is configured for this app"))
        st.html('<dl class="kv">' + "".join(f"<dt>{a}</dt><dd>{b}</dd>" for a, b in rows) + "</dl>")
        if USER is not None:
            display_name_editor()
            avatar_picker()
        # Until JT_ADMIN_EMAILS exists the legacy owner password still unlocks
        # changes — tucked away here, never a second login screen.
        if owner_password_enabled():
            with st.expander("Admin access (until JT_ADMIN_EMAILS is set)", icon=":material/admin_panel_settings:",
                             expanded=False):
                if is_owner():
                    st.html('<p class="note">Admin access is unlocked for this session.</p>')
                    if st.button("Lock admin access", key="btn_sign_out", icon=":material/lock:"):
                        sign_out()
                        st.rerun()
                else:
                    with st.form("owner_signin", clear_on_submit=True, border=False):
                        st.text_input("Owner password", type="password", key="owner_pw", autocomplete="current-password")
                        st.form_submit_button("Unlock", key="btn_sign_in", icon=":material/lock_open:", type="primary",
                                              on_click=_owner_submit)
        # (signing out is in the account menu, top right — one place for it)


def owner_card() -> None:
    """Legacy owner access — used only while Firebase sign-in isn't configured."""
    with st.container(key="set_owner"):
        owner = is_owner()
        if owner:
            left = max(0, int(st.session_state.get(OWNER_SESSION_KEY, 0) - time.time()))
            sub = (f"Signed in as the owner in this browser session · ends in {left // 3600} h {left % 3600 // 60:02d} min. "
                   "Changes (dismiss, companies, Run check, test email) are enabled.")
        elif owner_configured():
            sub = "Anyone with the link can browse. Sign in with the owner password to make changes."
        else:
            sub = ("Changes are turned off: no owner password is set. Add a JT_OWNER_PASSWORD secret "
                   "(12+ characters) to this app's Streamlit secrets to enable them.")
        st.html(f'<div class="set-head"><span class="ic">{_ms("admin_panel_settings" if owner else "lock")}</span><div>'
                '<div class="eyebrow">Security</div><h2 class="section-title">Owner access</h2>'
                f'<p class="section-sub">{escape(sub)}</p></div></div>')
        if owner:
            o1, _o2 = st.columns([1, 3])
            with o1:
                if st.button("Sign out", key="btn_sign_out", icon=":material/logout:", use_container_width=True):
                    sign_out()
                    toast("Signed out", "success")
                    st.rerun()
        elif owner_configured():
            # the submit callback wipes the password from session state right away
            with st.form("owner_signin", clear_on_submit=True, border=False):
                st.text_input("Owner password", type="password", key="owner_pw", autocomplete="current-password")
                st.form_submit_button("Sign in", key="btn_sign_in", icon=":material/login:", type="primary",
                                      on_click=_owner_submit)


def _ask_clear_all() -> None:
    if owner_only("dismiss jobs"):
        st.session_state.confirm_clear = True


def _clear_all(keys: set[str]) -> None:
    """Dismiss every job that was active when this was confirmed."""
    st.session_state.confirm_clear = False
    if not owner_only("dismiss jobs"):
        return
    if USER is not None:
        try:
            USER.dismiss(keys)
        except user_store.UserStoreError as e:
            personal_changed()
            toast(str(e), "error")
            return
        _personal_patched(dismissed_add=keys)
    # the confirmed jobs only: a job a scan published since the page was drawn
    # was never shown to the admin, so it stays active (and gets its email)
    if USER is not None:            # their own copy already changed: the shared commit needn't hold the click
        if _save_change_soon("data/seen_jobs.json", dismiss_jobs(keys), [], "chore: dismiss all alerts"):
            toast("All alerts cleared", "success")
        return
    _, saved, err = _save_change("data/seen_jobs.json", dismiss_jobs(keys), [], "chore: dismiss all alerts")
    toast("All alerts cleared" if saved else f"Cleared here, but not saved permanently. {err}",
          "success" if saved else "error")


def _dismiss_all_mine(keys: set[str]) -> None:
    """Member Home's Dismiss all: the jobs listed when this was confirmed go
    to the viewer's OWN dismissed list (one batched write). The shared
    listings (data/seen_jobs.json) and every other person's dismissals are
    never touched, so other members keep seeing these jobs."""
    st.session_state.mh_clear_confirm = False
    if USER is None:
        toast(USER_PROBLEM or "Sign in to dismiss jobs.", "error")
        return
    if not keys:
        return
    try:
        USER.dismiss(keys)
    except user_store.UserStoreError as e:
        personal_changed()
        toast(str(e), "error")
        return
    _personal_patched(dismissed_add=keys)
    toast(f"Dismissed {_plural(len(keys), 'job')} for you. Restore any of them from Jobs → Dismissed.", "success")


def _toggle_theme() -> None:
    st.session_state.dark_mode = not st.session_state.dark_mode


_STATUS_WORDS = {"healthy": "healthy", "delayed": "delayed", "failing": "failing", "pending": "pending",
                 "checking": "checking now"}


SCAN_CHOICES = scan_schedule.ALLOWED_INTERVALS


def _save_scan_interval() -> None:
    """Save the global scan interval (admins only). Checked here on the server
    — the role, then the value — and again in _save_change; the control is
    simply never drawn for anyone else. Only settings.json changes: no scan
    starts, and the scanner's state (last scans, jobs) is untouched."""
    if not owner_only("change the scan interval"):
        return
    try:
        hours = scan_schedule.validate_interval(st.session_state.get("scan_interval_pick"))
    except scan_schedule.InvalidInterval as e:
        toast(str(e), "error")
        return
    _, saved, err = _save_change("data/settings.json", lambda latest: scan_schedule.with_interval(latest, hours),
                                 {}, f"chore: set the scan interval to {hours} h [skip ci]")
    if saved:
        toast(f"Scan interval saved — {interval_label(hours).lower()}. It applies from the next hourly check; "
              "no scan was started.", "success")
    else:
        toast(f"Not saved permanently. {err}", "error")


def scan_schedule_card() -> None:
    """Settings → Global job scan interval (admins). The shared scanner's
    schedule: what the hourly plan tick in check_jobs.yml reads."""
    st.session_state.setdefault("scan_interval_pick", SCAN_HOURS)
    with st.container(key="set_engine"):
        by_source: dict[str, list[str]] = {}
        for c in companies:
            by_source.setdefault(_source_label(c), []).append((c.get("name") or "").strip() or "Unnamed")
        st.html(f'<div class="set-head"><span class="ic">{_ms("manage_search")}</span><div><div class="eyebrow">Scanning</div>'
                '<h2 class="section-title">Global job scan interval</h2><p class="section-sub">How often the one shared '
                'scanner checks every portal, for everybody. Members can’t change it; their alert emails go out at '
                f'most every {scan_schedule.MEMBER_ALERT_HOURS} hours whatever this is.</p></div></div>'
                '<dl class="kv">'
                f'<dt>Current interval</dt><dd><b>{escape(interval_label(SCAN_HOURS))}</b></dd>'
                f'<dt>Next scheduled scan</dt><dd><span class="num">{escape(_ist_stamp(NEXT_SCAN))}</span>'
                f'<span class="kv-note">Asia/Kolkata time.{" A scan is overdue: the scheduler retries it within half an hour." if SCAN_DUE else ""} '
                'GitHub may start a scheduled scan later than this.</span></dd>'
                '<dt>Roles kept</dt><dd>Fresher and entry-level roles located in India</dd>'
                '<dt>Detail pages</dt><dd>Every candidate job’s own posting is read before it can be classified and '
                'emailed; if it can’t be read, it is retried on the next scan</dd></dl>')
        with st.container(key="scan_pick_row"):
            st.pills("Global job scan interval", list(SCAN_CHOICES), key="scan_interval_pick",
                     format_func=interval_label, selection_mode="single", label_visibility="collapsed")
            st.button("Save interval", key="btn_save_interval", icon=":material/schedule:", type="primary",
                      on_click=_save_scan_interval,
                      disabled=st.session_state.get("scan_interval_pick") in (None, SCAN_HOURS))
        with st.expander("Technical details", icon=":material/code:"):
            tech = [("Scrapers", "; ".join(f"{escape(src)} for {escape(', '.join(sorted(names)))}"
                                           for src, names in sorted(by_source.items())) or "No portals yet"),
                    ("Scheduler", 'scan_scheduler.yml, a self-dispatching GitHub Actions chain, asks check_jobs.yml to '
                                  'plan as each window opens; its ticks (cron <span class="num">'
                                  f'{scan_schedule.PLAN_CRON}</span>, UTC) are the safety net. scan_schedule.py starts a '
                                  'scan only at the interval boundaries, from '
                                  f'<span class="num">{scan_schedule.SETTING}</span> in settings.json'),
                    ("Personal alerts", f'after every scan and every {scan_schedule.MEMBER_ALERT_HOURS} h (cron '
                                        f'<span class="num">{scan_schedule.ALERTS_CRON}</span>); each person at most '
                                        f'once per {scan_schedule.MEMBER_ALERT_HOURS} h')]
            st.html('<dl class="kv tech">' + "".join(f"<dt>{a}</dt><dd>{b}</dd>" for a, b in tech) + "</dl>")


def tracker_status_card() -> None:
    """Settings → Tracker status (admins): the portals' health and the scan
    times — what the sidebar's status card used to show, kept in one place.
    The same numbers as Home's chips and metrics (nothing is fetched)."""
    breakdown = " · ".join(f"{n_by_status[k]} {_STATUS_WORDS[k]}" for k in _STATUS_WORDS if n_by_status[k]) or "No portals yet"
    tiles = [
        ("Portal health", f'<span class="pill {overall[0]}"><i></i>{escape(overall[1])}</span>',
         f"{_plural(len(companies), 'company', 'companies')} checked by the scanner"),
        ("Companies monitored", f'<span class="v num">{len(companies)}</span>', escape(breakdown)),
        ("Last scan", f'<span class="v num">{escape(_ago(last_scan, NOW))}</span>',
         escape(_ist_stamp(last_scan)) if last_scan else "No scan has finished yet"),
        ("Next scheduled", f'<span class="v num">{escape(_ist(NEXT_SCAN))}</span>',
         f"{escape(interval_label(SCAN_HOURS))} · {escape(NEXT_SCAN_NOTE)}"),
    ]
    with st.container(key="set_status"):
        st.html(f'<div class="set-head"><span class="ic">{_ms("monitor_heart")}</span><div><div class="eyebrow">Monitoring</div>'
                '<h2 class="section-title">Tracker status</h2><p class="section-sub">How the portals are doing and when they '
                'were last scanned. Home shows the same at a glance.</p></div></div>'
                '<dl class="status-grid" role="status">'
                + "".join(f'<div class="st-tile"><dt>{k}</dt><dd>{v}<span class="n">{n}</span></dd></div>' for k, v, n in tiles)
                + "</dl>")
        st.button("Open monitoring", key="btn_set_monitoring", icon=":material/monitor_heart:", on_click=go,
                  args=("monitoring",))


def scan_details_rows(now: datetime) -> list[tuple[str, str, str]]:
    """(label, value, note) for Settings → Scan details, from the scanner's
    own record of its latest completed scan in companies.json (the shared
    copy every page already uses — no extra GitHub call) and the real
    scheduler's rules (scan_schedule). Never a guessed time or count:
    whatever isn't known says so."""
    run = scan_schedule.latest_run(companies)
    if run is not None:
        last = _ist_full(run["finished_at"])
        failed, total = run["portals_failed"], run["portals"]
        last_note = ("Completed, but no portal could be read" if total and failed >= total
                     else f"Completed · {failed} of {_plural(total, 'portal')} couldn't be read" if failed
                     else f"Completed · {_plural(total, 'portal')} read" if total else "Completed")
        if total and failed >= total:
            jobs, jobs_note = "Unavailable", "No portal could be read in that scan."
        else:
            jobs = str(run["jobs_found"])
            jobs_note = ("Eligible fresher roles in India that scan found across all portals, including ones "
                         "already listed" + (" (the portals that failed aren't counted)" if failed else "") + ".")
    else:
        stamps = [d for d in (_parse_iso(c.get("last_checked", "")) for c in companies) if d]
        if stamps:      # a scan by the scanner before it kept this record
            last, last_note = _ist_full(max(stamps)), "Completed"
            jobs, jobs_note = "Unavailable", "Recorded from the next completed scan on."
        else:
            last, last_note = "Unavailable", "No completed scan is recorded yet."
            jobs, jobs_note = "Unavailable", "No completed scan is recorded yet."
    state, when = scan_schedule.next_scan_state(now, SCAN_HOURS, scan_schedule.last_scan(companies))
    retry = int(scan_schedule.CHAIN_RETRY.total_seconds() // 60)
    offset = int(scan_schedule.CHAIN_OFFSET.total_seconds() // 60)
    if state == "scheduled":
        nxt, nxt_note = _ist_full(when), (f"{interval_label(SCAN_HOURS)}, {_plural(offset, 'minute')} after each window "
                                          "opens. GitHub may start it a little later.")
    elif state == "due":
        nxt, nxt_note = "Running or starting", (f"It was due at {_ist_full(when)}; it shows here once it has finished. "
                                                f"The scheduler retries every {retry} min until it does.")
    elif when is not None:
        nxt, nxt_note = "Overdue", (f"Expected at {_ist_full(when)} and no scan has finished since. The scheduler "
                                    f"retries every {retry} min; GitHub may start it later.")
    else:
        nxt, nxt_note = "Unavailable", f"No scan has been recorded yet; the scheduler retries every {retry} min."
    return [("Last scan", last, last_note), ("Next scheduled scan", nxt, nxt_note), ("Jobs found", jobs, jobs_note)]


def scan_details_card() -> None:
    """Settings → Scan details: the administrator only. Asked again here on
    the server (not only by the page's member check), and nothing is read
    or computed for anyone else."""
    if not is_owner():
        return
    rows = scan_details_rows(NOW)
    with st.container(key="set_scan_details"):
        st.html(f'<div class="set-head"><span class="ic">{_ms("history")}</span><div><div class="eyebrow">Scanning</div>'
                '<h2 class="section-title">Scan details</h2><p class="section-sub">The latest completed scan and the next '
                'one, from the scanner’s own records. Times are IST (Asia/Kolkata).'
                + (" GitHub couldn’t be read just now, so these may be out of date." if DATA_STALE else "")
                + '</p></div></div><dl class="kv scan-details">'
                + "".join(f'<dt>{escape(k)}</dt><dd><span class="num">{escape(v)}</span>'
                          f'<span class="kv-note">{escape(n)}</span></dd>' for k, v, n in rows)
                + "</dl>")


def page_settings():
    page_header("Settings", "Your account, your job search and how the app looks"
                + ("" if MEMBER_VIEW else " — plus how the tracker is configured"))
    if AUTH_GATE:
        if st.session_state.get("_delete_open"):     # an open confirmation is drawn where the eye lands
            account_management_card()
            account_card()
        else:
            account_card()
            account_management_card()
    else:
        owner_card()
    personal_summary_card()
    with st.container(key="set_appearance"):
        a1, a2 = st.columns([4, 1], vertical_alignment="center")
        with a1:
            st.html(f'<div class="set-head"><span class="ic">{_ms("dark_mode" if st.session_state.dark_mode else "light_mode")}</span><div>'
                    '<div class="eyebrow">General</div><h2 class="section-title">Appearance</h2><p class="section-sub">'
                    f'{"Dark" if st.session_state.dark_mode else "Light"} theme · kept in this page’s address, so it survives reloads and bookmarks</p></div></div>')
        with a2:
            st.button("Use light theme" if st.session_state.dark_mode else "Use dark theme", key="btn_theme",
                      icon=":material/light_mode:" if st.session_state.dark_mode else ":material/dark_mode:",
                      use_container_width=True, on_click=_toggle_theme)
    if MEMBER_VIEW:                      # the tracker's own settings are the admin's
        st.html('<div class="app-foot"><span>Fresher Job Tracker</span><span>Fresher and entry-level roles in India</span></div>')
        return
    st.html('<div class="admin-head"><span class="ic">' + _ms("admin_panel_settings") + '</span><div>'
            '<div class="eyebrow">Admins only</div><h2 class="section-title">Administration</h2>'
            '<p class="section-sub">The shared tracker: its schedule, health and data. Members never see this section.</p>'
            '</div></div>')
    scan_details_card()
    tracker_status_card()
    scan_schedule_card()
    with st.container(key="set_data"):
        synced = bool(os.environ.get("GITHUB_TOKEN"))
        d1, d2 = st.columns([4, 1], vertical_alignment="center")
        with d1:
            st.html(f'<div class="set-head"><span class="ic">{_ms("cloud_sync" if synced else "cloud_off")}</span><div>'
                    '<div class="eyebrow">Data</div><h2 class="section-title">Data &amp; sync</h2><p class="section-sub">'
                    + (f"Live data from github.com/{GITHUB_REPO}, cached for up to 5 minutes."
                       if synced else "Reading the files bundled with this deployment — no GitHub token is configured.")
                    + "</p></div></div>")
        with d2:
            st.button("Reload data", key="btn_reload", icon=":material/refresh:", use_container_width=True,
                      on_click=_refresh_click)
    with st.container(key="set_maint"):
        st.html(f'<div class="set-head"><span class="ic red">{_ms("warning")}</span><div><div class="eyebrow">Danger zone</div>'
                '<h2 class="section-title">Dismiss all jobs</h2><p class="section-sub">Dismiss every active job at once. '
                'They stay in the history (Jobs → Dismissed) and are never emailed again.</p></div></div>')
        if not st.session_state.confirm_clear:
            m1, _m2 = st.columns([1, 3])
            with m1:
                with st.container(key="danger"):
                    st.button(f"Dismiss all {len(_all_active_jobs)} jobs", key="btn_clear_all", disabled=not _all_active_jobs,
                              use_container_width=True, on_click=_ask_clear_all)
        else:
            unsent = sum(1 for j in _all_active_jobs if alert_pending(j))
            with st.container(key="danger_confirm"):
                st.html(f'<p class="note" style="color:var(--text);">Dismiss all <b>{len(_all_active_jobs)}</b> active jobs?'
                        + (f" This also cancels <b>{unsent}</b> alert{'s' if unsent != 1 else ''} that haven't been emailed yet "
                           "— they won't be emailed." if unsent else " They stay in the history and are never emailed again.")
                        + "</p>")
                y, n_ = st.columns([1, 3])
                with y:
                    st.button("Yes, dismiss all", key="btn_clear_all_confirm", type="primary", use_container_width=True,
                              on_click=_clear_all, args=({job_key(j) for j in _all_active_jobs},))
                with n_:
                    st.button("Cancel", key="btn_clear_all_cancel", on_click=st.session_state.__setitem__,
                              args=("confirm_clear", False))
    st.html('<div class="app-foot"><span>Fresher Job Tracker</span><span>Fresher and entry-level roles in India</span></div>')


# ═══════════════════════════════════════════════════════════════════════════════
# PERSONAL PAGES — the same for admins and members
# ═══════════════════════════════════════════════════════════════════════════════
PREF_STEPS = ["locations", "job_types", "job_families", "experience", "work_modes"]
_PREF_INFO = {   # question · short label · {value: label} · hint
    "locations": ("Where do you want to work?", "Locations", {v: v for v in job_filters.LOCATIONS},
                  "Pick any — leave it empty for anywhere in India."),
    "job_types": ("What type of roles are you looking for?", "Job types", job_filters.JOB_TYPES,
                  "Saved for later: postings don’t state the job type yet, so jobs aren’t filtered by it."),
    "job_families": ("What kind of work interests you?", "Job families", {v: v for v in job_filters.JOB_FAMILIES},
                     "Matched against each job’s title."),
    "experience": ("What’s your experience level?", "Experience", job_filters.EXPERIENCE_LABELS,
                   "As the tracker read it from each posting."),
    "work_modes": ("How do you prefer to work?", "Work mode", job_filters.WORK_MODES,
                   "Saved for later: postings don’t say remote or hybrid yet, so jobs aren’t filtered by it."),
}


# What the job lists and Tailored alerts really match on, and what is only
# kept until the postings carry it (see job_filters' module note).
MATCHED_PREFS = ("locations", "job_families", "experience")
SAVED_ONLY_PREFS = ("job_types", "work_modes")
SAVED_ONLY_NOTE = "Saved for later — not used for matching yet"


def pref_lines(prefs: dict, fields=tuple(PREF_STEPS)) -> list[str]:
    """"Hyderabad · Bengaluru", "Software engineering", … — one per chosen field."""
    return [" · ".join(_PREF_INFO[f][2].get(v, v) for v in prefs.get(f) or []) for f in fields if prefs.get(f)]


def pref_list_html(prefs: dict, empty: str) -> str:
    """<li> rows for a summary: what is used to match jobs, then what is
    only saved for later (never presented as a match)."""
    used, saved = pref_lines(prefs, MATCHED_PREFS), pref_lines(prefs, SAVED_ONLY_PREFS)
    html = '<li class="pg">Used to match jobs</li>' + ("".join(f"<li>{escape(x)}</li>" for x in used)
                                                       or f'<li class="muted">{escape(empty)}</li>')
    if saved:
        html += f'<li class="pg">{SAVED_ONLY_NOTE}</li>' + "".join(f'<li class="muted">{escape(x)}</li>' for x in saved)
    return html


def save_preferences(values: dict) -> bool:
    try:
        saved = USER.set_preferences(_VIEW["mode"] if _VIEW else "general", values.get("job_families") or [],
                                     values.get("locations") or [], values.get("experience") or [],
                                     values.get("job_types") or [], values.get("work_modes") or [])
        # one write of one field: patch this session's copy (no read-back)
        _personal_patched(profile={"preferences": saved}) if isinstance(saved, dict) else personal_changed()
        return True
    except (user_store.UserStoreError, ValueError) as e:
        personal_changed()
        toast(str(e) if isinstance(e, user_store.UserStoreError) else "Those preferences couldn't be saved.", "error")
        return False


def _personal_unavailable() -> bool:
    if USER is not None and ME is not None:
        return False
    empty_state("cloud_off", "Personal settings are unavailable",
                USER_PROBLEM or "Personal data isn't switched on for this app yet.")
    return True


def page_preferences():
    page_header("Job preferences", "The starting filters of your job lists. Which jobs are emailed to you is set "
                                   "separately, in “Tailor my email alerts”.", eyebrow="Your account")
    if _personal_unavailable():
        return
    with st.container(key="set_prefs"):
        with st.form("prefs_form", border=False):
            for cls, group, sub, fields in (
                    ("", "Used to match jobs", "Your job lists start from these.",
                     MATCHED_PREFS),
                    ("later", SAVED_ONLY_NOTE, "Postings don’t state these yet, so no job is filtered by them.",
                     SAVED_ONLY_PREFS)):
                st.html(f'<div class="pref-group {cls}"><div class="eyebrow">{escape(group)}</div><p>{escape(sub)}</p></div>')
                for f in fields:
                    question, short, options, hint = _PREF_INFO[f]
                    st.html(f'<div class="pref-q"><b>{escape(short)}</b><span>{escape(hint)}</span></div>')
                    st.pills(short, list(options), selection_mode="multi",
                             default=_VIEW["prefs"].get(f) or [], key=f"pref_{f}", format_func=options.get,
                             label_visibility="collapsed")
            st.form_submit_button("Save preferences", key="btn_prefs_save", type="primary", icon=":material/check:",
                                  on_click=_save_preferences_click)


def _save_preferences_click() -> None:
    """The preferences form's Save (a callback: the page is drawn once, after it)."""
    if save_preferences({f: list(st.session_state.get(f"pref_{f}") or []) for f in PREF_STEPS}):
        toast("Preferences saved", "success")


def page_alerts():
    page_header("My alerts", "Emails about new jobs, sent to your own verified address", eyebrow="Your account")
    if _personal_unavailable():
        return
    my_alerts_card()
    email_tailoring_card()
    a, b, _c = st.columns([1, 1, 2])
    with a:
        st.button("Job preferences", key="btn_alerts_prefs", icon=":material/tune:", on_click=go, args=("preferences",),
                  use_container_width=True)
    with b:
        st.button("My companies", key="btn_alerts_cos", icon=":material/apartment:", on_click=go,
                  args=("companies",), kwargs={"view": "mine"}, use_container_width=True)


def _onboarding_next(step: int, field: str | None) -> None:
    vals = st.session_state.setdefault("ob_values", {})
    if field:
        vals[field] = list(st.session_state.get(f"ob_{field}") or [])
    st.session_state.ob_step = step


def _onboarding_finish(with_preferences: bool) -> None:
    vals = st.session_state.get("ob_values", {}) if with_preferences else {}
    if with_preferences and not save_preferences(vals):
        return
    try:
        USER.complete_onboarding()
    except user_store.UserStoreError as e:
        toast(str(e), "error")
        return
    finally:
        personal_changed()
    for key in ("ob_values", "ob_step"):
        st.session_state.pop(key, None)
    go("home")
    toast("You're all set" if with_preferences else "You can set your preferences anytime from your account menu",
          "success")


def page_onboarding():
    """First sign-in: five quick questions, then a summary. Nothing here is
    required, and alerts stay off until the person turns them on."""
    st.html('<style>[data-testid="stSidebar"], .st-key-nav_toggle { display: none !important; }</style>')
    step = int(st.session_state.get("ob_step", 0))
    vals = st.session_state.setdefault("ob_values", {})
    first = first_name(SHOWN_ACCOUNT) if ACCOUNT else ""
    with st.container(key="onboard"):
        dots = "".join(f'<i class="{"on" if i < step else "now" if i == step else ""}"></i>' for i in range(len(PREF_STEPS) + 1))
        if step < len(PREF_STEPS):
            field = PREF_STEPS[step]
            question, short, options, hint = _PREF_INFO[field]
            st.html(f'<div class="ob-head"><div class="eyebrow sparked">Welcome{", " + escape(first) if first else ""}'
                    f'</div><div class="ob-dots" aria-label="Step {step + 1} of {len(PREF_STEPS)}">{dots}</div>'
                    f'<h1 class="ob-title">{escape(question)}</h1><p class="ob-sub">{escape(hint)}</p></div>')
            st.pills(short, list(options), selection_mode="multi", default=vals.get(field) or [], key=f"ob_{field}",
                     format_func=options.get, label_visibility="collapsed")
            with st.container(key="ob_actions", horizontal=True):
                if step:
                    st.button("Back", key="btn_ob_back", icon=":material/arrow_back:", on_click=_onboarding_next,
                              args=(step - 1, field))
                st.button("Continue", key="btn_ob_next", type="primary", icon=":material/arrow_forward:",
                          icon_position="right", on_click=_onboarding_next, args=(step + 1, field))
                st.button("Skip for now", key="btn_onboarding_skip", type="tertiary", on_click=_onboarding_finish,
                          args=(False,))
        else:
            summary = pref_list_html(vals, "No preferences — you’ll see every qualifying fresher job.")
            st.html(f'<div class="ob-head"><div class="ob-dots">{dots}</div><div class="ob-done">{_ms("task_alt", "s28")}</div>'
                    '<h1 class="ob-title">You’re ready</h1><p class="ob-sub">Your job lists start from these. Change them '
                    'anytime from your account menu. Alert emails stay off until you turn them on.</p>'
                    f'<ul class="ob-summary">{summary}</ul></div>')
            with st.container(key="ob_actions", horizontal=True):
                st.button("Back", key="btn_ob_back", icon=":material/arrow_back:", on_click=_onboarding_next,
                          args=(step - 1, None))
                st.button("Start discovering jobs", key="btn_ob_finish", type="primary", icon=":material/arrow_forward:",
                          icon_position="right", on_click=_onboarding_finish, args=(True,))


def page_my_companies():
    """The companies THIS person follows — chosen from the shared catalogue,
    which itself never changes here."""
    following = sorted((c for c in companies if str(c.get("id")) in (ME["watchlist"] if ME else set())),
                       key=lambda c: (c.get("name") or "").lower())
    page_header("My companies",
                "Companies you follow narrow your job lists and your alert emails. "
                "The tracker's company list itself isn't changed.", eyebrow="Your account" if not MEMBER_VIEW else None)
    if _personal_unavailable():
        return
    if not MEMBER_VIEW:
        st.button("All tracked companies", key="btn_cos_all", icon=":material/arrow_back:", type="tertiary",
                  on_click=go, args=("companies",))
    if following:
        with st.container(key="set_following"):
            st.html(f'<h2 class="section-title">{_ms("check_circle")}Following {len(following)}</h2>')
            for c in following:
                cid = str(c.get("id"))
                with st.container(key=f"fol_{_widget_key('f', cid)}"):
                    r1, r2, r3 = st.columns([5, 1.2, 1.2], vertical_alignment="center")
                    with r1:
                        st.html(f'<div class="co-mini">{_avatar(c.get("name") or "")}<div><b>{escape(c.get("name") or "")}</b>'
                                f'<span>{_plural(company_active(c), "active job")}</span></div></div>')
                    with r2:
                        st.button("View", key=f"fv_{_widget_key('f', cid)}", on_click=go, args=("companies",),
                                  kwargs={"company": cid}, use_container_width=True)
                    with r3:
                        st.button("Unfollow", key=f"fu_{_widget_key('f', cid)}", on_click=set_following,
                                  args=(cid, False, c.get("name") or ""), use_container_width=True)
    picking = bool(following) or st.session_state.get("co_pick")
    if not following and not picking:
        empty_state("apartment", "You aren't following any companies yet.",
                    "Follow the companies you care about — your job lists and alert emails will focus on them.",
                    lambda: st.button("Choose companies", key="btn_co_pick", type="primary", icon=":material/add:",
                                      on_click=lambda: st.session_state.update(co_pick=True)))
        return
    if following:
        email_tailoring_card()
    rest = [c for c in companies if str(c.get("id")) in _catalogue_ids() and c not in following]
    if rest:
        with st.container(key="set_co_add"):
            st.html(f'<h2 class="section-title">{_ms("add_circle")}Add companies</h2>'
                    '<p class="section-sub">Companies the tracker monitors.</p>')
            for c in sorted(rest, key=lambda c: (c.get("name") or "").lower()):
                cid = str(c.get("id"))
                with st.container(key=f"add_{_widget_key('a', cid)}"):
                    r1, r2 = st.columns([5, 1.2], vertical_alignment="center")
                    with r1:
                        st.html(f'<div class="co-mini">{_avatar(c.get("name") or "")}<div><b>{escape(c.get("name") or "")}</b>'
                                f'<span>{_plural(company_active(c), "active job")}</span></div></div>')
                    with r2:
                        st.button("Follow", key=f"fa_{_widget_key('a', cid)}", type="primary", icon=":material/add:",
                                  on_click=set_following, args=(cid, True, c.get("name") or ""), use_container_width=True)


def page_member_home():
    """A member's Home: their own job search — no scanner, no admin tools."""
    first = first_name(SHOWN_ACCOUNT) if ACCOUNT else ""
    mine = for_me(active_jobs)
    # The listings are shared (the scanner finds them for everyone), but Home
    # is this person's own feed: the companies they follow — or every company
    # once they choose "all companies" — narrowed by their preferences. A new
    # account follows none, so its Home starts empty, with the way to fill it.
    empty_scope = follows_nothing()
    narrowed = narrowed_for_me()
    hi = f'Hi{", " + escape(first) if first else ""}'
    if empty_scope:
        title = f'{hi} — start your <em>job feed</em>'
        sub = ("Home shows jobs from the companies you follow, and you don’t follow any yet. Choose companies to "
               "fill it — or browse every listing the tracker has found on the Jobs page.")
    elif narrowed:
        title = f'{hi} — here are your <em>matches</em>'
        sub = f'{_plural(len(mine), "job")} match your preferences right now.'
    else:
        title = f'{hi} — discover your next <em>opportunity</em>'
        sub = (f'{_plural(len(mine), "open fresher and entry-level job")} across every company the tracker monitors. '
               'Set preferences or follow companies to narrow them to your matches.')
    with st.container(key="mh_hero"):
        # the same discovery feed as the original Home hero (_opportunity_cards)
        st.html(f'<div class="hero mh-hero"><div><div class="eyebrow sparked">Your job search</div>'
                f'<h1 class="page-title">{title}</h1><p class="hero-sub">{sub}</p></div>{_opportunity_cards()}</div>')
    if ME is not None:
        notif = user_store.notification_settings_from(ME["profile"])
        cards_row = st.container(key="mh_cards")
        cols = cards_row.columns(3)
        cards = [
            ("set_mh_prefs", "tune", "Job preferences",
             pref_list_html(_VIEW["prefs"], "None yet — every qualifying job from your companies is shown."),
             "Edit preferences", ("preferences",), {}),
            ("set_mh_companies", "apartment", "Companies",
             (f'<li>Following {len(ME["watchlist"])}</li>' if ME["watchlist"] else '<li class="muted">Not following any yet</li>'),
             "Choose companies", ("companies",), {"view": "mine"}),
            ("set_mh_alerts", "notifications", "Alert emails",
             (f'<li>{escape(alerts_summary(notif))}</li>' if notif["enabled"]
              else '<li class="muted">Off</li>'), "Manage alerts", ("alerts",), {}),
        ]
        for col, (key, icon, title, body, cta, args, kw) in zip(cols, cards):
            with col, st.container(key=key):
                st.html(f'<div class="mh-card"><div class="h">{_ms(icon, "s20")}<b>{title}</b></div><ul>{body}</ul></div>')
                st.button(cta, key=f"btn_{key}", on_click=go, args=args, kwargs=kw, use_container_width=True)
    elif USER_PROBLEM:
        st.html(f'<p class="note">{_ms("info", "s16")} {escape(USER_PROBLEM)}</p>')
    with st.container(key="mh_list_bar"):
        if empty_scope:
            st.html('<div class="sec-head"><div><div class="eyebrow sparked">Your companies</div><h2 class="section-title">Your jobs</h2>'
                    '<p class="section-sub">Fresher and entry-level roles from the companies you follow appear here</p></div></div>')
        elif narrowed:
            st.html('<div class="sec-head"><div><div class="eyebrow sparked">Matching jobs</div><h2 class="section-title">Your matches</h2>'
                    '<p class="section-sub">Newest fresher and entry-level roles that fit your preferences</p></div></div>')
        else:
            st.html('<div class="sec-head"><div><div class="eyebrow sparked">Latest opportunities</div><h2 class="section-title">Latest jobs</h2>'
                    '<p class="section-sub">Newest fresher and entry-level roles from every tracked company — the same '
                    'listings every member sees</p></div></div>')
        # this person's own Dismiss all (asks first): never the shared listings
        if mine and USER is not None and not st.session_state.get("mh_clear_confirm"):
            st.button(f"Dismiss all ({len(mine)})", key="btn_mh_clear", icon=":material/clear_all:",
                      on_click=st.session_state.__setitem__, args=("mh_clear_confirm", True))
    if mine and USER is not None and st.session_state.get("mh_clear_confirm"):
        with st.container(key="mh_clear_ask"):
            st.html(f'<p class="note" style="color:var(--text);">Dismiss all <b>{len(mine)}</b> '
                    f'{"matching " if narrowed else ""}{"job" if len(mine) == 1 else "jobs"}? They’re hidden for you only '
                    '— other members still see them. You can restore any of them from Jobs → Dismissed.</p>')
            y, n_ = st.columns([1, 3])
            with y:
                st.button("Yes, dismiss all", key="btn_mh_clear_yes", type="primary", use_container_width=True,
                          on_click=_dismiss_all_mine, args=({job_key(j) for j in mine},))
            with n_:
                st.button("Cancel", key="btn_mh_clear_no", on_click=st.session_state.__setitem__,
                          args=("mh_clear_confirm", False))
    if not mine:
        if empty_scope:
            def _acts():
                c1, c2 = st.columns(2)
                with c1:
                    st.button("Choose companies", key="btn_mh_follow", type="primary", icon=":material/add:",
                              on_click=go, args=("companies",), kwargs={"view": "mine"})
                with c2:
                    st.button("Browse all jobs", key="btn_mh_browse", on_click=go, args=("jobs",))
            empty_state("apartment", "You aren’t following any companies yet",
                        "Follow the companies you care about and their fresher and entry-level roles appear here. "
                        "Every listing the tracker has found stays open to browse on the Jobs page.", _acts)
        elif narrowed:
            empty_state("search_off", "No jobs match your preferences right now",
                        "New roles appear after each scan. You can widen your preferences or browse every job.",
                        lambda: st.button("Browse all jobs", key="btn_mh_all", on_click=go, args=("jobs",)))
        elif dismissed_jobs:            # this person dismissed them (their own list); others still see them
            empty_state("inbox", "You’ve dismissed every job",
                        "New fresher and entry-level roles appear here after each scan. The ones you dismissed are "
                        "in Jobs → Dismissed, where you can restore them.",
                        lambda: st.button(f"View {_plural(len(dismissed_jobs), 'dismissed job')}", key="btn_mh_dismissed",
                                          on_click=lambda: (go("jobs"), st.session_state.update(jobs_tab="dismissed"))))
        else:
            empty_state("travel_explore", "No open jobs right now",
                        "New fresher and entry-level roles appear here after each scan.")
        return
    with st.container(key="joblist_home"):
        for i, j in enumerate(mine[:HOME_LIMIT]):
            job_row(j, f"m{i}", "home")
        with st.container(key="list_foot_h"):
            f1, f2 = st.columns([3, 1], vertical_alignment="center")
            with f1:
                st.html(f'<span class="muted num" style="font-size:13px;">Showing {min(HOME_LIMIT, len(mine))} of '
                        f'{_plural(len(mine), "match", "matches") if narrowed else _plural(len(mine), "job")}</span>')
            with f2:
                st.button("View all jobs", key="btn_all_jobs", icon=":material/arrow_forward:", icon_position="right",
                          on_click=go, args=("jobs",))


def personal_summary_card() -> None:
    """Settings: what this person has chosen, with a way to each page."""
    if ME is None:
        return
    notif = user_store.notification_settings_from(ME["profile"])
    used, saved = pref_lines(_VIEW["prefs"], MATCHED_PREFS), pref_lines(_VIEW["prefs"], SAVED_ONLY_PREFS)
    with st.container(key="set_personal"):
        st.html(f'<div class="set-head"><span class="ic">{_ms("person_search")}</span><div><div class="eyebrow">Job discovery</div>'
                '<h2 class="section-title">Your job search</h2><p class="section-sub">Saved to your own account.</p></div></div>'
                '<dl class="kv">'
                f'<dt>Used to match jobs</dt><dd>{"<br>".join(escape(x) for x in used) or "None yet"}</dd>'
                + (f'<dt>Saved for later</dt><dd>{"<br>".join(escape(x) for x in saved)}'
                   '<span class="kv-note">Not used for matching yet — postings don’t state job type or work mode.</span></dd>'
                   if saved else "") +
                f'<dt>Companies</dt><dd>{"Following " + str(len(ME["watchlist"])) if ME["watchlist"] else "Not following any yet"}</dd>'
                f'<dt>Alert emails</dt><dd>{escape(alerts_summary(notif))}</dd>'
                '</dl>')
        a, b, c = st.columns(3)
        with a:
            st.button("Edit preferences", key="btn_set_prefs", icon=":material/tune:", on_click=go, args=("preferences",),
                      use_container_width=True)
        with b:
            st.button("My companies", key="btn_set_cos", icon=":material/apartment:", on_click=go,
                      args=("companies",), kwargs={"view": "mine"}, use_container_width=True)
        with c:
            st.button("My alerts", key="btn_set_alerts", icon=":material/notifications:", on_click=go, args=("alerts",),
                      use_container_width=True)


# ═══════════════════════════════════════════════════════════════════════════════
# ROUTER
# ═══════════════════════════════════════════════════════════════════════════════
_report_background_saves()
page = st.session_state.page
jobs_by_id = {_jid(j): j for j in all_records}
if page == "jobs" and st.session_state.job_id:
    job = jobs_by_id.get(st.session_state.job_id)
    if job:
        page_job_detail(job)
    else:
        st.session_state.job_id = None
        page_jobs()
elif page == "companies" and st.session_state.company_view == "add":
    page_add_company()
elif page == "companies" and (MEMBER_VIEW or st.session_state.company_view == "mine") and not st.session_state.company_id:
    page_my_companies()
elif page == "companies" and st.session_state.company_id:
    comp = next((c for c in companies if str(c.get("id")) == str(st.session_state.company_id)), None)
    if comp:
        page_company_detail(comp)
    else:
        st.session_state.company_id = None
        page_companies()
else:
    {"home": page_member_home if MEMBER_VIEW else page_home, "jobs": page_jobs, "companies": page_companies,
     "monitoring": page_monitoring, "email": page_email, "settings": page_settings, "alerts": page_alerts,
     "preferences": page_preferences, "onboarding": page_onboarding}[page]()
st.session_state.page_before = st.session_state.page

# mirror the view into the URL (only when it changed, so no extra reruns)
want = {"page": st.session_state.page}
if st.session_state.job_id:
    want["job"] = st.session_state.job_id
if st.session_state.company_id:
    want["company"] = str(st.session_state.company_id)
if st.session_state.company_view:
    want["view"] = st.session_state.company_view
if st.session_state.dark_mode:
    want["theme"] = "dark"
if st.session_state.nav_collapsed:
    want["menu"] = "hidden"
if dict(st.query_params) != want:
    st.query_params.from_dict(want)
st.session_state._url_written = want   # a later mismatch means browser Back/Forward


def _warm_scraper_import() -> None:
    """Monitoring and Settings name each portal's scraper, which imports
    scraper.py (and the classifier's ~70 regexes): about 0.35 s the first
    time in a server process, paid by whoever opened Monitoring first. Do it
    once in the background after an admin's page is drawn instead."""
    import sys
    guards = _guards()
    if "scraper" in sys.modules or guards.get("scraper_warming"):
        return
    guards["scraper_warming"] = True

    def _load():
        try:
            import scraper  # noqa: F401
        except Exception:                 # the page imports it again (and reports) if it is really broken
            return
        _stamp_fresh_local_modules()      # just imported: current, so the next click doesn't reload it
    threading.Thread(target=_load, daemon=True).start()


_stamp_fresh_local_modules()
if not MEMBER_VIEW:
    _warm_scraper_import()

# ── the company logos this page drew (each image sent once) ──────────────────
_page_logos = {k: e for k, e in _USED_LOGOS.items() if k not in _FRAGMENT_LOGOS}
if _page_logos:
    st.html(_logo_styles(_page_logos))

# ── toast ─────────────────────────────────────────────────────────────────────
# Shown once and faded out with CSS; a sleep()+rerun would freeze the app.
if st.session_state.toast:
    msg = escape(st.session_state.toast, quote=False)
    ok = st.session_state.toast_kind == "success"
    st.session_state.toast = None
    st.html(f"""
<style>
@keyframes jt-toast {{ 0%, 85% {{ opacity: 1; }} 100% {{ opacity: 0; visibility: hidden; }} }}
.jt-toast {{ animation: jt-toast-in .28s cubic-bezier(.2,.7,.2,1) both, jt-toast {4 if ok else 6}s ease-in forwards; }}
</style>
<div class="jt-toast" role="status" style="--tone:var({'--green' if ok else '--red'});">
  <span class="ic">{_ms('check_circle' if ok else 'error', 's20')}</span>
  <div class="msg">{msg}</div>
</div>""")
