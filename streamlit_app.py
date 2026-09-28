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
    dismiss_jobs,
    fetch_remote_json,
    friendly_github_error,
    job_key,
    update_json,
    visible_jobs,
)
from notifier import category_label, friendly_reason, safe_url

log = logging.getLogger("streamlit_app")

BASE = Path(__file__).parent
GITHUB_REPO = "manojanumolu/job-tracker"
LOGO_PATH = BASE / "assets" / "logo.png"

st.set_page_config(
    page_title="Fresher Job Tracker",
    page_icon=str(LOGO_PATH) if LOGO_PATH.exists() else "💼",
    layout="wide",
    initial_sidebar_state="collapsed",
)

if "dark_mode" not in st.session_state:
    st.session_state.dark_mode = False

# ── theme tokens (Stitch "Fresher Job Tracker Redesign" palette) ─────────────
LIGHT = {
    "bg": "#faf8ff", "lowest": "#ffffff", "low": "#f2f3ff", "container": "#eaedff",
    "high": "#e2e7ff", "highest": "#dae2fd",
    "on-surface": "#131b2e", "variant": "#464555", "outline": "#777587", "outline-variant": "#c7c4d8",
    "primary": "#3525cd", "primary-btn": "#3525cd", "primary-btn-hover": "#4f46e5", "on-primary": "#ffffff",
    "primary-fixed": "#e2dfff", "on-primary-fixed": "#0f0069",
    "secondary": "#006c49", "secondary-container": "#6cf8bb", "on-secondary-container": "#00714d",
    "tertiary-fixed": "#ffddb8", "on-tertiary-fixed": "#2a1700", "amber": "#885500",
    "error": "#ba1a1a", "error-container": "#ffdad6", "on-error-container": "#93000a",
    "shadow": "0 1px 2px 0 rgba(0,0,0,0.05)",
    "shadow-md": "0 4px 6px -1px rgba(0,0,0,0.1), 0 2px 4px -2px rgba(0,0,0,0.1)",
    "header-bg": "rgba(250,248,255,0.85)",
    "glow-a": "rgba(53,37,205,0.05)", "glow-b": "rgba(0,108,73,0.10)",
}
DARK = {
    "bg": "#0e1220", "lowest": "#161b2b", "low": "#1b2133", "container": "#232a40",
    "high": "#2b3450", "highest": "#343e5c",
    "on-surface": "#e6e8f5", "variant": "#b9bbcf", "outline": "#8e90a6", "outline-variant": "#464555",
    "primary": "#c3c0ff", "primary-btn": "#4f46e5", "primary-btn-hover": "#6159f0", "on-primary": "#ffffff",
    "primary-fixed": "#2e2a6b", "on-primary-fixed": "#e2dfff",
    "secondary": "#4edea3", "secondary-container": "#005236", "on-secondary-container": "#6ffbbe",
    "tertiary-fixed": "#653e00", "on-tertiary-fixed": "#ffddb8", "amber": "#ffb95f",
    "error": "#ffb4ab", "error-container": "#93000a", "on-error-container": "#ffdad6",
    "shadow": "0 1px 2px 0 rgba(0,0,0,0.35)",
    "shadow-md": "0 4px 6px -1px rgba(0,0,0,0.4), 0 2px 4px -2px rgba(0,0,0,0.3)",
    "header-bg": "rgba(14,18,32,0.85)",
    "glow-a": "rgba(195,192,255,0.07)", "glow-b": "rgba(78,222,163,0.07)",
}
TH = DARK if st.session_state.dark_mode else LIGHT
_root_vars = ":root {" + "".join(f"--{k}:{v};" for k, v in TH.items()) + "}"

# NOTE: inline <svg> doesn't paint in this app's hosting environment, so every
# icon is a Material Symbols ligature (the font Streamlit itself bundles, with
# Stitch's Outlined cut preferred when Google Fonts is reachable). The .ms box
# is fixed-width with overflow hidden, so a font failure never spills words.
# Widgets that need styling are wrapped in st.container(key=...) and targeted
# via .st-key-*; raw HTML tags are never opened and closed across st.* calls.
_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Manrope:wght@600;700;800&display=swap');
@import url('https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:opsz,wght,FILL,GRAD@20..48,400,0..1,0&display=block');

:root {
  --font-head: 'Manrope', 'Inter', ui-sans-serif, system-ui, -apple-system, 'Segoe UI', sans-serif;
  --font-body: 'Inter', ui-sans-serif, system-ui, -apple-system, 'Segoe UI', sans-serif;
  --font-mono: ui-monospace, SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace;
}

/* ── Streamlit chrome ── */
#MainMenu, footer:not(.site-footer), header[data-testid="stHeader"] { display: none !important; }
.stDeployButton, [data-testid="stToolbar"], [data-testid="stDecoration"] { display: none !important; }
section[data-testid="stSidebar"], [data-testid="collapsedControl"] { display: none !important; }
.block-container, [data-testid="stMainBlockContainer"] { padding: 0 !important; max-width: 100% !important; }
.stApp, [data-testid="stAppViewContainer"], .stMain { background: var(--bg) !important; }
.stApp { font-family: var(--font-body); color: var(--on-surface); -webkit-font-smoothing: antialiased; }
.stApp p, .stApp label, .stApp input, .stApp button, .stApp textarea { font-family: var(--font-body); }
[data-testid="stElementContainer"]:has(> .stHtml:empty) { display: none; }

/* ── icons ── */
.ms {
  font-family: 'Material Symbols Outlined', 'Material Symbols Rounded' !important;
  font-weight: normal; font-style: normal; line-height: 1; letter-spacing: normal;
  text-transform: none; white-space: nowrap; direction: ltr; -webkit-font-smoothing: antialiased;
  font-feature-settings: 'liga'; font-variation-settings: 'FILL' 0, 'wght' 400, 'GRAD' 0, 'opsz' 24;
  display: inline-block; overflow: hidden; flex-shrink: 0; vertical-align: middle;
  font-size: 18px; width: 1em; height: 1em;
}
.ms.s12 { font-size: 12px; } .ms.s14 { font-size: 14px; } .ms.s15 { font-size: 15px; }
.ms.s16 { font-size: 16px; } .ms.s18 { font-size: 18px; } .ms.s20 { font-size: 20px; } .ms.s28 { font-size: 28px; }
[data-testid="stIconMaterial"] { font-family: 'Material Symbols Rounded' !important; }

/* ── type scale (Stitch tokens) ── */
.t-display { font-family: var(--font-head); font-size: 48px; line-height: 56px; letter-spacing: -0.02em; font-weight: 700; }
.t-hsm { font-family: var(--font-head); font-size: 20px; line-height: 28px; letter-spacing: -0.01em; font-weight: 600; }
.t-title { font-family: var(--font-head); font-size: 16px; line-height: 24px; letter-spacing: -0.005em; font-weight: 600; }
.t-body-lg { font-size: 16px; line-height: 26px; }
.t-body-md { font-size: 14px; line-height: 22px; }
.t-body-sm { font-size: 13px; line-height: 18px; letter-spacing: 0.005em; }
.t-label-md { font-size: 12px; line-height: 16px; letter-spacing: 0.02em; font-weight: 600; }
.t-label-sm { font-size: 11px; line-height: 14px; letter-spacing: 0.04em; font-weight: 600; }
.t-badge { font-size: 10px; line-height: 12px; letter-spacing: 0.06em; font-weight: 700; text-transform: uppercase; }
.mono { font-family: var(--font-mono) !important; }
.c-variant { color: var(--variant); } .c-outline { color: var(--outline); } .c-primary { color: var(--primary); }
.c-secondary { color: var(--secondary); } .c-surface { color: var(--on-surface); } .c-error { color: var(--error); }
.c-amber { color: var(--amber); }
.dot { width: 8px; height: 8px; border-radius: 999px; display: inline-block; flex-shrink: 0; }
.dot.sm { width: 6px; height: 6px; }
.bg-secondary { background: var(--secondary); } .bg-outline { background: var(--outline); }
.bg-primary { background: var(--primary); } .bg-error { background: var(--error); } .bg-amber { background: var(--amber); }
@keyframes jt-pulse { 50% { opacity: .45; } }
@keyframes jt-ping { 75%, 100% { transform: scale(2); opacity: 0; } }
.pulse { animation: jt-pulse 2s cubic-bezier(.4,0,.6,1) infinite; }
.ping { position: relative; }
.ping::after { content: ""; position: absolute; inset: 0; border-radius: inherit; background: inherit; animation: jt-ping 1.4s cubic-bezier(0,0,.2,1) infinite; }
.stApp a, .btn-apply, .btn-add, .hdr-icon { text-decoration: none !important; }
.truncate { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }

/* ── generic cards ── */
.card, .st-key-jobs_toolbar, [class*="st-key-jc_"], .st-key-portals_card, .st-key-add_card, .st-key-ns_card,
.st-key-jobs_empty {
  background: var(--lowest) !important; border-radius: 12px !important; box-shadow: var(--shadow) !important;
}

