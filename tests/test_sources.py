"""Company job sources (mocked HTTP), the no-landing-page-fallback rule and
per-company health. Payload shapes mirror the live APIs (Sep 2026)."""
import html
import json

import httpx
import pytest

import scraper
import sources


def _mock(monkeypatch, handler):
    real_client = httpx.Client
    monkeypatch.setattr(scraper.httpx, "Client",
                        lambda *a, **kw: real_client(*a, **{**kw, "transport": httpx.MockTransport(handler)}))


def _no_browser(monkeypatch):
    calls = []
    monkeypatch.setattr(scraper, "_scan_playwright", lambda company: calls.append(company) or scraper.Scan(company["name"]))
    return calls


# ---------------------------------------------------------------------------
# Accenture findjobs API
# ---------------------------------------------------------------------------

def _acc_row(req, title, level, years_line, desc_extra="", locs=("Hyderabad",), bucket="Experience: 0-2 years"):
    return {
        "requisitionId": req, "title": title, "careerLevel": level, "jobProfile": f"{title}",
        "yearsOfExperience": bucket, "country": "India", "location": list(locs),
        "jobDetailUrl": f"https://www.accenture.com/{{0}}/careers/jobdetails?id={req}_en&title={title.replace(' ', '+')}",
        "jobDescription": f"<b>Designation:</b> {title}<br><b>Qualifications:</b>Any Graduation<br>"
                          f"<b>Years of Experience:</b>{years_line}<br>{desc_extra}"
                          "<b>About Accenture</b><br>Combining unmatched experience and specialized skills across more "
                          "than 40 industries. Our 784,000 people deliver on the promise of technology.",
        "qualification": "<p>TBD</p>",
    }


ACC_ROWS = [
    _acc_row("AIOC-1", "Trust & Safety New Associate", "New Associate", "0 to 1 years"),
    _acc_row("AIOC-2", "Recruiting Associate", "Associate", "1 to 3 years"),
    _acc_row("AIOC-3", "Pharmacovigilance Services New Associate", "New Associate", "6months-1year"),
    # ATCI layout: "Good to have skills : NA" used to hide the requirement below it
    _acc_row("ATCI-4", "Custom Software Engineer", "Associate", "",
             "Project Role : Custom Software Engineer<br>Must have skills : Java<br>Good to have skills : NA<br>"
             "Minimum 3 year(s) of experience is required<br>Educational Qualification : 15 years full time education<br>"),
    # level only visible in structured fields
    {**_acc_row("ATCI-5", "Technology Platform Engineer", "Senior Analyst", "0 to 1 years"),
     "jobProfile": "System Development Senior Analyst"},
    _acc_row("AIOC-6", "Order Management New Associate", "New Associate", "0 to 1 years", locs=("Gurugram", "Bengaluru")),
]


