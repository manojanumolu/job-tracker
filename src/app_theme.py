"""The app's design tokens and stylesheet (moved out of streamlit_app.py,
Nov 2026 — Milestone 4). Pure data and one pure function: no Streamlit
import, so it can be tested and measured on its own.

streamlit_app.py sends ``CSS`` (the stylesheet below, minified) and the
active palette as ``:root`` variables on every run.
"""

from __future__ import annotations

import functools
import re

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
.st-key-at_fetch, .st-key-at_form, .st-key-at_export, .st-key-at_dupe,
.st-key-job_main, .st-key-job_side, .st-key-co_side, .st-key-empty, .st-key-hero, .st-key-job_hero, .st-key-co_hero, .st-key-mail_status,
:is(.st-key-danger_confirm, .st-key-mh_clear_ask, .st-key-delete_confirm, .st-key-at_confirm), .mon tbody tr, [class*="st-key-jr_"], [class*="st-key-cr_"] {
  background: var(--surface) !important; border: 1px solid var(--border) !important; border-radius: var(--r-lg) !important;
  box-shadow: var(--hi), var(--shadow) !important;
}
.st-key-add_form, .st-key-email_form, .st-key-test_panel, [class*="st-key-set_"], .st-key-job_main, .st-key-job_side,
.st-key-at_fetch, .st-key-at_form, .st-key-at_export, .st-key-co_side, .st-key-empty, .st-key-mail_status { padding: 22px 24px !important; gap: 16px !important; }
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
.job-beyond { font-size: 12.5px; line-height: 18px; color: var(--muted); margin-top: 8px; display: flex; align-items: center; gap: 8px; min-width: 0; flex-wrap: wrap; }
.job-beyond .pill { font-size: 11.5px; line-height: 16px; padding: 2px 9px 2px 8px; }
.job-beyond .pill .ms { font-size: 14px; }
.beyond-head { margin-top: 18px; }
.st-key-xfilt [data-testid="stHorizontalBlock"] { gap: 8px !important; flex-wrap: wrap !important; }
.st-key-xfilt [data-testid="stColumn"] { flex: 1 1 200px !important; width: auto !important; min-width: 0 !important; max-width: 320px; }
.list-foot { padding: 12px 20px; display: flex; align-items: center; justify-content: space-between; gap: 12px; font-size: 13px; color: var(--muted); }
.st-key-list_foot_h, :is(.st-key-list_foot_j, .st-key-list_foot_at) { padding: 6px 4px 0 !important; }
.st-key-list_foot_h > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"], .st-key-list_foot_h > [data-testid="stHorizontalBlock"],
:is(.st-key-list_foot_j, .st-key-list_foot_at) > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"], :is(.st-key-list_foot_j, .st-key-list_foot_at) > [data-testid="stHorizontalBlock"] { align-items: center !important; gap: 8px !important; flex-wrap: nowrap !important; }
.st-key-list_foot_h [data-testid="stColumn"], :is(.st-key-list_foot_j, .st-key-list_foot_at) [data-testid="stColumn"] { flex: 0 0 auto !important; width: auto !important; min-width: 0 !important; }
.st-key-list_foot_h [data-testid="stColumn"]:first-child, :is(.st-key-list_foot_j, .st-key-list_foot_at) [data-testid="stColumn"]:first-child { flex: 1 1 auto !important; }
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
:is(.st-key-danger_confirm, .st-key-mh_clear_ask, .st-key-delete_confirm, .st-key-at_confirm) { padding: 16px 18px !important; gap: 12px !important; border-color: color-mix(in srgb, var(--red) 35%, var(--border)) !important;
  background: linear-gradient(180deg, var(--red-soft), transparent 140%), var(--surface) !important; }
