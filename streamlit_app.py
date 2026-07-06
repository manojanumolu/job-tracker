import json
import time
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

# ── page config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Fresher Job Tracker",
    page_icon="💼",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── inject Google Fonts + CSS design tokens (mirrors Claude Design exactly) ───
st.markdown("""
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Geist:wght@400;500;600;700&family=Geist+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
* { box-sizing: border-box; }
:root {
  --bg: #f6f6f7;
  --card: #ffffff;
  --card-2: #fafafa;
  --border: #eaeaec;
  --border-strong: #dcdce0;
  --text: #17171a;
  --muted: #6c6c76;
  --faint: #9a9aa4;
  --accent: oklch(0.56 0.19 277);
  --accent-fg: #ffffff;
  --accent-soft: oklch(0.56 0.19 277 / 0.10);
  --good: oklch(0.66 0.16 155);
  --good-soft: oklch(0.66 0.16 155 / 0.12);
  --bad: oklch(0.63 0.2 22);
  --bad-soft: oklch(0.63 0.2 22 / 0.12);
  --shadow: 0 1px 2px rgba(20,20,30,.05), 0 2px 8px rgba(20,20,30,.04);
  --dot: rgba(20,20,30,.045);
  --accent-hex: #4f46e5;
  --accent-soft-hex: rgba(79,70,229,0.10);
  --good-hex: #22c55e;
  --good-soft-hex: rgba(34,197,94,0.12);
  --bad-hex: #ef4444;
  --bad-soft-hex: rgba(239,68,68,0.12);
}

/* ── hide Streamlit chrome ── */
#MainMenu, footer, header { visibility: hidden; }
.stDeployButton { display: none; }
[data-testid="stToolbar"] { display: none; }
[data-testid="stSidebar"] { display: none; }
section[data-testid="stSidebarContent"] { display: none; }
.block-container { padding: 0 !important; max-width: 100% !important; }
.stApp { background: var(--bg); background-image: radial-gradient(1000px 460px at 50% -140px, var(--accent-soft-hex), transparent 68%), radial-gradient(var(--dot) 1px, transparent 1.4px); background-size: auto, 24px 24px; background-position: center top, center top; font-family: 'Geist', ui-sans-serif, system-ui, sans-serif; -webkit-font-smoothing: antialiased; letter-spacing: -0.01em; color: var(--text); }

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
  transition: all .15s !important;
  padding: 8px 13px !important;
}
.stButton > button:hover { border-color: var(--accent-hex) !important; color: var(--accent-hex) !important; }

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
.stTextInput label { font-size: 12px !important; font-weight: 600 !important; color: var(--muted) !important; font-family: 'Geist', sans-serif !important; }

/* ── data editor / table ── */
[data-testid="stDataFrame"] { border: none !important; }

/* ── success/error messages ── */
.stSuccess, .stError, .stInfo { border-radius: 10px !important; }

@keyframes fjt-spin { to { transform: rotate(360deg); } }
</style>
""", unsafe_allow_html=True)

# ── helpers ──────────────────────────────────────────────────────────────────

BASE = Path(__file__).parent


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return default


def _save_json(path: Path, data) -> None:
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


# ── load data ─────────────────────────────────────────────────────────────────
companies: list[dict] = _load_json(BASE / "companies.json", [])
settings: dict = _load_json(BASE / "settings.json", {"recipient_email": ""})
seen_jobs: list[dict] = _load_json(BASE / "seen_jobs.json", [])

# sort newest first
seen_jobs = list(reversed(seen_jobs[-50:]))

# ── session state ──────────────────────────────────────────────────────────────
if "toast" not in st.session_state:
    st.session_state.toast = None
if "toast_type" not in st.session_state:
    st.session_state.toast_type = "success"
if "email_val" not in st.session_state:
    st.session_state.email_val = settings.get("recipient_email", "")


def show_toast(msg: str, kind: str = "success"):
    st.session_state.toast = msg
    st.session_state.toast_type = kind


# ═══════════════════════════════════════════════════════════════════════════════
# HEADER
# ═══════════════════════════════════════════════════════════════════════════════
active_count = sum(1 for c in companies if c.get("status") not in ("broken",))
last_checked_times = [c.get("last_checked", "") for c in companies if c.get("last_checked")]
last_checked_str = _rel_time(max(last_checked_times)) if last_checked_times else "never"

