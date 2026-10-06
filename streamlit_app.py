import base64
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from urllib.parse import urlparse

import streamlit as st

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
import firebase_auth
from access import (
    ACCOUNT_KEY,
    OWNER_SESSION_KEY,
    OWNER_SESSION_S,
    AttemptLimiter,
    Cooldown,
    OwnerRequired,
    account_is_admin,
    admins_configured,
    alert_recipient,
    mask_email,
    owner_configured,
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
    st.session_state.company_view = "add" if qp.get("view") == "add" else None
    st.session_state.confirm_remove = None
    if "dark_mode" in st.session_state:
        st.session_state.dark_mode = qp.get("theme") == "dark"
if "dark_mode" not in st.session_state:
    # the theme lives in the URL (?theme=dark) so it survives reloads/bookmarks
    st.session_state.dark_mode = st.query_params.get("theme") == "dark"

st.set_page_config(
    page_title=f"{PAGES[st.session_state.page][0]} · Fresher Job Tracker",
    page_icon=str(LOGO_PATH) if LOGO_PATH.exists() else "💼",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── sign-in diagnostics (read-only) ───────────────────────────────────────────
# ?diag=auth shows what THIS session's server process can see of the sign-in
# plumbing — counts, cookie NAMES and booleans only, never a value, token or
# address — so a deployment can be checked without its logs. It stops before
# any data is loaded.
if st.query_params.get("diag") == "auth":
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

    st.html("<h3>Sign-in diagnostics</h3><table>" + "".join(
        f"<tr><td>{a}</td><td><code>{b}</code></td></tr>" for a, b in _auth_diag()) + "</table>")
    print("[auth-diag] " + " | ".join(f"{a}={b}" for a, b in _auth_diag()), flush=True)
    st.stop()

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
.chip:hover { border-color: var(--border-strong); box-shadow: var(--badge-hi), 0 4px 10px -6px rgba(16,24,40,.18); }
.chip .ms { font-size: 16px; color: var(--muted); }
.dot { width: 8px; height: 8px; border-radius: 999px; display: inline-block; flex-shrink: 0; position: relative; }
.dot.healthy { background: var(--green); } .dot.delayed { background: var(--amber); } .dot.failing { background: var(--red); }
.dot.pending { background: var(--gray); } .dot.checking { background: var(--accent); }
.dot.healthy::after, .dot.checking::after { content: ""; position: absolute; inset: 0; border-radius: inherit; background: inherit; animation: jt-pulse 2.4s ease-out infinite; }
.tag { font-family: var(--label) !important; font-size: 10.5px; line-height: 16px; letter-spacing: .08em; text-transform: uppercase; color: var(--muted); border: 1px solid var(--border-strong); border-radius: 6px; padding: 0 6px; font-weight: 500; }

/* ── surfaces ── */
.panel, [class*="st-key-filters_"], .st-key-add_form, .st-key-email_form, .st-key-test_panel, [class*="st-key-set_"],
.st-key-job_main, .st-key-job_side, .st-key-co_side, .st-key-empty, .st-key-hero, .st-key-job_hero, .st-key-co_hero, .st-key-mail_status,
.st-key-danger_confirm, .mon tbody tr, [class*="st-key-jr_"], [class*="st-key-cr_"] {
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

/* Opportunity cards: three crisp, abstract job cards and a "fresher" check
   tile resting in depth over a soft glow — the product's subject (roles being
   found), not a monitoring metaphor. No names or fake listings: an icon tile,
   placeholder text bars and a badge shape. Decorative (aria-hidden), pure CSS,
   transform/opacity animation only; nothing travels along a path. */
.hero { position: relative; }
.opps-wrap { position: relative; align-self: stretch; min-height: 270px; }
.opps { position: absolute; inset: 0; container-type: inline-size; }
.opps > .glow-bg { position: absolute; inset: -30px -40px -60px -10%; z-index: -1; pointer-events: none; opacity: var(--opp-glow);
  background: radial-gradient(48% 52% at 58% 44%, color-mix(in srgb, var(--accent) 30%, transparent), transparent 72%),
    radial-gradient(36% 40% at 80% 70%, color-mix(in srgb, var(--accent-2) 26%, transparent), transparent 72%),
    radial-gradient(30% 34% at 30% 76%, color-mix(in srgb, #3b82f6 22%, transparent), transparent 72%);
  animation: jt-glow-drift 12s ease-in-out infinite alternate; }
.oc { position: absolute; display: flex; align-items: center; gap: 12px; padding: 14px 16px; border-radius: 14px;
  background: var(--oc-bg); border: 1px solid var(--oc-border); box-shadow: var(--hi), var(--oc-shadow);
  backdrop-filter: blur(10px) saturate(1.2); -webkit-backdrop-filter: blur(10px) saturate(1.2);
  animation: jt-float var(--t, 9s) ease-in-out infinite; will-change: transform; }
.oc::after { content: ""; position: absolute; inset: -1px; border-radius: inherit; pointer-events: none; opacity: 0;   /* the brighten */
  box-shadow: 0 0 0 1px color-mix(in srgb, var(--accent) 40%, transparent), 0 14px 34px -14px color-mix(in srgb, var(--accent) 60%, transparent); }
.oc .ic { width: 38px; height: 38px; border-radius: 11px; display: flex; align-items: center; justify-content: center; flex-shrink: 0;
  color: var(--accent-text); border: 1px solid color-mix(in srgb, var(--accent) 18%, transparent); box-shadow: var(--badge-hi);
  background: linear-gradient(180deg, color-mix(in srgb, var(--accent-soft) 60%, var(--surface)), var(--accent-soft)); }
.oc .ic.teal { color: var(--accent-2); border-color: color-mix(in srgb, var(--accent-2) 22%, transparent);
  background: linear-gradient(180deg, color-mix(in srgb, var(--accent-2) 8%, var(--surface)), color-mix(in srgb, var(--accent-2) 16%, transparent)); }
.oc .ic.blue { color: #3b82f6; border-color: color-mix(in srgb, #3b82f6 22%, transparent);
  background: linear-gradient(180deg, color-mix(in srgb, #3b82f6 8%, var(--surface)), color-mix(in srgb, #3b82f6 15%, transparent)); }
.oc .tx { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 7px; }
.oc .tx i { display: block; height: 7px; border-radius: 6px; background: var(--oc-bar); }
.oc .tx i.l1 { width: 82%; height: 8px; background: var(--oc-bar-strong); } .oc .tx i.l2 { width: 56%; } .oc .tx i.l3 { width: 38%; }
.oc .badge { display: inline-flex; align-items: center; gap: 4px; padding: 3px 8px 3px 6px; border-radius: 999px; width: max-content;
  color: var(--green); background: var(--green-soft); border: 1px solid color-mix(in srgb, var(--green) 28%, transparent); box-shadow: var(--badge-hi); }
.oc .badge .ms { font-size: 14px; } .oc .badge b { display: block; width: 30px; height: 6px; border-radius: 6px; background: color-mix(in srgb, var(--green) 40%, transparent); }
/* the composition: one card in front, one behind, one lower, one check tile */
.oc.a { left: 6%; top: 36%; width: 74%; z-index: 3; --t: 9s; padding: 16px 18px; }
.oc.a .ic { width: 44px; height: 44px; border-radius: 13px; }
.oc.a::after { animation: jt-brighten 10s ease-in-out infinite; }
.oc.b { left: 30%; top: 10%; width: 64%; z-index: 2; --t: 11s; animation-delay: -4s; animation-name: jt-float, jt-dim; animation-duration: 11s, 10s; }
.oc.c { left: 0; top: 71%; width: 54%; z-index: 4; --t: 8s; animation-delay: -2s; padding: 12px 14px; }
.oc.c .ic { width: 32px; height: 32px; border-radius: 10px; }
.oc.d { left: 74%; top: 65%; z-index: 5; padding: 12px; --t: 7s; --tilt: 6deg; animation-name: jt-float-s; animation-delay: -3s; border-radius: 16px; }
.oc.d .ic { width: 40px; height: 40px; border-radius: 12px; color: var(--green); border-color: color-mix(in srgb, var(--green) 25%, transparent);
  background: linear-gradient(180deg, color-mix(in srgb, var(--green-soft) 50%, var(--surface)), var(--green-soft)); }
.oc.d .spark-dot { position: absolute; top: 7px; right: 7px; width: 7px; height: 7px; border-radius: 50%;
  background: radial-gradient(circle at 35% 35%, #fff 0 20%, var(--accent-2) 70%); box-shadow: 0 0 8px color-mix(in srgb, var(--accent-2) 70%, transparent); }

/* ── metric modules ── */
.stats { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 14px; }
.stat { position: relative; padding: 18px 18px 16px; min-width: 0; border: 1px solid var(--border); border-radius: var(--r-lg);
  background: radial-gradient(160px 90px at 0% 0%, color-mix(in srgb, var(--tint, var(--accent)) 6%, transparent), transparent 70%),
    linear-gradient(180deg, var(--surface), color-mix(in srgb, var(--surface) 70%, var(--surface-2)));
  box-shadow: var(--hi), var(--shadow); transition: transform var(--normal) var(--ease), box-shadow var(--normal) var(--ease), border-color var(--normal) var(--ease); overflow: hidden; }
.stat::before { content: ""; position: absolute; left: 16px; right: 40%; top: 0; height: 2px; border-radius: 0 0 2px 2px;
  background: linear-gradient(90deg, var(--tint, var(--accent)), transparent); opacity: .65; transition: opacity var(--normal) var(--ease); }
.stat:hover { transform: translateY(-2px); box-shadow: var(--hi), var(--shadow-lg); border-color: color-mix(in srgb, var(--tint, var(--accent)) 30%, var(--border)); }
.stat:hover::before { opacity: 1; }
.stat .ic { width: 36px; height: 36px; border-radius: 11px; display: flex; align-items: center; justify-content: center; margin-bottom: 14px;
  color: var(--tint, var(--accent)); border: 1px solid color-mix(in srgb, var(--tint, var(--accent)) 18%, transparent);
  background: linear-gradient(180deg, color-mix(in srgb, var(--tint, var(--accent)) 9%, transparent), color-mix(in srgb, var(--tint, var(--accent)) 16%, transparent));
  box-shadow: var(--badge-hi); transition: transform var(--normal) var(--ease), box-shadow var(--normal) var(--ease); }
.stat:hover .ic { transform: translateY(-1px) scale(1.05);
  box-shadow: var(--badge-hi), 0 6px 16px -6px color-mix(in srgb, var(--tint, var(--accent)) 60%, transparent); }
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
:is(.st-key-h_q, .st-key-j_q) :is([data-baseweb="input"], [data-testid="stTextInputRootElement"]) { min-height: 48px !important; border-radius: var(--r) !important; background: var(--surface-2) !important; }
:is(.st-key-h_q, .st-key-j_q) input { font-size: 15px !important; }
:is(.st-key-h_q, .st-key-j_q, .st-key-co_q) :is([data-baseweb="input"], [data-testid="stTextInputRootElement"])::before {
  content: "search"; font-family: 'Material Symbols Rounded' !important; font-feature-settings: 'liga'; font-weight: 400;
  font-size: 20px; width: 20px; overflow: hidden; white-space: nowrap; flex-shrink: 0;
  color: var(--muted); margin-left: 14px; align-self: center; line-height: 1; box-sizing: content-box; transition: color var(--fast);
}
:is(.st-key-h_q, .st-key-j_q, .st-key-co_q) :is([data-baseweb="input"], [data-testid="stTextInputRootElement"]):focus-within::before { color: var(--accent-text); }
[class*="st-key-filters_"] [data-baseweb="select"] > div, [class*="st-key-filters_"] .stSelectbox [role="group"] { border-radius: 999px !important; min-height: 36px !important; padding-left: 6px; background: var(--surface) !important; }

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
[class*="st-key-jr_"] > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"],
[class*="st-key-jr_"] > [data-testid="stHorizontalBlock"] { gap: 16px !important; align-items: center !important; flex-wrap: nowrap !important; }
[class*="st-key-jr_"] [data-testid="stColumn"] { width: auto !important; min-width: 0 !important; flex: 0 0 auto !important; }
[class*="st-key-jr_"] [data-testid="stColumn"]:first-child { flex: 1 1 auto !important; }
[class*="st-key-jr_"] [data-testid="stColumn"]:last-child [data-testid="stVerticalBlock"] { flex-direction: row !important; gap: 8px !important; align-items: center; flex-wrap: nowrap; }
[class*="st-key-jr_"] [data-testid="stColumn"]:last-child [data-testid="stElementContainer"] { width: auto !important; flex: 0 0 auto; }
.job { display: flex; gap: 16px; min-width: 0; align-items: flex-start; }
.logo { --h: 245; width: 44px; height: 44px; border-radius: 13px; display: flex; align-items: center; justify-content: center; flex-shrink: 0;
  font-size: 17px; font-weight: 800; letter-spacing: -0.02em;
  color: hsl(var(--h) var(--av-sat) var(--av-fg)); background: linear-gradient(140deg, hsl(var(--h) var(--av-sat) var(--av-bg)), hsl(calc(var(--h) + 28) var(--av-sat) var(--av-bg)));
  box-shadow: inset 0 0 0 1px hsl(var(--h) var(--av-sat) var(--av-bd)), var(--hi); transition: transform var(--normal) var(--ease); }
.logo.lg { width: 64px; height: 64px; border-radius: 18px; font-size: 26px; }
.logo.sm { width: 34px; height: 34px; border-radius: 10px; font-size: 14px; }
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
  [class*="st-key-jr_"] > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"], [class*="st-key-jr_"] > [data-testid="stHorizontalBlock"] { flex-wrap: wrap !important; gap: 14px !important; }
  [class*="st-key-jr_"] [data-testid="stColumn"]:first-child { flex: 1 1 100% !important; }
  [class*="st-key-jr_"] [data-testid="stColumn"]:last-child { flex: 1 1 100% !important; padding-left: 60px; }
}
@container (max-width: 420px) {
  [class*="st-key-jr_"] [data-testid="stColumn"]:last-child { padding-left: 0; }
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
.st-key-danger [data-testid^="stBaseButton"] { color: var(--red) !important; }
.st-key-danger [data-testid^="stBaseButton"]:hover:not(:disabled) { color: var(--red) !important; background: var(--red-soft) !important; border-color: color-mix(in srgb, var(--red) 45%, transparent) !important; }
.st-key-danger_confirm { padding: 16px 18px !important; gap: 12px !important; border-color: color-mix(in srgb, var(--red) 35%, var(--border)) !important;
  background: linear-gradient(180deg, var(--red-soft), transparent 140%), var(--surface) !important; }
.st-key-danger_confirm [data-testid="stBaseButton-secondary"] { color: var(--text) !important; }
.st-key-danger_confirm [data-testid="stBaseButton-primary"] { background: var(--red) !important; border-color: var(--red) !important; color: #fff !important; box-shadow: 0 8px 18px -10px var(--red) !important; }
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
[class*="st-key-cr_"] > [data-testid="stLayoutWrapper"], [class*="st-key-cr_"] > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"] { height: 100%; }
[class*="st-key-cr_"] > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"],
[class*="st-key-cr_"] > [data-testid="stHorizontalBlock"] { gap: 14px !important; flex-direction: column !important; align-items: stretch !important; flex-wrap: nowrap !important; }
[class*="st-key-cr_"] [data-testid="stColumn"] { width: 100% !important; min-width: 0 !important; flex: 0 0 auto !important; }
[class*="st-key-cr_"] [data-testid="stColumn"]:first-child { flex: 1 1 auto !important; }
[class*="st-key-cr_"] [data-testid="stColumn"]:last-child [data-testid^="stBaseButton"] { width: 100%; }
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
.mon tbody tr:hover { transform: translateY(-2px); box-shadow: var(--hi), var(--shadow-lg) !important; }
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
  background: var(--surface) !important; border: 1px solid var(--border-strong) !important; border-radius: 10px !important;
  min-height: 40px; transition: border-color var(--fast), box-shadow var(--normal) var(--ease), background var(--fast);
}
.stTextInput [data-baseweb="input"] *, [data-testid="stTextInputRootElement"] * { background-color: transparent !important; }
.stTextInput [data-baseweb="input"]:hover, [data-testid="stTextInputRootElement"]:hover, [data-baseweb="select"] > div:hover, .stSelectbox [role="group"]:hover { border-color: color-mix(in srgb, var(--accent) 35%, var(--border-strong)) !important; }
.stTextInput [data-baseweb="input"]:focus-within, [data-testid="stTextInputRootElement"]:focus-within, [data-baseweb="select"] > div:focus-within, .stSelectbox [role="group"]:focus-within {
  border-color: var(--accent) !important; box-shadow: var(--ring), 0 8px 24px -14px var(--accent) !important; background: var(--surface) !important;
}
.stTextInput input { font-size: 14px !important; color: var(--text) !important; -webkit-text-fill-color: var(--text); padding: 8px 12px !important; }
.stTextInput input::placeholder { color: var(--muted) !important; -webkit-text-fill-color: var(--muted); opacity: 1; }
.stSelectbox input, .stSelectbox [role="group"] * { font-size: 14px !important; color: var(--text) !important; -webkit-text-fill-color: var(--text); }
.stSelectbox svg { fill: var(--muted) !important; color: var(--muted) !important; }
[data-baseweb="select"] * { font-size: 14px !important; color: var(--text) !important; }
[data-baseweb="select"] svg { fill: var(--muted) !important; }
[data-baseweb="popover"] ul, [data-baseweb="popover"] [role="listbox"] { background: var(--raised) !important; border-radius: 12px !important; }
[data-baseweb="popover"] > div { border-radius: 12px !important; box-shadow: var(--shadow-lg) !important; border: 1px solid var(--border) !important; overflow: hidden; }
[data-baseweb="popover"] li { color: var(--text) !important; font-size: 14px !important; }
[data-baseweb="popover"] li:hover, [data-baseweb="popover"] li[aria-selected="true"] { background: var(--accent-soft) !important; }
[data-testid="InputInstructions"] { display: none !important; }
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
[data-testid="stSidebarUserContent"] [data-testid="stLayoutWrapper"]:has(> .st-key-side_foot_wrap) { margin-top: auto; }
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
/* application status, integrated into the rail: a quiet inset surface, not another card */
.side-foot { margin-top: auto; padding: 12px 12px 11px; border-radius: 12px; border: 1px solid var(--border); position: relative; overflow: hidden;
  background: linear-gradient(180deg, color-mix(in srgb, var(--surface-2) 70%, transparent), color-mix(in srgb, var(--surface-2) 95%, transparent));
  font-size: 12px; line-height: 17px; color: var(--muted); box-shadow: var(--hi); transition: border-color var(--normal) var(--ease); }
.side-foot::before { content: ""; position: absolute; left: 12px; right: 12px; top: 0; height: 1px; background: linear-gradient(90deg, transparent, color-mix(in srgb, var(--tone, var(--green)) 50%, transparent), transparent); }
.side-foot.failing { --tone: var(--red); } .side-foot.delayed { --tone: var(--amber); } .side-foot.pending, .side-foot.checking { --tone: var(--gray); }
.side-foot .st { display: flex; align-items: center; gap: 9px; color: var(--text); font-weight: 700; font-size: 13px; }
.side-acct { margin-top: 10px; padding: 10px 11px; border-radius: 12px; border: 1px solid var(--border); display: flex; align-items: center; gap: 9px; min-width: 0; }
.side-acct .who { min-width: 0; flex: 1; }
.side-acct .nm { font-size: 13px; font-weight: 700; color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.side-acct .em { font-size: 11.5px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.side-acct .role { font-size: 10.5px; font-weight: 700; letter-spacing: .04em; text-transform: uppercase; padding: 3px 7px; border-radius: 999px;
  color: var(--muted); background: var(--surface-2); flex: none; }
.side-acct .role.admin { color: var(--accent-text); background: var(--accent-soft); }
.side-foot .meta { margin-top: 7px; padding-left: 17px; display: flex; flex-direction: column; gap: 3px; }
.side-foot .meta .txt { display: flex; align-items: center; gap: 6px; } .side-foot .meta .ms { color: var(--muted); opacity: .8; }
.st-key-side_foot_wrap { margin-top: auto; padding-top: 16px; }

/* mobile top nav (phones only) */
.st-key-mnav { display: none !important; }

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
.how > div:hover { border-color: color-mix(in srgb, var(--accent) 30%, var(--border-strong)); background: color-mix(in srgb, var(--surface) 85%, transparent); }

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
  .opps-wrap { min-height: 230px; } .oc.c { display: none; } .oc.a { left: 0; width: 86%; top: 34%; } .oc.b { left: 18%; width: 78%; } .oc.d { left: 70%; top: 66%; }
  .oc { gap: 10px; padding: 12px 13px; } .oc.a { padding: 13px 14px; } .oc.a .ic { width: 38px; height: 38px; } .oc .ic { width: 32px; height: 32px; }
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
  .side-foot { padding: 12px 0; text-align: center; border: none; background: none; box-shadow: none; } .side-foot .txt, .side-foot .meta { display: none; } .side-foot::before { display: none; } .side-foot .st { justify-content: center; }
  .st-key-job_cols > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"], .st-key-job_cols > [data-testid="stHorizontalBlock"] { flex-wrap: wrap !important; }
  .st-key-job_cols [data-testid="stColumn"]:nth-child(n) { flex: 1 1 100% !important; width: 100% !important; }
  .how { grid-template-columns: minmax(0, 1fr); }
  .mail-facts { grid-template-columns: minmax(0, 1fr); } .mail-facts > div + div { border-left: none; border-top: 1px solid var(--border); }
}
/* phone: sidebar hidden, top nav bar instead */
@media (max-width: 767px) {
  section[data-testid="stSidebar"] { display: none !important; }
  [data-testid="stMainBlockContainer"], .block-container { padding: 0 16px 36px !important; }
  [data-testid="stMainBlockContainer"] > div > [data-testid="stVerticalBlock"] { gap: 18px; }
  .st-key-mnav {
    display: flex !important; position: sticky; top: 0; z-index: 30; margin: 0 -16px; padding: 6px 4px !important; width: calc(100% + 32px) !important; max-width: none !important;
    background: color-mix(in srgb, var(--surface) 88%, transparent); backdrop-filter: blur(14px); -webkit-backdrop-filter: blur(14px); border-bottom: 1px solid var(--border);
  }
  /* all six destinations visible at once: equal columns, icon above a short label */
  .st-key-mnav > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"], .st-key-mnav > [data-testid="stHorizontalBlock"] { flex-wrap: nowrap !important; gap: 0 !important; }
  .st-key-mnav [data-testid="stColumn"] { flex: 1 1 0 !important; width: auto !important; min-width: 0 !important; }
  .st-key-mnav [data-testid^="stBaseButton"] { width: 100% !important; border: none !important; background: transparent !important; box-shadow: none !important; min-height: 52px !important; padding: 4px 0 !important; color: var(--text-2) !important; }
  .st-key-mnav [data-testid^="stBaseButton"]:hover { transform: none; }
  .st-key-mnav [data-testid^="stBaseButton"] > div, .st-key-mnav [data-testid^="stBaseButton"] > div > span { flex-direction: column !important; align-items: center !important; gap: 3px !important; min-width: 0; max-width: 100%; }
  .st-key-mnav [data-testid="stIconMaterial"] { font-size: 20px !important; margin: 0 !important; padding: 2px 12px; border-radius: 999px; transition: background var(--normal) var(--ease), color var(--fast); }
  .st-key-mnav p { font-size: 10.5px !important; line-height: 13px !important; font-weight: 600 !important; letter-spacing: -0.01em; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 100%; padding: 0 1px; }
  .page-title, .detail-title { font-size: 24px; line-height: 31px; }
  .st-key-hero { padding: 22px 18px 20px !important; }
  /* phones: not a shrunk desktop — a compact strip of one card and the
     check tile above the eyebrow, so nothing sits over the heading */
  .hero { grid-template-columns: minmax(0, 1fr); gap: 0; }
  .opps-wrap { order: -1; min-height: 0; height: 58px; margin-bottom: 16px; }
  .opps > .glow-bg { inset: -22px -18px -20px -18px; }
  .oc.b, .oc.c { display: none; }
  .oc.a { left: 0; top: 0; width: min(250px, 74%); padding: 9px 12px; gap: 10px; border-radius: 12px; --t: 10s; }
  .oc.a .ic { width: 34px; height: 34px; border-radius: 10px; } .oc .tx { gap: 5px; } .oc .tx i.l3, .oc.a .badge { display: none; }
  .oc.d { left: auto; right: 0; top: 4px; padding: 8px; border-radius: 13px; }
  .oc.d .ic { width: 34px; height: 34px; }
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
  .jt-toast { left: 16px; right: 16px; bottom: 16px; max-width: none; }
}
@media (max-width: 360px) {
  /* size each destination to its label so "Companies" is never cut off */
  .st-key-mnav [data-testid="stColumn"]:nth-child(n) { flex: 1 1 auto !important; }
  .st-key-mnav p { font-size: 10px !important; letter-spacing: -0.02em; text-overflow: clip; }
  .st-key-mnav [data-testid="stIconMaterial"] { padding: 2px 8px; }
  .stat .v { font-size: 21px; line-height: 27px; }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation-duration: .001ms !important; animation-iteration-count: 1 !important; transition-duration: .001ms !important; scroll-behavior: auto !important; }
  /* the cards rest in place: no float, no brighten, no glow drift */
  .oc, .oc::after, .opps > .glow-bg { animation: none !important; }
  .oc.d { transform: rotate(6deg); }
  .btn.primary::after, [data-testid="stBaseButton-primary"]::after { display: none; }
}
"""

# @import rules only count at the very top of a stylesheet, so the design
# tokens go after the CSS (with them first, the web fonts never loaded)
st.html(f"<style>{_CSS}\n{_root_vars}</style>")


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


# The scan schedule is whatever check_jobs.yml's cron says (always UTC on
# GitHub). It is read from the workflow so the UI follows any change there,
# and shown in India time. IST is a fixed +05:30 (India has no DST), so no
# tz database is needed.
IST = timezone(timedelta(hours=5, minutes=30), "IST")
_WORKFLOW = BASE / ".github" / "workflows" / "check_jobs.yml"
_DEFAULT_CRON = "0 */3 * * *"


def _schedule_cron() -> str:
    try:
        m = re.search(r"""cron:\s*["']([^"']+)["']""", _WORKFLOW.read_text("utf-8"))
    except OSError:
        m = None
    return m.group(1).strip() if m else _DEFAULT_CRON


def _cron_field(spec: str, lo: int, hi: int) -> list[int]:
    """Values of one cron field: *, */n, a, a-b, a-b/n, a/n and comma lists."""
    vals: set[int] = set()
    for part in spec.split(","):
        rng, _, step = part.partition("/")
        if rng == "*":
            a, b = lo, hi
        elif "-" in rng:
            a, b = (int(x) for x in rng.split("-", 1))
        else:
            a = int(rng)
            b = hi if step else a
        vals.update(range(a, b + 1, int(step) if step else 1))
    return sorted(v for v in vals if lo <= v <= hi)


def _cron_times(expr: str) -> tuple[list[int], list[int]]:
    """(minutes, hours) of a cron line; the day fields of this workflow are
    all '*', so only minute and hour decide the slots."""
    try:
        minute, hour = expr.split()[:2]
        mins, hrs = _cron_field(minute, 0, 59), _cron_field(hour, 0, 23)
        if mins and hrs:
            return mins, hrs
    except ValueError:
        pass
    return _cron_times(_DEFAULT_CRON) if expr != _DEFAULT_CRON else ([0], list(range(0, 24, 3)))


def _next_slot(now: datetime | None = None, expr: str | None = None) -> datetime:
    """The next scheduled slot of check_jobs.yml's cron (UTC).
    GitHub Actions only *schedules* at these times — runs usually start later
    (measured Sep 2026: median 5 h apart, up to 9 h), so this is shown as a
    scheduled time, never as a countdown."""
    now = now or datetime.now(timezone.utc)
    mins, hrs = _cron_times(expr or _schedule_cron())
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return min(t for d in (0, 1) for h in hrs for m in mins
               if (t := day + timedelta(days=d, hours=h, minutes=m)) > now)


def _cadence(expr: str | None = None) -> str:
    """'Every 3 hours' / 'Every hour' / 'Daily' / '4 times a day'."""
    mins, hrs = _cron_times(expr or _schedule_cron())
    if len(mins) == 1 and len(hrs) == 1:
        return "Daily"
    if len(mins) == 1 and 24 % len(hrs) == 0 and hrs == list(range(hrs[0], 24, 24 // len(hrs))):
        n = 24 // len(hrs)
        return "Every hour" if n == 1 else f"Every {n} hours"
    return f"{len(mins) * len(hrs)} times a day"


def _ist(dt: datetime) -> str:
    """'11:30 PM IST'."""
    return dt.astimezone(IST).strftime("%I:%M %p").lstrip("0") + " IST"


def _ist_stamp(dt: datetime) -> str:
    """'Oct 05, 7:29 PM IST'."""
    return f"{dt.astimezone(IST):%b %d}, {_ist(dt)}"


def _ms(name: str, size: str = "") -> str:
    return f'<span class="ms {size}" aria-hidden="true">{name}</span>'


def _initial(name: str) -> str:
    name = (name or "").strip()
    return escape(name[:1].upper()) if name[:1].isalnum() else "•"


# company tints: well-separated hues, assigned in tracking order so every
# tracked company gets its own; anything else falls back to a name hash
_HUES = (250, 175, 25, 320, 205, 140, 285, 0, 55, 230)


def _avatar(name: str, size: str = "") -> str:
    """Company identity: the initial on a tint, so companies are told apart
    at a glance (no logos are fetched or invented)."""
    key = (name or "").strip().lower()
    order = [(c.get("name") or "").strip().lower() for c in companies]
    idx = order.index(key) if key in order else int(hashlib.sha1(key.encode("utf-8")).hexdigest()[:4], 16)
    hue = _HUES[idx % len(_HUES)]
    return f'<div class="logo{" " + size if size else ""}" style="--h:{hue}" aria-hidden="true">{_initial(name)}</div>'


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


def _trigger_scrape() -> tuple[bool, str]:
    """Ask GitHub Actions to run check_jobs.yml right now (workflow_dispatch).
    Owner only, and at most once per cooldown for the whole app."""
    if not is_owner():
        return False, "Only the owner can start a check."
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        return False, "Can't start a check — no GitHub token is configured for this app."
    try:
        from github import Github
        repo = Github(token).get_repo(GITHUB_REPO)
        workflow = repo.get_workflow("check_jobs.yml")
        if workflow.create_dispatch(ref="main"):
            return True, "Check started — it takes a few minutes. Use Refresh afterwards to see new jobs."
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


@st.cache_resource(show_spinner=False)
def _guards() -> dict:
    """Limits shared by every session of this server process."""
    return {"signin": AttemptLimiter(), "check": Cooldown(_CHECK_COOLDOWN_S),
            "refresh": Cooldown(_REFRESH_COOLDOWN_S),
            "firebase_signin": AttemptLimiter()}


def is_owner() -> bool:
    return owner_session_valid(st.session_state)


def sign_in(password: str) -> tuple[bool, str]:
    limiter = _guards()["signin"]
    wait = limiter.locked_for()
    if wait:
        return False, f"Too many failed attempts — try again in {int(wait // 60) + 1} min."
    if not owner_configured():
        return False, "Owner access isn't set up for this app."
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
    """Reload from GitHub — at most every few seconds for the whole app, so
    repeated clicks can't spend the token's API quota."""
    if _guards()["refresh"].try_start():
        _remote_snapshot.clear()


# Latest data straight from GitHub, shared by all sessions and refreshed at
# most every 5 minutes (or on Refresh) — the local checkout can be hours old.
@st.cache_data(ttl=300, show_spinner=False)
def _remote_snapshot() -> dict:
    return fetch_remote_json(["companies.json", "settings.json", "seen_jobs.json"])


def _load_synced(name: str, default):
    """Prefer the GitHub copy (read-only here — saves go through
    _save_change); fall back to the local checkout."""
    remote = _remote_snapshot().get(name)
    if remote is not None and isinstance(remote, type(default)):
        return remote
    local = _load(BASE / name, default)
    # a corrupt or hand-edited file ("null", "[]" for settings) must not crash the app
    return local if isinstance(local, type(default)) else default


def _save_change(name: str, mutate, default, message: str):
    """Apply one change to the latest copy of ``name`` (merge-safe).
    Every write goes through here, so the owner check is repeated here: a
    button that forgot its own check still can't change anything."""
    if not is_owner():
        log.warning("blocked a change to %s from a session without owner access", name)
        return None, False, "Only the owner can make changes."
    data, saved, err = update_json(BASE / name, name, mutate, default, message)
    _remote_snapshot.clear()
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
#     server-side session state for ACCOUNT_SESSION_S.
#   * Google — Streamlit's st.login verified Google's ID token and keeps the
#     identity in its signed 30-day cookie, so a reload or a new tab is
#     already signed in. The Firebase account is linked right after sign-in
#     (while Google's ID token is fresh) to obtain the Firebase UID.
# Admin rights come from the account (access.JT_ADMIN_EMAILS, verified email).
# No token, password or secret is ever kept in session state.
_OIDC_KEYS = ("client_id", "client_secret", "server_metadata_url")
ACCOUNT_SESSION_S = 12 * 3600
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
    except Exception:
        return None
    if not isinstance(email, str) or "@" not in email:
        return None
    return {"email": email.strip().lower(), "email_verified": verified is True,
            "name": name if isinstance(name, str) else ""}


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
        return
    st.session_state._fb_link = {"state": "linked"}
    st.session_state[firebase_auth.SESSION_KEY] = {**user.as_session(), "expires_at": time.time() + ACCOUNT_SESSION_S}
    print("[auth] google sign-in ok; firebase linked", flush=True)


def resolve_account(google: dict | None, config) -> dict | None:
    """The signed-in account for this run (also stored, without secrets,
    under ACCOUNT_KEY for the server-side admin check), or None."""
    account = None
    fb_user = firebase_auth.session_user(st.session_state)
    if not st.session_state.get("_signed_out"):
        g = _google_identity() if google else None
        if g:
            if fb_user is None:
                _link_google_to_firebase(google, config)
                fb_user = firebase_auth.session_user(st.session_state)
            uid = fb_user.get("uid") if fb_user and fb_user.get("email") == g["email"] else ""
            account = {**g, "uid": uid, "provider": "google.com"}
        elif fb_user and fb_user.get("provider") == "password":
            account = {"email": fb_user.get("email") or "", "email_verified": fb_user.get("email_verified") is True,
                       "name": "", "uid": fb_user["uid"], "provider": "password"}
    elif fb_user and fb_user.get("provider") == "password":
        # signed in again with email/password after an explicit sign-out
        account = {"email": fb_user.get("email") or "", "email_verified": fb_user.get("email_verified") is True,
                   "name": "", "uid": fb_user["uid"], "provider": "password"}
    if account is None and not admins_configured() and owner_session_valid(st.session_state):
        account = {"email": "", "email_verified": False, "name": "Owner", "uid": "", "provider": "owner"}
    if account:
        st.session_state[ACCOUNT_KEY] = account
    else:
        st.session_state.pop(ACCOUNT_KEY, None)
    return account


def account_sign_out() -> bool:
    """End this sign-in. Returns True when Streamlit's Google sign-in is
    being ended too (the browser is then redirected)."""
    for k in (firebase_auth.SESSION_KEY, ACCOUNT_KEY, OWNER_SESSION_KEY, "_fb_link", "_fb_link_tried", "_login_error"):
        st.session_state.pop(k, None)
    st.session_state._signed_out = True
    if _google_identity() is not None:
        st.logout()              # clears Streamlit's sign-in cookie, back to the login page
        return True
    return False


def firebase_sign_in(email: str, password: str) -> tuple[bool, str]:
    limiter = _guards()["firebase_signin"]
    wait = limiter.locked_for()
    if wait:
        return False, f"Too many failed attempts — try again in {int(wait // 60) + 1} min."
    if _AUTH_CONFIG is None:
        return False, _AUTH_PROBLEM
    try:
        user = firebase_auth.sign_in_with_password(email, password, config=_AUTH_CONFIG)
    except firebase_auth.FirebaseAuthError as e:
        if e.code in _FB_CREDENTIAL_ERRORS:
            limiter.failed()
        return False, str(e)
    st.session_state[firebase_auth.SESSION_KEY] = {**user.as_session(), "expires_at": time.time() + ACCOUNT_SESSION_S}
    st.session_state.pop("_signed_out", None)
    return True, ""


def _login_submit() -> None:
    """Login form callback: wipe the password from session state first."""
    email = st.session_state.get("login_email", "")
    password = st.session_state.get("login_pw", "")
    st.session_state.login_pw = ""
    ok, msg = firebase_sign_in(email, password)
    if ok:
        st.session_state.pop("_login_error", None)
    else:
        st.session_state._login_error = msg


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


def _login_google() -> None:
    for k in ("_signed_out", "_login_error", "_fb_link", "_fb_link_tried"):
        st.session_state.pop(k, None)
    if _GOOGLE["provider"]:
        st.login(_GOOGLE["provider"])
    else:
        st.login()


_LOGIN_CSS = """
[data-testid="stSidebar"], [data-testid="stSidebarCollapsedControl"], .st-key-mnav { display: none !important; }
.login-head, .login-head *, .st-key-login_card, .st-key-login_card *, .login-foot { font-family: var(--font) !important; }
.login-head { max-width: 440px; margin: 7vh auto 0; text-align: center; }
.login-head .lmark { width: 54px; height: 54px; margin: 0 auto 14px; border-radius: 16px; display: grid; place-items: center;
  background: linear-gradient(135deg, var(--accent), var(--accent-2)); box-shadow: 0 14px 30px -14px var(--accent); }
.login-head .lmark img { width: 34px; height: 34px; }
.login-head .eyebrow { justify-content: center; }
.login-head h1 { margin: 6px 0 10px; font-size: 30px; font-weight: 800; line-height: 1.15; letter-spacing: -.02em; color: var(--text); }
.login-head p { margin: 0 auto; max-width: 360px; color: var(--muted); font-size: 14.5px; line-height: 1.55; }
.st-key-login_card { max-width: 420px; margin: 22px auto 0 !important; padding: 26px 24px 22px !important;
  border: 1px solid var(--border) !important; border-radius: var(--r-lg) !important; background: var(--surface) !important;
  box-shadow: var(--shadow-lg, 0 24px 60px -32px rgba(15, 23, 42, .35)) !important; }
.login-or { display: flex; align-items: center; gap: 10px; color: var(--muted); font-size: 12px; font-weight: 600;
  text-transform: uppercase; letter-spacing: .06em; margin: 4px 0; }
.login-or::before, .login-or::after { content: ""; flex: 1; height: 1px; background: var(--border); }
.login-err { display: flex; gap: 9px; align-items: flex-start; padding: 11px 13px; border-radius: 12px; font-size: 13.5px; line-height: 1.5;
  color: var(--text); background: color-mix(in srgb, var(--red) 9%, transparent); border: 1px solid color-mix(in srgb, var(--red) 35%, var(--border)); }
.login-foot { max-width: 420px; margin: 14px auto 0; text-align: center; color: var(--muted); font-size: 12.5px; }
"""


def render_login_page() -> None:
    logo = _logo_data_uri()
    img = f'<img src="{logo}" alt="">' if logo else ""
    st.html(f"<style>{_LOGIN_CSS}</style>"
            f'<div class="login-head"><div class="lmark">{img}</div>'
            '<div class="eyebrow">Fresher Job Tracker</div>'
            '<h1>Sign in to your <em>opportunities</em></h1>'
            '<p>Fresher and entry-level roles in India, read from each company\'s own careers page.</p></div>')
    with st.container(key="login_card"):
        err = st.session_state.get("_login_error")
        if err:
            st.html(f'<div class="login-err" role="alert">{_ms("error", "s20")}<div>{escape(err)}</div></div>')
        if _GOOGLE:
            st.button("Continue with Google", key="btn_login_google", type="primary", use_container_width=True,
                      icon=":material/account_circle:", on_click=_login_google)
            st.html('<div class="login-or">or sign in with email</div>')
        with st.form("login_form", clear_on_submit=True, border=False):
            st.text_input("Email", key="login_email", autocomplete="email")
            st.text_input("Password", type="password", key="login_pw", autocomplete="current-password")
            st.form_submit_button("Sign in", key="btn_login_email", icon=":material/login:",
                                  use_container_width=True, on_click=_login_submit)
        if _GOOGLE_PROBLEM and not _GOOGLE:
            st.html(f'<p class="note">{_ms("info", "s16")} {escape(_GOOGLE_PROBLEM)}</p>')
        # until admins come from sign-in (JT_ADMIN_EMAILS), the owner can always
        # get in — even if Google or Firebase misbehaves
        if owner_configured() and not admins_configured():
            with st.expander("Trouble signing in? Use the owner password", icon=":material/key:"):
                with st.form("login_owner_form", clear_on_submit=True, border=False):
                    st.text_input("Owner password", type="password", key="login_owner_pw",
                                  autocomplete="current-password")
                    st.form_submit_button("Sign in as owner", key="btn_login_owner", icon=":material/admin_panel_settings:",
                                          use_container_width=True, on_click=_login_owner_submit)
    st.html('<div class="login-foot">Private app — accounts are for invited users.</div>')


_AUTH_CONFIG, _AUTH_PROBLEM, _AUTH_PRESENT = _firebase_config()
AUTH_GATE = _AUTH_CONFIG is not None
_promote_admin_setting()
_GOOGLE, _GOOGLE_PROBLEM = _google_setup() if AUTH_GATE else (None, "")
ACCOUNT: dict | None = None
if AUTH_GATE:
    ACCOUNT = resolve_account(_GOOGLE, _AUTH_CONFIG)
    if ACCOUNT is None:
        render_login_page()
        st.stop()
else:
    st.session_state.pop(ACCOUNT_KEY, None)


def account_label(account: dict) -> str:
    return (account.get("name") or account.get("email") or "Signed in").strip()


# ── data ──────────────────────────────────────────────────────────────────────
companies: list[dict] = []
for _i, _c in enumerate(c for c in _load_synced("companies.json", []) if isinstance(c, dict)):
    # every company needs a unique id for its widgets/links; a missing or
    # repeated id (hand-edited file) gets one for this page view only — the
    # file itself is never rewritten here
    _c = dict(_c)
    if not _c.get("id") or any(str(x.get("id")) == str(_c.get("id")) for x in companies):
        _c["id"] = f"_row{_i}"
    companies.append(_c)
settings: dict = _load_synced("settings.json", {"recipient_email": ""})
# seen_jobs.json is also the scraper's dedup history: jobs are dismissed
# (hidden), never deleted, or the scraper would email them again
all_records: list[dict] = []
_seen_keys: set[str] = set()
for _j in _load_synced("seen_jobs.json", []):
    # a duplicated record (same id + URL) would give two widgets the same
    # key and crash the page — show each job once
    if isinstance(_j, dict) and job_key(_j) not in _seen_keys:
        _seen_keys.add(job_key(_j))
        all_records.append(_j)
active_jobs: list[dict] = visible_jobs(all_records)                                   # newest-first
dismissed_jobs: list[dict] = [j for j in reversed(all_records) if j.get("dismissed")]  # newest-first

NOW = datetime.now(timezone.utc)
# cron asks for every 3 h, but GitHub starts scheduled runs late: measured
# gaps were median 5 h, 90th percentile 7.5 h, max 9 h — so a scan only
# counts as "delayed" once it is overdue by more than that
_STALE_H = 10
SCHEDULE_NOTE = f"{_cadence().lower()} (GitHub may start scans later)"
HOME_LIMIT = 8
JOBS_PAGE = 12
_CATEGORY_PILL = {"FRESHER": "fresher", "ENTRY_LEVEL": "entry"}
LEGACY_LABEL = "Keyword match"  # records from before the classifier existed

for _k, _v in (("toast", None), ("toast_kind", "success"), ("jobs_page", 0), ("test_status", None),
               ("confirm_remove", None),
               ("just_added", None), ("job_from", "jobs"), ("confirm_clear", False)):
    if _k not in st.session_state:
        st.session_state[_k] = _v
if st.session_state.pop("_clear_add_form", False):
    for _k in ("new_name", "new_url", "new_website"):
        st.session_state[_k] = ""


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
elif last_scan is None:
    overall = ("pending", "Waiting for first scan")
elif scan_stale:
    overall = ("delayed", "Scans delayed")
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
    return sum(1 for j in company_jobs(c) if not j.get("dismissed"))


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
        f".st-key-nav_{current} p {{font-weight: 700 !important;}}"
        f".st-key-mob_{current} [data-testid^='stBaseButton'] {{color: var(--accent-text) !important;}}"
        f".st-key-mob_{current} [data-testid='stIconMaterial'] {{background: var(--accent-soft); color: var(--accent-text) !important;}}"
        f".st-key-mob_{current} p {{font-weight: 700 !important;}}</style>")

with st.sidebar:
    logo = _logo_data_uri()
    st.html(f"""<div class="brand"><div class="mark {overall[0]}">{f'<img src="{logo}" alt="">' if logo else ''}</div>
      <div><div class="n">Fresher Job Tracker</div><div class="s">Fresher opportunities</div></div></div>""")
    for gi, (group, items) in enumerate(NAV_GROUPS):
        st.html(f'<div class="nav-group{" first" if gi == 0 else ""}">{group}</div>')
        for key in items:
            label, icon = PAGES[key]
            st.button(label, key=f"nav_{key}", icon=icon, type="tertiary", use_container_width=True,
                      on_click=go, args=(key,))
    with st.container(key="side_foot_wrap"):
        st.html(f"""<div class="side-foot {overall[0]}" role="status">
          <div class="st" title="{escape(overall[1], quote=True)}"><span class="dot {overall[0]}"></span><span class="txt">{escape(overall[1])}</span></div>
          <div class="meta"><span class="txt">{_ms("apartment", "s14")}{_plural(len(companies), 'company', 'companies')} monitored</span>
            <span class="txt num">{_ms("history", "s14")}Last scan {_ago(last_scan, NOW)}</span></div>
        </div>""")
    if ACCOUNT:
        _admin = account_is_admin(ACCOUNT) or is_owner()
        with st.container(key="side_account"):
            st.html(f'<div class="side-acct" title="{escape(ACCOUNT.get("email") or "", quote=True)}">{_avatar(account_label(ACCOUNT), "sm")}'
                    f'<div class="who"><div class="nm">{escape(account_label(ACCOUNT))}</div>'
                    f'<div class="em">{escape(ACCOUNT.get("email") or "")}</div></div>'
                    f'<span class="role {"admin" if _admin else ""}">{"Admin" if _admin else "Member"}</span></div>')
            if st.button("Sign out", key="btn_account_sign_out", icon=":material/logout:", type="tertiary",
                         use_container_width=True):
                if not account_sign_out():
                    st.rerun()
                st.stop()

# phones: the sidebar is hidden and this compact bar takes over (CSS decides)
with st.container(key="mnav"):
    cols = st.columns(len(PAGES))
    for col, key in zip(cols, PAGES):
        with col:
            label, icon = PAGES[key]
            st.button({"email": "Email", "monitoring": "Monitor", "companies": "Companies"}.get(key, label), key=f"mob_{key}",
                      icon=icon, on_click=go, args=(key,))


# ═══════════════════════════════════════════════════════════════════════════════
# SHARED PIECES
# ═══════════════════════════════════════════════════════════════════════════════
def _eyebrow(page: str) -> str:
    return next((g for g, items in NAV_GROUPS if page in items), "Discover")


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
            f'<span class="chip num">{_ms("schedule", "s16")}Next scheduled {_ist(_next_slot())}</span>')


def scan_actions():
    a, b = st.columns(2)
    with a:
        if st.button("Refresh", key="btn_refresh", icon=":material/refresh:", help="Reload the latest jobs and portal status"):
            _refresh_data()
            toast("Showing the latest data", "success")
            st.rerun()
    with b:
        if st.button("Run check", key="btn_run_check", icon=":material/travel_explore:", type="primary",
                     help="Ask GitHub Actions to scan every portal now (takes a few minutes)"):
            cooldown = _guards()["check"]
            if not owner_only("start a check"):
                pass
            elif not cooldown.try_start():
                toast(f"A check was just started — try again in {int(cooldown.remaining() // 60) + 1} min", "error")
            else:
                ok, msg = _trigger_scrape()
                if ok:
                    st.session_state.last_check_trigger = time.time()
                else:
                    cooldown.cancel()
                toast(msg, "success" if ok else "error")
            st.rerun()


def apply_link(j: dict, label: str = "Apply") -> str:
    url = safe_url(j.get("url", ""))
    if not url:
        return '<span class="btn disabled">No link</span>'
    title = (j.get("title") or "this job").strip() or "this job"
    return (f'<a class="btn primary" href="{escape(url, quote=True)}" target="_blank" rel="noopener noreferrer" '
            f'aria-label="{escape(label, quote=True)}: {escape(title, quote=True)} (opens the company site)">'
            f'{label} {_ms("arrow_outward", "s16")}</a>')


def filter_jobs(prefix: str, jobs: list[dict]) -> list[dict]:
    """Search · location · category · company · sort · clear."""
    locs = sorted({loc for j in jobs for loc in job_locations(j)}, key=str.lower)
    comps = sorted({(j.get("company") or "").strip() for j in jobs if (j.get("company") or "").strip()}, key=str.lower)
    cats = {"all": "All categories", "FRESHER": "Fresher", "ENTRY_LEVEL": "Entry level", "legacy": LEGACY_LABEL}
    sorts = {"new": "Newest first", "old": "Oldest first", "company": "Company A–Z", "title": "Title A–Z"}
    keys = {k: f"{prefix}_{k}" for k in ("q", "loc", "cat", "co", "sort")}
    defaults = {"q": "", "loc": "All locations", "cat": "all", "co": "All companies", "sort": "new"}
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
    return out, active, _clear


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
        meta.append(f'<span class="mi num" title="{escape(j.get("date") or "", quote=True)} UTC">{_ms("schedule")}'
                    f'Found {_ago(d, NOW) if d else escape(j["date"])}</span>')
    with st.container(key=f"jr_{idx}"):
        c1, c2 = st.columns([5, 2], vertical_alignment="center")
        with c1:
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
        with c2:
            st.html(apply_link(j))
            st.button("Details", key=_widget_key("crit", jkey), on_click=go, args=("jobs",),
                      kwargs={"job": _jid(j), "origin": origin})
            if j.get("dismissed"):
                if st.button("Restore", icon=":material/undo:", key=_widget_key("restore", jkey),
                             help="Restore to active jobs (it won't be emailed again)"):
                    if not owner_only("restore jobs"):
                        st.rerun()
                    _, saved, err = _save_change("seen_jobs.json", _restore_jobs({jkey}), [], "chore: restore 1 alert(s)")
                    toast("Job restored" if saved else f"Restored here, but not saved permanently. {err}",
                          "success" if saved else "error")
                    st.rerun()
            elif st.button("Dismiss", icon=":material/close:", key=_widget_key("dismiss", jkey),
                           help="Dismiss — hide this job (it won't be emailed again)"):
                if not owner_only("dismiss jobs"):
                    st.rerun()
                _, saved, err = _save_change("seen_jobs.json", dismiss_jobs({jkey}), [], "chore: dismiss 1 alert(s)")
                toast("Removed 1 alert(s)" if saved else f"Removed here, but not saved permanently. {err}",
                      "success" if saved else "error")
                st.rerun()


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
    """The hero's decorative opportunity cards (aria-hidden; abstract, no names
    or listings): a job card in front, one behind, a smaller one below and a
    fresher check tile."""
    bars = '<span class="tx"><i class="l1"></i><i class="l2"></i>{}</span>'
    badge = f'<span class="badge">{_ms("check")}<b></b></span>'
    return ('<div class="opps-wrap" aria-hidden="true"><div class="opps"><span class="glow-bg"></span>'
            f'<div class="oc b"><span class="ic blue">{_ms("apartment", "s20")}</span>{bars.format("")}</div>'
            f'<div class="oc a"><span class="ic">{_ms("work", "s24")}</span>{bars.format(badge)}</div>'
            f'<div class="oc c"><span class="ic teal">{_ms("school", "s20")}</span>{bars.format("")}</div>'
            f'<div class="oc d"><span class="ic">{_ms("task_alt", "s24")}</span><span class="spark-dot"></span></div>'
            '</div></div>')


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
            + _stat("Last scan", _ago(last_scan, NOW), f"Next scheduled {_ist(_next_slot())}", "update",
                    "t-amber" if scan_stale else "", small=True)
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
                    f"The tracker checks {_plural(len(companies), 'company', 'companies')} on a schedule of every 3 hours "
                    "(GitHub often starts it later). New fresher and "
                    "entry-level roles in India appear here — and in your inbox — as soon as a portal publishes them.",
                    _acts)
        return

    st.html('<div class="sec-head"><div><div class="eyebrow sparked">Latest opportunities</div><h2 class="section-title">Latest jobs</h2>'
            '<p class="section-sub">Newest fresher and entry-level roles found on the portals you track</p></div></div>')
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
    st.html(f"""<div class="how">
      <div><span class="ic">{_ms("travel_explore")}</span><div><b>Tracks career portals</b><p>Each company's job list is scanned on a schedule — no manual searching.</p></div></div>
      <div><span class="ic">{_ms("article")}</span><div><b>Reads every posting</b><p>A job is judged from its own page, never from its title alone.</p></div></div>
      <div><span class="ic">{_ms("mark_email_read")}</span><div><b>Emails each role once</b><p>Only India fresher and entry-level jobs that pass every check are sent.</p></div></div>
    </div>""")


def page_jobs():
    page_header("Jobs", f"{_plural(len(all_records), 'job')} found by the tracker · {len(active_jobs)} active · "
                        f"{len(dismissed_jobs)} dismissed")
    st.session_state.setdefault("jobs_tab", "active")
    with st.container(key="jobs_tab_wrap"):
        tab = st.pills("Show", ["active", "dismissed"], key="jobs_tab", label_visibility="collapsed",
                       format_func=lambda t: f"Active ({len(active_jobs)})" if t == "active" else f"Dismissed ({len(dismissed_jobs)})")
    tab = tab or "active"
    source = active_jobs if tab == "active" else dismissed_jobs
    if not source:
        empty_state("inbox", "No alerts yet" if tab == "active" else "Nothing dismissed",
                    "New jobs appear here after the next scan." if tab == "active"
                    else "Jobs you dismiss are kept here so they're never emailed twice — you can restore them anytime.")
        return
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
                if st.button("Previous", key="alerts_prev", icon=":material/chevron_left:", disabled=page == 0):
                    st.session_state.jobs_page = page - 1
                    st.rerun()
            with f3:
                if st.button("Next", key="alerts_next", icon=":material/chevron_right:", icon_position="right",
                             disabled=end >= total):
                    st.session_state.jobs_page = page + 1
                    st.rerun()


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
    exp_lines = list(dict.fromkeys(exp_lines))
    ev_exp = [str(x) for x in ev.get("experience") or [] if x] if isinstance(ev.get("experience"), list) else []
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
              {'<span class="pill neutral">Dismissed</span>' if j.get('dismissed') else ''}{"".join(chips)}</div>
          </div>
        </div>""")
        with st.container(key="job_actions"):
            a = st.columns(4)
            with a[0]:
                st.html(apply_link(j, "Apply on company site"))
            with a[1]:
                if j.get("dismissed"):
                    if st.button("Restore", key="detail_restore", icon=":material/undo:"):
                        if not owner_only("restore jobs"):
                            st.rerun()
                        _, saved, err = _save_change("seen_jobs.json", _restore_jobs({jkey}), [], "chore: restore 1 alert(s)")
                        toast("Job restored" if saved else f"Restored here, but not saved permanently. {err}",
                              "success" if saved else "error")
                        st.rerun()
                else:
                    with st.container(key="danger"):
                        dismiss = st.button("Dismiss", key="detail_dismiss", icon=":material/close:")
                    if dismiss:
                        if not owner_only("dismiss jobs"):
                            st.rerun()
                        _, saved, err = _save_change("seen_jobs.json", dismiss_jobs({jkey}), [], "chore: dismiss 1 alert(s)")
                        toast("Removed 1 alert(s)" if saved else f"Removed here, but not saved permanently. {err}",
                              "success" if saved else "error")
                        go(back, **back_kwargs)
                        st.rerun()
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
                    ("Found", f'<span class="num">{escape(j.get("date") or "—")} UTC</span>' + (f' <span class="muted">({_ago(d, NOW)})</span>' if d else "")),
                    ("Status", "Dismissed" if j.get("dismissed") else "Active")]
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
            with st.container(key=f"cr_{_widget_key('c', str(c.get('id')))}"):
                r1, r2 = st.columns([6, 1], vertical_alignment="center")
                with r1:
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
                with r2:
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

            _, saved, err = _save_change("companies.json", _add, [], f"chore: add {n}")
            toast(f"{n} added — it will be checked on the next scan" if saved
                  else f"{n} added, but not saved permanently. {err}", "success" if saved else "error")
            st.session_state.just_added = new["id"]
            st.session_state._clear_add_form = True
            go("companies")
        st.rerun()


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
      <div class="detail-chips"><span class="pill {k}"><i></i>{lbl}</span>
        {'<span class="tag">Core</span>' if c.get('locked') else ''}
        <span class="chip">{_ms("work")}<span>{company_active(c)} active · {sum(1 for j in jobs if j.get("dismissed"))} dismissed</span></span>
        <span class="chip num">{_ms("schedule")}Checked {_ago(_parse_iso(c.get("last_checked", "")), NOW)}</span></div></div></div>""")
    with hero, st.container(key="co_actions"):
        a = st.columns(4)
        with a[0]:
            if curl:
                st.html(f'<a class="btn primary" href="{escape(curl, quote=True)}" target="_blank" rel="noopener">Career portal {_ms("arrow_outward", "s16")}</a>')
        with a[1]:
            if site:
                st.html(f'<a class="btn ghost" href="{escape(site, quote=True)}" target="_blank" rel="noopener">Website {_ms("arrow_outward", "s16")}</a>')
        with a[2]:
            with st.container(key="danger"):
                if st.session_state.confirm_remove != c.get("id"):
                    if st.button("Stop tracking", key="btn_remove_company", icon=":material/delete:"):
                        if owner_only("remove companies"):
                            st.session_state.confirm_remove = c.get("id")
                        st.rerun()
    if st.session_state.confirm_remove == c.get("id"):
        with st.container(key="danger_confirm"):
            st.html(f'<p class="note" style="color:var(--text);">Stop tracking <b>{escape(name)}</b>? Jobs already found stay in your history.</p>')
            y, n_ = st.columns([1, 4])
            with y:
                if st.button("Yes, stop tracking", key="btn_remove", type="primary"):
                    if not owner_only("remove companies"):
                        st.session_state.confirm_remove = None
                        st.rerun()
                    cid = c.get("id")
                    _, saved, err = _save_change("companies.json", lambda latest: [x for x in latest if x.get("id") != cid],
                                                 [], f"chore: remove {name}")
                    toast(f"Removed {name}" if saved else f"Removed {name}, but not saved permanently. {err}",
                          "success" if saved else "error")
                    go("companies")
                    st.rerun()
            with n_:
                if st.button("Cancel", key="btn_cancel_remove"):
                    st.session_state.confirm_remove = None
                    st.rerun()

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
                f'<p class="section-sub">{_plural(len(jobs), "job")} found · {sum(1 for j in jobs if j.get("dismissed"))} dismissed</p></div></div>')
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
                    "history", "t-amber" if scan_stale else "t-teal", small=True)
            + _stat("Next scheduled scan", _ist(_next_slot()), "GitHub may start it later", "schedule", "", small=True)
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
        note = {"failing": f'{reason or "Last scan failed"} — <a class="link" href="{ACTIONS_URL}" target="_blank" rel="noopener">see log</a>',
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
    st.html(f'<div class="legend">{_ms("info", "s20")}<p class="note">Statuses come from the scraper: <b>Healthy</b> = scanned successfully in the last {_STALE_H} hours · '
            f'<b>Delayed</b> = no successful scan for {_STALE_H}+ hours · <b>Failing</b> = the last scan partly failed '
            f'(errors, rate limits or unreadable job pages) · <b>Broken</b> = the job list could not be read (blocked, HTTP error, '
            f'site changed) · <b>Needs configuration</b> = the URL shows no job postings · '
            f'<b>Pending</b> = not scanned yet. Full run logs: <a class="link" href="{ACTIONS_URL}" target="_blank" rel="noopener">GitHub Actions</a>.</p></div>')


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
        last_txt = (f'<span class="num">{"Sent" if last_is_sent else "Covered a job found"} {last_mail:%b %d, %H:%M} UTC</span> <span class="muted">({_ago(last_mail, NOW)})</span>'
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
        if recipient_source == "secret":
            note = (f'{_ms("lock", "s16")} Kept in a private secret, so it never appears in the public repository. '
                    f"To change it, {how}.")
        elif recipient_source == "settings.json":
            note = (f'<span style="color:var(--amber);">{_ms("warning", "s16")} Read from settings.json, which is public in the '
                    f"GitHub repository. To keep it private, {how}, then remove it from settings.json.</span>")
        else:
            note = f'{_ms("info", "s16")} No recipient is set. To turn alerts on, {how}.'
        if not is_owner() and recipient:
            note += " Only the owner sees the full address."
        st.html(f'<p class="note">{note}</p>')

    with st.container(key="test_panel"):
        ts = st.session_state.test_status
        status = ("Not sent this session" if ts is None else
                  f"Sent at {ts[1]} UTC" if ts[0] else f"Failed at {ts[1]} UTC")
        t1, t2 = st.columns([4, 1], vertical_alignment="center")
        with t1:
            st.html(f'<div><h2 class="section-title">{_ms("send")}Test delivery</h2><p class="section-sub">Send a sample alert to the recipient above to confirm emails arrive. '
                    f'<span class="num" style="color:var({"--green" if ts and ts[0] else "--red" if ts else "--muted"});">{status}</span></p></div>')
        with t2:
            test = st.button("Send test email", key="btn_test", icon=":material/send:", use_container_width=True)
    if test:
        stamp = datetime.now(timezone.utc).strftime("%H:%M")
        # owner only, and only ever to the configured recipient — never to an
        # address typed in here, so the system mailbox can't be used to send
        # mail to anyone else
        if not owner_only("send test emails"):
            pass
        elif not recipient:
            toast("No alert recipient is set — see Recipient above", "error")
        else:
            try:
                # re-checks the owner sign-in itself and sends only to the configured recipient
                send_test_email(st.session_state, settings)
            except OwnerRequired:
                owner_only("send test emails")
            except Exception as ex:
                log.warning("test mail failed: %s", type(ex).__name__)
                st.session_state.test_status = (False, stamp)
                toast("Email isn't configured for this app (Gmail address / app password missing)." if "GMAIL_" in str(ex)
                      else "Couldn't send the test email. Check the Gmail app password and try again.", "error")
            else:
                st.session_state.test_status = (True, stamp)
                toast(f"Test email sent — check {recipient}", "success")
        st.rerun()

    with st.container(key="set_behavior"):
        steps = [("travel_explore", "A check runs", "Scheduled every 3 hours (GitHub often starts runs later). Every tracked portal is scanned."),
                 ("fact_check", "Only qualifying jobs are kept", "Fresher and entry-level jobs in India whose own posting was read — never a guess from a title."),
                 ("outgoing_mail", "New jobs go out in one email", "Each job is emailed at most once. Dismissed jobs are never emailed, and stay in the history so they're never sent again."),
                 ("replay", "Failures retry safely", "If sending fails, the jobs stay unsent and the next check retries. A job is recorded before its email goes out, so a failed save can't cause a duplicate.")]
        st.html(f'<div><h2 class="section-title">{_ms("route")}How alerts work</h2></div><ol class="timeline">'
                + "".join(f'<li class="step"><span class="node">{_ms(i)}</span><div><b>{t}</b><p>{d}</p></div></li>' for i, t, d in steps)
                + '</ol><p class="note" style="font-size:13px;">Mail is sent through the Gmail account configured in the repository\'s GitHub Actions secrets — credentials are never shown here.</p>')


# ── Settings → Account (when sign-in is on) ─────────────────────────────────
def account_card() -> None:
    admin = is_owner()
    method = {"google.com": "Google", "password": "email &amp; password",
              "owner": "the owner password"}.get(ACCOUNT.get("provider"), "Firebase")
    with st.container(key="set_account"):
        st.html(f'<div class="set-head">{_avatar(account_label(ACCOUNT))}<div style="min-width:0;">'
                '<div class="eyebrow">Account</div>'
                f'<h2 class="section-title">{escape(account_label(ACCOUNT))}</h2>'
                f'<p class="section-sub">Signed in with {method}'
                + (f' · {escape(ACCOUNT["email"])}' if ACCOUNT.get("email") else "") + '</p></div></div>')
        link = st.session_state.get("_fb_link") or {}
        if ACCOUNT.get("provider") == "owner":
            fb = "— (owner password; sign in with Google to link your Firebase account)"
        elif ACCOUNT.get("uid"):
            fb = f'<span class="num">{escape(ACCOUNT["uid"])}</span>'
        elif link.get("state") == "failed":
            fb = (f'Not linked — Firebase refused the Google sign-in <span class="num">({escape(link.get("code") or "")})</span>'
                  + (f'<span class="kv-note">{escape(_GOOGLE_ERROR_HINTS[link["code"]])}</span>'
                     if link.get("code") in _GOOGLE_ERROR_HINTS else ""))
        else:
            fb = "Linked at your next Google sign-in"
        if admin:
            role = "Admin — can manage companies, jobs, checks and email"
        elif admins_configured():
            role = "Member — read-only"
        else:
            role = "Member — read-only (admins are set with the JT_ADMIN_EMAILS secret)"
        rows = [("Role", role), ("Email verified", "Yes" if ACCOUNT.get("email_verified") else "No"), ("Firebase account", fb)]
        st.html('<dl class="kv">' + "".join(f"<dt>{a}</dt><dd>{b}</dd>" for a, b in rows) + "</dl>")
        # Until JT_ADMIN_EMAILS exists the legacy owner password still unlocks
        # changes — tucked away here, never a second login screen.
        if not admins_configured() and owner_configured():
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
        a1, _a2 = st.columns([1, 3])
        with a1:
            if st.button("Sign out", key="btn_account_sign_out_settings", icon=":material/logout:",
                         use_container_width=True):
                if not account_sign_out():
                    st.rerun()
                st.stop()


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


def page_settings():
    page_header("Settings", "Preferences and read-only details of how the tracker is configured")
    with st.container(key="set_appearance"):
        a1, a2 = st.columns([4, 1], vertical_alignment="center")
        with a1:
            st.html(f'<div class="set-head"><span class="ic">{_ms("dark_mode" if st.session_state.dark_mode else "light_mode")}</span><div>'
                    '<div class="eyebrow">General</div><h2 class="section-title">Appearance</h2><p class="section-sub">'
                    f'{"Dark" if st.session_state.dark_mode else "Light"} theme · kept in this page’s address, so it survives reloads and bookmarks</p></div></div>')
        with a2:
            if st.button("Use light theme" if st.session_state.dark_mode else "Use dark theme", key="btn_theme",
                         icon=":material/light_mode:" if st.session_state.dark_mode else ":material/dark_mode:",
                         use_container_width=True):
                st.session_state.dark_mode = not st.session_state.dark_mode
                st.rerun()
    if AUTH_GATE:
        account_card()
    elif _AUTH_PRESENT:
        with st.container(key="set_firebase"):
            st.html(f'<div class="set-head"><span class="ic">{_ms("key_off")}</span><div><div class="eyebrow">Sign-in</div>'
                    '<h2 class="section-title">Sign-in isn’t available yet</h2>'
                    f'<p class="section-sub">{escape(_AUTH_PROBLEM)}</p></div></div>')
    if not AUTH_GATE:
        owner_card()
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
            if st.button("Reload data", key="btn_reload", icon=":material/refresh:", use_container_width=True):
                _refresh_data()
                toast("Showing the latest data", "success")
                st.rerun()
    with st.container(key="set_engine"):
        by_source: dict[str, list[str]] = {}
        for c in companies:
            by_source.setdefault(_source_label(c), []).append((c.get("name") or "").strip() or "Unnamed")
        rows = [("Schedule", f'{_cadence()} · Next scheduled <span class="num">{_ist(_next_slot())}</span>'
                             '<span class="kv-note">GitHub may start scheduled scans later than the scheduled time.</span>'),
                ("Roles kept", "Fresher and entry-level roles located in India"),
                ("Detail pages", "Every candidate job's own posting is read before it can be classified and emailed; "
                                 "if it can't be read, it is retried on the next scan")]
        st.html(f'<div class="set-head"><span class="ic">{_ms("manage_search")}</span><div><div class="eyebrow">Monitoring</div>'
                '<h2 class="section-title">Scanning</h2><p class="section-sub">How and when the tracker looks for new roles.</p></div></div>'
                '<dl class="kv">' + "".join(f"<dt>{a}</dt><dd>{b}</dd>" for a, b in rows) + "</dl>")
        # implementation facts, for debugging only: collapsed by default
        with st.expander("Technical details", icon=":material/code:"):
            tech = [("Scrapers", "; ".join(f"{escape(src)} for {escape(', '.join(sorted(names)))}"
                                           for src, names in sorted(by_source.items())) or "No portals yet"),
                    ("Schedule (UTC)", f'cron <span class="num">{escape(_schedule_cron())}</span> in .github/workflows/check_jobs.yml')]
            st.html('<dl class="kv tech">' + "".join(f"<dt>{a}</dt><dd>{b}</dd>" for a, b in tech) + "</dl>")
    with st.container(key="set_maint"):
        st.html(f'<div class="set-head"><span class="ic red">{_ms("warning")}</span><div><div class="eyebrow">Danger zone</div>'
                '<h2 class="section-title">Dismiss all jobs</h2><p class="section-sub">Dismiss every active job at once. '
                'They stay in the history (Jobs → Dismissed) and are never emailed again.</p></div></div>')
        if not st.session_state.confirm_clear:
            m1, _m2 = st.columns([1, 3])
            with m1:
                with st.container(key="danger"):
                    if st.button(f"Dismiss all {len(active_jobs)} jobs", key="btn_clear_all", disabled=not active_jobs,
                                 use_container_width=True):
                        if owner_only("dismiss jobs"):
                            st.session_state.confirm_clear = True
                        st.rerun()
        else:
            unsent = sum(1 for j in active_jobs if alert_pending(j))
            with st.container(key="danger_confirm"):
                st.html(f'<p class="note" style="color:var(--text);">Dismiss all <b>{len(active_jobs)}</b> active jobs?'
                        + (f" This also cancels <b>{unsent}</b> alert{'s' if unsent != 1 else ''} that haven't been emailed yet "
                           "— they won't be emailed." if unsent else " They stay in the history and are never emailed again.")
                        + "</p>")
                y, n_ = st.columns([1, 3])
                with y:
                    if st.button("Yes, dismiss all", key="btn_clear_all_confirm", type="primary", use_container_width=True):
                        st.session_state.confirm_clear = False
                        if not owner_only("dismiss jobs"):
                            st.rerun()
                        _, saved, err = _save_change("seen_jobs.json", dismiss_jobs(None), [], "chore: dismiss all alerts")
                        toast("All alerts cleared" if saved else f"Cleared here, but not saved permanently. {err}",
                              "success" if saved else "error")
                        st.rerun()
                with n_:
                    if st.button("Cancel", key="btn_clear_all_cancel"):
                        st.session_state.confirm_clear = False
                        st.rerun()
    st.html('<div class="app-foot"><span>Fresher Job Tracker</span><span>Fresher and entry-level roles in India</span></div>')


# ═══════════════════════════════════════════════════════════════════════════════
# ROUTER
# ═══════════════════════════════════════════════════════════════════════════════
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
elif page == "companies" and st.session_state.company_id:
    comp = next((c for c in companies if str(c.get("id")) == str(st.session_state.company_id)), None)
    if comp:
        page_company_detail(comp)
    else:
        st.session_state.company_id = None
        page_companies()
else:
    {"home": page_home, "jobs": page_jobs, "companies": page_companies, "monitoring": page_monitoring,
     "email": page_email, "settings": page_settings}[page]()
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
if dict(st.query_params) != want:
    st.query_params.from_dict(want)
st.session_state._url_written = want   # a later mismatch means browser Back/Forward

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
