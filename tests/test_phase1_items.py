"""Regression tests for the remaining audit items A–G (Sep 30 follow-up)."""
import json
import re
from datetime import datetime, timedelta, timezone

from test_streamlit_app import _html, _nav, _qp, app  # noqa: F401  (fixture re-export)

NOW = datetime.now(timezone.utc)


def _set_companies(at, companies):
    (at.tmp_path / "companies.json").write_text(json.dumps(companies))


def _rec(title, url, company="Sanofi", **extra):
    return {"title": title, "url": url, "company": company, "location": "Pune · India", "category": "FRESHER",
            "reason": "experience starts at 0: '0-1 years'", "date": "Sep 29, 18:57", "id": f"{company}_{title}",
            "notified": True, **extra}


# ── B: scan timing is described truthfully ───────────────────────────────────

def test_gap_within_real_github_cadence_is_not_delayed(app):
    """Measured scheduled-run gaps: median 5 h, max 9 h — an 8.5 h old scan is normal."""
    at = app([])
    _set_companies(at, [{"id": "a", "name": "Alpha", "url": "https://a.example/jobs", "status": "active",
                         "last_checked": (NOW - timedelta(hours=8, minutes=30)).isoformat()}])
    _nav(at, "monitoring")
    html = _html(at)
    assert "<i></i>Healthy</span>" in html and "Delayed</span>" not in html
    assert "Scans delayed" not in html


def test_no_exact_countdown_is_promised(app):
    at = app([])
    html = _html(at)
    assert "Next scan in" not in html and "due now" not in html.lower()
    assert "Next scheduled" in html
    _nav(at, "monitoring")
    html = _html(at)
    assert "GitHub may start it later" in html and "Every 3 hours (UTC)" not in html
    assert "may start scans later" in html


# ── C: one meaning for each count ────────────────────────────────────────────

def test_active_counts_are_consistent_across_pages(app):
    jobs = [_rec("Graduate A", "https://sanofi.wd3.myworkdayjobs.com/S/job/P/A_R1000001"),
            _rec("Graduate B", "https://sanofi.wd3.myworkdayjobs.com/S/job/P/B_R1000002", dismissed=True),
            _rec("Graduate C", "https://sanofi.wd3.myworkdayjobs.com/S/job/P/C_R1000003", dismissed=True)]
    at = app(jobs)
    _set_companies(at, [{"id": "sanofi", "name": "Sanofi", "url": "https://sanofi.wd3.myworkdayjobs.com/S",
                         "status": "active", "last_checked": NOW.isoformat()}])
    at.run()
    assert '<div class="k">Active jobs</div><div class="v">1</div>' in _html(at)
    _nav(at, "companies")
    html = _html(at)
    assert "<span>Active jobs</span>" in html and "1<span class=\"l\"> active jobs</span>" in html
    _nav(at, "monitoring")
    assert '<td class="num" data-l="Active jobs">1</td>' in _html(at)
    at.button(key="nav_companies").click().run()
    at.button(key=[b.key for b in at.button if b.key and b.key.startswith("view_")][0]).click().run()
    assert "1 active · 2 dismissed" in _html(at)
    # the Jobs page keeps "found" = everything recorded
    _nav(at, "jobs")
    assert "3 jobs found by the tracker · 1 active · 2 dismissed" in _html(at)


# ── E: theme survives a reload ───────────────────────────────────────────────

def test_dark_theme_is_kept_in_the_url(app):
    at = app([], page="settings")
    at.button(key="btn_theme").click().run()
    assert at.session_state.dark_mode is True and _qp(at, "theme") == ["dark"]
    _nav(at, "jobs")
    assert _qp(at, "theme") == ["dark"]            # kept while navigating
    reloaded = app([], query={"page": "home", "theme": "dark"})   # a browser reload
    assert reloaded.session_state.dark_mode is True
    assert "#0b1020" in _html(reloaded)
    light = app([], query={"page": "home"})
    assert light.session_state.dark_mode is False and "theme" not in light.query_params


# ── G: company rename keeps its jobs and never re-alerts ─────────────────────

def test_jobs_follow_the_company_id_after_a_rename(app):
    new = _rec("Associate QA", "https://careers.npci.org.in/jobs/Careers/190737000005689329/Associate-QA",
               company="npcl", company_id="c1783687965")
    legacy = _rec("Old Associate", "https://careers.npci.org.in/jobs/Careers/190737000001111111/Old", company="NPCI")
    at = app([new, legacy])
    _set_companies(at, [{"id": "c1783687965", "name": "NPCI", "url": "https://careers.npci.org.in/jobs/Careers",
                         "status": "active", "last_checked": NOW.isoformat()}])
    _nav(at, "companies")
    assert "2<span class=\"l\"> active jobs</span>" in _html(at)   # id-linked + name-linked legacy record


def test_renamed_company_does_not_re_alert_known_postings():
    import scraper
    url = "https://careers.npci.org.in/jobs/Careers/190737000005689329/Associate-Quality-Assurance-NBBL"
    seen = [{"title": "Associate Quality Assurance, NBBL", "url": url, "company": "npcl", "location": "Hyderabad",
             "notified": True}]
    again = {"title": "Associate Quality Assurance, NBBL", "url": url, "company": "NPCI", "location": "Hyderabad",
             "category": "FRESHER", "reason": "r"}
    assert scraper.merge_new_jobs(seen, [again], company_id="c1783687965") == []
    other = {**again, "url": url.replace("190737000005689329", "190737000009999999")}
    new = scraper.merge_new_jobs(seen, [other], company_id="c1783687965")
    assert len(new) == 1 and new[0]["company_id"] == "c1783687965"


def test_next_scheduled_time_is_shown_in_ist(app):
    at = app([])
    html = _html(at)
    assert re.search(r"Next scheduled \d{1,2}:\d{2} [AP]M IST", html) and " ist" not in html
    assert not re.search(r"Next scheduled \d{2}:\d{2} UTC", html)


def test_icon_only_buttons_have_accessible_labels(app):
    """The ✕/↺ row buttons and the tablet icon rail are icon-only visually;
    their text labels must exist (hidden visually, read by screen readers)."""
    rec = {"title": "Graduate A", "url": "https://sanofi.wd3.myworkdayjobs.com/S/job/P/A_R1000001",
           "company": "Sanofi", "category": "FRESHER", "reason": "r", "date": "Sep 29, 18:57", "id": "A"}
    at = app([rec, {**rec, "title": "Graduate B", "url": rec["url"].replace("1000001", "1000002"), "id": "B",
                    "dismissed": True}])
    labels = {b.key: b.label for b in at.button if b.key}
    assert all(labels[k] == "Dismiss" for k in labels if k.startswith("dismiss_"))
    _nav(at, "jobs")
    at.pills(key="jobs_tab").set_value("dismissed").run()
    labels = {b.key: b.label for b in at.button if b.key}
    assert [labels[k] for k in labels if k.startswith("restore_")] == ["Restore"]
    import re
    from pathlib import Path
    css = (Path(__file__).resolve().parent.parent / "streamlit_app.py").read_text(encoding="utf-8")
    assert '[class*="st-key-nav_"] p { display: none' not in css      # would remove the rail labels from screen readers
    assert re.search(r'st-key-dismiss_"\] \[data-testid="stMarkdownContainer"\].*clip', css)