def test_accenture_api(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        seen.setdefault("bodies", []).append(body)
        start = int(body.split('name="startIndex"')[1].split("\r\n\r\n")[1].split("\r\n")[0])
        return httpx.Response(200, json={"data": ACC_ROWS if start == 0 else [], "totalHits": {"total": 6}})

    _mock(monkeypatch, handler)
    no_browser = _no_browser(monkeypatch)
    jobs, status = scraper.scrape_company({"id": "accenture", "name": "Accenture", "url": "https://www.accenture.com/in-en/careers"})
    assert status == "active" and not no_browser
    assert sorted((j["title"], j["category"]) for j in jobs) == [
        ("Order Management New Associate", "FRESHER"), ("Trust & Safety New Associate", "FRESHER")]
    by = {j["title"]: j for j in jobs}
    assert by["Trust & Safety New Associate"]["url"] == \
        "https://www.accenture.com/in-en/careers/jobdetails?id=AIOC-1_en&title=Trust+&+Safety+New+Associate"
    assert by["Order Management New Associate"]["location"] == "Gurugram · Bengaluru · India"
    # the request asks Accenture for India, newest first, 0-2 years bucket only
    body = seen["bodies"][0]
    assert 'name="jobCountry"\r\n\r\nIndia' in body and 'name="sortBy"\r\n\r\n2' in body
    assert "yearsOfExperience.keyword" in body and "Experience: 0-2 years" in body


def test_accenture_listing_evidence():
    atci = sources.accenture_listing(ACC_ROWS[3])
    assert atci.detail.ok and "Minimum 3 year(s)" in atci.detail.description
    result, job = scraper.decide("Accenture", atci, atci.detail)
    assert job is None and result.category.value == "EXPERIENCED" and "3" in result.reason
    senior = sources.accenture_listing(ACC_ROWS[4])
    result, job = scraper.decide("Accenture", senior, senior.detail)
    assert job is None and "Senior Analyst" in result.reason


def test_accenture_empty_result_is_failing_not_healthy(monkeypatch):
    _mock(monkeypatch, lambda r: httpx.Response(200, json={"data": [], "totalHits": {"total": 0}}))
    _no_browser(monkeypatch)
    scan = scraper.scan_company({"id": "accenture", "name": "Accenture", "url": "x"})
    assert scan.status == "failing" and "no postings" in scan.reason


@pytest.mark.parametrize("response, status", [
    (httpx.Response(403, text="denied"), "broken"),
    (httpx.Response(429), "failing"),
    (httpx.Response(500), "failing"),
    (httpx.Response(200, text="<html><title>Just a moment...</title>challenge</html>"), "broken"),
    (httpx.Response(200, json={"unexpected": True}), "broken"),
])
def test_source_errors_are_reported(monkeypatch, response, status):
    _mock(monkeypatch, lambda r: response)
    no_browser = _no_browser(monkeypatch)
    scan = scraper.scan_company({"id": "accenture", "name": "Accenture", "url": "x"})
    assert scan.jobs == [] and scan.status == status and scan.reason
    assert not no_browser   # never falls back to scraping the landing page


def test_source_timeout_is_failing(monkeypatch):
    def handler(request):
        raise httpx.ReadTimeout("slow")
    _mock(monkeypatch, handler)
    _no_browser(monkeypatch)
    scan = scraper.scan_company({"id": "accenture", "name": "Accenture", "url": "x"})
    assert scan.status == "failing" and "timeout" in scan.reason


# ---------------------------------------------------------------------------
# Sanofi: API success with zero accepted jobs never scrapes the landing page
# ---------------------------------------------------------------------------

def test_workday_zero_accepted_does_not_fall_back_to_landing_page(monkeypatch):
    postings = [{"title": "Senior Manager, Finance", "locationsText": "Hyderabad", "externalPath": "/job/a_R1"},
                {"title": "Associate Project Specialist", "locationsText": "Hyderabad", "externalPath": "/job/b_R2"}]

    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"jobPostings": postings, "total": 2})
        return httpx.Response(200, json={"jobPostingInfo": {
            "jobDescription": "<p>Experience : 2-4 years post qualification experience</p>",
            "location": "Hyderabad", "country": {"descriptor": "India"}}})

    _mock(monkeypatch, handler)
    no_browser = _no_browser(monkeypatch)
    jobs, status = scraper.scrape_company({"id": "sanofi", "name": "Sanofi", "url": "https://jobs.sanofi.com/en"})
    assert jobs == [] and status == "active"
    assert no_browser == []   # the career-stories landing page is never scraped


def test_workday_list_failure_does_not_fall_back_either(monkeypatch):
    _mock(monkeypatch, lambda r: httpx.Response(500))
    no_browser = _no_browser(monkeypatch)
    jobs, status = scraper.scrape_company({"id": "sanofi", "name": "Sanofi", "url": "https://jobs.sanofi.com/en"})
    assert jobs == [] and status == "failing" and no_browser == []


def test_many_unreadable_details_mark_company_failing(monkeypatch):
    postings = [{"title": f"Graduate Trainee {i}", "locationsText": "Pune", "externalPath": f"/job/g{i}_R{i}"} for i in range(5)]
    _mock(monkeypatch, lambda r: httpx.Response(200, json={"jobPostings": postings}) if r.method == "POST"
          else httpx.Response(429))
    scan = scraper.scan_company({"id": "sanofi", "name": "Sanofi", "url": "x"})
    assert scan.jobs == [] and scan.status == "failing" and "5 of 5 job pages" in scan.reason


