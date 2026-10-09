"""Regression tests for the Oct 2026 visual redesign: the new components must
stay truthful, accessible and lightweight, and never change behaviour."""
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from test_streamlit_app import NEW_RECORD, _html, _key, _nav, app  # noqa: F401  (fixture re-export)

NOW = datetime.now(timezone.utc)
SRC = (Path(__file__).resolve().parent.parent / "streamlit_app.py").read_text(encoding="utf-8")
CSS = SRC[SRC.index('_CSS = """'):SRC.index('"""', SRC.index('_CSS = """') + 10)]

GATE = {k: True for k in ("real_job_posting", "india_location", "detail_read", "detail_is_this_job",
                          "no_conflicting_experience", "not_programme_story_talent_recruiter",
                          "evidence_not_staff_context", "fresher_or_entry_evidence")}
GATED = {"title": "Graduate Trainee", "url": "https://sanofi.wd3.myworkdayjobs.com/S/job/Pune/Graduate-Trainee_R1234567",
         "company": "Sanofi", "location": "Pune · India", "category": "FRESHER",
         "reason": "experience starts at 0: '0-1 years'", "date": "Sep 29, 18:57", "id": "Sanofi_Graduate_Trainee",
         "notified": True, "notify_state": "sent",
         "evidence": {"experience": ["0-1 years"], "experience_lines": ["Experience: 0-1 years"],
                      "fresher_evidence": "0-1 years", "detail_read": True, "detail_match": "same title",
                      "job_id": "workday:R1234567", "checks": dict(GATE)}}


def job_key(job):
    from config_store import job_key as _jk
    return _jk(job)


def _set_companies(at, companies):
    (at.tmp_path / "companies.json").write_text(json.dumps(companies))


# ── Home ─────────────────────────────────────────────────────────────────────

def test_home_hero_states_the_purpose_and_live_status(app):
    at = app([GATED])
    html = _html(at)
    assert '<h1 class="page-title">Discover your next <em>opportunity</em></h1>' in html
    assert "Discover jobs" in html                                  # page name kept as the eyebrow
    assert 'class="chip live healthy"' in html or 'class="chip live' in html
    assert "Last scan" in html and re.search(r"Next scheduled \d{1,2}:\d{2} [AP]M IST", html)
    # no invented AI claims or vanity numbers
    assert "AI-powered" not in html and "Verified" not in html


def test_hero_opportunity_cards_are_abstract_decoration(app):
    """The hero shows a few crisp job cards: hidden from screen readers, no
    company names or fake listings, and none of the tracking language."""
    at = app([])
    html = _html(at)
    cards = re.search(r'<div class="opps-wrap" aria-hidden="true">(.*?<span class="spark-dot"></span></div>)', html, re.S).group(1)
    assert cards.count('<div class="oc ') == 4
    for icon in ("work", "apartment", "school", "task_alt"):          # job, company, entry-level, fresher check
        assert f'aria-hidden="true">{icon}</span>' in cards
    words = re.sub(r"<[^>]+>", " ", cards).split()                     # only icon ligature names, no text
    assert set(words) <= {"work", "apartment", "school", "task_alt", "check"}
    for gone in ("aurora", "radar", "globe", "orbit"):
        assert gone not in html.lower()
    for gone in ('class="node', 'class="pt', 'class="ln', 'class="flow', "portals monitored"):
        assert gone not in html
    for gone in (".flow ", ".flow .node", ".aurora .node", "border-top: 1.5px solid"):
        assert gone not in CSS


def test_radar_is_gone_for_good(app):
    """Job Tracker has its own identity; the radar belongs to another product."""
    at = app([])
    html = _html(at)
    assert 'class="radar' not in html and "on radar" not in html and "blip" not in html
    assert ".radar" not in CSS and "conic-gradient" not in CSS and "jt-sweep" not in CSS
    assert ":material/radar:" not in SRC and '_ms("radar")' not in SRC


def test_brand_subtitle_has_no_radar(app):
    html = _html(app([]))
    assert '<div class="s">Fresher opportunities</div>' in html
    for gone in ("job radar", "india entry-level opportunities"):
        assert gone not in html.lower()