st.markdown(f"""
<header style="position:sticky;top:0;z-index:20;background:rgba(246,246,247,0.82);backdrop-filter:blur(14px);border-bottom:1px solid var(--border);margin-bottom:0;">
  <div style="max-width:1120px;margin:0 auto;padding:14px 24px;display:flex;align-items:center;gap:16px;flex-wrap:wrap;">
    <div style="display:flex;align-items:center;gap:11px;margin-right:auto;">
      <div style="width:36px;height:36px;border-radius:10px;background:var(--accent-hex);color:#fff;display:grid;place-items:center;box-shadow:var(--shadow);">
        <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="20" height="14" x="2" y="7" rx="2"/><path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16"/></svg>
      </div>
      <div style="line-height:1.15;">
        <div style="font-size:15.5px;font-weight:650;letter-spacing:-0.02em;white-space:nowrap;">Fresher Job Tracker</div>
        <div style="font-size:12px;color:var(--muted);">Entry-level posting monitor</div>
      </div>
    </div>
    <div style="display:flex;align-items:center;gap:8px;">
      <div style="display:flex;align-items:center;gap:7px;padding:6px 11px;border:1px solid var(--border);border-radius:999px;background:var(--card);font-size:12.5px;box-shadow:var(--shadow);">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="var(--good-hex)" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>
        <span style="color:var(--muted);">Last checked</span>
        <span style="font-weight:600;font-family:'Geist Mono',monospace;letter-spacing:-0.02em;">{last_checked_str}</span>
      </div>
      <div style="display:flex;align-items:center;gap:7px;padding:6px 11px;border:1px solid var(--border);border-radius:999px;background:var(--card);font-size:12.5px;box-shadow:var(--shadow);">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="var(--accent-hex)" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>
        <span style="color:var(--muted);">Next check</span>
        <span style="font-weight:600;font-family:'Geist Mono',monospace;letter-spacing:-0.02em;">in ~3 h</span>
      </div>
      <a href="https://github.com/manojanumolu/job-tracker" target="_blank" rel="noopener" style="height:34px;display:inline-flex;align-items:center;gap:7px;padding:0 11px;border:1px solid var(--border);background:var(--card);color:var(--text);border-radius:9px;text-decoration:none;font-size:12.5px;font-weight:500;box-shadow:var(--shadow);">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 19c-5 1.5-5-2.5-7-3m14 6v-3.87a3.37 3.37 0 0 0-.94-2.61c3.14-.35 6.44-1.54 6.44-7A5.44 5.44 0 0 0 20 4.77 5.07 5.07 0 0 0 19.91 1S18.73.65 16 2.48a13.38 13.38 0 0 0-7 0C6.27.65 5.09 1 5.09 1A5.07 5.07 0 0 0 5 4.77a5.44 5.44 0 0 0-1.5 3.78c0 5.42 3.3 6.61 6.44 7A3.37 3.37 0 0 0 9 18.13V22"/></svg>
        GitHub
      </a>
    </div>
  </div>
</header>
<div style="max-width:1120px;margin:0 auto;padding:28px 24px 0;">
""", unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════════
# TRACKED COMPANIES
# ═══════════════════════════════════════════════════════════════════════════════
st.markdown(f"""
<section style="background:var(--card);border:1px solid var(--border);border-radius:16px;box-shadow:var(--shadow);overflow:hidden;margin-bottom:24px;">
  <div style="display:flex;align-items:center;gap:14px;padding:18px 22px;border-bottom:1px solid var(--border);flex-wrap:wrap;">
    <div style="margin-right:auto;">
      <h2 style="margin:0;font-size:16px;font-weight:640;letter-spacing:-0.02em;">Tracked Companies</h2>
      <p style="margin:3px 0 0;font-size:12.5px;color:var(--muted);">{active_count} of {len(companies)} career pages responding &middot; checked every 3 hours</p>
    </div>
  </div>
  <div style="overflow-x:auto;">
    <table style="width:100%;border-collapse:collapse;min-width:720px;font-size:13.5px;">
      <thead>
        <tr style="text-align:left;color:var(--faint);font-size:11.5px;text-transform:uppercase;letter-spacing:0.05em;">
          <th style="padding:11px 22px;font-weight:600;">Company</th>
          <th style="padding:11px 16px;font-weight:600;">Career Page</th>
          <th style="padding:11px 16px;font-weight:600;">Status</th>
          <th style="padding:11px 16px;font-weight:600;">Last Job Found</th>
          <th style="padding:11px 16px;font-weight:600;">Last Checked</th>
          <th style="padding:11px 22px;font-weight:600;text-align:right;"></th>
        </tr>
      </thead>
      <tbody>
""", unsafe_allow_html=True)

# Render each company row
companies_to_delete = []
for i, c in enumerate(companies):
    status = c.get("status", "unknown")
    is_broken = status == "broken"
    status_label = "Broken" if is_broken else "Active" if status == "active" else "Pending"
    status_color = "var(--bad-hex)" if is_broken else ("var(--good-hex)" if status == "active" else "var(--faint)")
    status_bg = "var(--bad-soft-hex)" if is_broken else ("var(--good-soft-hex)" if status == "active" else "rgba(156,163,175,0.12)")
    initial = c.get("name", "?")[0].upper()
    host = _host(c.get("url", ""))
    last_job = c.get("last_job", "—") or "—"
    last_checked = _rel_time(c.get("last_checked", ""))
    locked = c.get("locked", False)
    core_badge = '<span title="Mandatory — cannot be removed" style="font-size:10px;color:var(--faint);border:1px solid var(--border);padding:1px 6px;border-radius:5px;letter-spacing:0.03em;">CORE</span>' if locked else ""
    delete_btn = "" if locked else f'<span id="del_{i}" style="font-size:11px;color:var(--faint);cursor:pointer;" title="Remove">&#x1F5D1;</span>'

    st.markdown(f"""
      <tr style="border-top:1px solid var(--border);">
        <td style="padding:14px 22px;">
          <div style="display:flex;align-items:center;gap:10px;">
            <span style="width:26px;height:26px;border-radius:7px;background:var(--accent-soft-hex);color:var(--accent-hex);display:grid;place-items:center;font-size:12px;font-weight:700;">{initial}</span>
            <span style="font-weight:550;">{c.get('name','')}</span>
            {core_badge}
          </div>
        </td>
        <td style="padding:14px 16px;">
          <a href="{c.get('url','')}" target="_blank" rel="noopener" style="display:inline-flex;align-items:center;gap:5px;color:var(--muted);text-decoration:none;font-family:'Geist Mono',monospace;font-size:12px;">
            {host}
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/></svg>
          </a>
        </td>
        <td style="padding:14px 16px;">
          <span style="display:inline-flex;align-items:center;gap:6px;padding:3px 9px 3px 8px;border-radius:999px;font-size:12px;font-weight:550;background:{status_bg};color:{status_color};">
            <span style="width:7px;height:7px;border-radius:50%;background:{status_color};"></span>
            {status_label}
          </span>
        </td>
        <td style="padding:14px 16px;color:var(--text);">{last_job}</td>
        <td style="padding:14px 16px;color:var(--muted);font-family:'Geist Mono',monospace;font-size:12px;">{last_checked}</td>
        <td style="padding:14px 22px;text-align:right;">{"" if locked else ""}</td>
      </tr>
    """, unsafe_allow_html=True)

st.markdown("</tbody></table></div></section>", unsafe_allow_html=True)

# Add Company form (expander)
with st.expander("➕ Add Company", expanded=False):
    col1, col2, col3 = st.columns([2, 3, 1])
    with col1:
        new_name = st.text_input("Company name", placeholder="Infosys", label_visibility="visible", key="new_name")
    with col2:
        new_url = st.text_input("Career page URL", placeholder="https://careers.infosys.com", label_visibility="visible", key="new_url")
    with col3:
        st.markdown("<br>", unsafe_allow_html=True)
        if st.button("Add", key="btn_add"):
            name = new_name.strip()
            url = new_url.strip()
            if not name:
                show_toast("Company name is required", "error")
            elif not url or not url.startswith("http"):
                show_toast("Enter a valid URL starting with http(s)://", "error")
            else:
                already = any(c.get("name", "").lower() == name.lower() for c in companies)
                if already:
                    show_toast(f"{name} is already tracked", "error")
                else:
                    companies.append({
                        "id": f"c{int(time.time())}",
                        "name": name,
                        "url": url,
                        "locked": False,
                        "status": "unknown",
                        "last_job": "",
                        "last_checked": "",
                    })
                    _save_json(BASE / "companies.json", companies)
                    try:
                        from config_store import _commit_file
                        _commit_file(BASE / "companies.json", "companies.json", f"chore: add {name}")
                    except Exception:
                        pass
                    show_toast(f"{name} added — will be checked next run", "success")
                    st.rerun()

# Remove non-locked companies
non_locked = [c for c in companies if not c.get("locked", False)]
if non_locked:
    st.markdown("<div style='margin-bottom:8px;'>", unsafe_allow_html=True)
    remove_name = st.selectbox(
        "Remove a company",
        options=["— select to remove —"] + [c["name"] for c in non_locked],
        key="remove_sel",
    )
    if remove_name and remove_name != "— select to remove —":
        if st.button(f"Remove {remove_name}", key="btn_remove"):
            companies = [c for c in companies if c.get("name") != remove_name]
            _save_json(BASE / "companies.json", companies)
            try:
                from config_store import _commit_file
                _commit_file(BASE / "companies.json", "companies.json", f"chore: remove {remove_name}")
            except Exception:
                pass
            show_toast(f"{remove_name} removed", "success")
            st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)