def test_detail_budget_leaves_jobs_pending(monkeypatch):
    postings = [{"title": f"Graduate Trainee {i}", "locationsText": "Pune", "externalPath": f"/job/g{i}_R{i}"} for i in range(5)]
    _mock(monkeypatch, lambda r: httpx.Response(200, json={"jobPostings": postings}) if r.method == "POST"
          else httpx.Response(200, json={"jobPostingInfo": {"jobDescription": "<p>Open to 2025 graduates.</p>",
                                                            "location": "Pune", "country": {"descriptor": "India"}}}))
    monkeypatch.setattr(scraper, "MAX_API_DETAIL_FETCHES", 2)
    scan = scraper.scan_company({"id": "sanofi", "name": "Sanofi", "url": "x"})
    assert len(scan.jobs) == 2 and scan.pending == 3
    assert scan.reason == "" and scan.status == "active"
    assert scan.user_note == "3 postings left for the next scan (detail-page budget)"


# ---------------------------------------------------------------------------
# Jibe (Avalara)
# ---------------------------------------------------------------------------

def _jibe(req, title, country, city, desc):
    return {"data": {"req_id": req, "slug": req, "title": title, "country": country,
                     "full_location": f"{city}, {country}", "description": desc, "qualifications": "", "responsibilities": ""}}


def test_jibe_source(monkeypatch):
    pages = {1: [_jibe("1", "Graduate Analyst", "India", "Pune", "<p>Open to 2025 graduates, freshers welcome.</p>"),
                 _jibe("2", "Graduate Analyst", "United States", "Durham", "<p>Freshers welcome.</p>")],
             2: [_jibe("3", "Billing Analyst", "India", "Pune", "<p>2-4 years of billing experience required.</p>")]}

    def handler(request):
        page = int(request.url.params["page"])
        return httpx.Response(200, json={"jobs": pages.get(page, []), "totalCount": 3})

    _mock(monkeypatch, handler)
    jobs, status = scraper.scrape_company({"id": "avalara", "name": "Avalara", "url": "https://careers.avalara.com"})
    assert status == "active"
    assert [(j["title"], j["url"]) for j in jobs] == [
        ("Graduate Analyst", "https://careers.avalara.com/careers-home/jobs/1?lang=en-us")]


# ---------------------------------------------------------------------------
# Avature (MetLife)
# ---------------------------------------------------------------------------

def _article(i, title, loc):
    return (f'<article class="article article--result" id="article--{i}"><h3><a class="link" '
            f'href="https://www.metlifecareers.com/en_US/ml/JobDetail/{title.replace(" ", "-")}/{i}"> {title} </a></h3>'
            f'<span class="list-item-location">{loc}</span><span class="list-item-ref">Job ID: {i}</span></article>')


def test_avature_source(monkeypatch):
    listing_pages = {0: [_article(101, "Actuarial Associate", "Tokyo, Japan"), _article(102, "Graduate Analyst", "Noida, India")],
                     2: [_article(103, "Associate Developer", "Hyderabad, India")]}
    # MetLife's real page shape: JSON-LD has only the title; the description
    # and location sit in Avature field blocks
    def page(title, loc, text):
        field = '<div class="article__content__view__field">'
        return (f'<script type="application/ld+json">{json.dumps({"@type": "JobPosting", "title": title})}</script>'
                f'<article class="article article--details">{field}<div class="article__content__view__field__value">'
                f'Location {loc}</div></div>{field}<div class="article__content__view__field__label">Working Schedule</div>'
                f'<div class="article__content__view__field__value">Full-Time</div></div>{field}'
                f'<div class="article__content__view__field__value"><p>{text}</p></div></div></article>')

    details = {
        "102": page("Graduate Analyst", "Noida, India", "Open to 2025 graduates. Experience: 0-1 years."),
        "103": page("Associate Developer", "Hyderabad, India", "Minimum 2 years of Java experience."),
    }
    requested = []

    def handler(request):
        url = str(request.url)
        requested.append(url)
        if "SearchJobs" in url:
            off = int(request.url.params.get("jobOffset", 0))
            return httpx.Response(200, text="<p>3 results</p>" + "".join(listing_pages.get(off, [])))
        jid = url.rstrip("/").rsplit("/", 1)[-1]
        return httpx.Response(200, text=details[jid])

    _mock(monkeypatch, handler)
    monkeypatch.setitem(scraper.COMPANY_SOURCES, "metlife",
                        [{"type": "avature", "search": "https://www.metlifecareers.com/en_US/ml/SearchJobs", "delay": 0}])
    jobs, status = scraper.scrape_company({"id": "metlife", "name": "MetLife", "url": "x"})
    assert status == "active"
    assert [(j["title"], j["category"], j["location"]) for j in jobs] == [("Graduate Analyst", "FRESHER", "Noida, India")]
    # the Tokyo job's detail page is never fetched
    assert not any(u.endswith("/101") for u in requested)


