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
    dismiss_jobs,
    fetch_remote_json,
    friendly_github_error,
    job_key,
    update_json,
    visible_jobs,
)
from notifier import category_label, safe_url

log = logging.getLogger("streamlit_app")

st.set_page_config(
    page_title="Fresher Job Tracker",
    page_icon="💼",
    layout="wide",
    initial_sidebar_state="collapsed",
)

if "dark_mode" not in st.session_state:
    st.session_state.dark_mode = False

# ── theme palettes ────────────────────────────────────────────────────────────
LIGHT = {
    "bg": "#f6f6f7", "card": "#ffffff", "card2": "#fafafa",
    "border": "#eaeaec", "border_strong": "#dcdce0",
    "text": "#17171a", "muted": "#6c6c76", "faint": "#9a9aa4",
    "accent": "#4f46e5", "accent_soft": "rgba(79,70,229,0.10)",
    "good": "#22c55e", "good_soft": "rgba(34,197,94,0.12)",
    "bad": "#ef4444", "bad_soft": "rgba(239,68,68,0.12)",
    "shadow": "0 1px 2px rgba(20,20,30,.05), 0 2px 8px rgba(20,20,30,.04)",
    "dot": "rgba(20,20,30,.045)",
    "header_bg": "rgba(246,246,247,0.88)",
}
DARK = {
    "bg": "#101012", "card": "#18181b", "card2": "#1e1e22",
    "border": "#2b2b30", "border_strong": "#38383e",
    "text": "#f3f3f5", "muted": "#a3a3ad", "faint": "#77777f",
    "accent": "#6d64f0", "accent_soft": "rgba(109,100,240,0.18)",
    "good": "#34d399", "good_soft": "rgba(52,211,153,0.16)",
    "bad": "#f87171", "bad_soft": "rgba(248,113,113,0.16)",
    "shadow": "0 1px 2px rgba(0,0,0,.4), 0 2px 10px rgba(0,0,0,.35)",
    "dot": "rgba(255,255,255,.045)",
    "header_bg": "rgba(16,16,18,0.88)",
}
TH = DARK if st.session_state.dark_mode else LIGHT

# ── CSS injection via st.html() — works in Streamlit 1.36+ ───────────────────
# NOTE: inline <svg> icons rendered through st.html() silently fail to paint in
# this app's hosting environment (confirmed empirically — the DOM node exists
# but nothing draws), while plain Unicode/emoji glyphs always render. Every
# icon in this file is therefore plain text/emoji, not svg, by design.
_root_vars = f"""
:root {{
  --bg: {TH['bg']};
  --card: {TH['card']};
  --card-2: {TH['card2']};
  --border: {TH['border']};
  --border-strong: {TH['border_strong']};
  --text: {TH['text']};
  --muted: {TH['muted']};
  --faint: {TH['faint']};
  --accent: {TH['accent']};
  --accent-soft: {TH['accent_soft']};
  --good: {TH['good']};
  --good-soft: {TH['good_soft']};
  --bad: {TH['bad']};
  --bad-soft: {TH['bad_soft']};
  --shadow: {TH['shadow']};
  --dot: {TH['dot']};
  --header-bg: {TH['header_bg']};
}}
"""