# ═══════════════════════════════════════════════════════════════════════════════
# TWO COLUMNS: RECENT ALERTS + NOTIFICATION SETTINGS
# ═══════════════════════════════════════════════════════════════════════════════
col_alerts, col_settings = st.columns([2, 1], gap="medium")

# ── Recent Alerts ─────────────────────────────────────────────────────────────
with col_alerts:
    alert_count = len(seen_jobs)
    st.markdown(f"""
<section style="background:var(--card);border:1px solid var(--border);border-radius:16px;box-shadow:var(--shadow);overflow:hidden;">
  <div style="display:flex;align-items:center;gap:10px;padding:18px 22px;border-bottom:1px solid var(--border);">
    <div style="margin-right:auto;">
      <h2 style="margin:0;font-size:16px;font-weight:640;letter-spacing:-0.02em;">Recent Alerts</h2>
      <p style="margin:3px 0 0;font-size:12.5px;color:var(--muted);">New fresher postings, newest first</p>
    </div>
    <span style="font-size:12px;font-weight:600;color:var(--accent-hex);background:var(--accent-soft-hex);padding:4px 10px;border-radius:999px;">{alert_count} new</span>
  </div>
""", unsafe_allow_html=True)

    if seen_jobs:
        for job in seen_jobs[:20]:
            title = job.get("title", "Untitled")
            company = job.get("company", "")
            date = job.get("date", "")
            url = job.get("url", "#")
            truncated = (title[:46] + "…") if len(title) > 47 else title
            st.markdown(f"""
  <div style="display:flex;align-items:center;gap:14px;padding:15px 22px;border-top:1px solid var(--border);">
    <span style="width:34px;height:34px;border-radius:9px;background:var(--accent-soft-hex);color:var(--accent-hex);display:grid;place-items:center;flex-shrink:0;">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="20" height="14" x="2" y="7" rx="2"/><path d="M16 21V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v16"/></svg>
    </span>
    <div style="min-width:0;margin-right:auto;">
      <div style="font-size:13.5px;font-weight:570;letter-spacing:-0.01em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">{truncated}</div>
      <div style="font-size:12px;color:var(--muted);margin-top:2px;">{company} &middot; <span style="font-family:'Geist Mono',monospace;">{date}</span></div>
    </div>
    <a href="{url}" target="_blank" rel="noopener" style="flex-shrink:0;display:inline-flex;align-items:center;gap:6px;padding:7px 12px;border:1px solid var(--border-strong);background:var(--card-2);color:var(--text);border-radius:8px;text-decoration:none;font-size:12.5px;font-weight:550;">
      Apply
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/></svg>
    </a>
  </div>
""", unsafe_allow_html=True)
    else:
        st.markdown("""
  <div style="padding:56px 22px;text-align:center;">
    <div style="width:46px;height:46px;border-radius:12px;background:var(--card-2);border:1px solid var(--border);display:inline-grid;place-items:center;color:var(--faint);">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M6 8a6 6 0 0 1 12 0c0 7 3 9 3 9H3s3-2 3-9"/><path d="M10.3 21a1.94 1.94 0 0 0 3.4 0"/></svg>
    </div>
    <p style="margin:14px 0 4px;font-size:14px;font-weight:570;">No alerts yet</p>
    <p style="margin:0;font-size:12.5px;color:var(--muted);">We'll email you the moment a new fresher role appears.</p>
  </div>
""", unsafe_allow_html=True)

    st.markdown("</section>", unsafe_allow_html=True)