def test_avature_detail_for_a_different_job_is_unreadable(monkeypatch):
    other = '<script type="application/ld+json">{"@type": "JobPosting", "title": "Someone else"}</script>'
    _mock(monkeypatch, lambda r: httpx.Response(200, text=other + '<div class="article__content__view__field">'
                                                "<div>Freshers welcome, no experience required at all.</div></div>"))
    with httpx.Client() as client:
        d = sources.avature_detail(client, "https://m.example/JobDetail/x/1", "Graduate Analyst", "1")
    assert not d.ok and "not this job" in d.reason


def test_avature_renamed_posting_verified_by_job_id(monkeypatch):
    # live MetLife case: stale JobPosting title, page <title> names the same job ID
    body = ('<html><head><title> Actuarial Specialist - Noida, India - 20761 - MetLife</title></head><body>'
            '<script type="application/ld+json">{"@type": "JobPosting", "title": "Actuarial Analyst"}</script>'
            '<div class="article__content__view__field"><div class="article__content__view__field__value">Location Noida, India</div></div>'
            '<div class="article__content__view__field"><div class="article__content__view__field__value">'
            'Freshers welcome. Graduates in actuarial science.</div></div></body></html>')
    _mock(monkeypatch, lambda r: httpx.Response(200, text=body))
    with httpx.Client() as client:
        ok = sources.avature_detail(client, "https://m.example/JobDetail/Actuarial-Analyst/20761", "Actuarial Specialist", "20761")
        wrong = sources.avature_detail(client, "https://m.example/JobDetail/X/20762", "Graduate Trainee", "20762")
    assert ok.ok and ok.location == "Noida, India"
    assert not wrong.ok


def test_conflicting_template_requirement_is_not_emailed():
    # live PwC posting: "0 - 1 Yrs" field, but the body keeps a template
    # requirement "with at least [X] years" and "Proven experience as ..."
    listing = sources.Listing(title="IN_Specialist 3_ SAP SD_SAP_Advisory_Mumbai", url="https://pwc.example/job/1",
                              location="Mumbai Shivaji Park", posting_evidence=True)
    detail = sources.Detail(ok=True, location="Mumbai Shivaji Park | India", posting_evidence=True, description=(
        "Proven experience as an SAP SD Consultant, with at least [X] years of hands-on experience in SAP SD.\n"
        "*Years of experience required\n0  -  1   Yrs experience"))
    result, job = scraper.decide("PwC", listing, detail)
    assert job is None and result.category.value == "EXPERIENCED"


# ---------------------------------------------------------------------------
# Zoho Recruit (NPCI)
# ---------------------------------------------------------------------------

def test_zoho_source(monkeypatch):
    jobs_data = [
        {"id": "1901", "Posting_Title": "Associate Quality Assurance, NBBL", "City": "Hyderabad", "State": "Telangana",
         "Publish": True, "Job_Description": "Job Title: Associate QA. Experience: 0-2 years. Freshers can apply."},
        {"id": "1902", "Posting_Title": "Senior Associate Onboarding", "City": "Chennai", "State": "Tamil Nadu",
         "Publish": True, "Job_Description": "Senior role, 5+ years."},
        {"id": "1903", "Posting_Title": "Associate Internal Audit", "City": "Mumbai", "State": "Maharashtra",
         "Publish": True, "Job_Description": "Qualified CA with 1-3 years of post-qualification experience."},
    ]
    page = f'<input type="hidden" value="{html.escape(json.dumps(jobs_data))}" id="jobs">'
    _mock(monkeypatch, lambda r: httpx.Response(200, text=page))
    jobs, status = scraper.scrape_company({"id": "c1783687965", "name": "NPCI", "url": "https://www.npci.org.in/careers"})
    assert status == "active"
    assert [(j["title"], j["url"]) for j in jobs] == [
        ("Associate Quality Assurance, NBBL", "https://careers.npci.org.in/jobs/Careers/1901/Associate-Quality-Assurance-NBBL")]