def test_web_fonts_are_imported_first_so_they_load():
    """@import is ignored unless it is the first rule of a stylesheet."""
    assert 'st.html(f"<style>{_CSS}\\n{_root_vars}</style>")' in SRC
    assert CSS.split('"""', 1)[1].lstrip().startswith("@import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans")
    assert "JetBrains" not in SRC and "--font: 'Plus Jakarta Sans'" in CSS


def test_sidebar_has_status_but_no_repository_link(app):
    at = app([])
    html = _html(at)
    assert "Source on GitHub" not in html and "Source on GitHub" not in SRC
    foot = re.search(r'<div class="side-foot[^"]*" role="status">(.*?)</div>\s*</div>', html, re.S).group(1)
    assert "monitored" in foot and "Last scan" in foot and "github" not in foot.lower()


def test_failing_portal_shows_as_a_warning_contact(app):
    at = app([])
    _set_companies(at, [{"id": "a", "name": "Alpha", "url": "https://a.example/jobs", "status": "broken",
                         "last_checked": NOW.isoformat(), "status_reason": "HTTP 403"},
                        {"id": "b", "name": "Beta", "url": "https://b.example/jobs", "status": "active",
                         "last_checked": NOW.isoformat()}])
    at.run()
    html = _html(at)
    # the hero says so in words (the decorative aurora carries no status)
    assert 'class="chip live failing"' in html and "1 portal failing" in html


def test_metric_modules_keep_real_numbers(app):
    at = app([GATED])
    html = _html(at)
    assert '<div class="k">Active jobs</div><div class="v">1</div>' in html
    assert '<div class="k">New this week</div>' in html


# ── job cards & company identity ─────────────────────────────────────────────

def test_job_card_badge_and_meta_are_truthful(app):
    at = app([GATED, NEW_RECORD])
    html = _html(at)
    assert '<span class="pill fresher"><span class="ms " aria-hidden="true">task_alt</span>Fresher</span>' in html
    assert '<span class="pill entry">' in html and "Entry level</span>" in html
    assert "Pune · India" in html and "0–1 years experience" in html


def test_tracked_companies_get_distinct_avatar_tints(app):
    at = app([])
    _nav(at, "companies")
    html = _html(at)
    hues = re.findall(r'<div class="logo(?: ini2)?" style="--h:(\d+)" aria-hidden="true">', html)
    marks = html.count('<div class="logo co-mark" style="--tile:')         # companies with their own mark
    n = len(json.loads((at.tmp_path / "companies.json").read_text("utf-8")))
    assert marks and len(hues) + marks == n and len(set(hues)) == min(len(hues), 10)


def test_icon_only_row_actions_still_have_text_labels(app):
    at = app([GATED, {**GATED, "url": GATED["url"].replace("1234567", "7654321"), "id": "B", "dismissed": True}])
    labels = {b.key: b.label for b in at.button if b.key}
    assert [labels[k] for k in labels if k.startswith("dismiss_")] == ["Dismiss"]
    assert re.search(r'st-key-dismiss_"\] \[data-testid="stMarkdownContainer"\].*clip', CSS)


# ── job details: evidence panel ──────────────────────────────────────────────

def test_evidence_panel_lists_every_check_with_a_text_status(app):
    at = app([GATED])
    at.button(key=_key("crit", GATED)).click().run()
    html = _html(at)
    assert "8 of 8 checks passed" in html
    assert html.count('<span class="sr-only">Passed: </span>') == 8       # not colour/icon-only
    assert "Verified" not in html and "guarantee" not in html.lower()    # never claims more than the checks


def test_failed_check_is_shown_as_failed(app):
    rec = json.loads(json.dumps(GATED))
    rec["evidence"]["checks"]["india_location"] = False
    at = app([rec])
    at.button(key=_key("crit", rec)).click().run()
    html = _html(at)
    assert "7 of 8 checks passed" in html and '<span class="sr-only">Failed: </span>Located in India' in html


def test_detail_dismiss_is_styled_destructive_and_still_works(app):
    at = app([GATED])
    at.button(key=_key("crit", GATED)).click().run()
    at.button(key="detail_dismiss").click().run()
    assert not at.exception
    seen = json.loads((at.tmp_path / "seen_jobs.json").read_text("utf-8"))
    assert seen[0]["dismissed"] is True and seen[0]["notified"] is True     # dismissing never re-queues an email


