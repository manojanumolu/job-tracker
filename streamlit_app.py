import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

st.set_page_config(
    page_title="Fresher Job Tracker",
    page_icon="💼",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── CSS injection via st.html() — works in Streamlit 1.36+ ───────────────────
st.html("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600;700&family=Geist+Mono:wght@400;500&display=swap');

* { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --bg: #f6f6f7;
  --card: #ffffff;
  --card-2: #fafafa;
  --border: #eaeaec;
  --border-strong: #dcdce0;
  --text: #17171a;
  --muted: #6c6c76;
  --faint: #9a9aa4;
  --accent: #4f46e5;
  --accent-soft: rgba(79,70,229,0.10);
  --good: #22c55e;
  --good-soft: rgba(34,197,94,0.12);
  --bad: #ef4444;
  --bad-soft: rgba(239,68,68,0.12);
  --shadow: 0 1px 2px rgba(20,20,30,.05), 0 2px 8px rgba(20,20,30,.04);
  --dot: rgba(20,20,30,.045);
}

/* ── hide Streamlit chrome ── */
#MainMenu, footer, header[data-testid="stHeader"] { visibility: hidden; height: 0; }
.stDeployButton, [data-testid="stToolbar"] { display: none !important; }
section[data-testid="stSidebar"] { display: none !important; }
.block-container { padding: 0 !important; max-width: 100% !important; }
.stApp {
  background-color: var(--bg) !important;
  background-image:
    radial-gradient(900px 400px at 50% -120px, rgba(79,70,229,0.08), transparent 68%),
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
.stButton > button {
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
.stButton > button:hover {
  border-color: var(--accent) !important;
  color: var(--accent) !important;
  background: var(--card-2) !important;
}
/* ── accent button (Send Test Mail) ── */
.accent-btn > button {
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
.accent-btn > button:hover {
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
</style>
""")

# ── helpers ──────────────────────────────────────────────────────────────────
BASE = Path(__file__).parent


def _load(path: Path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return default


def _save(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")


def _host(url: str) -> str:
    try:
        from urllib.parse import urlparse
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
        return iso


def _commit(path: Path, repo_path: str, msg: str):
    import os
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        return
    try:
        from github import Github, GithubException
        repo = Github(token).get_repo("manojanumolu/job-tracker")
        content = path.read_text("utf-8")
        try:
            existing = repo.get_contents(repo_path)
            repo.update_file(repo_path, msg, content, existing.sha)
        except GithubException:
            repo.create_file(repo_path, msg, content)
    except Exception:
        pass


# ── load data ─────────────────────────────────────────────────────────────────
companies: list[dict] = _load(BASE / "companies.json", [])
settings: dict = _load(BASE / "settings.json", {"recipient_email": ""})
seen_jobs: list[dict] = list(reversed(_load(BASE / "seen_jobs.json", [])[-50:]))

# ── session state ──────────────────────────────────────────────────────────────
if "email_val" not in st.session_state:
    st.session_state.email_val = settings.get("recipient_email", "")
if "toast" not in st.session_state:
    st.session_state.toast = None
if "toast_kind" not in st.session_state:
    st.session_state.toast_kind = "success"


def toast(msg: str, kind: str = "success"):
    st.session_state.toast = msg
    st.session_state.toast_kind = kind


# ═══════════════════════════════════════════════════════════════════════════════
# HEADER
# ═══════════════════════════════════════════════════════════════════════════════
last_checked_times = [c.get("last_checked", "") for c in companies if c.get("last_checked")]
last_checked_str = _rel_time(max(last_checked_times)) if last_checked_times else "never"
active_count = sum(1 for c in companies if c.get("status") not in ("broken",))

st.html(f"""
<div style="position:sticky;top:0;z-index:20;background:rgba(246,246,247,0.88);backdrop-filter:blur(14px);border-bottom:1px solid #eaeaec;">
  <div style="max-width:1120px;margin:0 auto;padding:14px 24px;display:flex;align-items:center;gap:16px;flex-wrap:wrap;">
    <div style="display:flex;align-items:center;gap:11px;margin-right:auto;">
      <div style="width:36px;height:36px;border-radius:10px;background:#4f46e5;color:#fff;display:grid;place-items:center;box-shadow:0 1px 2px rgba(20,20,30,.1);">
        <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="20" height="14" x="2" y="7" rx="2"/><path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16"/></svg>
      </div>
      <div style="line-height:1.2;">
        <div style="font-size:15.5px;font-weight:700;letter-spacing:-0.02em;color:#17171a;font-family:'Geist',sans-serif;">Fresher Job Tracker</div>
        <div style="font-size:12px;color:#6c6c76;font-family:'Geist',sans-serif;">Entry-level posting monitor</div>
      </div>
    </div>
    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
      <div style="display:flex;align-items:center;gap:7px;padding:6px 11px;border:1px solid #eaeaec;border-radius:999px;background:#fff;font-size:12.5px;font-family:'Geist',sans-serif;">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#22c55e" stroke-width="2.2"><path d="M20 6 9 17l-5-5"/></svg>
        <span style="color:#6c6c76;">Last checked</span>
        <span style="font-weight:600;font-family:'Geist Mono',monospace;">{last_checked_str}</span>
      </div>
      <div style="display:flex;align-items:center;gap:7px;padding:6px 11px;border:1px solid #eaeaec;border-radius:999px;background:#fff;font-size:12.5px;font-family:'Geist',sans-serif;">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="#4f46e5" stroke-width="2.2"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>
        <span style="color:#6c6c76;">Next check</span>
        <span style="font-weight:600;font-family:'Geist Mono',monospace;">in ~3 h</span>
      </div>
      <a href="https://github.com/manojanumolu/job-tracker" target="_blank" rel="noopener"
         style="height:34px;display:inline-flex;align-items:center;gap:7px;padding:0 11px;border:1px solid #eaeaec;background:#fff;color:#17171a;border-radius:9px;text-decoration:none;font-size:12.5px;font-weight:500;font-family:'Geist',sans-serif;">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 19c-5 1.5-5-2.5-7-3m14 6v-3.87a3.37 3.37 0 0 0-.94-2.61c3.14-.35 6.44-1.54 6.44-7A5.44 5.44 0 0 0 20 4.77 5.07 5.07 0 0 0 19.91 1S18.73.65 16 2.48a13.38 13.38 0 0 0-7 0C6.27.65 5.09 1 5.09 1A5.07 5.07 0 0 0 5 4.77a5.44 5.44 0 0 0-1.5 3.78c0 5.42 3.3 6.61 6.44 7A3.37 3.37 0 0 0 9 18.13V22"/></svg>
        GitHub
      </a>
    </div>
  </div>
</div>
<div style="max-width:1120px;margin:0 auto;padding:28px 24px 0;font-family:'Geist',sans-serif;">
""")

# ═══════════════════════════════════════════════════════════════════════════════
# TRACKED COMPANIES TABLE
# ═══════════════════════════════════════════════════════════════════════════════
rows_html = ""
for c in companies:
    status = c.get("status", "unknown")
    is_broken = status == "broken"
    is_active = status == "active"
    status_label = "Broken" if is_broken else ("Active" if is_active else "Pending")
    status_color = "#ef4444" if is_broken else ("#22c55e" if is_active else "#9a9aa4")
    status_bg = "rgba(239,68,68,0.12)" if is_broken else ("rgba(34,197,94,0.12)" if is_active else "rgba(156,163,175,0.12)")
    initial = c.get("name", "?")[0].upper()
    host = _host(c.get("url", ""))
    last_job = c.get("last_job", "") or "—"
    last_checked = _rel_time(c.get("last_checked", ""))
    core_badge = '<span style="font-size:10px;color:#9a9aa4;border:1px solid #eaeaec;padding:1px 6px;border-radius:5px;letter-spacing:0.03em;margin-left:6px;">CORE</span>' if c.get("locked") else ""
    rows_html += f"""
<tr style="border-top:1px solid #eaeaec;">
  <td style="padding:14px 22px;">
    <div style="display:flex;align-items:center;gap:10px;">
      <span style="width:26px;height:26px;border-radius:7px;background:rgba(79,70,229,0.10);color:#4f46e5;display:grid;place-items:center;font-size:12px;font-weight:700;flex-shrink:0;">{initial}</span>
      <span style="font-weight:600;font-size:13.5px;">{c.get('name','')}</span>{core_badge}
    </div>
  </td>
  <td style="padding:14px 16px;">
    <a href="{c.get('url','')}" target="_blank" rel="noopener"
       style="display:inline-flex;align-items:center;gap:5px;color:#6c6c76;text-decoration:none;font-family:'Geist Mono',monospace;font-size:12px;">
      {host}
      <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/></svg>
    </a>
  </td>
  <td style="padding:14px 16px;">
    <span style="display:inline-flex;align-items:center;gap:6px;padding:3px 9px 3px 8px;border-radius:999px;font-size:12px;font-weight:600;background:{status_bg};color:{status_color};">
      <span style="width:7px;height:7px;border-radius:50%;background:{status_color};flex-shrink:0;"></span>
      {status_label}
    </span>
  </td>
  <td style="padding:14px 16px;color:#17171a;font-size:13.5px;">{last_job}</td>
  <td style="padding:14px 16px;color:#6c6c76;font-family:'Geist Mono',monospace;font-size:12px;">{last_checked}</td>
  <td style="padding:14px 22px;"></td>
</tr>"""

st.html(f"""
<section style="background:#fff;border:1px solid #eaeaec;border-radius:16px;box-shadow:0 1px 2px rgba(20,20,30,.05),0 2px 8px rgba(20,20,30,.04);overflow:hidden;margin-bottom:16px;">
  <div style="display:flex;align-items:center;padding:18px 22px;border-bottom:1px solid #eaeaec;">
    <div>
      <h2 style="font-size:16px;font-weight:700;letter-spacing:-0.02em;color:#17171a;font-family:'Geist',sans-serif;">Tracked Companies</h2>
      <p style="margin-top:3px;font-size:12.5px;color:#6c6c76;font-family:'Geist',sans-serif;">{active_count} of {len(companies)} career pages responding &middot; checked every 3 hours</p>
    </div>
  </div>
  <div style="overflow-x:auto;">
    <table style="width:100%;border-collapse:collapse;min-width:700px;font-size:13.5px;font-family:'Geist',sans-serif;">
      <thead>
        <tr style="text-align:left;">
          <th style="padding:11px 22px;font-size:11px;font-weight:700;color:#9a9aa4;text-transform:uppercase;letter-spacing:0.06em;">Company</th>
          <th style="padding:11px 16px;font-size:11px;font-weight:700;color:#9a9aa4;text-transform:uppercase;letter-spacing:0.06em;">Career Page</th>
          <th style="padding:11px 16px;font-size:11px;font-weight:700;color:#9a9aa4;text-transform:uppercase;letter-spacing:0.06em;">Status</th>
          <th style="padding:11px 16px;font-size:11px;font-weight:700;color:#9a9aa4;text-transform:uppercase;letter-spacing:0.06em;">Last Job Found</th>
          <th style="padding:11px 16px;font-size:11px;font-weight:700;color:#9a9aa4;text-transform:uppercase;letter-spacing:0.06em;">Last Checked</th>
          <th style="padding:11px 22px;"></th>
        </tr>
      </thead>
      <tbody>{rows_html}</tbody>
    </table>
  </div>
</section>
""")

# ── Add Company expander ───────────────────────────────────────────────────────
with st.expander("➕  Add Company"):
    c1, c2, c3 = st.columns([2, 3, 1])
    with c1:
        new_name = st.text_input("Company name", placeholder="e.g. Infosys", key="new_name")
    with c2:
        new_url = st.text_input("Career page URL", placeholder="https://careers.infosys.com", key="new_url")
    with c3:
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("Add", key="btn_add"):
            name = new_name.strip()
            url = new_url.strip()
            if not name:
                toast("Company name is required", "error")
            elif not url.startswith("http"):
                toast("URL must start with https://", "error")
            elif any(c.get("name", "").lower() == name.lower() for c in companies):
                toast(f"{name} is already tracked", "error")
            else:
                companies.append({
                    "id": f"c{int(time.time())}",
                    "name": name, "url": url, "locked": False,
                    "status": "unknown", "last_job": "", "last_checked": "",
                })
                _save(BASE / "companies.json", companies)
                _commit(BASE / "companies.json", "companies.json", f"chore: add {name}")
                toast(f"{name} added", "success")
                st.rerun()

# ── Remove company ─────────────────────────────────────────────────────────────
non_locked = [c for c in companies if not c.get("locked", False)]
if non_locked:
    r1, r2 = st.columns([4, 1])
    with r1:
        remove_name = st.selectbox(
            "Remove a company",
            options=["— select to remove —"] + [c["name"] for c in non_locked],
            key="remove_sel",
        )
    with r2:
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("Remove", key="btn_remove") and remove_name != "— select to remove —":
            companies = [c for c in companies if c.get("name") != remove_name]
            _save(BASE / "companies.json", companies)
            _commit(BASE / "companies.json", "companies.json", f"chore: remove {remove_name}")
            toast(f"{remove_name} removed", "success")
            st.rerun()

st.html("<div style='height:24px;'></div>")

# ═══════════════════════════════════════════════════════════════════════════════
# TWO COLUMNS — RECENT ALERTS  +  NOTIFICATION SETTINGS
# ═══════════════════════════════════════════════════════════════════════════════
col_alerts, col_settings = st.columns([2, 1], gap="medium")

# ── Recent Alerts ──────────────────────────────────────────────────────────────
def _alert_row(j: dict) -> str:
    raw_title = j.get('title', '')
    title = raw_title[:50] + ('…' if len(raw_title) > 50 else '')
    return f"""
  <div style="display:flex;align-items:center;gap:14px;padding:15px 22px;border-top:1px solid #eaeaec;font-family:'Geist',sans-serif;">
    <span style="width:34px;height:34px;border-radius:9px;background:rgba(79,70,229,0.10);color:#4f46e5;display:grid;place-items:center;flex-shrink:0;">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect width="20" height="14" x="2" y="7" rx="2"/><path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16"/></svg>
    </span>
    <div style="min-width:0;margin-right:auto;">
      <div style="font-size:13.5px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:#17171a;">{title}</div>
      <div style="font-size:12px;color:#6c6c76;margin-top:2px;">{j.get('company','')} &middot; <span style="font-family:'Geist Mono',monospace;">{j.get('date','')}</span></div>
    </div>
    <a href="{j.get('url','#')}" target="_blank" rel="noopener"
       style="flex-shrink:0;display:inline-flex;align-items:center;gap:6px;padding:7px 12px;border:1px solid #dcdce0;background:#fafafa;color:#17171a;border-radius:8px;text-decoration:none;font-size:12.5px;font-weight:600;">
      Apply
      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/></svg>
    </a>
  </div>"""


_EMPTY_ALERTS_HTML = """
  <div style="padding:56px 22px;text-align:center;font-family:'Geist',sans-serif;">
    <div style="width:46px;height:46px;border-radius:12px;background:#fafafa;border:1px solid #eaeaec;display:inline-grid;place-items:center;color:#9a9aa4;margin-bottom:14px;">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/></svg>
    </div>
    <p style="font-size:14px;font-weight:600;color:#17171a;">No alerts yet</p>
    <p style="font-size:12.5px;color:#6c6c76;margin-top:4px;">We'll email you the moment a new fresher role appears.</p>
  </div>"""

with col_alerts:
    alert_count = len(seen_jobs)
    alerts_body = "".join(_alert_row(j) for j in seen_jobs[:20]) if seen_jobs else _EMPTY_ALERTS_HTML
    st.html(f"""
<section style="background:#fff;border:1px solid #eaeaec;border-radius:16px;box-shadow:0 1px 2px rgba(20,20,30,.05),0 2px 8px rgba(20,20,30,.04);overflow:hidden;">
  <div style="display:flex;align-items:center;padding:18px 22px;border-bottom:1px solid #eaeaec;">
    <div style="margin-right:auto;">
      <h2 style="font-size:16px;font-weight:700;letter-spacing:-0.02em;color:#17171a;font-family:'Geist',sans-serif;">Recent Alerts</h2>
      <p style="margin-top:3px;font-size:12.5px;color:#6c6c76;font-family:'Geist',sans-serif;">New fresher postings, newest first</p>
    </div>
    <span style="font-size:12px;font-weight:600;color:#4f46e5;background:rgba(79,70,229,0.10);padding:4px 10px;border-radius:999px;">{alert_count} new</span>
  </div>
  {alerts_body}
</section>
""")

# ── Notification Settings ──────────────────────────────────────────────────────
with col_settings:
    st.html("""
<section style="background:#fff;border:1px solid #eaeaec;border-radius:16px;box-shadow:0 1px 2px rgba(20,20,30,.05),0 2px 8px rgba(20,20,30,.04);padding:22px;">
  <div style="display:flex;align-items:center;gap:10px;margin-bottom:4px;">
    <span style="width:30px;height:30px;border-radius:8px;background:rgba(79,70,229,0.10);color:#4f46e5;display:grid;place-items:center;">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect width="20" height="16" x="2" y="4" rx="2"/><path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"/></svg>
    </span>
    <h2 style="font-size:16px;font-weight:700;letter-spacing:-0.02em;color:#17171a;font-family:'Geist',sans-serif;">Notification Settings</h2>
  </div>
  <p style="font-size:12.5px;color:#6c6c76;margin-bottom:20px;margin-left:40px;font-family:'Geist',sans-serif;">Where alert emails are delivered.</p>
""")

    email_val = st.text_input(
        "Recipient email",
        value=st.session_state.email_val,
        placeholder="you@example.com",
        key="email_input",
    )
    st.session_state.email_val = email_val

    if st.button("Save", key="btn_save_email"):
        e = email_val.strip()
        if not re.match(r"[^@\s]+@[^@\s]+\.[^@\s]+", e):
            toast("Enter a valid email address", "error")
        else:
            settings["recipient_email"] = e
            _save(BASE / "settings.json", settings)
            _commit(BASE / "settings.json", "settings.json", "chore: update recipient email")
            toast(f"Saved — alerts go to {e}", "success")

    st.html("""
  <div style="height:1px;background:#eaeaec;margin:20px 0;"></div>
  <p style="font-size:12px;font-weight:600;color:#6c6c76;margin-bottom:6px;font-family:'Geist',sans-serif;">Verify delivery</p>
  <p style="font-size:12.5px;color:#6c6c76;margin-bottom:12px;font-family:'Geist',sans-serif;">Send yourself a sample alert to confirm emails arrive.</p>
</section>
""")

    with st.container():
        st.markdown('<div class="accent-btn">', unsafe_allow_html=True)
        if st.button("✈  Send Test Mail", key="btn_test", use_container_width=True):
            recipient = settings.get("recipient_email", "").strip()
            if not recipient:
                toast("Save a recipient email first", "error")
            else:
                try:
                    import sys
                    sys.path.insert(0, str(BASE))
                    from notifier import test_mail
                    test_mail()
                    toast(f"Test email sent — check {recipient}", "success")
                except Exception as ex:
                    toast(f"Failed: {ex}", "error")
        st.markdown('</div>', unsafe_allow_html=True)

# ── Footer ─────────────────────────────────────────────────────────────────────
st.html("""
</div>
<footer style="max-width:1120px;margin:32px auto 0;padding:0 24px 48px;display:flex;align-items:center;justify-content:space-between;color:#9a9aa4;font-size:12px;font-family:'Geist',sans-serif;">
  <span>Fresher Job Tracker &middot; internal tool</span>
  <a href="https://github.com/manojanumolu/job-tracker" target="_blank" rel="noopener"
     style="display:inline-flex;align-items:center;gap:6px;color:#6c6c76;text-decoration:none;">
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 19c-5 1.5-5-2.5-7-3m14 6v-3.87a3.37 3.37 0 0 0-.94-2.61c3.14-.35 6.44-1.54 6.44-7A5.44 5.44 0 0 0 20 4.77 5.07 5.07 0 0 0 19.91 1S18.73.65 16 2.48a13.38 13.38 0 0 0-7 0C6.27.65 5.09 1 5.09 1A5.07 5.07 0 0 0 5 4.77a5.44 5.44 0 0 0-1.5 3.78c0 5.42 3.3 6.61 6.44 7A3.37 3.37 0 0 0 9 18.13V22"/></svg>
    Source repository
  </a>
</footer>
""")

# ── Toast ──────────────────────────────────────────────────────────────────────
if st.session_state.toast:
    msg = st.session_state.toast
    kind = st.session_state.toast_kind
    icon_bg = "rgba(34,197,94,0.12)" if kind == "success" else "rgba(239,68,68,0.12)"
    icon_color = "#22c55e" if kind == "success" else "#ef4444"
    icon = '<path d="M20 6 9 17l-5-5"/>' if kind == "success" else '<circle cx="12" cy="12" r="10"/><path d="M12 8v4"/><path d="M12 16h.01"/>'
    sw = "2.4" if kind == "success" else "2.2"
    st.html(f"""
<div style="position:fixed;bottom:24px;right:24px;z-index:9999;display:flex;align-items:center;gap:11px;padding:13px 16px;border-radius:12px;background:#fff;border:1px solid #dcdce0;box-shadow:0 10px 30px rgba(0,0,0,.15);max-width:340px;font-family:'Geist',sans-serif;">
  <span style="width:26px;height:26px;border-radius:7px;flex-shrink:0;display:grid;place-items:center;background:{icon_bg};color:{icon_color};">
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="{sw}" stroke-linecap="round" stroke-linejoin="round">{icon}</svg>
  </span>
  <div style="font-size:13px;font-weight:500;color:#17171a;line-height:1.4;">{msg}</div>
</div>
""")
    time.sleep(3)
    st.session_state.toast = None
    st.rerun()