/* ── layout wrappers ── */
.st-key-page_wrap { max-width: 1280px; margin: 0 auto; padding: 8px 32px 48px; gap: 24px !important; }
:is(.st-key-main_grid, .st-key-main_grid > [data-testid="stLayoutWrapper"]) > [data-testid="stHorizontalBlock"] { gap: 24px !important; }
:is(.st-key-main_grid, .st-key-main_grid > [data-testid="stLayoutWrapper"]) > [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:first-child { flex: 8 1 0 !important; width: auto !important; min-width: 0 !important; }
:is(.st-key-main_grid, .st-key-main_grid > [data-testid="stLayoutWrapper"]) > [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:last-child { flex: 4 1 0 !important; width: auto !important; min-width: 0 !important; }
.st-key-feed_col, .st-key-rail_col { gap: 16px !important; }
.st-key-job_stream { gap: 8px !important; }

/* ── header ── */
.st-key-app_header {
  position: sticky; top: 0; z-index: 50; background: var(--header-bg) !important;
  backdrop-filter: blur(24px); -webkit-backdrop-filter: blur(24px);
  box-shadow: 0 1px 8px rgba(0,0,0,0.04);
  padding: 0 max(32px, calc((100% - 1280px) / 2 + 32px)) !important;
}
:is(.st-key-app_header, .st-key-app_header > [data-testid="stLayoutWrapper"]) > [data-testid="stHorizontalBlock"] { flex-wrap: nowrap !important; gap: 4px !important; align-items: center !important; min-height: 64px; }
.st-key-app_header [data-testid="stColumn"] { flex: 0 0 auto !important; width: auto !important; min-width: 0 !important; }
.st-key-app_header [data-testid="stColumn"]:first-child { flex: 1 1 auto !important; }
.st-key-app_header [data-testid="stColumn"]:nth-child(2) { margin-right: 4px; }
.hdr-brand { display: flex; align-items: center; gap: 16px; min-width: 0; }
.hdr-logo { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
.hdr-logo img { height: 32px; width: 32px; border-radius: 8px; display: block; }
.hdr-logo .name { font-family: var(--font-head); font-size: 16px; font-weight: 600; letter-spacing: -0.025em; line-height: 1; color: var(--on-surface); }
.hdr-logo .sub { font-size: 11px; line-height: 1; margin-top: 4px; color: var(--variant); letter-spacing: .04em; }
.hdr-nav { display: flex; align-items: center; gap: 16px; margin-left: 16px; }
.hdr-nav a { font-size: 14px; line-height: 22px; color: var(--variant) !important; transition: color .15s; white-space: nowrap; }
.hdr-nav a:hover { color: var(--on-surface) !important; }
.hdr-nav a.active { color: var(--primary) !important; font-weight: 600; }
.hdr-pills { display: flex; align-items: center; gap: 4px; }
.pill { display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px; border-radius: 999px; white-space: nowrap; }
.pill.mon { background: var(--container); color: var(--secondary); }
.pill.chk { background: var(--low); color: var(--variant); gap: 4px; }
.pill.nxt { background: var(--lowest); color: var(--variant); padding: 4px 8px; }
.pill.warn { background: var(--tertiary-fixed); color: var(--on-tertiary-fixed); }
.hdr-icon {
  width: 34px; height: 34px; border-radius: 8px; display: inline-flex; align-items: center; justify-content: center;
  background: var(--lowest); color: var(--variant) !important; transition: background .15s, color .15s;
}
.hdr-icon:hover { background: var(--high); color: var(--on-surface) !important; }
.st-key-app_header [data-testid^="stBaseButton"] {
  width: 34px !important; height: 34px !important; min-height: 34px !important; padding: 0 !important;
  border-radius: 8px !important; border: none !important; background: var(--lowest) !important;
  color: var(--variant) !important; box-shadow: none !important;
}
.st-key-app_header [data-testid^="stBaseButton"]:hover { background: var(--high) !important; color: var(--on-surface) !important; }
.st-key-app_header [data-testid^="stBaseButton"] [data-testid="stIconMaterial"] { font-size: 18px !important; }
.st-key-app_header [data-testid="stElementContainer"] { width: auto !important; }

/* ── hero ── */
.hero { position: relative; overflow: hidden; border-radius: 12px; background: var(--low); padding: 32px; box-shadow: var(--shadow); }
.hero .glow-a { position: absolute; top: -96px; right: -96px; width: 384px; height: 384px; border-radius: 999px; background: var(--glow-a); filter: blur(64px); pointer-events: none; }
.hero .glow-b { position: absolute; bottom: -80px; left: 33%; width: 320px; height: 320px; border-radius: 999px; background: var(--glow-b); filter: blur(40px); pointer-events: none; }
.hero-inner { position: relative; z-index: 1; display: flex; flex-direction: column; gap: 16px; }
.hero-top { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px; }
.status-badge { display: inline-flex; align-items: center; gap: 8px; padding: 4px 12px; border-radius: 999px; text-transform: uppercase; font-weight: 700; letter-spacing: .05em; }
.status-badge.ok { background: var(--secondary-container); color: var(--on-secondary-container); }
.status-badge.warn { background: var(--tertiary-fixed); color: var(--on-tertiary-fixed); }
.status-badge.idle { background: var(--container); color: var(--variant); }
.engine-chip { display: inline-flex; align-items: center; gap: 6px; padding: 4px 12px; border-radius: 999px; background: var(--lowest); color: var(--variant); box-shadow: var(--shadow); }
.hero h1 { color: var(--on-surface); margin: 0; padding: 0; }
.hero h1 .accent { color: var(--primary); text-decoration: underline wavy; text-decoration-color: color-mix(in srgb, var(--primary) 22%, transparent); text-underline-offset: 6px; text-decoration-thickness: 2px; }
.hero p.lead { color: var(--variant); margin: 8px 0 0; max-width: 672px; }
.strip { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; padding-top: 4px; }
.strip-item { display: flex; align-items: center; gap: 12px; padding: 12px; border-radius: 8px; background: var(--lowest); box-shadow: var(--shadow); min-width: 0; }
.strip-ic { width: 32px; height: 32px; border-radius: 999px; display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
.strip-ic.a { background: var(--high); color: var(--primary); }
.strip-ic.b { background: var(--secondary-container); color: var(--secondary); }
.strip-ic.c { background: var(--high); color: var(--secondary); }
.strip-ic.d { background: var(--primary-fixed); color: var(--primary); }
.strip-ic.w { background: var(--tertiary-fixed); color: var(--amber); }
.strip-k { text-transform: uppercase; color: var(--variant); }
.strip-v { color: var(--on-surface); }

/* ── metric cards ── */
.metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 16px; }
.metric { padding: 16px; display: flex; flex-direction: column; justify-content: space-between; gap: 8px; transition: box-shadow .2s; min-width: 0; }
.metric:hover { box-shadow: var(--shadow-md) !important; }
.metric-top { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.metric-mid { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; min-width: 0; }
.metric-num { font-family: var(--font-head); font-size: 48px; line-height: 1; font-weight: 700; letter-spacing: -0.02em; color: var(--on-surface); }
.metric-num.mono { letter-spacing: -0.04em; }
.tag-active { padding: 2px 8px; border-radius: 4px; background: var(--secondary-container); color: var(--secondary); }
.tag-warn { padding: 2px 8px; border-radius: 4px; background: var(--error-container); color: var(--on-error-container); }
.bar-track { width: 100%; height: 6px; border-radius: 999px; background: var(--container); overflow: hidden; }
.bar-fill { height: 6px; border-radius: 999px; background: var(--primary); }
.spark { display: flex; align-items: flex-end; gap: 3px; height: 24px; }
.spark span { flex: 1; border-radius: 2px 2px 0 0; background: var(--primary); min-height: 2px; opacity: .85; }
.spark span.zero { background: var(--container); opacity: 1; }

/* ── feed toolbar ── */
.st-key-jobs_toolbar { padding: 16px !important; gap: 8px !important; }
:is(.st-key-jobs_toolbar, .st-key-jobs_toolbar > [data-testid="stLayoutWrapper"]) > [data-testid="stHorizontalBlock"] { align-items: center !important; gap: 8px !important; }
:is(.st-key-jobs_toolbar, .st-key-jobs_toolbar > [data-testid="stLayoutWrapper"]) > [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:first-child { flex: 1 1 auto !important; width: auto !important; min-width: 0 !important; }
:is(.st-key-jobs_toolbar, .st-key-jobs_toolbar > [data-testid="stLayoutWrapper"]) > [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:last-child { flex: 0 0 260px !important; width: 260px !important; min-width: 240px !important; }
.st-key-job_search [data-baseweb="input"]::before {
  content: "search"; font-family: 'Material Symbols Outlined', 'Material Symbols Rounded'; font-size: 18px;
  width: 18px; overflow: hidden; color: var(--outline); padding-left: 12px; align-self: center; line-height: 1;
}
.st-key-job_search input { padding-left: 8px !important; }
.st-key-job_filter_wrap { border-top: 1px solid var(--container); padding-top: 8px; }
.st-key-job_filter_wrap [data-testid="stButtonGroup"] > div { gap: 8px !important; flex-wrap: wrap; }
.st-key-job_filter_wrap :is([data-testid^="stBaseButton-pills"], button[data-variant="pills"]) {
  border: none !important; border-radius: 999px !important; padding: 4px 12px !important; min-height: 0 !important;
  background: var(--container) !important; color: var(--variant) !important; box-shadow: none !important;
}
.st-key-job_filter_wrap :is([data-testid^="stBaseButton-pills"], button[data-variant="pills"]) p { font-size: 12px !important; line-height: 16px !important; font-weight: 500 !important; letter-spacing: .02em; color: inherit !important; }
.st-key-job_filter_wrap :is([data-testid^="stBaseButton-pills"], button[data-variant="pills"]):hover { color: var(--on-surface) !important; }
.st-key-job_filter_wrap :is([data-testid="stBaseButton-pillsActive"], button[data-variant="pills"][aria-checked="true"]) {
  background: var(--primary-btn) !important; color: var(--on-primary) !important; box-shadow: var(--shadow) !important;
}
.st-key-job_filter_wrap :is([data-testid="stBaseButton-pillsActive"], button[data-variant="pills"][aria-checked="true"]) p { font-weight: 600 !important; }

/* ── job cards ── */
[class*="st-key-jc_"] { padding: 16px !important; gap: 8px !important; transition: box-shadow .2s; overflow: hidden; }
[class*="st-key-jc_"]:hover { box-shadow: var(--shadow-md) !important; }
[class*="st-key-jc_"]:hover .job-title { color: var(--primary); }
[class*="st-key-jc_"] [data-testid="stHorizontalBlock"] { flex-wrap: nowrap !important; gap: 8px !important; }
[class*="st-key-jc_"] [data-testid="stColumn"] { flex: 0 0 auto !important; width: auto !important; min-width: 0 !important; }
[class*="st-key-jc_"] [data-testid="stColumn"]:first-child { flex: 1 1 auto !important; }
.job-head { display: flex; align-items: flex-start; gap: 16px; min-width: 0; }
.avatar { width: 48px; height: 48px; border-radius: 12px; background: var(--high); color: var(--primary); display: flex; align-items: center; justify-content: center; font-family: var(--font-head); font-size: 24px; font-weight: 700; flex-shrink: 0; }
.badges { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-bottom: 4px; }
.badge { display: inline-flex; align-items: center; gap: 4px; padding: 2px 8px; border-radius: 999px; }
.badge.fresher { padding: 2px 10px; background: var(--secondary-container); color: var(--on-secondary-container); }
.badge.entry { background: var(--primary-fixed); color: var(--on-primary-fixed); }
.badge.neutral { background: var(--container); color: var(--variant); }
.badge.strong { background: var(--high); color: var(--on-surface); }
.job-title { color: var(--on-surface); transition: color .15s; margin: 0; overflow-wrap: anywhere; }
.job-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 12px; color: var(--variant); margin-top: 4px; }
.job-meta .co { font-weight: 600; color: var(--on-surface); }
.job-meta .loc { display: inline-flex; align-items: center; gap: 4px; }
.reason { padding: 12px; border-radius: 8px; background: var(--low); color: var(--variant); display: flex; align-items: center; gap: 8px; margin: 0; }
.job-foot { display: flex; flex-wrap: nowrap; align-items: center; gap: 8px; color: var(--outline); min-height: 30px; }
.btn-apply {
  display: inline-flex; align-items: center; gap: 4px; height: 30px; padding: 0 16px; border-radius: 8px; white-space: nowrap;
  background: var(--primary-btn); color: var(--on-primary) !important; box-shadow: var(--shadow); transition: background .15s, box-shadow .15s;
}
.btn-apply:hover { background: var(--primary-btn-hover); box-shadow: var(--shadow-md); }
.btn-apply.off { background: var(--container); color: var(--outline) !important; box-shadow: none; }
[class*="st-key-jc_"] [data-testid="stBaseButton-secondary"] { padding: 6px 12px !important; min-height: 30px !important; }
[class*="st-key-jc_"] [data-testid="stBaseButton-tertiary"] {
  width: 32px !important; height: 32px !important; min-height: 32px !important; padding: 0 !important; border-radius: 8px !important;
  color: var(--outline) !important; background: transparent !important; border: none !important;
}
[class*="st-key-jc_"] [data-testid="stBaseButton-tertiary"]:hover { color: var(--error) !important; background: var(--container) !important; }

/* ── empty state ── */
.st-key-jobs_empty { padding: 32px !important; align-items: center; text-align: center; gap: 8px !important; }
.empty-ic { width: 56px; height: 56px; border-radius: 999px; background: var(--container); color: var(--outline); display: inline-flex; align-items: center; justify-content: center; }
.empty { display: flex; flex-direction: column; align-items: center; gap: 8px; }
.empty h4 { color: var(--on-surface); margin: 0; }
.empty p { color: var(--variant); max-width: 448px; margin: 0; }

/* ── pager ── */
:is(.st-key-jobs_pager, .st-key-jobs_pager > [data-testid="stLayoutWrapper"]) > [data-testid="stHorizontalBlock"] { align-items: center !important; gap: 8px !important; flex-wrap: nowrap !important; }
.st-key-jobs_pager [data-testid="stColumn"] { flex: 1 1 0 !important; width: auto !important; min-width: 0 !important; }
.pager-label { text-align: center; color: var(--variant); padding: 6px 0; }

/* ── right rail cards ── */
.st-key-portals_card, .st-key-add_card, .st-key-ns_card { padding: 16px !important; gap: 8px !important; }
.card-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.card-head .lh { display: flex; align-items: center; gap: 8px; min-width: 0; }
.card-head h2, .card-head h3 { margin: 0; color: var(--on-surface); }
.count-chip { padding: 2px 8px; border-radius: 999px; background: var(--container); font-family: var(--font-mono); font-size: 11px; font-weight: 600; color: var(--on-surface); }
.btn-add { display: inline-flex; align-items: center; gap: 4px; padding: 6px 12px; border-radius: 8px; background: var(--primary-btn); color: var(--on-primary) !important; transition: background .15s; }
.btn-add:hover { background: var(--primary-btn-hover); }
.sq-ic { width: 32px; height: 32px; border-radius: 8px; display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
.sq-ic.a { background: var(--high); color: var(--primary); }
.sq-ic.b { background: var(--secondary-container); color: var(--secondary); }
.portal-list { display: flex; flex-direction: column; padding-top: 4px; }
.portal { padding: 8px 0 6px; display: flex; flex-direction: column; min-width: 0; }
.portal + .portal { border-top: 1px solid var(--low); }
.portal-name { display: flex; align-items: center; gap: 8px; min-width: 0; }
.portal-name .t-title { line-height: 1.25; color: var(--on-surface); }
.state { display: inline-flex; align-items: center; gap: 4px; font-weight: 500; }
.core { font-size: 9.5px; letter-spacing: .06em; font-weight: 700; color: var(--outline); border: 1px solid var(--outline-variant); border-radius: 4px; padding: 0 5px; line-height: 14px; }
.portal a.host { color: var(--variant) !important; display: inline-flex; align-items: center; gap: 4px; max-width: 100%; transition: color .15s; }
.portal a.host:hover { color: var(--primary) !important; }
.portal .pmeta { display: flex; align-items: center; gap: 8px; color: var(--outline); margin-top: 2px; min-width: 0; }
.card-head p, .card-sub { margin: 0; color: var(--variant); }
.field-label { display: block; color: var(--on-surface); margin: 0 0 4px; }
.fake-select { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 8px 12px; border-radius: 8px; background: var(--low); color: var(--on-surface); }
.hint { color: var(--outline); margin: 4px 0 0; }
.sync { display: flex; align-items: center; gap: 6px; }
.divider { border-top: 1px solid var(--low); margin: 4px 0 0; }
.row-between { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
:is(.st-key-ns_card, .st-key-ns_card > [data-testid="stLayoutWrapper"]) > [data-testid="stHorizontalBlock"] { gap: 8px !important; flex-wrap: nowrap !important; align-items: flex-end !important; }
:is(.st-key-ns_card, .st-key-ns_card > [data-testid="stLayoutWrapper"]) > [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:first-child { flex: 1 1 auto !important; min-width: 0 !important; width: auto !important; }
:is(.st-key-ns_card, .st-key-ns_card > [data-testid="stLayoutWrapper"]) > [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:last-child { flex: 0 0 auto !important; width: auto !important; min-width: 0 !important; }
.st-key-ns_card [data-testid="stBaseButton-secondary"] { min-height: 38px !important; }
.st-key-remove_row { gap: 4px !important; padding-top: 4px; border-top: 1px solid var(--low); }
.st-key-remove_row [data-testid="stHorizontalBlock"] { gap: 8px !important; }

/* ── widgets ── */
.stTextInput label p, .stCheckbox label p { font-size: 11px !important; line-height: 14px !important; letter-spacing: .04em; font-weight: 600 !important; color: var(--on-surface) !important; }
.stCheckbox label p { font-size: 13px !important; letter-spacing: 0; font-weight: 500 !important; }
.stTextInput [data-baseweb="input"], .stTextInput [data-baseweb="base-input"] {
  background: var(--low) !important; border: none !important; border-radius: 8px !important;
}
.stTextInput [data-baseweb="input"] * { background-color: transparent !important; }
.stTextInput [data-baseweb="input"] { border: 1px solid transparent !important; transition: box-shadow .15s, background .15s; }
.stTextInput [data-baseweb="input"]:focus-within { background: var(--lowest) !important; box-shadow: 0 0 0 2px color-mix(in srgb, var(--primary) 20%, transparent) !important; border-color: transparent !important; }
.stTextInput input { font-size: 13px !important; line-height: 18px !important; color: var(--on-surface) !important; padding: 8px 12px !important; background: transparent !important; -webkit-text-fill-color: var(--on-surface); }
.stTextInput input::placeholder { color: var(--outline) !important; -webkit-text-fill-color: var(--outline); opacity: 1; }
[data-testid="InputInstructions"] { display: none !important; }

[data-testid^="stBaseButton"] {
  border-radius: 8px !important; box-shadow: none !important; min-height: 34px !important; padding: 6px 12px !important;
  transition: background .15s, color .15s, box-shadow .15s !important;
}
[data-testid^="stBaseButton"] [data-testid="stMarkdownContainer"] { display: flex !important; align-items: center; padding: 0 !important; margin: 0 !important; min-height: 0 !important; }
[data-testid^="stBaseButton"] p { margin: 0 !important; padding: 0 !important; font-size: 12px !important; line-height: 16px !important; font-weight: 600 !important; letter-spacing: .02em; }
[data-testid="stBaseButton-secondary"] { background: var(--container) !important; color: var(--on-surface) !important; border: none !important; }
[data-testid="stBaseButton-secondary"]:hover { background: var(--high) !important; color: var(--on-surface) !important; }
[data-testid="stBaseButton-secondary"]:disabled { opacity: .45 !important; }
[data-testid="stBaseButton-primary"] { background: var(--primary-btn) !important; color: var(--on-primary) !important; border: none !important; }
[data-testid="stBaseButton-primary"]:hover { background: var(--primary-btn-hover) !important; }
[data-testid="stBaseButton-tertiary"] { color: var(--primary) !important; }
.st-key-test_mail_btn [data-testid="stBaseButton-secondary"] { color: var(--primary) !important; }
.st-key-test_mail_btn [data-testid="stBaseButton-secondary"] p { color: var(--primary) !important; }

/* ── dialog ── */
[data-testid="stDialog"] [role="dialog"] { background: var(--lowest) !important; border-radius: 12px !important; color: var(--on-surface); }
[data-testid="stDialog"] h2, [data-testid="stDialog"] [data-testid="stHeading"] * { font-family: var(--font-head) !important; color: var(--on-surface) !important; }
.dlg { display: flex; flex-direction: column; gap: 8px; }
.dlg .ok { padding: 12px; border-radius: 8px; background: color-mix(in srgb, var(--secondary-container) 40%, transparent); color: var(--on-secondary-container); display: flex; align-items: center; gap: 8px; }
.dlg .muted-box { padding: 12px; border-radius: 8px; background: var(--low); }
.dlg ul { margin: 0; padding-left: 18px; color: var(--variant); }
.dlg li { margin: 2px 0; }

/* ── footer ── */
.site-footer { background: var(--low); margin-top: 8px; }
.site-footer .in { max-width: 1280px; margin: 0 auto; padding: 24px 32px; display: flex; align-items: center; justify-content: space-between; gap: 16px; flex-wrap: wrap; }
.site-footer .l { display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px; color: var(--variant); }
.site-footer a { color: var(--outline) !important; }
.site-footer a:hover { color: var(--primary) !important; }

/* ── responsive (Stitch breakpoints: xl 1280, lg 1024, md 768, sm 640) ── */
@media (max-width: 1279px) { .hdr-pills { display: none; } }
@media (max-width: 1023px) {
  .hdr-nav { display: none; }
  .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  :is(.st-key-main_grid, .st-key-main_grid > [data-testid="stLayoutWrapper"]) > [data-testid="stHorizontalBlock"] { flex-wrap: wrap !important; }
  :is(.st-key-main_grid, .st-key-main_grid > [data-testid="stLayoutWrapper"]) > [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:nth-child(n) { flex: 1 1 100% !important; width: 100% !important; }
}
@media (max-width: 767px) {
  .st-key-page_wrap { padding: 4px 16px 32px; gap: 16px !important; }
  .st-key-app_header { padding: 0 16px !important; }
  .hero { padding: 24px; }
  .t-display { font-size: 32px; line-height: 40px; letter-spacing: -0.01em; }
  .strip { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .site-footer .in { padding: 24px 16px; justify-content: center; text-align: center; }
  .site-footer .l { justify-content: center; }
}
@media (max-width: 639px) {
  .metrics { grid-template-columns: minmax(0, 1fr); }
  .hdr-logo .sub { display: none; }
  :is(.st-key-jobs_toolbar, .st-key-jobs_toolbar > [data-testid="stLayoutWrapper"]) > [data-testid="stHorizontalBlock"] { flex-wrap: wrap !important; }
  :is(.st-key-jobs_toolbar, .st-key-jobs_toolbar > [data-testid="stLayoutWrapper"]) > [data-testid="stHorizontalBlock"] > [data-testid="stColumn"]:nth-child(n) { flex: 1 1 100% !important; width: 100% !important; min-width: 0 !important; }
  [class*="st-key-jc_"] .job-head { gap: 12px; }
  [class*="st-key-jc_"] .avatar { width: 40px; height: 40px; font-size: 20px; border-radius: 10px; }
  .strip-item { padding: 10px; gap: 8px; }
  .foot-host { display: none; }
}
@media (max-width: 420px) { .strip { grid-template-columns: minmax(0, 1fr); } }
"""

st.html(f"<style>{_root_vars}\n{_CSS}</style>")


# ── helpers ──────────────────────────────────────────────────────────────────
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


def _short_url(url: str) -> str:
    """"https://www.accenture.com/us-en/careers" -> "accenture.com/us-en/careers"."""
    try:
        p = urlparse(url)
    except ValueError:
        return url
    path = p.path.rstrip("/")
    return (p.netloc.replace("www.", "") + (path if len(path) <= 28 else path[:27] + "…")) or url


def _parse_iso(iso: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(iso)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _ago(dt: datetime | None, now: datetime | None = None) -> str:
    if dt is None:
        return "—"
    diff = int(((now or datetime.now(timezone.utc)) - dt).total_seconds())
    if diff < 60:
        return "just now"
    if diff < 3600:
        return f"{diff // 60}m ago"
    if diff < 86400:
        return f"{diff // 3600}h ago"
    return f"{diff // 86400}d ago"


def _rel_time(iso: str) -> str:
    return _ago(_parse_iso(iso)) if iso else "—"


def _found_at(date_str: str, now: datetime) -> datetime | None:
    """The scraper stamps jobs as "Jul 15, 13:45" (UTC, no year). Assume the
    most recent such date that isn't in the future."""
    try:
        dt = datetime.strptime(f"{now.year} {date_str}", "%Y %b %d, %H:%M").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return dt.replace(year=now.year - 1) if dt > now + timedelta(days=1) else dt


def _next_check_delta() -> str:
    """check_jobs.yml runs at minute 0 of every 3rd UTC hour (0, 3, 6, …)."""
    now = datetime.now(timezone.utc)
    nxt = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=3 - now.hour % 3)
    mins = int((nxt - now).total_seconds() // 60)
    return f"{mins}m" if mins < 60 else f"{mins // 60}h {mins % 60:02d}m"


def _ms(name: str, size: str = "", cls: str = "") -> str:
    return f'<span class="ms {size} {cls}" aria-hidden="true">{name}</span>'


@st.cache_resource(show_spinner=False)
def _logo_data_uri() -> str:
    try:
        return "data:image/png;base64," + base64.b64encode(LOGO_PATH.read_bytes()).decode("ascii")
    except OSError:
        return ""


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


def _widget_key(prefix: str, key: str) -> str:
    return f"{prefix}_{hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]}"


# ── load data ─────────────────────────────────────────────────────────────────
companies: list[dict] = [c for c in _load_synced("companies.json", []) if isinstance(c, dict)]
settings: dict = _load_synced("settings.json", {"recipient_email": ""})
# seen_jobs.json is also the scraper's dedup history: alerts are dismissed
# (hidden), never deleted, or the scraper would email them again
all_records: list = _load_synced("seen_jobs.json", [])
seen_jobs: list[dict] = visible_jobs(all_records)  # newest-first

ALERTS_PAGE_SIZE = 10
SPARK_WEEKS = 12
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+$")
_CHECK_COOLDOWN_S = 300
# cron runs every 3 h; allow one missed/slow run before calling it delayed
_MONITOR_STALE_H = 7
_CATEGORY_CLASS = {"FRESHER": "fresher", "ENTRY_LEVEL": "entry"}


# ── session state ──────────────────────────────────────────────────────────────
if "email_val" not in st.session_state:
    st.session_state.email_val = settings.get("recipient_email", "")
for _k, _v in (("toast", None), ("toast_kind", "success"), ("alerts_page", 0),
               ("show_remove_form", False), ("test_status", None)):
    if _k not in st.session_state:
        st.session_state[_k] = _v
if st.session_state.pop("_clear_add_form", False):
    st.session_state.new_name = ""
    st.session_state.new_url = ""


def toast(msg: str, kind: str = "success"):
    st.session_state.toast = msg
    st.session_state.toast_kind = kind


# ── derived, real-data metrics ───────────────────────────────────────────────
NOW = datetime.now(timezone.utc)
total_companies = len(companies)
broken_count = sum(1 for c in companies if c.get("status") == "broken")
active_count = sum(1 for c in companies if c.get("status") == "active")
responding = total_companies - broken_count
last_checked_dt = max((d for d in (_parse_iso(c.get("last_checked", "")) for c in companies) if d), default=None)
last_checked_str = _ago(last_checked_dt, NOW) if last_checked_dt else "never"
checked_ok = sum(1 for c in companies if c.get("status") == "active" and c.get("last_checked"))
recipient_saved = (settings.get("recipient_email") or "").strip()
_check_trigger = st.session_state.get("last_check_trigger", 0)

jobs_found = [(j, _found_at(j.get("date") or "", NOW)) for j in seen_jobs]
total_jobs = len(seen_jobs)
new_this_week = sum(1 for _, d in jobs_found if d and NOW - d <= timedelta(days=7))
latest_found = max((d for _, d in jobs_found if d), default=None)
# weekly discoveries over every record (dismissed ones were still found)
weekly = [0] * SPARK_WEEKS
for rec in all_records:
    if isinstance(rec, dict):
        d = _found_at(rec.get("date") or "", NOW)
        if d:
            w = (NOW - d).days // 7
            if 0 <= w < SPARK_WEEKS:
                weekly[SPARK_WEEKS - 1 - w] += 1
jobs_per_company: dict[str, int] = {}
for j in seen_jobs:
    jobs_per_company[j.get("company") or ""] = jobs_per_company.get(j.get("company") or "", 0) + 1
cat_counts = {cat: sum(1 for j in seen_jobs if j.get("category") == cat) for cat in _CATEGORY_CLASS}


def _company_state(c: dict) -> tuple[str, str, str]:
    """(label, text-color class, dot class) from the scraper's real status."""
    status = c.get("status")
    checked = _parse_iso(c.get("last_checked", ""))
    if (_check_trigger and time.time() - _check_trigger < 1200 and status != "broken"
            and (checked is None or checked.timestamp() < _check_trigger)):
        return "Checking", "c-primary", "bg-primary pulse"
    if status == "broken":
        return "Error", "c-error", "bg-error"
    if status == "active":
        return "Active", "c-secondary", "bg-secondary"
    return "Waiting", "c-outline", "bg-outline"


# ═══════════════════════════════════════════════════════════════════════════════
# HEADER
# ═══════════════════════════════════════════════════════════════════════════════
monitor_stale = last_checked_dt is None or NOW - last_checked_dt > timedelta(hours=_MONITOR_STALE_H)
logo_uri = _logo_data_uri()
logo_img = f'<img src="{logo_uri}" alt="Fresher Job Tracker logo">' if logo_uri else ""

with st.container(key="app_header"):
    lc, pc, b0, b1, b2, b3 = st.columns([6, 4, 0.4, 0.4, 0.4, 0.4], vertical_alignment="center")
    with lc:
        st.html(f"""
        <div class="hdr-brand">
          <div class="hdr-logo">{logo_img}
            <div style="display:flex;flex-direction:column;"><span class="name">Fresher Job Tracker</span><span class="sub">Entry-level posting monitor</span></div>
          </div>
          <nav class="hdr-nav">
            <a class="active" href="#live-feed">Live Feed</a>
            <a href="#company-radar">Company Radar</a>
            <a href="#add-portal">Add Portal</a>
            <a href="#email-alerts">Email Alerts</a>
          </nav>
        </div>""")
    with pc:
        mon_pill = (f'<span class="pill mon t-label-sm"><span class="dot sm bg-secondary pulse"></span>Monitoring {total_companies} {"company" if total_companies == 1 else "companies"}</span>'
                    if not monitor_stale else
                    '<span class="pill warn t-label-sm"><span class="dot sm bg-amber"></span>Monitoring delayed</span>')
        st.html(f"""
        <div class="hdr-pills">
          {mon_pill}
          <span class="pill chk t-label-sm" title="When the scraper last refreshed this data">{_ms("check_circle", "s14", "c-secondary")}Checked {last_checked_str}</span>
          <span class="pill nxt t-label-sm" title="The scraper runs automatically every 3 hours"><span class="mono c-outline">Next ~{_next_check_delta()}</span></span>
        </div>""")
    with b0:
        if st.button("", icon=":material/refresh:", key="btn_refresh", help="Reload the latest jobs and company status"):
            _remote_snapshot.clear()
            toast("Showing the latest data", "success")
            st.rerun()
    with b1:
        if st.button("", icon=":material/play_arrow:", key="btn_run_check", help="Run a fresh check now (takes a few minutes)"):
            since = time.time() - st.session_state.get("last_check_trigger", 0)
            if since < _CHECK_COOLDOWN_S:
                toast(f"A check was just started — try again in {int((_CHECK_COOLDOWN_S - since) // 60) + 1} min", "error")
            else:
                ok, msg = _trigger_scrape()
                if ok:
                    st.session_state.last_check_trigger = time.time()
                toast(msg, "success" if ok else "error")
            st.rerun()
    with b2:
        theme_icon = ":material/light_mode:" if st.session_state.dark_mode else ":material/dark_mode:"
        if st.button("", icon=theme_icon, key="btn_theme", help="Toggle light / dark mode"):
            st.session_state.dark_mode = not st.session_state.dark_mode
            st.rerun()
    with b3:
        st.html(f'<a class="hdr-icon" href="https://github.com/{GITHUB_REPO}" target="_blank" rel="noopener" '
                f'title="View the source repository on GitHub">{_ms("code")}</a>')


# ═══════════════════════════════════════════════════════════════════════════════
# PAGE BODY
# ═══════════════════════════════════════════════════════════════════════════════
def _hero_html() -> str:
    if last_checked_dt is None:
        badge = '<span class="status-badge idle t-label-md"><span class="dot bg-outline"></span>Awaiting first check</span>'
    elif monitor_stale:
        badge = f'<span class="status-badge warn t-label-md"><span class="dot bg-amber"></span>Monitoring delayed · last run {last_checked_str}</span>'
    else:
        badge = '<span class="status-badge ok t-label-md"><span class="dot bg-secondary ping"></span>Autonomous monitoring active</span>'
    pass_note = f"({checked_ok}/{total_companies} OK)" if total_companies else ""
    email_on = bool(recipient_saved)
    return f"""
    <section class="hero">
      <div class="glow-a"></div><div class="glow-b"></div>
      <div class="hero-inner">
        <div class="hero-top">
          {badge}
          <span class="engine-chip t-label-sm">{_ms("verified", "s15", "c-primary")}Engine: Fresher classifier (Workday + Playwright)</span>
        </div>
        <div style="max-width:768px;">
          <h1 class="t-display">Your job search, <span class="accent">on autopilot.</span></h1>
          <p class="lead t-body-lg">Fresher Job Tracker checks every tracked company's career portal every 3 hours,
          screens each posting for fresher and entry-level eligibility, keeps only India roles,
          and emails you the moment a genuine match appears.</p>
        </div>
        <div class="strip">
          <div class="strip-item"><div class="strip-ic a">{_ms("corporate_fare")}</div>
            <div style="min-width:0;"><div class="strip-k t-label-sm">Radar scope</div><div class="strip-v t-title truncate">{total_companies} Portals Tracked</div></div></div>
          <div class="strip-item"><div class="strip-ic b">{_ms("schedule")}</div>
            <div style="min-width:0;"><div class="strip-k t-label-sm">Next cycle</div><div class="strip-v t-title mono truncate">~{_next_check_delta()}</div></div></div>
          <div class="strip-item"><div class="strip-ic {"c" if not monitor_stale else "w"}">{_ms("cloud_done" if not monitor_stale else "cloud_off")}</div>
            <div style="min-width:0;"><div class="strip-k t-label-sm">Last pass</div><div class="strip-v t-title truncate">{last_checked_str} {pass_note}</div></div></div>
          <div class="strip-item"><div class="strip-ic {"d" if email_on else "w"}">{_ms("mark_email_read" if email_on else "unsubscribe")}</div>
            <div style="min-width:0;"><div class="strip-k t-label-sm">Email alerts</div><div class="strip-v t-title truncate">{"On · recipient set" if email_on else "Off · no recipient"}</div></div></div>
        </div>
      </div>
    </section>"""


def _metrics_html() -> str:
    names = [escape((c.get("name") or "").strip() or "Unnamed") for c in companies]
    names_str = ", ".join(names[:3]) + (f" +{len(names) - 3}" if len(names) > 3 else "")
    health_pct = round(100 * responding / total_companies) if total_companies else 0
    all_ok = total_companies and broken_count == 0
    comp_tag = ('<span class="tag-active t-label-sm">Active</span>' if all_ok else
                f'<span class="tag-warn t-label-sm">{broken_count} failing</span>' if broken_count else
                '<span class="tag-warn t-label-sm">None</span>')
    peak = max(weekly) or 1
    bars = "".join(
        f'<span class="{"zero" if n == 0 else ""}" style="height:{max(8, round(100 * n / peak))}%;" '
        f'title="{n} found {SPARK_WEEKS - 1 - i}w ago"></span>' for i, n in enumerate(weekly))
    trend = (f'<span class="mono t-label-sm c-secondary" style="display:inline-flex;align-items:center;gap:4px;">{_ms("trending_up", "s12")}+{new_this_week} this week</span>'
             if new_this_week else '<span class="mono t-label-sm c-outline">0 this week</span>')
    health_word, health_cls = (("Healthy", "c-secondary") if health_pct == 100 else
                               ("Degraded", "c-amber") if health_pct >= 50 else ("Failing", "c-error"))
    if not total_companies:
        health_word, health_cls = "No portals", "c-outline"
    latest_note = f"Latest found {_ago(latest_found, NOW)}" if latest_found else "Nothing found yet"
    return f"""
    <section class="metrics">
      <div class="card metric">
        <div class="metric-top"><span class="t-label-md c-variant">Companies Tracked</span>{comp_tag}</div>
        <div class="metric-mid"><span class="metric-num">{total_companies}</span><span class="t-label-sm c-variant truncate" style="font-weight:400;">{names_str}</span></div>
        <div class="bar-track"><div class="bar-fill" style="width:{health_pct}%;"></div></div>
      </div>
      <div class="card metric">
        <div class="metric-top"><span class="t-label-md c-variant">Fresh Jobs Listed</span>{trend}</div>
        <div class="metric-mid"><span class="metric-num">{total_jobs}</span><span class="t-label-sm c-secondary">{sum(1 for v in jobs_per_company.values() if v)} {"company" if len(jobs_per_company) == 1 else "companies"}</span></div>
        <div class="spark" title="Jobs found per week, last {SPARK_WEEKS} weeks">{bars}</div>
      </div>
      <div class="card metric">
        <div class="metric-top"><span class="t-label-md c-variant">New This Week</span><span class="mono t-label-sm c-primary">Last 7 days</span></div>
        <div class="metric-mid"><span class="metric-num c-primary">{new_this_week}</span><span class="t-label-sm c-variant" style="font-weight:400;">of {total_jobs} listed</span></div>
        <div style="display:flex;align-items:center;gap:4px;"><span class="dot {"bg-secondary" if latest_found else "bg-outline"}"></span><span class="t-body-sm c-variant">{latest_note}</span></div>
      </div>
      <div class="card metric">
        <div class="metric-top"><span class="t-label-md c-variant">Scraper Pipeline</span>{_ms("bolt", "s16", "c-secondary" if all_ok else "c-amber")}</div>
        <div class="metric-mid"><span class="metric-num mono">{health_pct}%</span><span class="t-label-sm {health_cls}">{health_word}</span></div>
        <div class="t-body-sm c-variant truncate">{responding} of {total_companies} career pages responding</div>
      </div>
    </section>"""


def _job_head_html(j: dict, found: datetime | None) -> str:
    raw_title = (j.get("title") or "Untitled posting").strip() or "Untitled posting"
    company = (j.get("company") or "").strip()
    initial = company[:1].upper() if company[:1].isalnum() else "•"
    badges = []
    cat = j.get("category")
    # records written before the classifier existed have no category
    if cat in _CATEGORY_CLASS:
        dot = '<span class="dot sm bg-secondary pulse"></span>' if cat == "FRESHER" else ""
        badges.append(f'<span class="badge {_CATEGORY_CLASS[cat]} t-badge">{dot}{escape(category_label(j))}</span>')
    url = safe_url(j.get("url", ""))
    if "myworkdayjobs.com" in url:
        badges.append('<span class="badge neutral t-badge">Workday</span>')
    if j.get("notified"):
        badges.append(f'<span class="t-label-sm c-secondary" style="display:inline-flex;align-items:center;gap:4px;">{_ms("mark_email_read", "s14")}Emailed</span>')
    else:
        badges.append(f'<span class="t-label-sm c-amber" style="display:inline-flex;align-items:center;gap:4px;">{_ms("schedule_send", "s14")}Email pending</span>')
    meta = []
    if company:
        meta.append(f'<span class="co">{escape(company)}</span>')
    if j.get("location"):
        meta.append(f'<span class="loc">{_ms("location_on", "s15")}{escape(j["location"])}</span>')
    if j.get("date"):
        meta.append(f'<span class="mono c-outline">Found {escape(j["date"])}</span>')
    sep = '<span aria-hidden="true">·</span>'
    return f"""
    <div class="job-head">
      <div class="avatar">{escape(initial)}</div>
      <div style="min-width:0;">
        <div class="badges">{"".join(badges)}</div>
        <h3 class="job-title t-hsm" title="{escape(raw_title, quote=True)}">{escape(raw_title)}</h3>
        <div class="job-meta t-body-sm">{sep.join(meta) or "&nbsp;"}</div>
      </div>
    </div>"""


def _job_reason_html(j: dict) -> str:
    if j.get("reason") or j.get("category") in _CATEGORY_CLASS:
        return f'<p class="reason t-body-sm">{_ms("check_circle", "s18", "c-secondary")}<span>{escape(friendly_reason(j))}</span></p>'
    host = _host(safe_url(j.get("url", "")))
    text = f"Direct posting on {escape(host)}" if host else "Posting link unavailable"
    return f'<p class="reason t-body-sm">{_ms("link", "s18", "c-outline")}<span>{text}</span></p>'


def _job_foot_html(j: dict, found: datetime | None) -> str:
    url = safe_url(j.get("url", ""))
    host = _host(url)
    found_txt = f"Found {_ago(found, NOW)}" if found else "Found date unknown"
    host_txt = (f'<span class="foot-host" aria-hidden="true">·</span><span class="foot-host truncate">{escape(host)}</span>'
                if host else "")
    return (f'<div class="job-foot t-label-sm"><span class="dot {"bg-secondary" if url else "bg-outline"}"></span>'
            f'<span style="white-space:nowrap;">{found_txt}</span>{host_txt}</div>')


def _job_apply_html(j: dict) -> str:
    url = safe_url(j.get("url", ""))
    if not url:
        return '<span class="btn-apply off t-label-md">No link</span>'
    return (f'<a class="btn-apply t-label-md" href="{escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">'
            f'Apply Now {_ms("arrow_outward", "s14")}</a>')


@st.dialog("Eligibility details")
def _job_details(j: dict) -> None:
    title = (j.get("title") or "Untitled posting").strip() or "Untitled posting"
    items = [f"<li>Company: {escape(j.get('company') or '—')}</li>"]
    if j.get("location"):
        items.append(f"<li>Location: {escape(j['location'])}</li>")
    if j.get("date"):
        items.append(f"<li>Found: {escape(j['date'])} UTC</li>")
    items.append(f"<li>Email alert: {'sent' if j.get('notified') else 'pending'}</li>")
    host = _host(safe_url(j.get("url", "")))
    if host:
        items.append(f"<li>Portal: {escape(host)}</li>")
    if j.get("reason") or j.get("category") in _CATEGORY_CLASS:
        verdict = f'<div class="ok t-label-md">{_ms("verified_user", "s20", "c-secondary")}Classifier verdict: {escape(category_label(j))} — {escape(friendly_reason(j))}</div>'
    else:
        verdict = (f'<div class="muted-box t-body-sm c-variant">{_ms("history", "s18", "c-outline")} '
                   "Recorded before the classifier stored match reasons, so no eligibility trace is available.</div>")
    trace = (f'<div class="muted-box"><div class="t-label-sm c-outline mono" style="text-transform:uppercase;">Classifier reason trace</div>'
             f'<p class="t-body-sm c-surface" style="margin:4px 0 0;">{escape(j["reason"])}</p></div>') if j.get("reason") else ""
    st.html(f"""
    <div class="dlg">
      <h3 class="t-title c-surface" style="margin:0;">{escape(title)}</h3>
      {verdict}
      <ul class="t-body-sm">{"".join(items)}</ul>
      {trace}
      <div style="display:flex;justify-content:flex-end;padding-top:8px;">{_job_apply_html(j)}</div>
    </div>""")


with st.container(key="page_wrap"):
    st.html(_hero_html())
    st.html(_metrics_html())

    with st.container(key="main_grid"):
        col_feed, col_rail = st.columns([8, 4])

    # ── LEFT: verified listings ──────────────────────────────────────────────
    with col_feed:
        with st.container(key="feed_col"):
            with st.container(key="jobs_toolbar"):
                t1, t2 = st.columns([3, 2], vertical_alignment="center")
                with t1:
                    st.html(f"""
                    <div id="live-feed" style="scroll-margin-top:80px;">
                      <h2 class="t-hsm c-surface" style="margin:0;">Verified Fresher Listings</h2>
                      <p class="t-body-sm c-variant" style="margin:0;">{total_jobs} {"posting" if total_jobs == 1 else "postings"} · newest first · pulled directly from company career portals</p>
                    </div>""")
                with t2:
                    query = st.text_input("Search jobs", key="job_search", placeholder="Filter title, company, city…",
                                          label_visibility="collapsed").strip().lower()
                filter_opts = ["all", "FRESHER", "ENTRY_LEVEL"] + sorted(
                    (c for c in jobs_per_company if c), key=lambda c: (-jobs_per_company[c], c.lower()))
                filter_labels = {"all": f"All ({total_jobs})",
                                 "FRESHER": f"Fresher only ({cat_counts['FRESHER']})",
                                 "ENTRY_LEVEL": f"Entry level ({cat_counts['ENTRY_LEVEL']})"}
                if st.session_state.get("job_filter") not in filter_opts:
                    st.session_state.job_filter = "all"
                with st.container(key="job_filter_wrap"):
                    chosen = st.pills("Filter", filter_opts, key="job_filter", label_visibility="collapsed",
                                      format_func=lambda o: filter_labels.get(o, f"{o} ({jobs_per_company.get(o, 0)})"))
            chosen = chosen or "all"

            def _match(j: dict) -> bool:
                if chosen in _CATEGORY_CLASS and j.get("category") != chosen:
                    return False
                if chosen not in ("all", *_CATEGORY_CLASS) and (j.get("company") or "") != chosen:
                    return False
                if query:
                    hay = " ".join(str(j.get(k) or "") for k in ("title", "company", "location", "reason")).lower()
                    return query in hay
                return True

            filtered = [(j, d) for j, d in jobs_found if _match(j)]
            sig = (chosen, query)
            if st.session_state.get("_feed_sig") != sig:
                st.session_state._feed_sig = sig
                st.session_state.alerts_page = 0
            total = len(filtered)
            max_page = max(0, (total - 1) // ALERTS_PAGE_SIZE) if total else 0
            page = min(st.session_state.alerts_page, max_page)
            st.session_state.alerts_page = page
            start, end = page * ALERTS_PAGE_SIZE, page * ALERTS_PAGE_SIZE + ALERTS_PAGE_SIZE

            with st.container(key="job_stream"):
                if not filtered:
                    with st.container(key="jobs_empty"):
                        if total_jobs == 0:
                            st.html(f"""
                            <div class="empty">
                              <div class="empty-ic">{_ms("notifications", "s28")}</div>
                              <h4 class="t-hsm">No alerts yet</h4>
                              <p class="t-body-md">Keep monitoring — the tracker checks every portal every 3 hours and emails you the moment a new fresher role appears.</p>
                            </div>""")
                        else:
                            st.html(f"""
                            <div class="empty">
                              <div class="empty-ic">{_ms("search_off", "s28")}</div>
                              <h4 class="t-hsm">No matching jobs in this view</h4>
                              <p class="t-body-md">None of the {total_jobs} listed postings match this filter. New roles appear here as soon as a portal publishes them.</p>
                            </div>""")
                            def _reset_filters():
                                st.session_state.job_filter = "all"
                                st.session_state.job_search = ""
                            st.button("Reset filters", key="btn_reset_filters", on_click=_reset_filters)
                for i, (j, found) in enumerate(filtered[start:end]):
                    jkey = job_key(j)
                    with st.container(key=f"jc_{i}"):
                        h1c, h2c = st.columns([12, 1])
                        with h1c:
                            st.html(_job_head_html(j, found))
                        with h2c:
                            if st.button("", icon=":material/close:", key=_widget_key("dismiss", jkey), type="tertiary",
                                         help="Dismiss this alert (it won't be emailed again)"):
                                _, saved, err = _save_change("seen_jobs.json", dismiss_jobs({jkey}), [],
                                                             "chore: dismiss 1 alert(s)")
                                if saved:
                                    toast("Removed 1 alert(s)", "success")
                                else:
                                    toast(f"Removed here, but not saved permanently. {err}", "error")
                                st.rerun()
                        st.html(_job_reason_html(j))
                        f1, f2, f3 = st.columns([6, 1, 1], vertical_alignment="center")
                        with f1:
                            st.html(_job_foot_html(j, found))
                        with f2:
                            if st.button("View Criteria", key=_widget_key("crit", jkey)):
                                _job_details(j)
                        with f3:
                            st.html(_job_apply_html(j))

            if total_jobs:
                with st.container(key="jobs_pager"):
                    p1, p2, p3, p4 = st.columns([1.1, 1.4, 1.1, 1.2], vertical_alignment="center")
                    with p1:
                        if st.button("← Prev", key="alerts_prev", disabled=(page == 0), use_container_width=True):
                            st.session_state.alerts_page = page - 1
                            st.rerun()
                    with p2:
                        label = f"{start + 1}–{min(end, total)} of {total}" if total else f"0 of {total_jobs}"
                        st.html(f'<div class="pager-label t-label-md mono">{label}</div>')
                    with p3:
                        if st.button("Next →", key="alerts_next", disabled=(end >= total), use_container_width=True):
                            st.session_state.alerts_page = page + 1
                            st.rerun()
                    with p4:
                        if st.button("Clear all", key="btn_clear_all", use_container_width=True,
                                     help="Hide every alert (already-seen jobs won't be emailed again)"):
                            _, saved, err = _save_change("seen_jobs.json", dismiss_jobs(None), [],
                                                         "chore: dismiss all alerts")
                            st.session_state.alerts_page = 0
                            if saved:
                                toast("All alerts cleared", "success")
                            else:
                                toast(f"Cleared here, but not saved permanently. {err}", "error")
                            st.rerun()

    # ── RIGHT: portals, add portal, email ────────────────────────────────────
    with col_rail:
        with st.container(key="rail_col"):
            with st.container(key="portals_card"):
                rows = []
                for c in companies:
                    name = escape((c.get("name") or "").strip() or "Unnamed")
                    label, txt_cls, dot_cls = _company_state(c)
                    page_url = safe_url(c.get("url", ""))
                    link = (f'<a class="host t-body-sm" href="{escape(page_url, quote=True)}" target="_blank" rel="noopener">'
                            f'<span class="truncate">{escape(_short_url(page_url))}</span>{_ms("open_in_new", "s12")}</a>'
                            if page_url else '<span class="t-body-sm c-error">Invalid career page URL</span>')
                    n = jobs_per_company.get(c.get("name") or "", 0)
                    last_job = (c.get("last_job") or "").strip()
                    first = (f'<span class="truncate" title="{escape(last_job, quote=True)}">Last match: {escape(last_job)}</span>'
                             if last_job else f'<span>{n} {"job" if n == 1 else "jobs"} listed</span>')
                    when = ('<span class="mono c-primary">Scanning now…</span>' if label == "Checking"
                            else f'<span class="mono" style="flex-shrink:0;">{_rel_time(c.get("last_checked", ""))}</span>')
                    core = '<span class="core" title="Built-in company">CORE</span>' if c.get("locked") else ""
                    rows.append(f"""
                    <div class="portal">
                      <div class="portal-name"><span class="t-title truncate">{name}</span>
                        <span class="state t-label-sm {txt_cls}"><span class="dot sm {dot_cls}"></span>{label}</span>{core}</div>
                      {link}
                      <div class="pmeta t-label-sm">{first}<span aria-hidden="true">·</span>{when}</div>
                    </div>""")
                empty_portals = '<p class="t-body-sm c-variant" style="margin:8px 0;">No career portals tracked yet — add one below.</p>'
                st.html(f"""
                <div id="company-radar" style="scroll-margin-top:80px;">
                  <div class="card-head">
                    <div>
                      <div class="lh"><h2 class="t-title">Monitored Career Portals</h2><span class="count-chip">{total_companies}</span></div>
                      <p class="t-label-sm" style="font-weight:400;">{active_count} active · {broken_count} failing · checked every 3 hours</p>
                    </div>
                    <a class="btn-add t-label-md" href="#add-portal">{_ms("add", "s16")}Add</a>
                  </div>
                  <div class="portal-list">{"".join(rows) or empty_portals}</div>
                </div>""")

                if companies:
                    if st.button("Remove portals", key="btn_toggle_remove", icon=":material/tune:",
                                 use_container_width=True, help="Pick companies to stop tracking"):
                        st.session_state.show_remove_form = not st.session_state.show_remove_form
                        st.rerun()
                if st.session_state.show_remove_form and companies:
                    with st.container(key="remove_row"):
                        st.html('<p class="t-label-sm c-variant" style="margin:4px 0;">Tick the companies to stop tracking</p>')
                        for c in companies:
                            st.checkbox(c.get("name") or "Unnamed", key=f"rm_{c.get('id')}")
                        to_remove = [c for c in companies if st.session_state.get(f"rm_{c.get('id')}")]
                        rb1, rb2 = st.columns([1, 1.6])
                        with rb1:
                            if st.button("Cancel", key="btn_cancel_remove", use_container_width=True):
                                st.session_state.show_remove_form = False
                                st.rerun()
                        with rb2:
                            if st.button(f"Remove selected ({len(to_remove)})", key="btn_remove", type="primary",
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

            # ── Add Career Portal ─────────────────────────────────────────────
            with st.container(key="add_card"):
                st.html(f"""
                <div id="add-portal" class="card-head" style="justify-content:flex-start;scroll-margin-top:80px;">
                  <div class="sq-ic a">{_ms("add_link")}</div>
                  <div><h3 class="t-title">Add Career Portal</h3><p class="t-label-sm" style="font-weight:400;">Tracked from the next scheduled check</p></div>
                </div>""")
                new_name = st.text_input("Company Name", placeholder="e.g. Cisco, MetLife", key="new_name")
                new_url = st.text_input("Career Page URL (Workday or ATS)",
                                        placeholder="https://cisco.wd3.myworkdayjobs.com/…", key="new_url")
                st.html(f"""
                <div>
                  <span class="field-label t-label-sm">Scrape Driver</span>
                  <div class="fake-select t-body-sm" title="Chosen automatically by the scraper">
                    <span>Auto-detect · Playwright headless browser</span>{_ms("lock", "s16", "c-outline")}</div>
                  <p class="hint t-label-sm" style="font-weight:400;">Selected automatically: companies with a configured Workday API use it; every other portal is read with Playwright.</p>
                </div>""")
                if st.button("+ Start Tracking", key="btn_add", type="primary", use_container_width=True):
                    name = new_name.strip()
                    url = new_url.strip()
                    if not name:
                        toast("Company name is required", "error")
                    elif not safe_url(url):
                        toast("Enter the full career page URL, starting with https://", "error")
                    elif any((c.get("name") or "").lower() == name.lower() for c in companies):
                        toast(f"{name} is already tracked", "error")
                    else:
                        new_company = {
                            "id": f"c{int(time.time())}",
                            "name": name, "url": url, "locked": False,
                            "status": "unknown", "last_job": "", "last_checked": "",
                        }

                        def _add(latest: list) -> list:
                            if not any((c.get("name") or "").lower() == name.lower() for c in latest):
                                latest.append(new_company)
                            return latest

                        _, saved, err = _save_change("companies.json", _add, [], f"chore: add {name}")
                        if saved:
                            toast(f"{name} added", "success")
                        else:
                            toast(f"{name} added, but not saved permanently. {err}", "error")
                        st.session_state._clear_add_form = True
                        st.rerun()

            # ── Email Alert Delivery ──────────────────────────────────────────
            with st.container(key="ns_card"):
                status_badge = ('<span class="tag-active t-label-sm" style="border-radius:999px;">✓ Active</span>'
                                if recipient_saved else
                                '<span class="tag-warn t-label-sm" style="border-radius:999px;">Not set</span>')
                st.html(f"""
                <div id="email-alerts" style="scroll-margin-top:80px;display:flex;flex-direction:column;gap:8px;">
                  <div class="card-head">
                    <div class="lh"><div class="sq-ic b">{_ms("forward_to_inbox")}</div>
                      <div><h3 class="t-title">Email Alert Delivery</h3><p class="t-label-sm" style="font-weight:400;">Sent after each check that finds new jobs</p></div></div>
                    {status_badge}
                  </div>
                  <p class="card-sub t-body-sm">Receive an alert whenever a genuine fresher posting passes the classifier.</p>
                  <span class="field-label t-label-sm" style="margin:0;">Recipient Email</span>
                </div>""")
                in_col, save_col = st.columns([3, 1], vertical_alignment="bottom")
                with in_col:
                    email_val = st.text_input("Recipient email", value=st.session_state.email_val,
                                              placeholder="you@gmail.com", key="email_input",
                                              label_visibility="collapsed")
                    st.session_state.email_val = email_val
                with save_col:
                    save_clicked = st.button("Save", key="btn_save_email")

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
                        st.rerun()

                synced = bool(os.environ.get("GITHUB_TOKEN"))
                sync_line = (f'<div class="sync t-label-sm c-secondary">{_ms("cloud_sync", "s14")}Saved to settings.json and synced to GitHub</div>'
                             if synced else
                             f'<div class="sync t-label-sm c-amber">{_ms("cloud_off", "s14")}No GitHub token — changes are saved on this server only</div>')
                ts = st.session_state.test_status
                if ts is None:
                    ts_html = '<span class="mono c-outline" style="font-size:11px;">Not sent this session</span>'
                elif ts[0]:
                    ts_html = f'<span class="mono c-secondary" style="font-size:11px;">Sent {escape(ts[1])} UTC</span>'
                else:
                    ts_html = f'<span class="mono c-error" style="font-size:11px;">Failed {escape(ts[1])} UTC</span>'
                st.html(f"""
                <div style="display:flex;flex-direction:column;gap:8px;">
                  {sync_line}
                  <div class="divider"></div>
                  <div class="row-between"><span class="t-label-sm c-surface">Verification Dispatch</span>{ts_html}</div>
                  <p class="card-sub t-body-sm">Send a sample alert through the configured Gmail account to confirm emails arrive.</p>
                </div>""")

                with st.container(key="test_mail_btn"):
                    if st.button("Send Test Email", key="btn_test", icon=":material/send:", use_container_width=True):
                        recipient = email_val.strip()
                        stamp = datetime.now(timezone.utc).strftime("%H:%M")
                        if not _EMAIL_RE.match(recipient):
                            toast("Enter a valid recipient email first", "error")
                        else:
                            persist_err = ""
                            if settings.get("recipient_email", "") != recipient:
                                _, persist_err = _set_recipient(recipient)
                            try:
                                from notifier import test_mail
                                test_mail(recipient)
                            except Exception as ex:
                                log.warning("test mail failed: %s", ex)
                                st.session_state.test_status = (False, stamp)
                                if "GMAIL_" in str(ex):
                                    toast("Email isn't configured for this app (Gmail address / app password missing).", "error")
                                else:
                                    toast("Couldn't send the test email. Check the Gmail app password and try again.", "error")
                            else:
                                st.session_state.test_status = (True, stamp)
                                if persist_err:
                                    toast(f"Test email sent, but the address wasn't saved permanently. {persist_err}", "error")
                                else:
                                    toast(f"Test email sent — check {recipient}", "success")
                        st.rerun()

# ── Footer ─────────────────────────────────────────────────────────────────────
st.html(f"""
<footer class="site-footer">
  <div class="in">
    <div class="l t-body-sm">
      <span class="mono c-primary" style="display:inline-flex;align-items:center;gap:4px;font-weight:500;"><span class="dot bg-secondary"></span>Fresher classifier</span>
      <span>·</span><span>India roles only</span>
      <span>·</span><span>Checks every 3 hours</span>
      <span>·</span><span class="c-surface">Workday &amp; Playwright scrapers</span>
    </div>
    <a class="t-body-sm" href="https://github.com/{GITHUB_REPO}" target="_blank" rel="noopener">Fresher Job Tracker · Source on GitHub</a>
  </div>
</footer>""")

# ── Toast ──────────────────────────────────────────────────────────────────────
# Shown once and faded out with CSS; the old sleep(3)+rerun froze the whole
# app for 3 s after every action, swallowing clicks made in the meantime.
if st.session_state.toast:
    msg = escape(st.session_state.toast, quote=False)
    kind = st.session_state.toast_kind
    st.session_state.toast = None
    ok = kind == "success"
    st.html(f"""
<style>
@keyframes jt-toast {{ 0%, 85% {{ opacity: 1; }} 100% {{ opacity: 0; visibility: hidden; }} }}
.jt-toast {{ animation: jt-toast {4 if ok else 6}s ease-in forwards; }}
@media (max-width: 640px) {{ .jt-toast {{ left: 16px; right: 16px; bottom: 16px; max-width: none !important; }} }}
</style>
<div class="jt-toast" role="status" style="position:fixed;bottom:24px;right:24px;z-index:9999;display:flex;align-items:center;gap:10px;padding:12px 16px;border-radius:12px;background:var(--lowest);box-shadow:0 20px 25px -5px rgba(15,23,42,.12),0 8px 10px -6px rgba(15,23,42,.08);max-width:360px;">
  <span style="width:28px;height:28px;border-radius:8px;flex-shrink:0;display:flex;align-items:center;justify-content:center;background:var({"--secondary-container" if ok else "--error-container"});color:var({"--secondary" if ok else "--error"});">{_ms("check_circle" if ok else "error", "s18")}</span>
  <div class="t-body-sm" style="font-weight:500;color:var(--on-surface);">{msg}</div>
</div>
""")