# ── monitoring & email ───────────────────────────────────────────────────────

def test_monitoring_calm_state_only_when_everything_is_healthy(app):
    at = app([])
    _set_companies(at, [{"id": "a", "name": "Alpha", "url": "https://a.example/jobs", "status": "active",
                         "last_checked": NOW.isoformat()}])
    _nav(at, "monitoring")
    assert "No monitoring issues." in _html(at)
    _set_companies(at, [{"id": "a", "name": "Alpha", "url": "https://a.example/jobs", "status": "failing",
                         "last_checked": NOW.isoformat(), "status_reason": "HTTP 429"},
                        {"id": "b", "name": "Beta", "url": "https://b.example/jobs", "status": "active",
                         "last_checked": (NOW - timedelta(hours=30)).isoformat()}])
    at.run()
    html = _html(at)
    assert "No monitoring issues." not in html and "HTTP 429" in html and "<i></i>Delayed</span>" in html


def test_monitoring_keeps_semantic_table_and_hides_empty_notes(app):
    at = app([])
    _set_companies(at, [{"id": "a", "name": "Alpha", "url": "https://a.example/jobs", "status": "active",
                         "last_checked": NOW.isoformat(), "scan_note": ""}])
    _nav(at, "monitoring")
    html = _html(at)
    assert "<table class=\"mon\"><thead>" in html and "<th>Company</th>" in html
    assert '<td data-l="Notes" class="empty">—</td>' in html


def test_email_status_card_says_whether_alerts_work(app):
    at = app([GATED], page="email")
    html = _html(at)
    assert "Alerts are on" in html and "Nothing waiting to send" in html and "Jobs emailed" in html
    (at.tmp_path / "settings.json").write_text(json.dumps({"recipient_email": ""}))
    at.run()
    assert "Alerts are off" in _html(at)


# ── browser Back / Forward (Streamlit 1.65 keeps the session on Back) ────────

def test_browser_back_changes_the_view_to_the_url(app):
    at = app([GATED])
    at.button(key=_key("crit", GATED)).click().run()
    assert at.session_state.page == "jobs" and at.session_state.job_id
    _nav(at, "companies")
    assert at.session_state.page == "companies" and not at.session_state.job_id
    # the browser goes Back: the URL returns to the job page within the same session
    job_id = hashlib.sha1(job_key(GATED).encode("utf-8")).hexdigest()[:12]
    at.query_params.clear()
    at.query_params["page"] = "jobs"
    at.query_params["job"] = job_id
    at.run()
    assert not at.exception
    assert at.session_state.page == "jobs" and at.session_state.job_id == job_id
    assert '<h1 class="detail-title">Graduate Trainee</h1>' in _html(at)


def test_in_app_navigation_still_wins_over_an_unchanged_url(app):
    at = app([GATED])
    _nav(at, "monitoring")
    _nav(at, "settings")
    assert at.session_state.page == "settings" and '<h1 class="page-title">Settings</h1>' in _html(at)
    at.button(key="btn_theme").click().run()
    assert at.session_state.dark_mode is True                      # theme toggle is not undone by the URL check


# ── CSS contract: motion, accessibility, phone ───────────────────────────────

def test_motion_respects_reduced_motion_and_stays_on_the_compositor():
    rm = CSS[CSS.index("@media (prefers-reduced-motion: reduce)"):]
    assert "animation-duration: .001ms" in rm and ".oc, .oc::after, .opps > .glow-bg { animation: none !important; }" in rm   # cards rest in place
    assert '[data-testid="stBaseButton-primary"]::after { display: none; }' in rm   # no light sweep either
    for name, body in re.findall(r"@keyframes ([\w-]+) \{(.*?)\}\s*\}", CSS, re.S):
        props = set(re.findall(r"([a-z-]+)\s*:", body))
        assert props <= {"opacity", "transform", "background-position"}, (name, props)
    # top-level entrances are opacity-only so they never trap the fixed toast
    assert '[data-testid="stVerticalBlock"] > * { animation: jt-fade' in CSS


def test_focus_states_exist_for_buttons_and_links():
    assert '[data-testid^="stBaseButton"]:focus-visible' in CSS
    assert ".btn:focus-visible" in CSS