# ── Notification Settings ──────────────────────────────────────────────────────
with col_settings:
    st.markdown("""
<section style="background:var(--card);border:1px solid var(--border);border-radius:16px;box-shadow:var(--shadow);padding:22px;">
  <div style="display:flex;align-items:center;gap:10px;margin-bottom:4px;">
    <span style="width:30px;height:30px;border-radius:8px;background:var(--accent-soft-hex);color:var(--accent-hex);display:grid;place-items:center;">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="20" height="16" x="2" y="4" rx="2"/><path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"/></svg>
    </span>
    <h2 style="margin:0;font-size:16px;font-weight:640;letter-spacing:-0.02em;">Notification Settings</h2>
  </div>
  <p style="margin:0 0 18px 40px;font-size:12.5px;color:var(--muted);">Where alert emails are delivered.</p>
""", unsafe_allow_html=True)

    email_val = st.text_input(
        "Recipient email",
        value=st.session_state.email_val,
        placeholder="you@example.com",
        key="email_input",
    )
    st.session_state.email_val = email_val

    col_save, col_gap = st.columns([1, 2])
    with col_save:
        if st.button("Save", key="btn_save_email"):
            e = email_val.strip()
            import re as _re
            if not _re.match(r"[^@\s]+@[^@\s]+\.[^@\s]+", e):
                show_toast("Enter a valid email address", "error")
            else:
                settings["recipient_email"] = e
                _save_json(BASE / "settings.json", settings)
                try:
                    from config_store import _commit_file
                    _commit_file(BASE / "settings.json", "settings.json", "chore: update recipient email")
                except Exception:
                    pass
                show_toast(f"Saved — alerts go to {e}", "success")

    st.markdown("""
  <div style="height:1px;background:var(--border);margin:20px 0;"></div>
  <div style="font-size:12px;font-weight:600;color:var(--muted);margin-bottom:7px;">Verify delivery</div>
  <p style="margin:0 0 12px;font-size:12.5px;color:var(--muted);">Send yourself a sample alert to confirm emails arrive.</p>
""", unsafe_allow_html=True)

    if st.button("✈ Send Test Mail", key="btn_test", use_container_width=True):
        recipient = settings.get("recipient_email", "").strip()
        if not recipient:
            show_toast("Save a recipient email first", "error")
        else:
            try:
                import sys
                sys.path.insert(0, str(BASE))
                from notifier import test_mail
                test_mail()
                show_toast(f"Test email sent — check {recipient}", "success")
            except Exception as ex:
                show_toast(f"Failed: {ex}", "error")

    st.markdown("</section>", unsafe_allow_html=True)