st.html("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600;700&family=Geist+Mono:wght@400;500&display=swap');

* { box-sizing: border-box; margin: 0; padding: 0; }

""" + _root_vars + """

/* ── hide Streamlit chrome ── */
#MainMenu, footer, header[data-testid="stHeader"] { visibility: hidden; height: 0; }
.stDeployButton, [data-testid="stToolbar"] { display: none !important; }
section[data-testid="stSidebar"] { display: none !important; }
.block-container { padding: 0 !important; max-width: 100% !important; }
.stApp {
  background-color: var(--bg) !important;
  background-image:
    radial-gradient(900px 400px at 50% -120px, var(--accent-soft), transparent 68%),
    radial-gradient(var(--dot) 1px, transparent 1.4px);
  background-size: auto, 24px 24px;
  background-position: center top, center top;
  font-family: 'Geist', ui-sans-serif, system-ui, sans-serif !important;
  -webkit-font-smoothing: antialiased;
  color: var(--text) !important;
}
.stApp * { font-family: 'Geist', ui-sans-serif, system-ui, sans-serif !important; }
/* keep Streamlit's built-in Material icon glyphs (expander arrow, etc.) working */
[data-testid="stIconMaterial"] { font-family: 'Material Symbols Rounded' !important; }

/* ── remove element padding/gap ── */
[data-testid="stVerticalBlockBorderWrapper"],
[data-testid="stVerticalBlock"] { gap: 0 !important; }
.element-container { margin: 0 !important; padding: 0 !important; }

/* ── buttons ── */
[data-testid^="stBaseButton"] {
  font-family: 'Geist', sans-serif !important;
  border-radius: 9px !important;
  border: 1px solid var(--border-strong) !important;
  background: var(--card-2) !important;
  color: var(--text) !important;
  font-size: 13px !important;
  font-weight: 550 !important;
  box-shadow: var(--shadow) !important;
  transition: border-color .15s, color .15s !important;
  padding: 8px 14px !important;
  height: auto !important;
  line-height: 1.4 !important;
}
[data-testid^="stBaseButton"]:hover {
  border-color: var(--accent) !important;
  color: var(--accent) !important;
  background: var(--card-2) !important;
}
[data-testid^="stBaseButton"]:disabled {
  opacity: 0.45 !important;
}
/* ── accent button (Send Test Mail) ── */
.st-key-test_mail_btn [data-testid^="stBaseButton"] {
  background: var(--accent) !important;
  color: #fff !important;
  border: none !important;
  border-radius: 11px !important;
  font-size: 14px !important;
  font-weight: 640 !important;
  padding: 13px !important;
  box-shadow: 0 6px 18px rgba(79,70,229,0.25), var(--shadow) !important;
  width: 100% !important;
}
.st-key-test_mail_btn [data-testid^="stBaseButton"]:hover {
  background: #4338ca !important;
  color: #fff !important;
}

/* ── text inputs ── */
.stTextInput > div > div > input {
  font-family: 'Geist Mono', monospace !important;
  font-size: 13px !important;
  border: 1px solid var(--border-strong) !important;
  border-radius: 9px !important;
  background: var(--card-2) !important;
  color: var(--text) !important;
  padding: 10px 12px !important;
}
.stTextInput > div > div > input:focus {
  border-color: var(--accent) !important;
  box-shadow: 0 0 0 3px rgba(79,70,229,0.12) !important;
}
.stTextInput input::placeholder {
  color: var(--faint) !important;
  opacity: 0.7 !important;
}
.stTextInput label {
  font-size: 12px !important;
  font-weight: 600 !important;
  color: var(--muted) !important;
  letter-spacing: 0 !important;
  margin-bottom: 6px !important;
}

/* ── selectbox ── */
.stSelectbox > div > div {
  border: 1px solid var(--border-strong) !important;
  border-radius: 9px !important;
  background: var(--card-2) !important;
  color: var(--text) !important;
}

/* ── expander ── */
.streamlit-expanderHeader {
  background: var(--card-2) !important;
  border: 1px solid var(--border-strong) !important;
  border-radius: 9px !important;
  font-size: 13px !important;
  font-weight: 600 !important;
  color: var(--text) !important;
  padding: 10px 14px !important;
}
.streamlit-expanderContent {
  border: 1px solid var(--border) !important;
  border-top: none !important;
  border-radius: 0 0 9px 9px !important;
  background: var(--card) !important;
  padding: 16px !important;
}

/* ── columns gap ── */
[data-testid="stHorizontalBlock"] { gap: 24px !important; }

/* ── page width wrapper ── */
.st-key-page_wrap { max-width: 1120px; margin: 0 auto; padding: 24px 24px 40px; }

/* ── sticky app header ── */
.st-key-app_header {
  position: sticky; top: 0; z-index: 20;
  background: var(--header-bg);
  backdrop-filter: blur(14px);
  border-bottom: 1px solid var(--border);
  padding: 10px 24px !important;
}
.st-key-app_header [data-testid="stHorizontalBlock"] { align-items: center !important; gap: 8px !important; }
.st-key-app_header [data-testid^="stBaseButton"] {
  width: 38px !important; height: 38px !important; min-height: 38px !important; padding: 0 !important;
  display: flex !important; align-items: center !important; justify-content: center !important;
  font-size: 16px !important; line-height: 1 !important; border-radius: 10px !important;
  border: 1px solid var(--border-strong) !important; background: var(--card) !important;
  box-shadow: none !important;
}
.st-key-app_header [data-testid^="stBaseButton"]:hover {
  border-color: var(--accent) !important; color: var(--accent) !important; background: var(--card) !important;
}
/* keep the whole right-hand cluster on one tidy row */
.st-key-app_header .element-container { display: flex; justify-content: flex-end; }
/* phones: Streamlit stacks every column full-width, which put each icon
   button on its own row and pushed the status pills off the left edge */
@media (max-width: 640px) {
  .st-key-app_header [data-testid="stHorizontalBlock"] { flex-wrap: wrap !important; }
  .st-key-app_header [data-testid="stColumn"] { flex: 0 0 auto !important; width: auto !important; min-width: 0 !important; }
  .st-key-app_header [data-testid="stColumn"]:nth-child(-n+2) { flex: 1 1 100% !important; width: 100% !important; }
  .st-key-app_header .element-container { justify-content: flex-start; }
  .hdr-pills { justify-content: flex-start !important; flex-wrap: wrap; }
  .st-key-page_wrap { padding: 16px 12px 32px; }
  /* one line per alert: checkbox · details · Apply */
  [class*="st-key-alert_row_"] [data-testid="stHorizontalBlock"] { flex-wrap: nowrap !important; gap: 6px !important; }
  [class*="st-key-alert_row_"] [data-testid="stColumn"] { flex: 0 0 auto !important; width: auto !important; min-width: 0 !important; }
  [class*="st-key-alert_row_"] [data-testid="stColumn"]:nth-child(2) { flex: 1 1 auto !important; }
  [class*="st-key-alert_row_"] .stCheckbox { padding-left: 12px !important; }
  .alert-icon { display: none !important; }
}

/* ── Tracked Companies card ── */
.st-key-tc_card {
  background: var(--card) !important;
  border: 1px solid var(--border) !important;
  border-radius: 16px !important;
  box-shadow: var(--shadow) !important;
  overflow: hidden !important;
  margin-bottom: 16px !important;
}
.st-key-tc_card [data-testid="stVerticalBlock"] { gap: 0 !important; }
.st-key-tc_header_row { padding: 14px 22px !important; border-bottom: 1px solid var(--border) !important; }
.st-key-tc_add_row { padding: 14px 22px !important; background: var(--card-2) !important; border-bottom: 1px solid var(--border) !important; }
.st-key-tc_remove_row { padding: 12px 22px !important; border-top: 1px solid var(--border) !important; }
.st-key-tc_header_row h2 { margin: 0 !important; }
.st-key-tc_header_row p { margin: 3px 0 0 !important; }

/* ── Recent Alerts card ── */
.st-key-alerts_card {
  background: var(--card) !important;
  border: 1px solid var(--border) !important;
  border-radius: 16px !important;
  box-shadow: var(--shadow) !important;
  overflow: hidden !important;
}
.st-key-alerts_card [data-testid="stVerticalBlock"] { gap: 0 !important; }
.st-key-alerts_header_row { padding: 16px 20px !important; border-bottom: 1px solid var(--border) !important; }
.st-key-alerts_header_row h2 { margin: 0 !important; }
.st-key-alerts_header_row p { margin: 3px 0 0 !important; }
.st-key-alerts_footer_row { padding: 12px 20px !important; border-top: 1px solid var(--border) !important; background: var(--card-2) !important; }
.st-key-alerts_footer_row [data-testid="stHorizontalBlock"] { gap: 8px !important; }
.st-key-alerts_page_label { text-align: center !important; font-size: 12px !important; color: var(--muted) !important; font-family: 'Geist Mono', monospace !important; padding-top: 8px !important; }
.st-key-alerts_card .stCheckbox { padding-left: 20px !important; padding-top: 2px !important; }

/* ── Notification Settings card ── */
.st-key-ns_card {
  background: var(--card) !important;
  border: 1px solid var(--border) !important;
  border-radius: 16px !important;
  box-shadow: var(--shadow) !important;
  padding: 22px !important;
}
.st-key-ns_card [data-testid="stVerticalBlock"] { gap: 14px !important; }
.st-key-ns_card [data-testid="stHorizontalBlock"] { gap: 10px !important; }
.st-key-tc_header_row [data-testid="stHorizontalBlock"] { gap: 10px !important; }
.st-key-tc_remove_row [data-testid="stHorizontalBlock"] { gap: 10px !important; }
.st-key-tc_remove_row .stCheckbox { padding: 4px 0 !important; }

/* ── header pills / icon buttons — all exactly 38px tall so the row reads as one unit ── */
.hdr-pill {
  display:inline-flex;align-items:center;gap:7px;height:38px;padding:0 14px;border:1px solid var(--border-strong);
  border-radius:10px;background:var(--card);font-size:12.5px;font-family:'Geist',sans-serif;
  cursor:default;color:var(--text);white-space:nowrap;
}
.hdr-pill .muted { color: var(--muted); }
.hdr-pill b { font-family:'Geist Mono',monospace; font-weight:600; }
.hdr-btn {
  height:38px;width:38px;display:inline-flex;align-items:center;justify-content:center;
  border:1px solid var(--border-strong);background:var(--card);color:var(--text);border-radius:10px;
  text-decoration:none;font-size:12px;font-weight:700;letter-spacing:0.02em;font-family:'Geist',sans-serif;
  cursor:pointer;transition:border-color .15s,color .15s;flex-shrink:0;
}
.hdr-btn:hover { border-color: var(--accent); color: var(--accent); }

/* ── emoji/symbol icons ──────────────────────────────────────────────────
   The blanket ".stApp * { font-family:'Geist' }" rule above can suppress a
   browser's normal fallback to a color-emoji/symbol font for glyphs Geist
   doesn't cover, making icons render blank in some browsers even though the
   character is present in the DOM. Every icon glyph gets this explicit
   fallback stack so it always paints regardless of that override. */
.icon-emoji,
.st-key-btn_refresh [data-testid^="stBaseButton"] p,
.st-key-btn_run_check [data-testid^="stBaseButton"] p,
.st-key-btn_theme [data-testid^="stBaseButton"] p {
  font-family: "Segoe UI Symbol", "Segoe UI Emoji", "Noto Color Emoji", "Noto Sans Symbols", "Apple Color Emoji", sans-serif !important;
}
</style>
""")

# ── helpers ──────────────────────────────────────────────────────────────────
BASE = Path(__file__).parent
GITHUB_REPO = "manojanumolu/job-tracker"


def _load(path: Path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return default


def _save(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")


def _host(url: str) -> str:
    try:
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return url


def _rel_time(iso: str) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso)
        diff = int((datetime.now(timezone.utc) - dt).total_seconds())
        if diff < 60:
            return "just now"
        if diff < 3600:
            return f"{diff // 60} min ago"
        if diff < 86400:
            return f"{diff // 3600}h ago"
        return f"{diff // 86400}d ago"
    except Exception:
        return "—"


def _next_check_str() -> str:
    """check_jobs.yml runs at minute 0 of every 3rd UTC hour (0, 3, 6, …)."""
    now = datetime.now(timezone.utc)
    nxt = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=3 - now.hour % 3)
    mins = int((nxt - now).total_seconds() // 60)
    return f"in {mins} min" if mins < 60 else f"in {mins // 60}h {mins % 60:02d}m"


def _trigger_scrape() -> tuple[bool, str]:
    """Ask GitHub Actions to run check_jobs.yml right now (workflow_dispatch)."""
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        return False, "Can't start a check — no GitHub token is configured for this app."
    try:
        from github import Github
        repo = Github(token).get_repo(GITHUB_REPO)
        workflow = repo.get_workflow("check_jobs.yml")
        if workflow.create_dispatch(ref="main"):
            return True, "Check started — it takes a few minutes. Use Reload afterwards to see new jobs."
        return False, "GitHub declined to start a new check."
    except Exception as e:
        log.warning("workflow dispatch failed: %s", e)
        if getattr(e, "status", None) == 403:
            return False, "The app's GitHub token isn't allowed to start checks (needs Actions: write)."
        return False, f"Couldn't start a check. {friendly_github_error(e)}"


# Latest data straight from GitHub, shared by all sessions and refreshed at
# most every 5 minutes (or on Reload) — the local checkout can be hours old.
@st.cache_data(ttl=300, show_spinner=False)
def _remote_snapshot() -> dict:
    return fetch_remote_json(["companies.json", "settings.json", "seen_jobs.json"])


def _load_synced(name: str, default):
    """Prefer the GitHub copy (read-only here — saves go through
    _save_change); fall back to the local checkout."""
    remote = _remote_snapshot().get(name)
    if remote is not None and isinstance(remote, type(default)):
        return remote
    return _load(BASE / name, default)


def _save_change(name: str, mutate, default, message: str):
    """Apply one change to the latest copy of ``name`` (merge-safe)."""
    data, saved, err = update_json(BASE / name, name, mutate, default, message)
    _remote_snapshot.clear()
    return data, saved, err


# ── load data ─────────────────────────────────────────────────────────────────
companies: list[dict] = [c for c in _load_synced("companies.json", []) if isinstance(c, dict)]
settings: dict = _load_synced("settings.json", {"recipient_email": ""})
# seen_jobs.json is also the scraper's dedup history: alerts are dismissed
# (hidden), never deleted, or the scraper would email them again
seen_jobs: list[dict] = visible_jobs(_load_synced("seen_jobs.json", []))  # newest-first

ALERTS_PAGE_SIZE = 10
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+$")
_CHECK_COOLDOWN_S = 300


def _widget_key(prefix: str, key: str) -> str:
    return f"{prefix}_{hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]}"


# ── session state ──────────────────────────────────────────────────────────────
if "email_val" not in st.session_state:
    st.session_state.email_val = settings.get("recipient_email", "")
if "toast" not in st.session_state:
    st.session_state.toast = None
if "toast_kind" not in st.session_state:
    st.session_state.toast_kind = "success"
if "alerts_page" not in st.session_state:
    st.session_state.alerts_page = 0


def toast(msg: str, kind: str = "success"):
    st.session_state.toast = msg
    st.session_state.toast_kind = kind


# ═══════════════════════════════════════════════════════════════════════════════
# HEADER
# ═══════════════════════════════════════════════════════════════════════════════
last_checked_times = [c.get("last_checked", "") for c in companies if c.get("last_checked")]
last_checked_str = _rel_time(max(last_checked_times)) if last_checked_times else "never"
active_count = sum(1 for c in companies if c.get("status") not in ("broken",))

with st.container(key="app_header"):
    lc, pc, bc0, bc1, bc2, bc3 = st.columns([5.2, 4.4, 0.42, 0.42, 0.42, 0.42], vertical_alignment="center")
    with lc:
        st.html("""
        <div style="display:flex;align-items:center;gap:10px;margin-right:auto;">
          <div class="icon-emoji" style="width:38px;height:38px;border-radius:10px;background:var(--accent);display:flex;align-items:center;justify-content:center;font-size:18px;line-height:1;flex-shrink:0;">💼</div>
          <div style="line-height:1.2;">
            <div style="font-size:15.5px;font-weight:700;letter-spacing:-0.02em;color:var(--text);font-family:'Geist',sans-serif;">Fresher Job Tracker</div>
            <div style="font-size:12px;color:var(--muted);font-family:'Geist',sans-serif;">Entry-level posting monitor</div>
          </div>
        </div>
        """)
    with pc:
        st.html(f"""
        <div class="hdr-pills" style="display:flex;align-items:center;justify-content:flex-end;gap:8px;">
          <div class="hdr-pill" title="Time since the scraper last ran and refreshed this data">
            <span style="color:var(--good);font-weight:700;">✓</span>
            <span class="muted">Last checked</span>
            <b>{last_checked_str}</b>
          </div>
          <div class="hdr-pill" title="The scraper runs automatically every 3 hours">
            <span class="icon-emoji">🕐</span> <span class="muted">Next check</span> <b>{_next_check_str()}</b>
          </div>
        </div>
        """)
    with bc0:
        if st.button("⟳", key="btn_refresh", help="Reload the latest jobs and company status"):
            _remote_snapshot.clear()
            toast("Showing the latest data", "success")
            st.rerun()
    with bc1:
        if st.button("▷", key="btn_run_check", help="Run a fresh check now (takes a few minutes)"):
            since = time.time() - st.session_state.get("last_check_trigger", 0)
            if since < _CHECK_COOLDOWN_S:
                toast(f"A check was just started — try again in {int((_CHECK_COOLDOWN_S - since) // 60) + 1} min", "error")
            else:
                ok, msg = _trigger_scrape()
                if ok:
                    st.session_state.last_check_trigger = time.time()
                toast(msg, "success" if ok else "error")
            st.rerun()
    with bc2:
        theme_icon = "☀" if st.session_state.dark_mode else "☾"
        if st.button(theme_icon, key="btn_theme", help="Toggle light / dark mode"):
            st.session_state.dark_mode = not st.session_state.dark_mode
            st.rerun()
    with bc3:
        st.html(f"""
        <a class="hdr-btn" href="https://github.com/{GITHUB_REPO}" target="_blank" rel="noopener" title="View the source repository on GitHub">GH</a>
        """)

# ═══════════════════════════════════════════════════════════════════════════════
# PAGE BODY
# ═══════════════════════════════════════════════════════════════════════════════
with st.container(key="page_wrap"):

    # ── TRACKED COMPANIES TABLE ──────────────────────────────────────────────
    rows_html = ""
    for c in companies:
        status = c.get("status", "unknown")
        is_broken = status == "broken"
        is_active = status == "active"
        status_label = "Broken" if is_broken else ("Active" if is_active else "Pending")
        status_color = "var(--bad)" if is_broken else ("var(--good)" if is_active else "var(--faint)")
        status_bg = "var(--bad-soft)" if is_broken else ("var(--good-soft)" if is_active else "var(--accent-soft)")
        name = escape((c.get("name") or "").strip() or "Unnamed")
        initial = name[0].upper() if name[0].isalnum() else "?"
        page_url = safe_url(c.get("url", ""))
        host = escape(_host(page_url) or "invalid URL")
        last_job = escape(c.get("last_job", "") or "—")
        last_checked = _rel_time(c.get("last_checked", ""))
        core_badge = '<span style="font-size:10px;color:var(--faint);border:1px solid var(--border);padding:1px 6px;border-radius:5px;letter-spacing:0.03em;margin-left:6px;">CORE</span>' if c.get("locked") else ""
        rows_html += f"""
<tr style="border-top:1px solid var(--border);">
  <td style="padding:14px 22px;">
    <div style="display:flex;align-items:center;gap:10px;">
      <span style="width:26px;height:26px;border-radius:7px;background:var(--accent-soft);color:var(--accent);display:grid;place-items:center;font-size:12px;font-weight:700;flex-shrink:0;">{initial}</span>
      <span style="font-weight:600;font-size:13.5px;color:var(--text);">{name}</span>{core_badge}
    </div>
  </td>
  <td style="padding:14px 16px;">
    <a href="{escape(page_url, quote=True) or '#'}" target="_blank" rel="noopener"
       style="display:inline-flex;align-items:center;gap:5px;color:var(--muted);text-decoration:none;font-family:'Geist Mono',monospace;font-size:12px;">
      {host} ↗
    </a>
  </td>
  <td style="padding:14px 16px;">
    <span style="display:inline-flex;align-items:center;gap:6px;padding:3px 9px 3px 8px;border-radius:999px;font-size:12px;font-weight:600;background:{status_bg};color:{status_color};">
      <span style="width:7px;height:7px;border-radius:50%;background:{status_color};flex-shrink:0;"></span>
      {status_label}
    </span>
  </td>
  <td style="padding:14px 16px;color:var(--text);font-size:13.5px;min-width:220px;max-width:320px;overflow-wrap:break-word;">{last_job}</td>
  <td style="padding:14px 16px;color:var(--muted);font-family:'Geist Mono',monospace;font-size:12px;">{last_checked}</td>
  <td style="padding:14px 22px;"></td>
</tr>"""

    if "show_add_form" not in st.session_state:
        st.session_state.show_add_form = False
    if "show_remove_form" not in st.session_state:
        st.session_state.show_remove_form = False

    with st.container(key="tc_card"):
        with st.container(key="tc_header_row"):
            hcol1, hcol2, hcol3 = st.columns([3.8, 1.15, 1.35], vertical_alignment="center")
            with hcol1:
                st.html(f"""
                <h2 style="font-size:16px;font-weight:700;letter-spacing:-0.02em;color:var(--text);font-family:'Geist',sans-serif;">Tracked Companies</h2>
                <p style="margin-top:3px;font-size:12.5px;color:var(--muted);font-family:'Geist',sans-serif;">{active_count} of {len(companies)} career pages responding &middot; checked every 3 hours</p>
                """)
            with hcol2:
                if st.button("➕  Add Company", key="btn_toggle_add", use_container_width=True,
                             help="Track a new company's career page"):
                    st.session_state.show_add_form = not st.session_state.show_add_form
                    st.session_state.show_remove_form = False
            with hcol3:
                if st.button("➖  Remove Company", key="btn_toggle_remove", use_container_width=True,
                             help="Pick companies to stop tracking"):
                    st.session_state.show_remove_form = not st.session_state.show_remove_form
                    st.session_state.show_add_form = False

        if st.session_state.show_add_form:
            with st.container(key="tc_add_row"):
                fc1, fc2, fc3 = st.columns([2, 3, 2])
                with fc1:
                    new_name = st.text_input("Company name", placeholder="e.g. Infosys", key="new_name")
                with fc2:
                    new_url = st.text_input("Career page URL", placeholder="https://careers.infosys.com", key="new_url")
                with fc3:
                    st.markdown("<br>", unsafe_allow_html=True)
                    fc3a, fc3b = st.columns(2)
                    with fc3a:
                        cancel_clicked = st.button("Cancel", key="btn_cancel_add", use_container_width=True)
                    with fc3b:
                        add_clicked = st.button("Add", key="btn_add", use_container_width=True)
            if cancel_clicked:
                st.session_state.show_add_form = False
                st.rerun()
            if add_clicked:
                name = new_name.strip()
                url = new_url.strip()
                if not name:
                    toast("Company name is required", "error")
                elif not safe_url(url):
                    toast("Enter the full career page URL, starting with https://", "error")
                elif any(c.get("name", "").lower() == name.lower() for c in companies):
                    toast(f"{name} is already tracked", "error")
                else:
                    new_company = {
                        "id": f"c{int(time.time())}",
                        "name": name, "url": url, "locked": False,
                        "status": "unknown", "last_job": "", "last_checked": "",
                    }

                    def _add(latest: list) -> list:
                        if not any(c.get("name", "").lower() == name.lower() for c in latest):
                            latest.append(new_company)
                        return latest

                    _, saved, err = _save_change("companies.json", _add, [], f"chore: add {name}")
                    if saved:
                        toast(f"{name} added", "success")
                    else:
                        toast(f"{name} added, but not saved permanently. {err}", "error")
                    st.session_state.show_add_form = False
                    st.rerun()

        st.html(f"""
      <div style="overflow-x:auto;">
        <table style="width:100%;border-collapse:collapse;min-width:700px;font-size:13.5px;font-family:'Geist',sans-serif;">
          <thead>
            <tr style="text-align:left;">
              <th style="padding:11px 22px;font-size:11px;font-weight:700;color:var(--faint);text-transform:uppercase;letter-spacing:0.06em;">Company</th>
              <th style="padding:11px 16px;font-size:11px;font-weight:700;color:var(--faint);text-transform:uppercase;letter-spacing:0.06em;">Career Page</th>
              <th style="padding:11px 16px;font-size:11px;font-weight:700;color:var(--faint);text-transform:uppercase;letter-spacing:0.06em;">Status</th>
              <th style="padding:11px 16px;font-size:11px;font-weight:700;color:var(--faint);text-transform:uppercase;letter-spacing:0.06em;">Last Job Found</th>
              <th style="padding:11px 16px;font-size:11px;font-weight:700;color:var(--faint);text-transform:uppercase;letter-spacing:0.06em;">Last Checked</th>
              <th style="padding:11px 22px;"></th>
            </tr>
          </thead>
          <tbody>{rows_html}</tbody>
        </table>
      </div>
    """)

        # ── Remove company — checkbox picker, any tracked company ────────────────
        if st.session_state.show_remove_form and companies:
            with st.container(key="tc_remove_row"):
                st.html('<p style="font-size:12px;font-weight:600;color:var(--muted);margin-bottom:8px;font-family:\'Geist\',sans-serif;">Tick the companies to stop tracking</p>')
                grid = st.columns(3)
                for i, c in enumerate(companies):
                    with grid[i % 3]:
                        st.checkbox(c.get("name") or "Unnamed", key=f"rm_{c.get('id')}")
                to_remove = [c for c in companies if st.session_state.get(f"rm_{c.get('id')}")]
                rb1, rb2, _sp = st.columns([1, 1.4, 2.6])
                with rb1:
                    if st.button("Cancel", key="btn_cancel_remove", use_container_width=True):
                        st.session_state.show_remove_form = False
                        st.rerun()
                with rb2:
                    if st.button(f"Remove selected ({len(to_remove)})", key="btn_remove",
                                 disabled=(len(to_remove) == 0), use_container_width=True):
                        removed_names = ", ".join(c.get("name", "") for c in to_remove)
                        removed_ids = {c.get("id") for c in to_remove}
                        _, saved, err = _save_change(
                            "companies.json",
                            lambda latest: [c for c in latest if c.get("id") not in removed_ids],
                            [], f"chore: remove {removed_names}",
                        )
                        for cid in removed_ids:
                            st.session_state.pop(f"rm_{cid}", None)
                        if saved:
                            toast(f"Removed {removed_names}", "success")
                        else:
                            toast(f"Removed {removed_names}, but not saved permanently. {err}", "error")
                        st.session_state.show_remove_form = False
                        st.rerun()

    st.html("<div style='height:24px;'></div>")

    # ═══════════════════════════════════════════════════════════════════════════
    # TWO COLUMNS — RECENT ALERTS  +  NOTIFICATION SETTINGS
    # ═══════════════════════════════════════════════════════════════════════════
    col_alerts, col_settings = st.columns([2, 1], gap="medium")

    # ── Recent Alerts ────────────────────────────────────────────────────────
    _BADGE_COLORS = {"FRESHER": ("var(--good)", "var(--good-soft)"),
                     "ENTRY_LEVEL": ("var(--accent)", "var(--accent-soft)")}

    def _alert_info_html(j: dict) -> str:
        raw_title = (j.get("title") or "Untitled posting").strip()
        title = raw_title[:80] + ("…" if len(raw_title) > 80 else "")
        meta = " &middot; ".join(
            x for x in (
                escape(j.get("company") or ""),
                escape(j.get("location") or ""),
                f'<span style="font-family:\'Geist Mono\',monospace;">{escape(j.get("date") or "")}</span>' if j.get("date") else "",
            ) if x
        )
        # records written before the classifier existed have no category
        badge = ""
        if j.get("category") in _BADGE_COLORS:
            fg, bg = _BADGE_COLORS[j["category"]]
            badge = (f'<span style="flex-shrink:0;font-size:10.5px;font-weight:700;letter-spacing:.03em;'
                     f'text-transform:uppercase;color:{fg};background:{bg};padding:2px 7px;border-radius:999px;">'
                     f'{escape(category_label(j))}</span>')
        return f"""
        <div style="display:flex;align-items:center;gap:12px;min-width:0;padding:12px 4px;">
          <span class="icon-emoji alert-icon" style="width:32px;height:32px;border-radius:9px;background:var(--accent-soft);color:var(--accent);display:flex;align-items:center;justify-content:center;flex-shrink:0;font-size:15px;">💼</span>
          <div style="min-width:0;flex:1;">
            <div style="display:flex;align-items:center;gap:8px;min-width:0;">
              <span title="{escape(raw_title, quote=True)}" style="font-size:13.5px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--text);min-width:0;">{escape(title)}</span>{badge}
            </div>
            <div style="font-size:12px;color:var(--muted);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">{meta or "&nbsp;"}</div>
          </div>
        </div>"""


    def _alert_apply_html(j: dict) -> str:
        url = safe_url(j.get("url", ""))
        if not url:
            return '<div style="padding:12px 20px 12px 0;text-align:right;font-size:12px;color:var(--faint);">No link</div>'
        return f"""
        <div style="padding:12px 20px 12px 0;text-align:right;">
          <a href="{escape(url, quote=True)}" target="_blank" rel="noopener"
             style="display:inline-flex;align-items:center;gap:6px;padding:7px 12px;border:1px solid var(--border-strong);background:var(--card-2);color:var(--text);border-radius:8px;text-decoration:none;font-size:12.5px;font-weight:600;white-space:nowrap;">
            Apply ↗
          </a>
        </div>"""


    _EMPTY_ALERTS_HTML = """
      <div style="padding:56px 22px;text-align:center;font-family:'Geist',sans-serif;">
        <div class="icon-emoji" style="width:46px;height:46px;border-radius:12px;background:var(--card-2);border:1px solid var(--border);display:inline-flex;align-items:center;justify-content:center;color:var(--faint);margin-bottom:14px;font-size:20px;">🔔</div>
        <p style="font-size:14px;font-weight:600;color:var(--text);">No alerts yet</p>
        <p style="font-size:12.5px;color:var(--muted);margin-top:4px;">We'll email you the moment a new fresher role appears.</p>
      </div>"""

    with col_alerts:
        with st.container(key="alerts_card"):
            total = len(seen_jobs)
            page = st.session_state.alerts_page
            max_page = max(0, (total - 1) // ALERTS_PAGE_SIZE) if total else 0
            page = min(page, max_page)
            st.session_state.alerts_page = page
            start = page * ALERTS_PAGE_SIZE
            end = start + ALERTS_PAGE_SIZE
            page_jobs = seen_jobs[start:end]

            with st.container(key="alerts_header_row"):
                hc1, hc2 = st.columns([5, 1], vertical_alignment="center")
                with hc1:
                    st.html("""
                    <h2 style="font-size:16px;font-weight:700;letter-spacing:-0.02em;color:var(--text);font-family:'Geist',sans-serif;">Recent Alerts</h2>
                    <p style="margin-top:3px;font-size:12.5px;color:var(--muted);font-family:'Geist',sans-serif;">New fresher postings, newest first</p>
                    """)
                with hc2:
                    st.html(f"""
                    <div style="text-align:right;"><span style="font-size:12px;font-weight:600;color:var(--accent);background:var(--accent-soft);padding:4px 10px;border-radius:999px;">{total} total</span></div>
                    """)

            if not page_jobs:
                st.html(_EMPTY_ALERTS_HTML)
            else:
                page_keys = {}
                for i, j in enumerate(page_jobs):
                    jkey = job_key(j)
                    wkey = _widget_key("chk", jkey)
                    page_keys[wkey] = jkey
                    if i > 0:
                        st.html('<div style="border-top:1px solid var(--border);"></div>')
                    with st.container(key=f"alert_row_{i}"):
                        rc1, rc2, rc3 = st.columns([0.5, 5, 1.2], vertical_alignment="center")
                        with rc1:
                            st.checkbox(f"Select {j.get('title', '')}", key=wkey, label_visibility="collapsed")
                        with rc2:
                            st.html(_alert_info_html(j))
                        with rc3:
                            st.html(_alert_apply_html(j))

                with st.container(key="alerts_footer_row"):
                    selected = {jkey for wkey, jkey in page_keys.items() if st.session_state.get(wkey)}
                    fc1, fc2, fc3, fc4, fc5 = st.columns([1.15, 1.0, 1.15, 1.35, 1.1], vertical_alignment="center")
                    with fc1:
                        if st.button("← Previous", key="alerts_prev", disabled=(page == 0), use_container_width=True):
                            st.session_state.alerts_page = page - 1
                            st.rerun()
                    with fc2:
                        st.html(f'<div class="st-key-alerts_page_label">{start + 1}–{min(end, total)} of {total}</div>')
                    with fc3:
                        if st.button("Next →", key="alerts_next", disabled=(end >= total), use_container_width=True):
                            st.session_state.alerts_page = page + 1
                            st.rerun()
                    with fc4:
                        if st.button(f"Remove ({len(selected)})", key="btn_remove_selected",
                                     disabled=(len(selected) == 0), use_container_width=True,
                                     help="Hide the checked alerts (they won't be emailed again)"):
                            _, saved, err = _save_change("seen_jobs.json", dismiss_jobs(selected), [],
                                                         f"chore: dismiss {len(selected)} alert(s)")
                            for wkey in page_keys:
                                st.session_state.pop(wkey, None)
                            if saved:
                                toast(f"Removed {len(selected)} alert(s)", "success")
                            else:
                                toast(f"Removed here, but not saved permanently. {err}", "error")
                            st.rerun()
                    with fc5:
                        if st.button("Clear all", key="btn_clear_all", use_container_width=True,
                                     disabled=(total == 0),
                                     help="Hide every alert (already-seen jobs won't be emailed again)"):
                            _, saved, err = _save_change("seen_jobs.json", dismiss_jobs(None), [],
                                                         "chore: dismiss all alerts")
                            for wkey in page_keys:
                                st.session_state.pop(wkey, None)
                            st.session_state.alerts_page = 0
                            if saved:
                                toast("All alerts cleared", "success")
                            else:
                                toast(f"Cleared here, but not saved permanently. {err}", "error")
                            st.rerun()

    # ── Notification Settings ───────────────────────────────────────────────
    with col_settings:
        with st.container(key="ns_card"):
            st.html("""
            <div style="display:flex;align-items:center;gap:10px;margin-bottom:4px;">
              <span class="icon-emoji" style="width:30px;height:30px;border-radius:8px;background:var(--accent-soft);color:var(--accent);display:flex;align-items:center;justify-content:center;font-size:16px;">✉</span>
              <h2 style="font-size:16px;font-weight:700;letter-spacing:-0.02em;color:var(--text);font-family:'Geist',sans-serif;">Notification Settings</h2>
            </div>
            <p style="font-size:12.5px;color:var(--muted);margin-bottom:4px;margin-left:40px;font-family:'Geist',sans-serif;">Where alert emails are delivered.</p>
            """)

            in_col, save_col = st.columns([2.7, 1], vertical_alignment="bottom")
            with in_col:
                email_val = st.text_input(
                    "Recipient email",
                    value=st.session_state.email_val,
                    placeholder="careers@gmail.com",
                    key="email_input",
                )
                st.session_state.email_val = email_val
            with save_col:
                save_clicked = st.button("Save", key="btn_save_email", use_container_width=True)

            def _set_recipient(addr: str) -> tuple[bool, str]:
                def _mutate(latest: dict) -> dict:
                    latest = latest if isinstance(latest, dict) else {}
                    latest["recipient_email"] = addr
                    return latest
                data, saved, err = _save_change("settings.json", _mutate, {}, "chore: update recipient email")
                settings.update(data)
                return saved, err

            if save_clicked:
                e = email_val.strip()
                if not _EMAIL_RE.match(e):
                    toast("Enter a valid email address", "error")
                else:
                    saved, err = _set_recipient(e)
                    if saved:
                        toast(f"Saved — alerts go to {e}", "success")
                    else:
                        toast(f"Saved here, but not saved permanently. {err}", "error")

            st.html("""
            <div style="height:1px;background:var(--border);margin:8px 0 4px;"></div>
            <p style="font-size:12px;font-weight:600;color:var(--muted);font-family:'Geist',sans-serif;">Verify delivery</p>
            <p style="font-size:12.5px;color:var(--muted);margin-bottom:4px;font-family:'Geist',sans-serif;">Send yourself a sample alert to confirm emails arrive.</p>
            """)

            with st.container(key="test_mail_btn"):
                if st.button("✈  Send Test Mail", key="btn_test", use_container_width=True):
                    recipient = email_val.strip()
                    if not _EMAIL_RE.match(recipient):
                        toast("Enter a valid recipient email first", "error")
                    else:
                        persist_err = ""
                        if settings.get("recipient_email", "") != recipient:
                            saved, persist_err = _set_recipient(recipient)
                        try:
                            from notifier import test_mail
                            test_mail(recipient)
                        except Exception as ex:
                            log.warning("test mail failed: %s", ex)
                            if "GMAIL_" in str(ex):
                                toast("Email isn't configured for this app (Gmail address / app password missing).", "error")
                            else:
                                toast("Couldn't send the test email. Check the Gmail app password and try again.", "error")
                        else:
                            if persist_err:
                                toast(f"Test email sent, but the address wasn't saved permanently. {persist_err}", "error")
                            else:
                                toast(f"Test email sent — check {recipient}", "success")

    # ── Footer ───────────────────────────────────────────────────────────────
    st.html(f"""
    <footer style="margin:32px auto 0;padding:0 0 8px;display:flex;align-items:center;justify-content:space-between;color:var(--faint);font-size:12px;font-family:'Geist',sans-serif;">
      <span>Fresher Job Tracker &middot; internal tool</span>
      <a href="https://github.com/{GITHUB_REPO}" target="_blank" rel="noopener"
         style="display:inline-flex;align-items:center;gap:6px;color:var(--muted);text-decoration:none;">
        Source repository
      </a>
    </footer>
    """)

# ── Toast ──────────────────────────────────────────────────────────────────────
# Shown once and faded out with CSS; the old sleep(3)+rerun froze the whole
# app for 3 s after every action, swallowing clicks made in the meantime.
if st.session_state.toast:
    msg = escape(st.session_state.toast, quote=False)
    kind = st.session_state.toast_kind
    st.session_state.toast = None
    icon_bg = "var(--good-soft)" if kind == "success" else "var(--bad-soft)"
    icon_color = "var(--good)" if kind == "success" else "var(--bad)"
    icon = "✅" if kind == "success" else "⚠️"
    st.html(f"""
<style>
@keyframes jt-toast {{ 0%, 85% {{ opacity: 1; }} 100% {{ opacity: 0; visibility: hidden; }} }}
.jt-toast {{ animation: jt-toast {6 if kind == "error" else 4}s ease-in forwards; }}
@media (max-width: 640px) {{ .jt-toast {{ left: 16px; right: 16px; bottom: 16px; max-width: none !important; }} }}
</style>
<div class="jt-toast" role="status" style="position:fixed;bottom:24px;right:24px;z-index:9999;display:flex;align-items:center;gap:11px;padding:13px 16px;border-radius:12px;background:var(--card);border:1px solid var(--border-strong);box-shadow:0 10px 30px rgba(0,0,0,.15);max-width:340px;font-family:'Geist',sans-serif;">
  <span class="icon-emoji" style="width:26px;height:26px;border-radius:7px;flex-shrink:0;display:flex;align-items:center;justify-content:center;background:{icon_bg};color:{icon_color};font-size:14px;">{icon}</span>
  <div style="font-size:13px;font-weight:500;color:var(--text);line-height:1.4;">{msg}</div>
</div>
""")
