"""Regression tests for the Sep 30 bug & UI audit (rendered-app findings)."""
import json
from datetime import datetime, timedelta, timezone

from test_streamlit_app import _html, _key, _nav, _seen, app  # noqa: F401  (fixture re-export)

NOW = datetime.now(timezone.utc)
GATE = {"real_job_posting": True, "india_location": True, "detail_read": True, "detail_is_this_job": True,
        "no_conflicting_experience": True, "not_programme_story_talent_recruiter": True,
        "evidence_not_staff_context": True, "fresher_or_entry_evidence": True}


def _rec(title, url, company="Sanofi", **extra):
    return {"title": title, "url": url, "company": company, "location": "Hyderabad · India", "category": "FRESHER",
            "reason": "fresher signal: 'Experience: freshers'", "date": "Sep 29, 18:57", "id": f"{company}_{title}",
            "notified": False, "first_seen": (NOW - timedelta(hours=2)).isoformat(),
            "evidence": {"experience": ["no experience requirement stated"],
                         "experience_lines": ["Experience: freshers / Prior experience in pharma sales is preferred"],
                         "fresher_evidence": "Experience: freshers", "detail_read": True,
                         "detail_match": "Workday record for the listing's path + same title",
                         "job_id": "workday:R2867486", "canonical_url": url, "checks": dict(GATE)}, **extra}


SALES = _rec("Scientific Sales Executive",
             "https://sanofi.wd3.myworkdayjobs.com/SanofiCareers/job/Bangalore/Scientific-Sales-Executive_R2867486")


def _set_companies(at, companies):
    (at.tmp_path / "companies.json").write_text(json.dumps(companies))


def test_detail_page_shows_stored_experience_evidence(app):
    at = app([SALES])
    at.button(key=_key("crit", SALES)).click().run()
    html = _html(at)
    assert "Not stated in the captured text" not in html
    assert "Experience: freshers" in html
    # the stored safety-gate evidence is visible (auditability)
    assert "workday:R2867486" in html and "8 of 8 checks passed" in html


def test_monitoring_hides_internal_scan_notes(app):
    at = app([])
    _set_companies(at, [
        {"id": "pwc", "name": "PwC", "url": "https://pwc.wd3.myworkdayjobs.com/Global_Experienced_Careers",
         "status": "active", "last_checked": NOW.isoformat(),
         "status_reason": "India facet ['locations']; India facet ['locations']"},
        {"id": "x", "name": "X", "url": "https://x.example/jobs", "status": "active", "last_checked": NOW.isoformat(),
         "status_reason": "", "scan_note": "12 postings left for the next scan (detail-page budget)"},
    ])
    _nav(at, "monitoring")
    html = _html(at)
    assert "India facet" not in html and "locationCountry" not in html
    assert "12 postings left for the next scan" in html     # useful notes stay, in plain words


def test_back_from_a_job_opened_on_a_company_page_returns_to_that_company(app):
    at = app([SALES], query={"page": "companies", "company": "sanofi"})
    assert '<h1 class="detail-title">Sanofi</h1>' in _html(at)
    at.button(key=_key("crit", SALES)).click().run()
    assert "Back to Sanofi" in [b.label for b in at.button]
    at.button(key="btn_back").click().run()
    assert not at.exception and '<h1 class="detail-title">Sanofi</h1>' in _html(at)


def test_restoring_a_never_emailed_job_does_not_email_it_later(app):
    from config_store import alert_pending
    at = app([{**SALES, "dismissed": True}])
    _nav(at, "jobs")
    at.pills(key="jobs_tab").set_value("dismissed").run()
    at.button(key=_key("restore", SALES)).click().run()
    rec = _seen(at)[0]
    assert not rec.get("dismissed")
    assert not alert_pending(rec)          # the tooltip promises it won't be emailed again


def test_test_email_does_not_change_the_saved_recipient(app, monkeypatch):
    import notifier
    sent = []
    monkeypatch.setattr(notifier, "test_mail", lambda to: sent.append(to))
    at = app([], page="email")
    at.text_input(key="email_input").set_value("someone.else@example.com").run()
    at.button(key="btn_test").click().run()
    settings = json.loads((at.tmp_path / "settings.json").read_text())
    assert settings["recipient_email"] == "me@example.com"
    assert "not saved" in _html(at).lower() or "Save" in _html(at)


def test_dismiss_all_needs_confirmation(app):
    at = app([SALES], page="settings")
    at.button(key="btn_clear_all").click().run()
    assert not _seen(at)[0].get("dismissed")            # first click only asks
    assert "btn_clear_all_confirm" in {b.key for b in at.button}
    assert "cancels" in _html(at) or "won't be emailed" in _html(at)
    at.button(key="btn_clear_all_confirm").click().run()
    assert _seen(at)[0].get("dismissed") is True