def test_zoho_layout_change_is_broken(monkeypatch):
    _mock(monkeypatch, lambda r: httpx.Response(200, text="<html>redesigned</html>"))
    scan = scraper.scan_company({"id": "c1783687965", "name": "NPCI", "url": "x"})
    assert scan.status == "broken" and "job data not found" in scan.reason


# ---------------------------------------------------------------------------
# Source registry
# ---------------------------------------------------------------------------

def test_every_configured_company_has_a_real_source():
    import json as _json
    from pathlib import Path
    companies = _json.loads((Path(__file__).parent.parent / "companies.json").read_text(encoding="utf-8"))
    for c in companies:
        assert scraper.sources_for(c), f"{c['name']} has no verified job source"


def test_workday_url_is_detected_for_user_added_companies():
    srcs = scraper.sources_for({"id": "c1", "name": "X", "url": "https://acme.wd5.myworkdayjobs.com/en-US/External"})
    assert srcs == [{"type": "workday", "api": "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/jobs"}]
    assert scraper.sources_for({"id": "c2", "name": "Y", "url": "https://careers.example.com/jobs"}) == []


# ---------------------------------------------------------------------------
# Final safety gate: every alert must pass all checks; evidence is stored
# ---------------------------------------------------------------------------

def _listing(**kw):
    base = dict(title="Graduate Analyst", url="https://acme.wd3.myworkdayjobs.com/S/job/Pune/Graduate-Analyst_R1234567",
                location="Pune", posting_evidence=True)
    return sources.Listing(**{**base, **kw})


def _detail(**kw):
    base = dict(ok=True, description="Open to 2025 graduates. Experience: 0-1 years.", location="Pune | India",
                posting_evidence=True, matched="ATS record of this listing")
    return sources.Detail(**{**base, **kw})


def test_gate_passes_and_records_evidence():
    result, job = scraper.decide("Acme", _listing(), _detail())
    assert job and result.category.value == "FRESHER"
    ev = job["evidence"]
    assert all(ev["checks"].values()) and len(ev["checks"]) == 8
    assert ev["experience"] == ["0-1 years"] and ev["fresher_evidence"] and ev["detail_read"] is True
    assert ev["job_id"] == "workday:R1234567" and ev["canonical_url"].endswith("_R1234567") and ev["detail_match"] == "ATS record of this listing"


@pytest.mark.parametrize("listing_kw, detail_kw, failed", [
    ({}, {"matched": ""}, "detail_is_this_job"),                                     # unverified detail
    ({"url": "https://acme.com/early-careers", "posting_evidence": False},
     {"posting_evidence": False}, "real_job_posting"),                               # not a posting URL, no JobPosting
])
def test_gate_blocks_alerts(listing_kw, detail_kw, failed):
    result, job = scraper.decide("Acme", _listing(**listing_kw), _detail(**detail_kw))
    assert job is None and result.category.value == "UNKNOWN" and failed in result.reason


def test_gate_checks_are_independent_of_the_classifier(monkeypatch):
    """Even if the classifier were fooled, the gate re-checks the posting."""
    from job_classifier import Category, Classification
    fooled = Classification(Category.FRESHER, "fresher signal: 'freshers'")
    monkeypatch.setattr(scraper, "classify_job", lambda *a, **k: fooled)
    cases = [
        (_listing(), _detail(description="We are hiring. 3+ years of experience required."), "no_conflicting_experience"),
        (_listing(), _detail(description="You will be responsible for hiring freshers."), "evidence_not_staff_context"),
        (_listing(title="Graduate Program", posting_evidence=False), _detail(posting_evidence=False, description="freshers welcome"),
         "not_programme_story_talent_recruiter"),
        (_listing(title="Senior Analyst"), _detail(description="freshers welcome"), "no_conflicting_experience"),
    ]
    for listing, detail, failed in cases:
        result, job = scraper.decide("Acme", listing, detail)
        assert job is None and failed in result.reason, (listing.title, result)


def test_unreadable_detail_never_accepted_even_with_zero_range_on_card():
    listing = _listing(card_text="Pune, India\n0-1 Yrs")
    for detail in (None, sources.Detail.unreadable("HTTP 429")):
        result, job = scraper.decide("Acme", listing, detail)
        assert job is None and result.category.value == "UNKNOWN"