def test_no_new_heavy_dependencies():
    assert "<script" not in SRC.lower()
    imports = set(re.findall(r'@import url\(\'https://fonts\.googleapis\.com/css2\?family=([^:&\']+)', CSS))
    assert imports <= {"Plus+Jakarta+Sans", "Material+Symbols+Rounded"}
    req = (Path(__file__).resolve().parent.parent / "requirements.txt").read_text()
    # Authlib: required by Streamlit's st.login (Google sign-in, Firebase auth spike)
    # firebase-admin: per-user data in Firestore (Phase 3), imported only once a service account is configured
    assert set(l.split(">=")[0] for l in req.split()) == {"streamlit", "httpx", "PyGithub", "python-dotenv", "playwright",
                                                          "Authlib", "firebase-admin"}


# ── schedule: read from the workflow's cron (UTC), shown in IST ─────────────

def _schedule_ns():
    code = SRC[SRC.index("IST = timezone("):SRC.index("def _ms(name: str")]
    ns = {"datetime": datetime, "timedelta": timedelta, "timezone": timezone, "re": re,
          "BASE": Path(__file__).resolve().parent.parent}
    exec(code, ns)
    return ns


def test_schedule_follows_the_workflow_cron():
    ns = _schedule_ns()
    workflow = (Path(__file__).resolve().parent.parent / ".github/workflows/check_jobs.yml").read_text("utf-8")
    assert f'cron: "{ns["_schedule_cron"]()}"' in workflow                 # the real configured value
    u = lambda h, m=0: datetime(2026, 10, 5, h, m, tzinfo=timezone.utc)
    assert ns["_next_slot"](u(16, 59), "0 */3 * * *") == u(18)
    assert ns["_next_slot"](u(18), "0 */3 * * *") == u(21)                  # strictly after now
    assert ns["_next_slot"](u(22, 30), "0 */3 * * *") == u(0) + timedelta(days=1)
    assert ns["_next_slot"](u(10), "30 9 * * *") == u(9, 30) + timedelta(days=1)
    assert ns["_next_slot"](u(1), "15 */6 * * *") == u(6, 15)
    assert ns["_next_slot"](u(1), "not a cron") == u(3)                     # falls back to every 3 h
    assert ns["_cadence"]("0 */3 * * *") == "Every 3 hours" and ns["_cadence"]("0 * * * *") == "Every hour"
    assert ns["_cadence"]("30 9 * * *") == "Daily" and ns["_cadence"]("0 1,5,9 * * *") == "3 times a day"


def test_utc_slots_are_converted_to_ist_for_display():
    ns = _schedule_ns()
    u = lambda h, m=0: datetime(2026, 10, 5, h, m, tzinfo=timezone.utc)
    assert ns["_ist"](u(18)) == "11:30 PM IST"
    assert ns["_ist"](u(0)) == "5:30 AM IST" and ns["_ist"](u(6, 30)) == "12:00 PM IST"
    assert ns["_ist"](u(21)) == "2:30 AM IST"                               # crosses midnight in India
    assert ns["_ist_stamp"](u(20)) == "Oct 06, 1:30 AM IST"


def test_settings_schedule_is_plain_language(app):
    at = app([], page="settings")
    html = _html(at)
    main = re.search(r'<dl class="kv">(.*?)</dl>', html, re.S).group(1)
    assert "Every 3 hours · Next scheduled" in main and " IST" in main
    assert "GitHub may start scheduled scans later than the scheduled time." in main
    assert "cron" not in main and ".github" not in main and "API" not in main
    # the implementation facts still exist, tucked into a collapsed section
    def blocks(node):                       # AppTest lists an expander with an icon as a status block
        for c in getattr(node, "children", {}).values():
            yield c
            yield from blocks(c)
    tech_box = [b.proto for b in blocks(at._tree) if getattr(b, "label", None) == "Technical details"]
    assert len(tech_box) == 1 and not tech_box[0].expanded
    tech = re.search(r'<dl class="kv tech">(.*?)</dl>', html, re.S).group(1)
    assert "0 */3 * * *" in tech and "check_jobs.yml" in tech and "Workday API" in tech