:is(.st-key-danger_confirm, .st-key-mh_clear_ask, .st-key-delete_confirm, .st-key-at_confirm) [data-testid="stBaseButton-secondary"] { color: var(--text) !important; }
:is(.st-key-danger_confirm, .st-key-mh_clear_ask, .st-key-delete_confirm, .st-key-at_confirm) [data-testid="stBaseButton-primary"] { background: var(--red) !important; border-color: var(--red) !important; color: #fff !important; box-shadow: 0 8px 18px -10px var(--red) !important; }
[class*="st-key-dismiss_"] [data-testid^="stBaseButton"], [class*="st-key-restore_"] [data-testid^="stBaseButton"] { width: 38px !important; padding: 0 !important; color: var(--muted) !important; }
[class*="st-key-dismiss_"] [data-testid^="stBaseButton"]:hover { color: var(--red) !important; border-color: color-mix(in srgb, var(--red) 40%, transparent) !important; background: var(--red-soft) !important; }
[class*="st-key-restore_"] [data-testid^="stBaseButton"]:hover { color: var(--accent-text) !important; }

/* segmented tabs (st.pills) */
.st-key-jobs_tab_wrap [data-testid="stButtonGroup"] > div, :is(.st-key-co_status_wrap, .st-key-at_section_wrap, .st-key-at_scope_wrap, .st-key-at_method_wrap, .st-key-at_range_wrap, .st-key-at_export) [data-testid="stButtonGroup"] > div {
  gap: 2px !important; background: color-mix(in srgb, var(--surface) 60%, var(--hover)); border: 1px solid var(--border); padding: 4px; border-radius: 12px; display: inline-flex !important; flex-wrap: wrap;
}
.st-key-jobs_tab_wrap button[data-variant="pills"], :is(.st-key-co_status_wrap, .st-key-at_section_wrap, .st-key-at_scope_wrap, .st-key-at_method_wrap, .st-key-at_range_wrap, .st-key-at_export) button[data-variant="pills"],
.st-key-jobs_tab_wrap [data-testid^="stBaseButton-pills"], :is(.st-key-co_status_wrap, .st-key-at_section_wrap, .st-key-at_scope_wrap, .st-key-at_method_wrap, .st-key-at_range_wrap, .st-key-at_export) [data-testid^="stBaseButton-pills"] {
  border: 1px solid transparent !important; border-radius: 9px !important; background: transparent !important; color: var(--muted) !important; min-height: 32px !important; padding: 0 13px !important;
}
.st-key-jobs_tab_wrap button[data-variant="pills"]:hover, :is(.st-key-co_status_wrap, .st-key-at_section_wrap, .st-key-at_scope_wrap, .st-key-at_method_wrap, .st-key-at_range_wrap, .st-key-at_export) button[data-variant="pills"]:hover { color: var(--text) !important; }
.st-key-jobs_tab_wrap button[data-variant="pills"][aria-checked="true"], :is(.st-key-co_status_wrap, .st-key-at_section_wrap, .st-key-at_scope_wrap, .st-key-at_method_wrap, .st-key-at_range_wrap, .st-key-at_export) button[data-variant="pills"][aria-checked="true"],
.st-key-jobs_tab_wrap [data-testid="stBaseButton-pillsActive"], :is(.st-key-co_status_wrap, .st-key-at_section_wrap, .st-key-at_scope_wrap, .st-key-at_method_wrap, .st-key-at_range_wrap, .st-key-at_export) [data-testid="stBaseButton-pillsActive"] {
  background: var(--surface) !important; color: var(--text) !important; border-color: var(--border) !important; box-shadow: var(--hi), var(--shadow) !important;
}
.st-key-jobs_tab_wrap button[data-variant="pills"] p, :is(.st-key-co_status_wrap, .st-key-at_section_wrap, .st-key-at_scope_wrap, .st-key-at_method_wrap, .st-key-at_range_wrap, .st-key-at_export) button[data-variant="pills"] p { color: inherit !important; font-size: 13.5px !important; }

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
:is([class*="st-key-ate_"], [class*="st-key-atdel_"]) [data-testid="stMarkdownContainer"],
:is([class*="st-key-ate_"], [class*="st-key-atdel_"]) [data-testid="stMarkdownContainer"] p,
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

/* ── Application Tracker (admins): the app's own cards, pills, rows and tabs, plus a few pieces of its own ── */
.stats.six { grid-template-columns: repeat(3, minmax(0, 1fr)); }
/* date, time and long-text fields (first used here): the same surface, edge, hover and focus as the text fields */
:is(.stDateInput, .stTimeInput, .stTextArea, .stMultiSelect) label p { font-size: 13px !important; line-height: 18px !important; font-weight: 600 !important; color: var(--text-2) !important; }
:is([data-testid="stDateInputField"], [data-testid="stTimeInputTimeDisplay"], [data-testid="stTextAreaRootElement"]) {
  background: var(--surface) !important; border: 1px solid var(--border-strong) !important; border-radius: var(--r) !important;
  min-height: 40px; transition: border-color var(--fast), box-shadow var(--normal) var(--ease); }
:is([data-testid="stDateInputField"], [data-testid="stTimeInputTimeDisplay"], [data-testid="stTextAreaRootElement"]) * { background-color: transparent !important; }
:is([data-testid="stDateInputField"], [data-testid="stTimeInputTimeDisplay"], [data-testid="stTextAreaRootElement"]):hover {
  border-color: color-mix(in srgb, var(--accent) 35%, var(--border-strong)) !important; }
:is([data-testid="stDateInputField"], [data-testid="stTimeInputTimeDisplay"], [data-testid="stTextAreaRootElement"]):focus-within {
  border-color: var(--accent) !important; box-shadow: var(--ring), 0 8px 24px -14px var(--accent) !important; }
:is([data-testid="stDateInputField"], [data-testid="stTimeInputTimeDisplay"]) [role="spinbutton"], [data-testid="stTextAreaRootElement"] textarea {
  font-size: 14px !important; color: var(--text) !important; -webkit-text-fill-color: var(--text); }
:is([data-testid="stDateInputField"], [data-testid="stTimeInputTimeDisplay"]) :is([data-placeholder="true"], [data-type="literal"]) {
  color: var(--muted) !important; -webkit-text-fill-color: var(--muted); }
[data-testid="stTextAreaRootElement"] textarea { line-height: 21px !important; padding: 10px 12px !important; }
.set-head .ic.amber { color: var(--amber); background: var(--amber-soft); }
.at-sec { display: flex; gap: 12px; align-items: flex-start; margin-top: 2px; }
.at-sec .ic { width: 32px; height: 32px; border-radius: 10px; display: flex; align-items: center; justify-content: center; flex-shrink: 0;
  color: var(--accent-text); background: var(--accent-soft); }
.st-key-at_form [data-testid="stHorizontalBlock"] { gap: 12px !important; }
.st-key-at_form [data-testid="stExpander"] [data-testid="stVerticalBlock"] { gap: 12px; }
.at-msg { display: flex; gap: 10px; align-items: flex-start; padding: 12px 14px; border-radius: var(--r); border: 1px solid transparent;
  font-size: 13.5px; line-height: 20px; color: var(--text); }
.at-msg b { font-weight: 600; } .at-msg p { margin: 2px 0 0; color: var(--text-2); }
.at-msg.ok { background: var(--green-soft); border-color: color-mix(in srgb, var(--green) 28%, transparent); } .at-msg.ok > .ms { color: var(--green); }
.at-msg.warn { background: var(--amber-soft); border-color: color-mix(in srgb, var(--amber) 28%, transparent); } .at-msg.warn > .ms { color: var(--amber); }
.at-msg.err { background: var(--red-soft); border-color: color-mix(in srgb, var(--red) 28%, transparent); } .at-msg.err > .ms { color: var(--red); }
.st-key-at_dupe { padding: 18px 20px !important; gap: 12px !important; border-color: color-mix(in srgb, var(--amber) 40%, var(--border)) !important;
  background: linear-gradient(180deg, var(--amber-soft), transparent 140%), var(--surface) !important; }
.at-dupe-kv { padding: 12px 14px !important; border-radius: var(--r); background: var(--surface); border: 1px solid var(--border); margin-top: 0 !important; }
.job-meta .at-fu.overdue, .job-meta .at-fu.overdue .ms { color: var(--red); }
.job-meta .at-fu.today, .job-meta .at-fu.today .ms { color: var(--amber); }
:is([class*="st-key-ate_"], [class*="st-key-atdel_"]) [data-testid^="stBaseButton"] { width: 38px !important; padding: 0 !important; color: var(--muted) !important; }
[class*="st-key-ate_"] [data-testid^="stBaseButton"]:hover { color: var(--accent-text) !important; }
[class*="st-key-atdel_"] [data-testid^="stBaseButton"]:hover { color: var(--red) !important; border-color: color-mix(in srgb, var(--red) 40%, transparent) !important; background: var(--red-soft) !important; }
:is(.st-key-at_status, .st-key-at_iv_form) { padding: 14px 16px !important; gap: 12px !important; border-radius: var(--r) !important;
  background: var(--surface-2) !important; border: 1px solid var(--border) !important; }
:is(.st-key-at_status, .st-key-at_iv_form) [data-testid="stHorizontalBlock"] { gap: 12px !important; }
:is(.st-key-at_fu_actions, [class*="st-key-atrnda_"]) { flex-direction: row !important; flex-wrap: wrap !important; gap: 8px !important; }
:is(.st-key-at_fu_actions, [class*="st-key-atrnda_"]) > * { width: auto !important; flex: 0 0 auto !important; }
[class*="st-key-atrnd_"] { padding: 14px 16px !important; gap: 4px !important; border-radius: var(--r) !important; background: var(--surface-2) !important;
  border: 1px solid var(--border) !important; }
.at-iv-h { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.at-iv-h b { font-size: 15px; line-height: 22px; color: var(--text); margin-right: auto; }
.at-iv .kv { grid-template-columns: 100px minmax(0, 1fr); gap: 6px 14px; margin-top: 10px !important; font-size: 13.5px; }
.at-notes { white-space: pre-wrap; font-size: 14px; line-height: 21px; color: var(--text-2); margin: 8px 0 0; overflow-wrap: anywhere; }
.step p.at-rec { font-size: 12px; line-height: 17px; }
.at-chart { margin: 0; padding: 18px 20px; border: 1px solid var(--border); border-radius: var(--r-lg); background: var(--surface);
  box-shadow: var(--hi), var(--shadow); height: 100%; box-sizing: border-box; }
.at-chart figcaption { margin-bottom: 16px; }
.at-plot { position: relative; padding-top: 20px; }
.at-ymax { position: absolute; top: 0; left: 0; font-size: 11.5px; line-height: 16px; color: var(--muted); }
.at-cols { display: flex; align-items: stretch; gap: 2px; height: 168px; border-top: 1px dashed var(--border); border-bottom: 1px solid var(--border-strong); }
.at-col { flex: 1 1 0; min-width: 0; display: flex; align-items: flex-end; justify-content: center; position: relative; }
.at-col .bar { width: min(26px, 72%); height: calc(var(--v) * 100%); min-height: 3px; border-radius: 4px 4px 0 0; background: var(--accent);
  transition: background var(--fast); }
.at-col .bar.zero { min-height: 0; }
.at-col:hover .bar { background: var(--accent-hover); }
.at-col:hover::after { content: attr(data-tip); position: absolute; left: 50%; bottom: calc(var(--v) * 100% + 8px); transform: translateX(-50%);
  white-space: nowrap; padding: 4px 8px; border-radius: 8px; background: var(--raised); border: 1px solid var(--border); box-shadow: var(--shadow-lg);
  font-size: 12px; line-height: 16px; font-weight: 600; color: var(--text); z-index: 2; pointer-events: none; }
.at-xl { display: flex; gap: 2px; margin-top: 6px; }
.at-xl span { flex: 1 1 0; min-width: 0; text-align: center; font-size: 11px; line-height: 15px; color: var(--muted); white-space: nowrap; }
.at-hbars { list-style: none; margin: 0 !important; padding: 0 !important; display: flex; flex-direction: column; gap: 10px; }
.at-hbars li { display: grid; grid-template-columns: minmax(0, 38%) minmax(0, 1fr) 32px; gap: 10px; align-items: center; font-size: 13px; line-height: 18px; }
.at-hbars .nm { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 600; color: var(--text); }
.at-hbars .track { height: 12px; display: flex; }
.at-hbars .bar { display: block; height: 100%; width: calc(var(--v) * 100%); min-width: 3px; border-radius: 0 4px 4px 0; background: var(--accent);
  transition: background var(--fast); }
.at-hbars li:hover .bar { background: var(--accent-hover); }
.at-hbars .v { text-align: right; color: var(--text-2); font-weight: 600; }
.at-list { margin: 10px 0 0 !important; padding: 0 0 0 18px !important; font-size: 13.5px; line-height: 22px; color: var(--text-2); }
.at-list b { color: var(--text); font-weight: 600; }
.st-key-at_charts > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"], .st-key-at_charts > [data-testid="stHorizontalBlock"] { gap: 14px !important; align-items: stretch !important; }
.st-key-at_range_wrap > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"], .st-key-at_range_wrap > [data-testid="stHorizontalBlock"] { gap: 12px !important; flex-wrap: wrap !important; }
@media (max-width: 1023px) {
  .stats.six { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .st-key-at_charts > [data-testid="stLayoutWrapper"] > [data-testid="stHorizontalBlock"], .st-key-at_charts > [data-testid="stHorizontalBlock"] { flex-wrap: wrap !important; }
  .st-key-at_charts [data-testid="stColumn"]:nth-child(n) { flex: 1 1 100% !important; width: 100% !important; }
}

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
/* Streamlit removes what the signed-in page drew only when the login page's
   run FINISHES (seconds, on Community Cloud): until then, neither the bar nor
   the open menu shows next to the sign-in form */
body:has(.st-key-login_card) :is(.st-key-acct_bar, [data-testid="stPopoverBody"]:has(.st-key-btn_account_sign_out)) { display: none !important; }
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
/* the sign-in check's way out: faded in after the moment a normal check takes */
.st-key-probe_way_out { margin-top: 14px; animation: jt-fade .25s ease-out 8s both; }
.probe-slow { margin: 0 0 10px; font-size: 13px; color: var(--muted); }
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
.st-key-jobs_tab_wrap button[data-variant="pills"]:hover, :is(.st-key-co_status_wrap, .st-key-at_section_wrap, .st-key-at_scope_wrap, .st-key-at_method_wrap, .st-key-at_range_wrap, .st-key-at_export) button[data-variant="pills"]:hover {
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


_STRING = re.compile(r"""("(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')""")


@functools.lru_cache(maxsize=8)
def minify_css(css: str) -> str:
    """The same stylesheet with comments and layout whitespace removed.
    Quoted strings (url('...'), content: "...") are kept byte for byte, runs
    of whitespace become one space, and whitespace is only dropped next to
    { } ; — never next to "," ">" or ":", where it can be significant
    ("a :hover" vs "a:hover"; custom properties such as --font keep their
    text exactly). tests/test_app_theme.py proves a browser parses both
    versions to the same rules."""
    out = []
    for i, part in enumerate(_STRING.split(css)):
        if i % 2:                                    # a quoted string
            out.append(part)
            continue
        part = re.sub(r"/\*.*?\*/", "", part, flags=re.S)
        part = re.sub(r"\s+", " ", part)
        part = re.sub(r"\s*([{};])\s*", r"\1", part)
        out.append(part)
    return "".join(out).replace(";}", "}").strip()


# what the app sends: smaller, and parsed to the same rules
CSS = minify_css(_CSS)