def test_company_status_filter_survives_the_status_disappearing(app):
    at = app([])
    _set_companies(at, [{"id": "a", "name": "Alpha", "url": "https://a.example/jobs", "status": "broken",
                         "last_checked": NOW.isoformat()},
                        {"id": "b", "name": "Beta", "url": "https://b.example/jobs", "status": "active",
                         "last_checked": NOW.isoformat()}])
    _nav(at, "companies")
    at.pills(key="co_status").set_value("failing").run()
    _set_companies(at, [{"id": "a", "name": "Alpha", "url": "https://a.example/jobs", "status": "active",
                         "last_checked": NOW.isoformat()},
                        {"id": "b", "name": "Beta", "url": "https://b.example/jobs", "status": "active",
                         "last_checked": NOW.isoformat()}])
    at.run()
    assert not at.exception
    assert "Alpha" in _html(at) and "Beta" in _html(at)


def test_duplicate_records_do_not_crash_the_lists(app):
    at = app([SALES, dict(SALES)])
    assert not at.exception
    _nav(at, "jobs")
    assert not at.exception


def test_up_to_range_is_not_described_as_no_experience():
    from notifier import friendly_reason
    assert friendly_reason({"category": "FRESHER", "reason": "experience starts at 0: 'Up to 2 years'"}) == \
        "Up to 2 years experience"
    assert friendly_reason({"category": "FRESHER", "reason": "experience starts at 0: 'Up to 6 months'"}) == \
        "Up to 6 months experience"
    assert friendly_reason({"category": "FRESHER", "reason": "experience starts at 0: '0-3yrs'"}) == "0–3 years experience"
    assert friendly_reason({"category": "FRESHER", "reason": "experience starts at 0: 'Minimum 0 year'"}) == \
        "No prior experience required"


def test_email_page_last_alert_uses_delivery_time(app):
    sent_at = (NOW - timedelta(days=2)).replace(microsecond=0)
    at = app([{**SALES, "notified": True, "notify_state": "sent", "notified_at": sent_at.isoformat(),
               "date": (NOW - timedelta(days=5)).strftime("%b %d, %H:%M")}], page="email")
    assert f"{sent_at:%b %d, %H:%M} UTC" in _html(at)


def test_healthy_scan_keeps_technical_notes_out_of_status_reason(monkeypatch):
    import httpx
    import scraper
    postings = [{"title": "Graduate Trainee", "locationsText": "Pune", "externalPath": "/job/g_R123456"}]
    real = httpx.Client
    monkeypatch.setattr(scraper.httpx, "Client", lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(
        lambda r: httpx.Response(200, json={"jobPostings": postings, "facets": [{"facetParameter": "locationCountry",
                  "values": [{"descriptor": "India", "id": "in"}]}]}) if r.method == "POST" else
        httpx.Response(200, json={"jobPostingInfo": {"jobDescription": "<p>Open to 2025 graduates.</p>",
                                                     "location": "Pune", "country": {"descriptor": "India"}}}))}))
    scan = scraper.scan_company({"id": "sanofi", "name": "Sanofi", "url": "x"})
    assert scan.status == "active" and scan.reason == "" and scan.user_note == ""
    assert any("India facet" in n for n in scan.notes)        # still logged for debugging


def test_phone_layout_rules():
    """Rendered-app findings: every phone nav destination visible (no hidden
    horizontal scroll), job cards keep their category/why line, stats wrap."""
    import re
    from pathlib import Path
    css = (Path(__file__).resolve().parent.parent / "streamlit_app.py").read_text(encoding="utf-8")
    phone = css[css.index("@media (max-width: 767px)"):]
    phone = phone[:phone.index('"""')]
    assert ".job-why { display: none" not in phone
    nav_row = re.search(r"\.st-key-mnav > \[data-testid=\"stLayoutWrapper\"\][^{]*\{([^}]*)\}", phone).group(1)
    assert "overflow-x" not in nav_row
    assert re.search(r"\.st-key-mnav \[data-testid=\"stColumn\"\] \{ flex: 1 1 0", phone)
    assert ".stat .v, .stat .n { white-space: normal; }" in phone


def test_evidence_lines_put_the_decisive_line_first():
    import scraper
    from sources import Detail, Listing
    listing = Listing(title="IN_Associate _ Internal Audit", location="Bengaluru Millenia", posting_evidence=True,
                      url="https://pwc.wd3.myworkdayjobs.com/Global_Experienced_Careers/job/B/IN-Senior-Associate_570925WD-1")
    detail = Detail(ok=True, location="Bengaluru Millenia | India", posting_evidence=True, matched="x", description=(
        "Experience in Internal Audit/ Process Audit concepts & methodology\n"
        "Experience in Internal Audit/ Process Audit concepts & methodology\n"
        "Our medicines reach more than 100 million people each year.\n"
        "Years of experience required:\n0-3yrs"))
    result, job = scraper.decide("PwC", listing, detail)
    ev = job["evidence"]
    assert ev["experience_lines"][0] == "0-3yrs"
    assert len(ev["experience_lines"]) == len(set(ev["experience_lines"]))
    assert not any("each year" in ln for ln in ev["experience_lines"])
    assert ev["job_id"] == "workday:570925WD-1"