# ── Footer ─────────────────────────────────────────────────────────────────────
st.markdown("""
</div>
<footer style="max-width:1120px;margin:32px auto 0;padding:0 24px 48px;display:flex;align-items:center;gap:10px;flex-wrap:wrap;color:var(--faint);font-size:12px;font-family:'Geist',sans-serif;">
  <span>Fresher Job Tracker &middot; internal tool</span>
  <span style="margin-left:auto;display:inline-flex;align-items:center;gap:6px;">
    <a href="https://github.com/manojanumolu/job-tracker" target="_blank" rel="noopener" style="display:inline-flex;align-items:center;gap:6px;color:var(--muted);text-decoration:none;">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 19c-5 1.5-5-2.5-7-3m14 6v-3.87a3.37 3.37 0 0 0-.94-2.61c3.14-.35 6.44-1.54 6.44-7A5.44 5.44 0 0 0 20 4.77 5.07 5.07 0 0 0 19.91 1S18.73.65 16 2.48a13.38 13.38 0 0 0-7 0C6.27.65 5.09 1 5.09 1A5.07 5.07 0 0 0 5 4.77a5.44 5.44 0 0 0-1.5 3.78c0 5.42 3.3 6.61 6.44 7A3.37 3.37 0 0 0 9 18.13V22"/></svg>
      Source repository
    </a>
  </span>
</footer>
""", unsafe_allow_html=True)

# ── Toast notifications ────────────────────────────────────────────────────────
if st.session_state.toast:
    msg = st.session_state.toast
    kind = st.session_state.toast_type
    icon_bg = "var(--good-soft-hex)" if kind == "success" else "var(--bad-soft-hex)"
    icon_color = "var(--good-hex)" if kind == "success" else "var(--bad-hex)"
    icon_svg = (
        '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>'
        if kind == "success" else
        '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 8v4"/><path d="M12 16h.01"/></svg>'
    )
    st.markdown(f"""
<div style="position:fixed;bottom:24px;right:24px;z-index:9999;display:flex;align-items:center;gap:11px;padding:13px 16px;border-radius:12px;background:var(--card);border:1px solid var(--border-strong);box-shadow:0 10px 30px rgba(0,0,0,.18);max-width:340px;animation:fjt-toast-in .28s cubic-bezier(.2,.8,.2,1);">
  <span style="width:26px;height:26px;border-radius:7px;flex-shrink:0;display:grid;place-items:center;background:{icon_bg};color:{icon_color};">
    {icon_svg}
  </span>
  <div style="font-size:13px;font-weight:500;line-height:1.4;">{msg}</div>
</div>
<style>
@keyframes fjt-toast-in {{ from {{ opacity:0;transform:translateY(12px) scale(.98); }} to {{ opacity:1;transform:none; }} }}
</style>
""", unsafe_allow_html=True)
    time.sleep(3)
    st.session_state.toast = None
    st.rerun()
