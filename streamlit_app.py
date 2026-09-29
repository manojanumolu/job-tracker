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

if "page" not in st.session_state:
    qp = st.query_params
    st.session_state.page = qp.get("page") if qp.get("page") in PAGES else "home"
    st.session_state.job_id = qp.get("job") or None
    st.session_state.company_id = qp.get("company") or None
    st.session_state.company_view = "add" if qp.get("view") == "add" else None
if "dark_mode" not in st.session_state:
    # the theme lives in the URL (?theme=dark) so it survives reloads/bookmarks
    st.session_state.dark_mode = st.query_params.get("theme") == "dark"

st.set_page_config(
    page_title=f"{PAGES[st.session_state.page][0]} · Fresher Job Tracker",
    page_icon=str(LOGO_PATH) if LOGO_PATH.exists() else "💼",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── theme ─────────────────────────────────────────────────────────────────────
LIGHT = {
    "bg": "#f8fafc", "surface": "#ffffff", "surface-2": "#f8fafc", "hover": "#f1f5f9",
    "border": "#e2e8f0", "border-strong": "#cbd5e1",
    "text": "#0f172a", "text-2": "#334155", "muted": "#64748b",
    "accent": "#4f46e5", "accent-hover": "#4338ca", "accent-text": "#4338ca", "accent-soft": "#eef2ff",
    "on-accent": "#ffffff",
    "green": "#15803d", "green-soft": "#f0fdf4", "amber": "#b45309", "amber-soft": "#fffbeb",
    "red": "#b91c1c", "red-soft": "#fef2f2", "gray": "#475569", "gray-soft": "#f1f5f9",
    "shadow": "0 1px 2px rgba(15,23,42,0.04)",
    "shadow-lg": "0 10px 30px -10px rgba(15,23,42,0.18)",
}
DARK = {
    "bg": "#0b1020", "surface": "#111827", "surface-2": "#0f172a", "hover": "#1e293b",
    "border": "#1f2937", "border-strong": "#334155",
    "text": "#f1f5f9", "text-2": "#cbd5e1", "muted": "#94a3b8",
    "accent": "#6366f1", "accent-hover": "#818cf8", "accent-text": "#a5b4fc", "accent-soft": "rgba(99,102,241,0.16)",
    "on-accent": "#ffffff",
    "green": "#4ade80", "green-soft": "rgba(74,222,128,0.12)", "amber": "#fbbf24", "amber-soft": "rgba(251,191,36,0.12)",
    "red": "#f87171", "red-soft": "rgba(248,113,113,0.12)", "gray": "#94a3b8", "gray-soft": "rgba(148,163,184,0.14)",
    "shadow": "0 1px 2px rgba(0,0,0,0.3)",
    "shadow-lg": "0 10px 30px -10px rgba(0,0,0,0.6)",
}
TH = DARK if st.session_state.dark_mode else LIGHT
_root_vars = ":root {" + "".join(f"--{k}:{v};" for k, v in TH.items()) + "}"

# One typeface (Inter) everywhere; numbers use tabular figures instead of a
# monospace font. Icons are Material Symbols ligatures — inline <svg> doesn't
# paint in this app's hosting environment. Widgets that need styling are
# wrapped in st.container(key=...) and targeted via .st-key-*; raw HTML tags
# are never opened in one st.* call and closed in another.
_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
@import url('https://fonts.googleapis.com/css2?family=Material+Symbols+Rounded:opsz,wght,FILL,GRAD@20..48,400,0..1,0&display=block');

:root { --font: 'Inter', ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif; --sidebar-w: 240px; }

/* ── Streamlit chrome ── */
#MainMenu, footer:not(.app-foot), header[data-testid="stHeader"] { display: none !important; }
.stDeployButton, [data-testid="stToolbar"], [data-testid="stDecoration"] { display: none !important; }
[data-testid="stSidebarHeader"], [data-testid="stSidebarCollapseButton"], [data-testid="stSidebarCollapsedControl"],
[data-testid="stExpandSidebarButton"], [data-testid="stSidebarResizeHandle"] { display: none !important; }
.stApp, [data-testid="stAppViewContainer"], .stMain { background: var(--bg) !important; }
.stApp, .stApp p, .stApp label, .stApp input, .stApp button, .stApp textarea, .stApp li, .stApp h1, .stApp h2, .stApp h3, .stApp h4,
[data-baseweb="popover"] * { font-family: var(--font) !important; }
.stApp { color: var(--text); -webkit-font-smoothing: antialiased; font-feature-settings: 'cv11', 'ss01'; }
.stApp h1, .stApp h2, .stApp h3, .stApp h4 { padding: 0 !important; margin: 0; letter-spacing: -0.015em; color: var(--text); }
.stApp a { text-decoration: none !important; }
.num { font-variant-numeric: tabular-nums; }
[data-testid="stMainBlockContainer"], .block-container {
  max-width: 1160px !important; padding: 32px 40px 48px !important; margin: 0 !important;
}
[data-testid="stMainBlockContainer"] > div > [data-testid="stVerticalBlock"] { gap: 24px; }

/* ── icons ── */
.ms {
  font-family: 'Material Symbols Rounded' !important; font-weight: normal; font-style: normal; line-height: 1;
  letter-spacing: normal; text-transform: none; white-space: nowrap; direction: ltr; font-feature-settings: 'liga';
  -webkit-font-smoothing: antialiased; display: inline-block; overflow: hidden; flex-shrink: 0; vertical-align: middle;
  font-size: 18px; width: 1em; height: 1em;
}
.ms.s16 { font-size: 16px; } .ms.s20 { font-size: 20px; } .ms.s24 { font-size: 24px; }
[data-testid="stIconMaterial"] { font-family: 'Material Symbols Rounded' !important; }

/* ── type ── */
.page-title { font-size: 26px; line-height: 34px; font-weight: 650; letter-spacing: -0.02em; color: var(--text); margin: 0; }
.page-sub { font-size: 14px; line-height: 20px; color: var(--muted); margin: 4px 0 0; display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; }
.section-title { font-size: 16px; line-height: 24px; font-weight: 600; color: var(--text); margin: 0; }
.section-sub { font-size: 13px; line-height: 18px; color: var(--muted); margin: 2px 0 0; }
.muted { color: var(--muted); } .text-2 { color: var(--text-2); } .accent { color: var(--accent-text); }
.sep { color: var(--border-strong); }

/* ── status pills: one system everywhere ── */
.pill { display: inline-flex; align-items: center; gap: 6px; padding: 2px 10px; border-radius: 999px; font-size: 12px; line-height: 20px; font-weight: 500; white-space: nowrap; }
.pill i { width: 6px; height: 6px; border-radius: 999px; background: currentColor; display: inline-block; }
.pill.healthy, .pill.fresher, .pill.on { color: var(--green); background: var(--green-soft); }
.pill.delayed, .pill.pending-mail { color: var(--amber); background: var(--amber-soft); }
.pill.failing, .pill.off { color: var(--red); background: var(--red-soft); }
.pill.pending, .pill.legacy, .pill.neutral { color: var(--gray); background: var(--gray-soft); }
.pill.entry, .pill.checking, .pill.new { color: var(--accent-text); background: var(--accent-soft); }
.dot { width: 8px; height: 8px; border-radius: 999px; display: inline-block; flex-shrink: 0; }
.dot.healthy { background: var(--green); } .dot.delayed { background: var(--amber); } .dot.failing { background: var(--red); }
.dot.pending { background: var(--gray); } .dot.checking { background: var(--accent); }

/* ── surfaces ── */
.panel, .st-key-stats, [class*="st-key-joblist_"], .st-key-company_list, .st-key-add_form, .st-key-email_form,
.st-key-test_panel, [class*="st-key-set_"], .st-key-mon_table, .st-key-job_main, .st-key-job_side, .st-key-co_side,
.st-key-co_jobs, .st-key-empty {
  background: var(--surface) !important; border: 1px solid var(--border) !important; border-radius: 12px !important;
  box-shadow: var(--shadow) !important;
}
.st-key-add_form, .st-key-email_form, .st-key-test_panel, [class*="st-key-set_"], .st-key-job_main, .st-key-job_side,
.st-key-co_side, .st-key-empty { padding: 20px 24px !important; gap: 16px !important; }

/* ── page header ── */
.st-key-page_head > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"],
.st-key-page_head > [data-testid="stHorizontalBlock"] { align-items: flex-end !important; gap: 16px !important; flex-wrap: wrap !important; }
.st-key-page_head [data-testid="stColumn"] { flex: 0 0 auto !important; width: auto !important; min-width: 0 !important; }
.st-key-page_head [data-testid="stColumn"]:first-child { flex: 1 1 320px !important; }
.st-key-page_actions > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"],
.st-key-page_actions > [data-testid="stHorizontalBlock"] { gap: 8px !important; flex-wrap: nowrap !important; }
.st-key-page_actions [data-testid="stColumn"] { flex: 0 0 auto !important; }

/* ── stats strip: one panel, divided cells ── */
.stats { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); }
.stat { padding: 16px 20px; min-width: 0; }
.stat + .stat { border-left: 1px solid var(--border); }
.stat .k { font-size: 13px; line-height: 18px; color: var(--muted); display: flex; align-items: center; gap: 6px; }
.stat .v { font-size: 24px; line-height: 32px; font-weight: 600; letter-spacing: -0.02em; color: var(--text); margin-top: 4px; font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.stat .n { font-size: 13px; line-height: 18px; color: var(--muted); margin-top: 2px; overflow-wrap: anywhere; }
.st-key-stats { padding: 0 !important; overflow: hidden; }

/* ── filter bar ── */
[class*="st-key-filters_"] > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"],
[class*="st-key-filters_"] > [data-testid="stHorizontalBlock"] { gap: 8px !important; flex-wrap: wrap !important; align-items: flex-end !important; }
[class*="st-key-filters_"] [data-testid="stColumn"] { flex: 1 1 150px !important; width: auto !important; min-width: 140px !important; max-width: 240px; }
[class*="st-key-filters_"] [data-testid="stColumn"]:first-child { max-width: none; }
[class*="st-key-filters_"] [data-testid="stColumn"]:first-child { flex: 2 1 240px !important; }
[class*="st-key-filters_"] [data-testid="stColumn"]:last-child { flex: 0 0 auto !important; min-width: 0 !important; }
:is(.st-key-h_q, .st-key-j_q, .st-key-co_q) :is([data-baseweb="input"], [data-testid="stTextInputRootElement"])::before {
  content: "search"; font-family: 'Material Symbols Rounded' !important; font-feature-settings: 'liga'; font-weight: 400;
  font-size: 18px; width: 18px; overflow: hidden; white-space: nowrap; flex-shrink: 0;
  color: var(--muted); margin-left: 12px; align-self: center; line-height: 1; box-sizing: content-box;
}

/* ── job list: one panel, divided rows ── */
[class*="st-key-joblist_"] { padding: 0 !important; gap: 0 !important; overflow: hidden; }
[class*="st-key-jr_"] { padding: 16px 20px !important; gap: 0 !important; transition: background .15s; }
[class*="st-key-jr_"] + [class*="st-key-jr_"], [class*="st-key-joblist_"] > [data-testid="stElementContainer"] + [class*="st-key-jr_"] { border-top: 1px solid var(--border); }
[class*="st-key-jr_"]:hover { background: var(--surface-2); }
[class*="st-key-jr_"] > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"],
[class*="st-key-jr_"] > [data-testid="stHorizontalBlock"] { gap: 16px !important; align-items: center !important; flex-wrap: nowrap !important; }
[class*="st-key-jr_"] [data-testid="stColumn"] { width: auto !important; min-width: 0 !important; flex: 0 0 auto !important; }
[class*="st-key-jr_"] [data-testid="stColumn"]:first-child { flex: 1 1 auto !important; }
[class*="st-key-jr_"] [data-testid="stColumn"]:last-child [data-testid="stVerticalBlock"] { flex-direction: row !important; gap: 8px !important; align-items: center; flex-wrap: nowrap; }
[class*="st-key-jr_"] [data-testid="stColumn"]:last-child [data-testid="stElementContainer"] { width: auto !important; flex: 0 0 auto; }
.job { display: flex; gap: 14px; min-width: 0; align-items: flex-start; }
.logo { width: 40px; height: 40px; border-radius: 10px; background: var(--accent-soft); color: var(--accent-text); display: flex; align-items: center; justify-content: center; font-size: 16px; font-weight: 600; flex-shrink: 0; }
.logo.lg { width: 56px; height: 56px; border-radius: 14px; font-size: 22px; }
.job-body { min-width: 0; flex: 1; }
.job-title { font-size: 16px; line-height: 22px; font-weight: 600; color: var(--text); margin: 0; overflow-wrap: anywhere; }
.job-meta { font-size: 14px; line-height: 20px; color: var(--muted); margin-top: 2px; display: flex; flex-wrap: wrap; align-items: center; gap: 2px 8px; }
.job-meta .co { color: var(--text-2); font-weight: 500; }
.job-why { font-size: 13px; line-height: 18px; color: var(--muted); margin-top: 8px; display: flex; align-items: center; gap: 8px; min-width: 0; }
.job-why span.t { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
.list-foot { padding: 12px 20px; border-top: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; gap: 12px; font-size: 13px; color: var(--muted); }
.st-key-list_foot_h, .st-key-list_foot_j { padding: 10px 20px !important; border-top: 1px solid var(--border); }
.st-key-list_foot_h > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"], .st-key-list_foot_h > [data-testid="stHorizontalBlock"],
.st-key-list_foot_j > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"], .st-key-list_foot_j > [data-testid="stHorizontalBlock"] { align-items: center !important; gap: 8px !important; flex-wrap: nowrap !important; }
.st-key-list_foot_h [data-testid="stColumn"], .st-key-list_foot_j [data-testid="stColumn"] { flex: 0 0 auto !important; width: auto !important; min-width: 0 !important; }
.st-key-list_foot_h [data-testid="stColumn"]:first-child, .st-key-list_foot_j [data-testid="stColumn"]:first-child { flex: 1 1 auto !important; }

/* buttons that look like links */
.btn { display: inline-flex; align-items: center; justify-content: center; gap: 6px; height: 36px; padding: 0 14px; border-radius: 8px; font-size: 14px; font-weight: 500; white-space: nowrap; transition: background .15s, border-color .15s; }
.btn.primary { background: var(--accent); color: var(--on-accent) !important; }
.btn.primary:hover { background: var(--accent-hover); }
.btn.ghost { background: var(--surface); color: var(--text) !important; border: 1px solid var(--border); }
.btn.ghost:hover { background: var(--hover); border-color: var(--border-strong); }
.btn.disabled { background: var(--hover); color: var(--muted) !important; }
.link { color: var(--accent-text) !important; font-weight: 500; display: inline-flex; align-items: center; gap: 4px; overflow-wrap: anywhere; }
.link:hover { text-decoration: underline !important; }

/* ── detail views ── */
.detail-head { display: flex; gap: 16px; align-items: flex-start; }
.detail-title { font-size: 26px; line-height: 34px; font-weight: 650; letter-spacing: -0.02em; margin: 0; color: var(--text); overflow-wrap: anywhere; }
.kv { display: grid; grid-template-columns: 160px minmax(0, 1fr); gap: 10px 16px; font-size: 14px; line-height: 20px; margin: 12px 0 0 !important; padding: 0 !important; }
.kv dt { color: var(--muted); margin: 0 !important; padding: 0 !important; } .kv dd { margin: 0 !important; padding: 0 !important; color: var(--text); min-width: 0; overflow-wrap: anywhere; }
.note { font-size: 14px; line-height: 20px; color: var(--muted); margin: 0; }
.why { display: flex; gap: 10px; align-items: flex-start; font-size: 14px; line-height: 20px; color: var(--text); }
.why .ms { color: var(--green); margin-top: 1px; }
.quote { margin-top: 10px; padding: 10px 12px; border-left: 3px solid var(--border-strong); background: var(--surface-2); border-radius: 0 8px 8px 0; font-size: 13px; line-height: 18px; color: var(--text-2); }
.st-key-job_cols > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"], .st-key-job_cols > [data-testid="stHorizontalBlock"],
.st-key-co_cols > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"], .st-key-co_cols > [data-testid="stHorizontalBlock"] { gap: 24px !important; align-items: flex-start !important; }
.st-key-job_actions > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"], .st-key-job_actions > [data-testid="stHorizontalBlock"],
.st-key-co_actions > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"], .st-key-co_actions > [data-testid="stHorizontalBlock"] { gap: 8px !important; flex-wrap: wrap !important; }
.st-key-job_actions [data-testid="stColumn"], .st-key-co_actions [data-testid="stColumn"] { flex: 0 0 auto !important; width: auto !important; min-width: 0 !important; }
.st-key-job_main, .st-key-job_side, .st-key-co_side { gap: 20px !important; }
.divider { border-top: 1px solid var(--border); margin: 0; }
[data-testid="stExpander"] details { border: 1px solid var(--border) !important; border-radius: 10px !important; background: var(--surface) !important; }
[data-testid="stExpander"] summary p { font-size: 14px !important; font-weight: 600 !important; color: var(--text) !important; }
[data-testid="stExpander"] summary:hover { color: var(--accent-text) !important; }

/* ── companies list ── */
.st-key-company_list { padding: 0 !important; gap: 0 !important; overflow: hidden; }
[class*="st-key-cr_"] { padding: 14px 20px !important; transition: background .15s; }
[class*="st-key-cr_"] + [class*="st-key-cr_"] { border-top: 1px solid var(--border); }
[class*="st-key-cr_"]:hover { background: var(--surface-2); }
[class*="st-key-cr_"] > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"],
[class*="st-key-cr_"] > [data-testid="stHorizontalBlock"] { gap: 16px !important; align-items: center !important; flex-wrap: nowrap !important; }
[class*="st-key-cr_"] [data-testid="stColumn"] { width: auto !important; min-width: 0 !important; flex: 0 0 auto !important; }
[class*="st-key-cr_"] [data-testid="stColumn"]:first-child { flex: 1 1 auto !important; }
.co-row { display: grid; grid-template-columns: minmax(0, 1.5fr) minmax(0, 1fr) 110px 90px 100px; gap: 16px; align-items: center; min-width: 0; }
.co-name { display: flex; gap: 12px; align-items: center; min-width: 0; }
.co-name .t { font-size: 15px; line-height: 20px; font-weight: 600; color: var(--text); display: flex; gap: 8px; align-items: center; }
.co-name .h { font-size: 13px; line-height: 18px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.co-cell { font-size: 14px; color: var(--text-2); font-variant-numeric: tabular-nums; white-space: nowrap; }
.co-cell .l { display: none; }
.list-head { display: grid; grid-template-columns: minmax(0, 1.5fr) minmax(0, 1fr) 110px 90px 100px 88px; gap: 16px; padding: 10px 20px; font-size: 13px; color: var(--muted); border-bottom: 1px solid var(--border); background: var(--surface-2); }
.tag { font-size: 12px; line-height: 18px; color: var(--muted); border: 1px solid var(--border); border-radius: 6px; padding: 0 6px; font-weight: 500; }

/* ── monitoring table ── */
.st-key-mon_table { padding: 0 !important; overflow: hidden; }
.mon { width: 100%; border-collapse: collapse; font-size: 14px; line-height: 20px; }
.mon th { text-align: left; font-weight: 500; color: var(--muted); font-size: 13px; padding: 10px 16px; background: var(--surface-2); border-bottom: 1px solid var(--border); white-space: nowrap; }
.mon td { padding: 14px 16px; border-bottom: 1px solid var(--border); color: var(--text-2); vertical-align: top; }
.mon tr:last-child td { border-bottom: none; }
.mon td.c { color: var(--text); font-weight: 600; }
.mon td .sub { color: var(--muted); font-size: 13px; font-weight: 400; margin-top: 2px; overflow-wrap: anywhere; }
.mon td.num { font-variant-numeric: tabular-nums; white-space: nowrap; }

/* ── widgets ── */
.stTextInput label p, .stSelectbox label p, .stCheckbox label p { font-size: 13px !important; line-height: 18px !important; font-weight: 500 !important; color: var(--text-2) !important; }
.stTextInput [data-baseweb="input"], [data-testid="stTextInputRootElement"], [data-baseweb="select"] > div, .stSelectbox [role="group"] {
  background: var(--surface) !important; border: 1px solid var(--border-strong) !important; border-radius: 8px !important;
  min-height: 38px; transition: border-color .15s, box-shadow .15s;
}
.stTextInput [data-baseweb="input"] *, [data-testid="stTextInputRootElement"] * { background-color: transparent !important; }
.stTextInput [data-baseweb="input"]:hover, [data-testid="stTextInputRootElement"]:hover, [data-baseweb="select"] > div:hover, .stSelectbox [role="group"]:hover { border-color: var(--muted) !important; }
.stTextInput [data-baseweb="input"]:focus-within, [data-testid="stTextInputRootElement"]:focus-within, [data-baseweb="select"] > div:focus-within, .stSelectbox [role="group"]:focus-within {
  border-color: var(--accent) !important; box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 18%, transparent) !important;
}
.stTextInput input { font-size: 14px !important; color: var(--text) !important; -webkit-text-fill-color: var(--text); padding: 8px 12px !important; }
.stTextInput input::placeholder { color: var(--muted) !important; -webkit-text-fill-color: var(--muted); opacity: 1; }
.stSelectbox input, .stSelectbox [role="group"] * { font-size: 14px !important; color: var(--text) !important; -webkit-text-fill-color: var(--text); }
.stSelectbox svg { fill: var(--muted) !important; color: var(--muted) !important; }
[data-baseweb="select"] * { font-size: 14px !important; color: var(--text) !important; }
[data-baseweb="select"] svg { fill: var(--muted) !important; }
[data-baseweb="popover"] ul, [data-baseweb="popover"] [role="listbox"] { background: var(--surface) !important; }
[data-baseweb="popover"] li { color: var(--text) !important; font-size: 14px !important; }
[data-baseweb="popover"] li:hover, [data-baseweb="popover"] li[aria-selected="true"] { background: var(--hover) !important; }
[data-testid="InputInstructions"] { display: none !important; }

[data-testid^="stBaseButton"] {
  border-radius: 8px !important; min-height: 36px !important; padding: 0 14px !important; box-shadow: none !important;
  transition: background .15s, border-color .15s, color .15s !important;
}
[data-testid^="stBaseButton"] [data-testid="stMarkdownContainer"] { display: flex !important; align-items: center; margin: 0 !important; padding: 0 !important; }
[data-testid^="stBaseButton"] p { margin: 0 !important; font-size: 14px !important; line-height: 20px !important; font-weight: 500 !important; white-space: nowrap; }
[data-testid="stBaseButton-secondary"] { background: var(--surface) !important; color: var(--text) !important; border: 1px solid var(--border) !important; }
[data-testid="stBaseButton-secondary"]:hover { background: var(--hover) !important; border-color: var(--border-strong) !important; color: var(--text) !important; }
[data-testid="stBaseButton-primary"] { background: var(--accent) !important; color: var(--on-accent) !important; border: 1px solid var(--accent) !important; }
[data-testid="stBaseButton-primary"]:hover { background: var(--accent-hover) !important; border-color: var(--accent-hover) !important; }
[data-testid^="stBaseButton"]:disabled { opacity: .45 !important; }
[data-testid="stBaseButton-tertiary"] { color: var(--muted) !important; background: transparent !important; border: none !important; }
[data-testid="stBaseButton-tertiary"]:hover { color: var(--text) !important; background: var(--hover) !important; }
.st-key-danger [data-testid^="stBaseButton"], .st-key-danger_confirm [data-testid="stBaseButton-secondary"] { color: var(--red) !important; }
.st-key-danger_confirm [data-testid="stBaseButton-secondary"] { color: var(--text) !important; }
.st-key-danger_confirm [data-testid="stBaseButton-primary"] { background: var(--red) !important; border-color: var(--red) !important; color: #fff !important; }
[class*="st-key-dismiss_"] [data-testid^="stBaseButton"], [class*="st-key-restore_"] [data-testid^="stBaseButton"] { width: 36px !important; padding: 0 !important; }

/* segmented tabs (st.pills) */
.st-key-jobs_tab_wrap [data-testid="stButtonGroup"] > div, .st-key-co_status_wrap [data-testid="stButtonGroup"] > div {
  gap: 2px !important; background: var(--hover); padding: 3px; border-radius: 10px; display: inline-flex !important; flex-wrap: wrap;
}
.st-key-jobs_tab_wrap button[data-variant="pills"], .st-key-co_status_wrap button[data-variant="pills"],
.st-key-jobs_tab_wrap [data-testid^="stBaseButton-pills"], .st-key-co_status_wrap [data-testid^="stBaseButton-pills"] {
  border: none !important; border-radius: 8px !important; background: transparent !important; color: var(--muted) !important; min-height: 32px !important; padding: 0 12px !important;
}
.st-key-jobs_tab_wrap button[data-variant="pills"][aria-checked="true"], .st-key-co_status_wrap button[data-variant="pills"][aria-checked="true"],
.st-key-jobs_tab_wrap [data-testid="stBaseButton-pillsActive"], .st-key-co_status_wrap [data-testid="stBaseButton-pillsActive"] {
  background: var(--surface) !important; color: var(--text) !important; box-shadow: 0 1px 2px rgba(15,23,42,.08) !important;
}
.st-key-jobs_tab_wrap button[data-variant="pills"] p, .st-key-co_status_wrap button[data-variant="pills"] p { color: inherit !important; font-size: 14px !important; }

/* ── sidebar ── */
section[data-testid="stSidebar"] {
  width: var(--sidebar-w) !important; min-width: var(--sidebar-w) !important; max-width: var(--sidebar-w) !important;
  background: var(--surface) !important; border-right: 1px solid var(--border) !important; transform: none !important;
}
section[data-testid="stSidebar"] > div, [data-testid="stSidebarContent"] { background: var(--surface) !important; }
[data-testid="stSidebarContent"] { padding: 0 !important; }
[data-testid="stSidebarUserContent"] { padding: 20px 12px 16px !important; margin: 0 !important; width: 100% !important; min-height: 100vh; display: flex; flex-direction: column; }
[data-testid="stSidebarUserContent"] > div { flex: 1 1 auto; display: flex; flex-direction: column; }
[data-testid="stSidebarUserContent"] > div > [data-testid="stVerticalBlock"] { flex: 1 1 auto; }
[data-testid="stSidebarUserContent"] [data-testid="stLayoutWrapper"]:has(> .st-key-side_foot_wrap) { margin-top: auto; }
[data-testid="stSidebarUserContent"] [data-testid="stVerticalBlock"] { gap: 2px !important; }
.brand { display: flex; align-items: center; gap: 10px; padding: 0 8px 20px; }
.brand img { width: 32px; height: 32px; border-radius: 9px; display: block; }
.brand .n { font-size: 15px; line-height: 20px; font-weight: 650; letter-spacing: -0.01em; color: var(--text); }
.brand .s { font-size: 12px; line-height: 16px; color: var(--muted); }
.nav-group { font-size: 12px; line-height: 16px; font-weight: 600; letter-spacing: .05em; text-transform: uppercase; color: var(--muted); padding: 16px 12px 6px; }
.nav-group.first { padding-top: 4px; }
[class*="st-key-nav_"] [data-testid^="stBaseButton"] {
  width: 100% !important; justify-content: flex-start !important; min-height: 38px !important; padding: 0 12px !important;
  border-radius: 8px !important; color: var(--text-2) !important; background: transparent !important; border: none !important; gap: 10px;
}
[class*="st-key-nav_"] [data-testid^="stBaseButton"] > div { justify-content: flex-start !important; gap: 10px !important; }
[class*="st-key-nav_"] [data-testid^="stBaseButton"]:hover { background: var(--hover) !important; color: var(--text) !important; }
[class*="st-key-nav_"] [data-testid="stIconMaterial"] { font-size: 20px !important; color: var(--muted); }
[class*="st-key-nav_"] p { font-size: 14px !important; font-weight: 500 !important; }
/* the ✕ / ↺ buttons on job rows are icon-only; their label is for screen readers */
[class*="st-key-dismiss_"] [data-testid="stMarkdownContainer"], [class*="st-key-restore_"] [data-testid="stMarkdownContainer"],
[class*="st-key-dismiss_"] [data-testid="stMarkdownContainer"] p, [class*="st-key-restore_"] [data-testid="stMarkdownContainer"] p { position: absolute !important; width: 1px !important; height: 1px !important; overflow: hidden !important; clip: rect(0 0 0 0) !important; clip-path: inset(50%) !important; white-space: nowrap !important; margin: -1px !important; padding: 0 !important; border: 0 !important; }
.side-foot { margin-top: auto; padding: 14px 12px 4px; border-top: 1px solid var(--border); font-size: 13px; line-height: 18px; color: var(--muted); }
.side-foot .st { display: flex; align-items: center; gap: 8px; color: var(--text); font-weight: 500; }
.side-foot a { color: var(--muted) !important; }
.side-foot a:hover { color: var(--accent-text) !important; }
.st-key-side_foot_wrap { margin-top: auto; }

/* mobile top nav (phones only) */
.st-key-mnav { display: none !important; }

/* ── footer ── */
.app-foot { font-size: 13px; color: var(--muted); display: flex; justify-content: space-between; gap: 12px; flex-wrap: wrap; padding-top: 8px; }
.app-foot a { color: var(--muted) !important; } .app-foot a:hover { color: var(--accent-text) !important; }

/* ── empty ── */
.empty { display: flex; flex-direction: column; align-items: center; text-align: center; gap: 8px; padding: 24px 8px; }
.empty .ic { width: 48px; height: 48px; border-radius: 12px; background: var(--accent-soft); color: var(--accent-text); display: flex; align-items: center; justify-content: center; margin-bottom: 4px; }
.empty h3 { font-size: 16px; line-height: 24px; font-weight: 600; }
.empty p { font-size: 14px; line-height: 20px; color: var(--muted); max-width: 460px; margin: 0; }
.st-key-empty_actions > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"], .st-key-empty_actions > [data-testid="stHorizontalBlock"] { justify-content: center !important; gap: 8px !important; }
.st-key-empty_actions [data-testid="stColumn"] { flex: 0 0 auto !important; width: auto !important; min-width: 0 !important; }

/* ── responsive ── */
@media (max-width: 1279px) {
  [data-testid="stMainBlockContainer"], .block-container { padding: 28px 28px 40px !important; }
}
@media (max-width: 1100px) {
  .co-row { grid-template-columns: minmax(0, 1fr) 110px 90px; } .list-head { grid-template-columns: minmax(0, 1fr) 110px 90px 88px; }
  .co-row > :nth-child(2), .co-row > :nth-child(5), .list-head > :nth-child(2), .list-head > :nth-child(5) { display: none; }
}
/* tablet: icon rail */
@media (max-width: 1023px) {
  :root { --sidebar-w: 72px; }
  .brand { justify-content: center; padding: 0 0 16px; } .brand > div { display: none; }
  .nav-group { font-size: 0; padding: 10px 0 4px; border-top: 1px solid var(--border); margin: 8px 8px 0; }
  .nav-group.first { display: none; }
  [class*="st-key-nav_"] [data-testid^="stBaseButton"] { justify-content: center !important; padding: 0 !important; }
  [class*="st-key-nav_"] [data-testid^="stBaseButton"] > div { justify-content: center !important; }
  /* icon rail: labels are hidden visually but kept for screen readers */
  [class*="st-key-nav_"] p { position: absolute !important; width: 1px !important; height: 1px !important; overflow: hidden !important; clip: rect(0 0 0 0) !important; clip-path: inset(50%) !important; white-space: nowrap !important; margin: -1px !important; padding: 0 !important; border: 0 !important; }
  [data-testid="stSidebarUserContent"] { padding: 16px 8px !important; }
  .side-foot { padding: 12px 0 0; text-align: center; } .side-foot .txt { display: none; } .side-foot .st { justify-content: center; }
  .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .stat:nth-child(3) { border-left: none; } .stat:nth-child(n+3) { border-top: 1px solid var(--border); }
  .st-key-job_cols > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"], .st-key-job_cols > [data-testid="stHorizontalBlock"],
  .st-key-co_cols > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"], .st-key-co_cols > [data-testid="stHorizontalBlock"] { flex-wrap: wrap !important; }
  .st-key-job_cols [data-testid="stColumn"]:nth-child(n), .st-key-co_cols [data-testid="stColumn"]:nth-child(n) { flex: 1 1 100% !important; width: 100% !important; }
}
/* phone: sidebar hidden, top nav bar instead */
@media (max-width: 767px) {
  section[data-testid="stSidebar"] { display: none !important; }
  [data-testid="stMainBlockContainer"], .block-container { padding: 0 16px 32px !important; }
  [data-testid="stMainBlockContainer"] > div > [data-testid="stVerticalBlock"] { gap: 20px; }
  .st-key-mnav {
    display: flex !important; position: sticky; top: 0; z-index: 30; margin: 0 -16px; padding: 6px 4px !important;
    background: color-mix(in srgb, var(--surface) 92%, transparent); backdrop-filter: blur(12px); border-bottom: 1px solid var(--border);
  }
  /* all six destinations visible at once: equal columns, icon above a short label */
  .st-key-mnav > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"], .st-key-mnav > [data-testid="stHorizontalBlock"] { flex-wrap: nowrap !important; gap: 0 !important; }
  .st-key-mnav [data-testid="stColumn"] { flex: 1 1 0 !important; width: auto !important; min-width: 0 !important; }
  .st-key-mnav [data-testid^="stBaseButton"] { width: 100% !important; border: none !important; background: transparent !important; min-height: 48px !important; padding: 4px 0 !important; color: var(--text-2) !important; }
  .st-key-mnav [data-testid^="stBaseButton"] > div, .st-key-mnav [data-testid^="stBaseButton"] > div > span { flex-direction: column !important; align-items: center !important; gap: 2px !important; min-width: 0; max-width: 100%; }
  .st-key-mnav [data-testid="stIconMaterial"] { font-size: 20px !important; margin: 0 !important; }
  .st-key-mnav p { font-size: 10px !important; line-height: 13px !important; letter-spacing: -0.01em; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 100%; padding: 0 2px; }
  .page-title, .detail-title { font-size: 22px; line-height: 30px; }
  .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .stat { padding: 14px 16px; } .stat .v { font-size: 20px; line-height: 28px; }
  .stat .v, .stat .n { white-space: normal; }   /* wrap instead of cutting off key facts */
  [class*="st-key-jr_"] { padding: 14px 16px !important; }
  [class*="st-key-jr_"] > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"], [class*="st-key-jr_"] > [data-testid="stHorizontalBlock"] { flex-wrap: wrap !important; gap: 10px !important; }
  [class*="st-key-jr_"] [data-testid="stColumn"]:first-child { flex: 1 1 100% !important; }
  [class*="st-key-jr_"] [data-testid="stColumn"]:last-child { margin-left: 54px; }
  .co-row { grid-template-columns: minmax(0, 1fr) auto; }
  .co-row > :nth-child(2), .co-row > :nth-child(4), .co-row > :nth-child(5), .list-head { display: none; }
  [class*="st-key-cr_"] { padding: 12px 16px !important; }
  .kv { grid-template-columns: minmax(0, 1fr); gap: 2px; } .kv dd { margin-bottom: 10px; }
  .mon thead { display: none; }
  .mon, .mon tbody, .mon tr, .mon td { display: block; width: 100%; }
  .mon tr { padding: 12px 16px; border-bottom: 1px solid var(--border); }
  .mon td { border: none; padding: 2px 0; }
  .mon td[data-l]::before { content: attr(data-l) ": "; color: var(--muted); }
  .st-key-add_form, .st-key-email_form, .st-key-test_panel, [class*="st-key-set_"], .st-key-job_main, .st-key-job_side, .st-key-co_side, .st-key-empty { padding: 16px !important; }
}
"""

st.html(f"<style>{_root_vars}\n{_CSS}</style>")


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


def _next_slot(now: datetime | None = None) -> datetime:
    """The next slot of check_jobs.yml's cron (minute 0 of every 3rd UTC hour).
    GitHub Actions only *schedules* at these times — runs usually start later
    (measured Sep 2026: median 5 h apart, up to 9 h), so this is shown as a
    scheduled time, never as a countdown."""
    now = now or datetime.now(timezone.utc)
    return now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=3 - now.hour % 3)


def _ms(name: str, size: str = "") -> str:
    return f'<span class="ms {size}" aria-hidden="true">{name}</span>'


def _initial(name: str) -> str:
    name = (name or "").strip()
    return escape(name[:1].upper()) if name[:1].isalnum() else "•"


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
    """Ask GitHub Actions to run check_jobs.yml right now (workflow_dispatch)."""
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
    """Apply one change to the latest copy of ``name`` (merge-safe)."""
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
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+$")
_CHECK_COOLDOWN_S = 300
# cron asks for every 3 h, but GitHub starts scheduled runs late: measured
# gaps were median 5 h, 90th percentile 7.5 h, max 9 h — so a scan only
# counts as "delayed" once it is overdue by more than that
_STALE_H = 10
SCHEDULE_NOTE = "scheduled every 3 hours; GitHub usually starts runs later, typically 3–8 hours apart"
HOME_LIMIT = 8
JOBS_PAGE = 12
_CATEGORY_PILL = {"FRESHER": "fresher", "ENTRY_LEVEL": "entry"}
LEGACY_LABEL = "Keyword match"  # records from before the classifier existed

for _k, _v in (("toast", None), ("toast_kind", "success"), ("jobs_page", 0), ("test_status", None),
               ("email_val", settings.get("recipient_email", "")), ("confirm_remove", None),
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
recipient = (settings.get("recipient_email") or "").strip()


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
st.html(f"<style>.st-key-nav_{current} [data-testid^='stBaseButton'], .st-key-nav_{current} [data-testid^='stBaseButton']:hover, "
        f".st-key-mob_{current} [data-testid^='stBaseButton'], .st-key-mob_{current} [data-testid^='stBaseButton']:hover"
        "{background: var(--accent-soft) !important; color: var(--accent-text) !important;}"
        f".st-key-nav_{current} [data-testid='stIconMaterial'], .st-key-mob_{current} [data-testid='stIconMaterial']"
        "{color: var(--accent-text) !important;}"
        f".st-key-nav_{current} p {{font-weight: 600 !important;}}</style>")

with st.sidebar:
    logo = _logo_data_uri()
    st.html(f"""<div class="brand">{f'<img src="{logo}" alt="">' if logo else ''}
      <div><div class="n">Fresher Job Tracker</div><div class="s">Entry-level job radar</div></div></div>""")
    for gi, (group, items) in enumerate(NAV_GROUPS):
        st.html(f'<div class="nav-group{" first" if gi == 0 else ""}">{group}</div>')
        for key in items:
            label, icon = PAGES[key]
            st.button(label, key=f"nav_{key}", icon=icon, type="tertiary", use_container_width=True,
                      on_click=go, args=(key,))
    with st.container(key="side_foot_wrap"):
        st.html(f"""<div class="side-foot">
          <div class="st" title="{escape(overall[1], quote=True)}"><span class="dot {overall[0]}"></span><span class="txt">{_plural(len(companies), 'company', 'companies')} monitored</span></div>
          <div class="txt" style="margin-top:4px;">{escape(overall[1])}</div>
          <div class="txt num" style="margin-top:2px;">Last scan {_ago(last_scan, NOW)}</div>
          <div class="txt" style="margin-top:10px;"><a href="https://github.com/{GITHUB_REPO}" target="_blank" rel="noopener">Source on GitHub</a></div>
        </div>""")

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
def page_header(title: str, sub_html: str = "", actions=None):
    with st.container(key="page_head"):
        c1, c2 = st.columns([3, 2], vertical_alignment="bottom")
        with c1:
            st.html(f'<h1 class="page-title">{escape(title)}</h1>' + (f'<div class="page-sub">{sub_html}</div>' if sub_html else ""))
        with c2:
            if actions:
                with st.container(key="page_actions"):
                    actions()


def status_line() -> str:
    return (f'<span class="dot {overall[0]}"></span><span>{escape(overall[1])}</span><span class="sep">·</span>'
            f'<span class="num">Last scan {_ago(last_scan, NOW)}</span><span class="sep">·</span>'
            f'<span class="num">Next scheduled {_next_slot():%H:%M} UTC</span>')


def scan_actions():
    a, b = st.columns(2)
    with a:
        if st.button("Refresh", key="btn_refresh", icon=":material/refresh:", help="Reload the latest jobs and portal status"):
            _remote_snapshot.clear()
            toast("Showing the latest data", "success")
            st.rerun()
    with b:
        if st.button("Run check", key="btn_run_check", icon=":material/play_arrow:",
                     help="Ask GitHub Actions to scan every portal now (takes a few minutes)"):
            since = time.time() - st.session_state.get("last_check_trigger", 0)
            if since < _CHECK_COOLDOWN_S:
                toast(f"A check was just started — try again in {int((_CHECK_COOLDOWN_S - since) // 60) + 1} min", "error")
            else:
                ok, msg = _trigger_scrape()
                if ok:
                    st.session_state.last_check_trigger = time.time()
                toast(msg, "success" if ok else "error")
            st.rerun()


def apply_link(j: dict, label: str = "Apply") -> str:
    url = safe_url(j.get("url", ""))
    if not url:
        return '<span class="btn disabled">No link</span>'
    return (f'<a class="btn primary" href="{escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">'
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


def job_row(j: dict, idx: str, origin: str):
    jkey = job_key(j)
    raw_title = (j.get("title") or "Untitled posting").strip() or "Untitled posting"
    company = (j.get("company") or "").strip()
    pill_cls, pill_label = job_category(j)
    meta = [f'<span class="co">{escape(company)}</span>'] if company else []
    if j.get("location"):
        meta.append(f'<span>{escape(j["location"])}</span>')
    d = found[id(j)]
    if d or j.get("date"):
        meta.append(f'<span class="num" title="{escape(j.get("date") or "", quote=True)} UTC">Found {_ago(d, NOW) if d else escape(j["date"])}</span>')
    sep = '<span class="sep">·</span>'
    with st.container(key=f"jr_{idx}"):
        c1, c2 = st.columns([5, 2], vertical_alignment="center")
        with c1:
            st.html(f"""
            <div class="job">
              <div class="logo">{_initial(company)}</div>
              <div class="job-body">
                <h3 class="job-title" title="{escape(raw_title, quote=True)}">{escape(raw_title)}</h3>
                <div class="job-meta">{sep.join(meta) or '&nbsp;'}</div>
                <div class="job-why"><span class="pill {pill_cls}">{escape(pill_label)}</span><span class="t">{escape(job_why(j))}</span></div>
              </div>
            </div>""")
        with c2:
            st.html(apply_link(j))
            st.button("Details", key=_widget_key("crit", jkey), on_click=go, args=("jobs",),
                      kwargs={"job": _jid(j), "origin": origin})
            if j.get("dismissed"):
                if st.button("Restore", icon=":material/undo:", key=_widget_key("restore", jkey),
                             help="Restore to active jobs (it won't be emailed again)"):
                    _, saved, err = _save_change("seen_jobs.json", _restore_jobs({jkey}), [], "chore: restore 1 alert(s)")
                    toast("Job restored" if saved else f"Restored here, but not saved permanently. {err}",
                          "success" if saved else "error")
                    st.rerun()
            elif st.button("Dismiss", icon=":material/close:", key=_widget_key("dismiss", jkey),
                           help="Dismiss — hide this job (it won't be emailed again)"):
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
def page_home():
    page_header("Discover jobs", status_line(), scan_actions)

    st.html(f"""
    <div class="panel st-key-stats"><div class="stats">
      <div class="stat"><div class="k">Active jobs</div><div class="v">{len(active_jobs)}</div>
        <div class="n">{_plural(len(dismissed_jobs), 'dismissed job') if dismissed_jobs else 'Fresher &amp; entry-level'}</div></div>
      <div class="stat"><div class="k">New this week</div><div class="v">{new_this_week}</div><div class="n">Found in the last 7 days</div></div>
      <div class="stat"><div class="k">Companies monitored</div><div class="v">{len(companies)}</div>
        <div class="n">{" · ".join(f"{n_by_status[k]} {k}" for k in ("healthy", "delayed", "failing", "pending") if n_by_status[k]) or "—"}</div></div>
      <div class="stat"><div class="k">Last scan</div><div class="v">{_ago(last_scan, NOW)}</div>
        <div class="n">Next scheduled {_next_slot():%H:%M} UTC</div></div>
    </div></div>""")

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

    st.html('<div><h2 class="section-title">Latest jobs</h2>'
            '<p class="section-sub">Newest fresher and entry-level roles found on the portals you track</p></div>')
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
    st.html(f"""
    <div class="detail-head">
      <div class="logo lg">{_initial(company)}</div>
      <div style="min-width:0;">
        <h1 class="detail-title">{escape(title)}</h1>
        <div class="page-sub"><span class="text-2" style="font-weight:500;">{escape(company or 'Unknown company')}</span>
          {f'<span class="sep">·</span><span>{escape(j["location"])}</span>' if j.get("location") else ''}
          <span class="sep">·</span><span class="pill {pill_cls}">{escape(pill_label)}</span>
          {'<span class="pill neutral">Dismissed</span>' if j.get('dismissed') else ''}</div>
      </div>
    </div>""")
    with st.container(key="job_actions"):
        a = st.columns(4)
        with a[0]:
            st.html(apply_link(j, "Apply on company site"))
        with a[1]:
            if j.get("dismissed"):
                if st.button("Restore", key="detail_restore", icon=":material/undo:"):
                    _, saved, err = _save_change("seen_jobs.json", _restore_jobs({jkey}), [], "chore: restore 1 alert(s)")
                    toast("Job restored" if saved else f"Restored here, but not saved permanently. {err}",
                          "success" if saved else "error")
                    st.rerun()
            elif st.button("Dismiss", key="detail_dismiss", icon=":material/close:"):
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
            rows = [("Company", escape(company or "—")),
                    ("Location", escape(j.get("location") or "Not captured for this posting")),
                    ("Category", escape(pill_label)),
                    ("Experience", experience),
                    ("Found", f'<span class="num">{escape(j.get("date") or "—")} UTC</span>' + (f' <span class="muted">({_ago(d, NOW)})</span>' if d else "")),
                    ("Status", "Dismissed" if j.get("dismissed") else "Active")]
            st.html('<h2 class="section-title">Job overview</h2><dl class="kv">'
                    + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in rows) + "</dl>")
            st.html('<div class="divider"></div>')
            reason = str(j.get("reason") or "").strip()
            classified = bool(reason) or j.get("category") in _CATEGORY_PILL
            st.html(f"""<h2 class="section-title">Why this matched</h2>
              <div class="why">{_ms("check_circle", "s20")}<div>{escape(job_why(j, long=True))}
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
                items = "".join(f'<li>{"✓" if ok else "✗"} {escape(labels.get(k, k))}</li>' for k, ok in checks.items())
                quote_lines = "".join(f'<div class="quote">{escape(x)}</div>' for x in exp_lines[:3])
                with st.expander(f"Evidence & safety checks — {passed} of {len(checks)} checks passed"):
                    st.html(f"""<dl class="kv">
                      <dt>Safety checks</dt><dd>{passed} of {len(checks)} checks passed</dd>
                      <dt>Job ID</dt><dd class="num">{escape(ev.get("job_id") or ats_job_id(j.get("url", "")) or "—")}</dd>
                      <dt>Detail matched by</dt><dd>{escape(ev.get("detail_match") or "—")}</dd>
                      <dt>Fresher evidence</dt><dd>{escape(ev.get("fresher_evidence") or "—")}</dd></dl>
                      {f'<div class="note" style="margin-top:8px;">From the posting:</div>{quote_lines}' if quote_lines else ''}
                      <ul class="note" style="margin:10px 0 0;padding-left:0;list-style:none;">{items}</ul>""")
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
            st.html('<h2 class="section-title">Source</h2><dl class="kv" style="grid-template-columns:110px minmax(0,1fr);">'
                    + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in src) + "</dl>")
            st.html('<div class="divider"></div>')
            if c:
                k, lbl = statuses.get(c.get("id"), ("pending", "Pending"))
                curl = safe_url(c.get("url", ""))
                co = [("Monitoring", f'<span class="pill {k}"><i></i>{lbl}</span>'),
                      ("Career portal", f'<a class="link" href="{escape(curl, quote=True)}" target="_blank" rel="noopener">{escape(_short_url(curl, 30))}</a>' if curl else "—"),
                      ("Last checked", f'<span class="num">{_ago(_parse_iso(c.get("last_checked", "")), NOW)}</span>'),
                      ("Active jobs", str(company_active(c)))]
                st.html('<h2 class="section-title">Company</h2><dl class="kv" style="grid-template-columns:110px minmax(0,1fr);">'
                        + "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in co) + "</dl>")
            else:
                st.html('<h2 class="section-title">Company</h2><p class="note">This company is no longer tracked.</p>')


def page_companies():
    def _acts():
        st.button("Add company", key="btn_open_add", type="primary", icon=":material/add:",
                  on_click=go, args=("companies",), kwargs={"view": "add"})
    page_header("Companies", f"{_plural(len(companies), 'career portal')} monitored · checks {SCHEDULE_NOTE}", _acts)
    if not companies:
        empty_state("apartment", "No companies yet", "Add a company's career page and the tracker will start checking it on the next scan.")
        return
    f1, f2 = st.columns([2, 3], vertical_alignment="center")
    with f1:
        q = st.text_input("Search companies", key="co_q", placeholder="Search companies", label_visibility="collapsed").strip().lower()
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
                      <div class="co-name"><div class="logo">{_initial(name)}</div>
                        <div style="min-width:0;"><div class="t">{escape(name)}{tags}</div>
                        <div class="h">{escape(_short_url(curl)) if curl else '<span style="color:var(--red)">Invalid career page URL</span>'}</div></div></div>
                      <div class="co-cell" style="overflow:hidden;text-overflow:ellipsis;">{f'<a class="link" href="{escape(site, quote=True)}" target="_blank" rel="noopener">{escape(_short_url(site, 28))}</a>' if site else '<span class="muted">Not set</span>'}</div>
                      <div><span class="pill {k}"><i></i>{lbl}</span></div>
                      <div class="co-cell">{company_active(c)}<span class="l"> active jobs</span></div>
                      <div class="co-cell muted">{_ago(_parse_iso(c.get("last_checked", "")), NOW)}</div>
                    </div>""")
                with r2:
                    st.button("View", key=f"view_{_widget_key('c', str(c.get('id')))}", icon=":material/chevron_right:",
                              icon_position="right", on_click=go, args=("companies",), kwargs={"company": c.get("id")})


def page_add_company():
    st.button("Back to Companies", key="btn_back", icon=":material/arrow_back:", type="tertiary",
              on_click=go, args=("companies",))
    page_header("Add a company", "Start monitoring a company's career portal for fresher and entry-level roles")
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
        if not n:
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
    st.html(f"""<div class="detail-head"><div class="logo lg">{_initial(name)}</div><div style="min-width:0;">
      <h1 class="detail-title">{escape(name)}</h1>
      <div class="page-sub"><span class="pill {k}"><i></i>{lbl}</span>
        {'<span class="tag">Core</span>' if c.get('locked') else ''}
        <span class="sep">·</span><span>{company_active(c)} active · {sum(1 for j in jobs if j.get("dismissed"))} dismissed</span></div></div></div>""")
    with st.container(key="co_actions"):
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
                        st.session_state.confirm_remove = c.get("id")
                        st.rerun()
    if st.session_state.confirm_remove == c.get("id"):
        with st.container(key="danger_confirm"):
            st.html(f'<p class="note" style="color:var(--text);">Stop tracking <b>{escape(name)}</b>? Jobs already found stay in your history.</p>')
            y, n_ = st.columns([1, 4])
            with y:
                if st.button("Yes, stop tracking", key="btn_remove", type="primary"):
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
                      ("Last checked", f'<span class="num">{_ago(checked, NOW)}</span>' + (f' <span class="muted num">({checked:%b %d, %H:%M} UTC)</span>' if checked else "")),
                      ("Scraper", escape(_source_label(c))),
                      ("Latest scan", escape(c.get("last_job") or "No matching job on the last scan"))]
            if k == "failing":
                why = escape(c.get("status_reason") or "The last scan failed.")
                health.append(("Problem", f'{why} <a class="link" href="{ACTIONS_URL}" target="_blank" rel="noopener">See the Actions log</a>'))
            st.html('<h2 class="section-title">Portal health</h2><dl class="kv" style="grid-template-columns:110px minmax(0,1fr);">'
                    + "".join(f"<dt>{a}</dt><dd>{b}</dd>" for a, b in health) + "</dl>")
            st.html('<div class="divider"></div>')
            cats = {"FRESHER": 0, "ENTRY_LEVEL": 0, "legacy": 0}
            for j in jobs:
                cats[j.get("category") if j.get("category") in _CATEGORY_PILL else "legacy"] += 1
            info = [("Career portal", f'<a class="link" href="{escape(curl, quote=True)}" target="_blank" rel="noopener">{escape(_short_url(curl, 30))}</a>' if curl else "Invalid URL"),
                    ("Website", f'<a class="link" href="{escape(site, quote=True)}" target="_blank" rel="noopener">{escape(_short_url(site, 30))}</a>' if site else '<span class="muted">Not set</span>'),
                    ("Classified", f"{cats['FRESHER']} fresher · {cats['ENTRY_LEVEL']} entry level"
                                   + (f" · {cats['legacy']} keyword match" if cats["legacy"] else ""))]
            st.html('<h2 class="section-title">Details</h2><dl class="kv" style="grid-template-columns:110px minmax(0,1fr);">'
                    + "".join(f"<dt>{a}</dt><dd>{b}</dd>" for a, b in info) + "</dl>")
    with main:
        st.html(f'<div><h2 class="section-title">Recent jobs from {escape(name)}</h2>'
                f'<p class="section-sub">{_plural(len(jobs), "job")} found · {sum(1 for j in jobs if j.get("dismissed"))} dismissed</p></div>')
        if jobs:
            with st.container(key="joblist_company"):
                for i, j in enumerate(jobs[:10]):
                    job_row(j, f"c{i}", "companies")
        else:
            empty_state("inbox", "No jobs found yet", "Matching fresher and entry-level roles from this portal will appear here.")


def page_monitoring():
    page_header("Monitoring", f"The scraper runs on GitHub Actions — {SCHEDULE_NOTE} — and checks every tracked portal", scan_actions)
    st.html(f"""
    <div class="panel st-key-stats"><div class="stats">
      <div class="stat"><div class="k"><span class="dot {overall[0]}"></span>Overall</div><div class="v" style="font-size:20px;">{escape(overall[1])}</div>
        <div class="n">{_plural(len(companies), 'portal')} tracked</div></div>
      <div class="stat"><div class="k">Last scan</div><div class="v">{_ago(last_scan, NOW)}</div>
        <div class="n num">{f"{last_scan:%b %d, %H:%M} UTC" if last_scan else "No scan recorded"}</div></div>
      <div class="stat"><div class="k">Next scheduled scan</div><div class="v">{_next_slot():%H:%M} UTC</div><div class="n">GitHub may start it later</div></div>
      <div class="stat"><div class="k">Portals</div><div class="v">{n_by_status['healthy']}<span class="muted" style="font-size:16px;font-weight:500;"> / {len(companies)} healthy</span></div>
        <div class="n">{n_by_status['delayed']} delayed · {n_by_status['failing']} failing · {n_by_status['pending'] + n_by_status['checking']} pending</div></div>
    </div></div>""")
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
          <td class="c">{escape((c.get('name') or '').strip() or 'Unnamed')}
            <div class="sub">{f'<a class="link" href="{escape(curl, quote=True)}" target="_blank" rel="noopener">{escape(_short_url(curl, 34))}</a>' if curl else 'Invalid URL'}</div></td>
          <td data-l="Status"><span class="pill {k}"><i></i>{lbl}</span></td>
          <td class="num" data-l="Last checked">{_ago(checked, NOW)}</td>
          <td class="num" data-l="Active jobs">{company_active(c)}</td>
          <td data-l="Scraper">{escape(_source_label(c))}</td>
          <td data-l="Notes">{note}</td></tr>""")
    with st.container(key="mon_table"):
        st.html('<table class="mon"><thead><tr><th>Company</th><th>Status</th><th>Last checked</th><th>Active jobs</th>'
                '<th>Scraper</th><th>Notes</th></tr></thead><tbody>' + "".join(rows) + "</tbody></table>")
    st.html(f'<p class="note">Statuses come from the scraper: <b>Healthy</b> = scanned successfully in the last {_STALE_H} hours · '
            f'<b>Delayed</b> = no successful scan for {_STALE_H}+ hours · <b>Failing</b> = the last scan partly failed '
            f'(errors, rate limits or unreadable job pages) · <b>Broken</b> = the job list could not be read (blocked, HTTP error, '
            f'site changed) · <b>Needs configuration</b> = the URL shows no job postings · '
            f'<b>Pending</b> = not scanned yet. Full run logs: <a class="link" href="{ACTIONS_URL}" target="_blank" rel="noopener">GitHub Actions</a>.</p>')


def page_email():
    notified = [j for j in all_records if j.get("notified")]
    # when the email actually went out (older records only know when the job was found)
    sent_times = [(_parse_iso(j.get("notified_at") or ""), found[id(j)]) for j in notified]
    last_mail, last_is_sent = max(((a or b, bool(a)) for a, b in sent_times if a or b), default=(None, False))
    pending = [j for j in all_records if alert_pending(j)]
    page_header("Email & Notifications", (
        f'<span class="pill {"on" if recipient else "off"}"><i></i>{"Alerts on" if recipient else "Alerts off"}</span>'
        + (f'<span>Sending to {escape(recipient)}</span>' if recipient else '<span>Add a recipient to turn alerts on</span>')))

    def _set_recipient(addr: str) -> tuple[bool, str]:
        def _mutate(latest: dict) -> dict:
            latest = latest if isinstance(latest, dict) else {}
            latest["recipient_email"] = addr
            return latest
        data, saved, err = _save_change("settings.json", _mutate, {}, "chore: update recipient email")
        settings.update(data)
        return saved, err

    with st.container(key="email_form"):
        st.html('<div><h2 class="section-title">Recipient</h2><p class="section-sub">Where new-job alerts are delivered.</p></div>')
        i1, i2 = st.columns([4, 1], vertical_alignment="bottom")
        with i1:
            email_val = st.text_input("Recipient email", value=st.session_state.email_val, placeholder="you@example.com",
                                      key="email_input")
            st.session_state.email_val = email_val
        with i2:
            save = st.button("Save", key="btn_save_email", type="primary", use_container_width=True)
        synced = bool(os.environ.get("GITHUB_TOKEN"))
        if email_val.strip() and email_val.strip() != recipient:
            st.html(f'<p class="note" style="color:var(--amber);">{_ms("edit", "s16")} Not saved yet — alerts still go to '
                    f'{escape(recipient) or "nobody"} until you press Save.</p>')
        st.html(f'<p class="note">{_ms("cloud_done" if synced else "cloud_off", "s16")} '
                + ("Saved to settings.json in the GitHub repository, which the scraper reads on every run."
                   if synced else "No GitHub token — changes are saved on this server only and the scraper won't see them.")
                + "</p>")
    if save:
        e = email_val.strip()
        if not _EMAIL_RE.match(e):
            toast("Enter a valid email address", "error")
        else:
            saved, err = _set_recipient(e)
            toast(f"Saved — alerts go to {e}" if saved else f"Saved here, but not saved permanently. {err}",
                  "success" if saved else "error")
            st.rerun()

    with st.container(key="test_panel"):
        ts = st.session_state.test_status
        status = ("Not sent this session" if ts is None else
                  f"Sent at {ts[1]} UTC" if ts[0] else f"Failed at {ts[1]} UTC")
        t1, t2 = st.columns([4, 1], vertical_alignment="center")
        with t1:
            st.html(f'<div><h2 class="section-title">Test delivery</h2><p class="section-sub">Send a sample alert to confirm emails arrive. '
                    f'<span class="num" style="color:var({"--green" if ts and ts[0] else "--red" if ts else "--muted"});">{status}</span></p></div>')
        with t2:
            test = st.button("Send test email", key="btn_test", icon=":material/send:", use_container_width=True)
    if test:
        to = email_val.strip()
        stamp = datetime.now(timezone.utc).strftime("%H:%M")
        if not _EMAIL_RE.match(to):
            toast("Enter a valid recipient email first", "error")
        else:
            # a test never changes where real alerts go — that's what Save is for
            unsaved = (settings.get("recipient_email") or "").strip() != to
            try:
                from notifier import test_mail
                test_mail(to)
            except Exception as ex:
                log.warning("test mail failed: %s", ex)
                st.session_state.test_status = (False, stamp)
                toast("Email isn't configured for this app (Gmail address / app password missing)." if "GMAIL_" in str(ex)
                      else "Couldn't send the test email. Check the Gmail app password and try again.", "error")
            else:
                st.session_state.test_status = (True, stamp)
                toast(f"Test email sent to {to}. This address is not saved as the alert recipient — press Save to use it."
                      if unsaved else f"Test email sent — check {to}", "success")
        st.rerun()

    with st.container(key="set_behavior"):
        rows = [("Last alert", f'<span class="num">{"Sent" if last_is_sent else "Covered a job found"} {last_mail:%b %d, %H:%M} UTC</span> <span class="muted">({_ago(last_mail, NOW)})</span>'
                 if last_mail else "No alert emails recorded yet"),
                ("Jobs emailed", str(len(notified))),
                ("Waiting to send", f"{len(pending)} — sent after the next check" if pending else "0")]
        st.html('<div><h2 class="section-title">Delivery</h2></div><dl class="kv">'
                + "".join(f"<dt>{a}</dt><dd>{b}</dd>" for a, b in rows) + "</dl>")
        st.html("""<div class="divider"></div><div><h2 class="section-title">How alerts work</h2></div>
          <ul class="note" style="margin:0;padding-left:18px;display:flex;flex-direction:column;gap:6px;">
            <li>After every check (scheduled every 3 hours; GitHub often starts runs later), jobs found for the first time are sent in one email.</li>
            <li>Only fresher and entry-level jobs in India whose own posting was read are emailed — never a guess from a title.</li>
            <li>Each job is emailed at most once. Dismissed jobs are never emailed, and stay in the history so they're never sent again.</li>
            <li>If sending fails, the jobs stay unsent and the next check retries. A job is recorded before its email goes out, so a failed save can't cause a duplicate.</li>
            <li>Mail is sent through the Gmail account configured in the repository's GitHub Actions secrets — credentials are never shown here.</li>
          </ul>""")


def page_settings():
    page_header("Settings", "Preferences and read-only details of how the tracker is configured")
    with st.container(key="set_appearance"):
        a1, a2 = st.columns([4, 1], vertical_alignment="center")
        with a1:
            st.html('<div><h2 class="section-title">Appearance</h2><p class="section-sub">'
                    f'{"Dark" if st.session_state.dark_mode else "Light"} theme · kept in this page’s address, so it survives reloads and bookmarks</p></div>')
        with a2:
            if st.button("Use light theme" if st.session_state.dark_mode else "Use dark theme", key="btn_theme",
                         icon=":material/light_mode:" if st.session_state.dark_mode else ":material/dark_mode:",
                         use_container_width=True):
                st.session_state.dark_mode = not st.session_state.dark_mode
                st.rerun()
    with st.container(key="set_data"):
        synced = bool(os.environ.get("GITHUB_TOKEN"))
        d1, d2 = st.columns([4, 1], vertical_alignment="center")
        with d1:
            st.html('<div><h2 class="section-title">Data &amp; sync</h2><p class="section-sub">'
                    + (f"Live data from github.com/{GITHUB_REPO}, cached for up to 5 minutes."
                       if synced else "Reading the files bundled with this deployment — no GitHub token is configured.")
                    + "</p></div>")
        with d2:
            if st.button("Reload data", key="btn_reload", icon=":material/refresh:", use_container_width=True):
                _remote_snapshot.clear()
                toast("Showing the latest data", "success")
                st.rerun()
    with st.container(key="set_engine"):
        by_source: dict[str, list[str]] = {}
        for c in companies:
            by_source.setdefault(_source_label(c), []).append((c.get("name") or "").strip() or "Unnamed")
        rows = [("Schedule", 'Scheduled every 3 hours (cron <span class="num">0 */3 * * *</span>, set in .github/workflows/check_jobs.yml). '
                             'GitHub Actions starts scheduled runs late under load — typically 3–8 hours apart.'),
                ("Roles kept", "Fresher and entry-level roles located in India"),
                ("Scrapers", "; ".join(f"{escape(src)} for {escape(', '.join(sorted(names)))}"
                                       for src, names in sorted(by_source.items())) or "No portals yet"),
                ("Detail pages", "Each candidate job's own posting is read before it is classified; "
                                 "if it can't be read the job is not emailed and is retried next scan")]
        st.html('<div><h2 class="section-title">Scanning</h2><p class="section-sub">Defined in the repository; change them there.</p></div>'
                '<dl class="kv">' + "".join(f"<dt>{a}</dt><dd>{b}</dd>" for a, b in rows) + "</dl>")
    with st.container(key="set_maint"):
        st.html('<div><h2 class="section-title">Maintenance</h2><p class="section-sub">Dismiss every active job at once. '
                'They stay in the history (Jobs → Dismissed) and are never emailed again.</p></div>')
        if not st.session_state.confirm_clear:
            m1, _m2 = st.columns([1, 3])
            with m1:
                with st.container(key="danger"):
                    if st.button(f"Dismiss all {len(active_jobs)} jobs", key="btn_clear_all", disabled=not active_jobs,
                                 use_container_width=True):
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
                        _, saved, err = _save_change("seen_jobs.json", dismiss_jobs(None), [], "chore: dismiss all alerts")
                        toast("All alerts cleared" if saved else f"Cleared here, but not saved permanently. {err}",
                              "success" if saved else "error")
                        st.rerun()
                with n_:
                    if st.button("Cancel", key="btn_clear_all_cancel"):
                        st.session_state.confirm_clear = False
                        st.rerun()
    st.html(f"""<div class="app-foot"><span>Fresher Job Tracker</span>
      <a href="https://github.com/{GITHUB_REPO}" target="_blank" rel="noopener">Source on GitHub</a></div>""")


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

# ── toast ─────────────────────────────────────────────────────────────────────
# Shown once and faded out with CSS; a sleep()+rerun would freeze the app.
if st.session_state.toast:
    msg = escape(st.session_state.toast, quote=False)
    ok = st.session_state.toast_kind == "success"
    st.session_state.toast = None
    st.html(f"""
<style>
@keyframes jt-toast {{ 0%, 85% {{ opacity: 1; }} 100% {{ opacity: 0; visibility: hidden; }} }}
.jt-toast {{ animation: jt-toast {4 if ok else 6}s ease-in forwards; }}
@media (max-width: 640px) {{ .jt-toast {{ left: 16px; right: 16px; bottom: 16px; max-width: none !important; }} }}
</style>
<div class="jt-toast" role="status" style="position:fixed;bottom:24px;right:24px;z-index:9999;display:flex;align-items:center;gap:10px;padding:12px 16px;border-radius:12px;background:var(--surface);border:1px solid var(--border);box-shadow:var(--shadow-lg);max-width:380px;">
  <span style="display:flex;color:var({'--green' if ok else '--red'});">{_ms('check_circle' if ok else 'error', 's20')}</span>
  <div style="font-size:14px;line-height:20px;color:var(--text);">{msg}</div>
</div>""")
