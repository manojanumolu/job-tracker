import httpx
import pytest

import scraper

LIST_URL = "https://sanofi.wd3.myworkdayjobs.com/wday/cxs/sanofi/SanofiCareers/jobs"
DETAIL_BASE = "https://sanofi.wd3.myworkdayjobs.com/wday/cxs/sanofi/SanofiCareers"

POSTINGS = [
    # ambiguous title, experience only visible on the detail page
    ("Associate Project Specialist – Medical Communications", "Hyderabad", "/job/Hyderabad/APS_R1",
     "<p>At least 3 years of experience in medical communications.</p>", "Hyderabad", [], "India"),
    # genuine graduate role, multi-location in the list view
    ("Graduate Trainee - Data", "2 Locations", "/job/Hyderabad/GT_R2",
     "<ul><li>Open to 2025 graduates</li><li>0-1 years of experience</li></ul>", "Hyderabad",
     ["Pune"], "India"),
    # entry-level title but an explicit requirement in the description
    ("Entry Level Analyst", "Hyderabad", "/job/Hyderabad/ELA_R3",
     "<p>Requires 2+ years of relevant experience.</p>", "Hyderabad", [], "India"),
    # entry-level role outside India
    ("Graduate Engineer", "Paris", "/job/Paris/GE_R4", "<p>New grads welcome</p>", "Paris", [], "France"),
    # senior — rejected without a detail fetch
    ("Senior Manager, Finance", "Hyderabad", "/job/Hyderabad/SM_R5", "", "Hyderabad", [], "India"),
    # "N Locations" that resolve outside India
    ("New Grad Software Engineer", "2 Locations", "/job/Boston/NG_R6", "<p>New grads</p>", "Boston",
     ["Cambridge"], "United States of America"),
]


@pytest.fixture
def mock_workday(monkeypatch):
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requested.append(url)
        if request.method == "POST" and url == LIST_URL:
            return httpx.Response(200, json={"jobPostings": [
                {"title": t, "locationsText": loc, "externalPath": ext}
                for t, loc, ext, *_ in POSTINGS
            ]})
        for t, _loc, ext, desc, primary, extra, country in POSTINGS:
            if url == DETAIL_BASE + ext:
                return httpx.Response(200, json={"jobPostingInfo": {
                    "title": t, "jobDescription": desc, "location": primary,
                    "additionalLocations": extra, "country": {"descriptor": country},
                }})
        return httpx.Response(404)

    real_client = httpx.Client

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(scraper.httpx, "Client", client_factory)
    return requested


def test_workday_uses_detail_page(mock_workday):
    jobs = scraper._scrape_api({"id": "sanofi", "name": "Sanofi", "url": "https://jobs.sanofi.com/en"})
    titles = [j["title"] for j in jobs]
    assert titles == ["Graduate Trainee - Data"]
    job = jobs[0]
    assert job["url"] == "https://sanofi.wd3.myworkdayjobs.com/SanofiCareers/job/Hyderabad/GT_R2"
    assert job["company"] == "Sanofi"
    assert job["category"] in {"FRESHER", "ENTRY_LEVEL"}
    assert job["reason"]
    # senior title and concrete non-India list location never hit the detail API
    assert DETAIL_BASE + "/job/Hyderabad/SM_R5" not in mock_workday
    assert DETAIL_BASE + "/job/Paris/GE_R4" not in mock_workday
    # multi-location posting is resolved via the detail API
    assert DETAIL_BASE + "/job/Boston/NG_R6" in mock_workday


@pytest.mark.parametrize("failure", [
    429, 403, 500, 502, 404,
    httpx.ReadTimeout("timed out"), httpx.ConnectError("refused"),
    "cloudflare", "not-json", "empty-info", "empty-description", "other-job",
])
def test_workday_detail_failure_never_emails(monkeypatch, failure):
    """P0: when the posting's own detail can't be read, the job is never
    alerted on its title alone ("Graduate Analyst" here really needs 3 years)."""
    postings = [{"title": "Graduate Analyst", "locationsText": "Hyderabad", "externalPath": "/job/x"},
                {"title": "Entry Level Analyst", "locationsText": "Pune", "externalPath": "/job/y"}]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"jobPostings": postings})
        if isinstance(failure, Exception):
            raise failure
        if isinstance(failure, int):
            return httpx.Response(failure)
        if failure == "cloudflare":
            return httpx.Response(403, text="<html><title>Just a moment...</title></html>")
        if failure == "not-json":
            return httpx.Response(200, text="<html><title>Just a moment...</title></html>")
        if failure == "empty-info":
            return httpx.Response(200, json={"jobPostingInfo": {}})
        if failure == "empty-description":
            return httpx.Response(200, json={"jobPostingInfo": {"jobDescription": "", "location": "Hyderabad"}})
        # Workday served a different posting than the one we asked for
        return httpx.Response(200, json={"jobPostingInfo": {
            "title": "Graduate Trainee - Operations", "jobDescription": "<p>Freshers welcome. No prior experience required.</p>",
            "location": "Hyderabad", "country": {"descriptor": "India"}}})

    real_client = httpx.Client
    monkeypatch.setattr(scraper.httpx, "Client",
                        lambda *a, **kw: real_client(*a, **{**kw, "transport": httpx.MockTransport(handler)}))
    assert scraper._scrape_api({"id": "sanofi", "name": "Sanofi", "url": "x"}) == []


def test_unknown_company_has_no_api():
    assert scraper._scrape_api({"id": "some-new-company", "name": "X", "url": "https://example.com/careers"}) == []


def test_html_to_text_keeps_block_boundaries():
    text = scraper._html_to_text("<p>Intro&nbsp;text</p><ul><li>2-4 years</li><li>Python</li></ul>")
    assert text.splitlines() == ["Intro\xa0text", "2-4 years", "Python"]


def test_jsonld_location():
    posting = {"jobLocation": [
        {"address": {"addressLocality": "Hyderabad", "addressCountry": {"name": "IN"}}},
        {"address": {"addressLocality": "Pune", "addressRegion": "MH", "addressCountry": "India"}},
    ]}
    # ISO country codes are normalised so the India check can match them
    assert scraper._jsonld_location(posting) == "Hyderabad | India | Pune | MH | India"
    assert scraper._jsonld_location({"jobLocation": {"address": "Bengaluru, India"}}) == "Bengaluru, India"
    assert scraper._jsonld_location({}) == ""


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Hyderabad", True), ("Bengaluru, Karnataka", True), ("Remote - India", True),
        ("Paris, France", False), ("Indiana, USA", False), ("International", False),
    ],
)
def test_is_india(text, expected):
    assert scraper._is_india(text) is expected


@pytest.mark.parametrize(
    "text",
    ["India", "Bengaluru, India", "Hyderabad, India", "Pune, India", "Chennai, India",
     "Remote - India", "India / Singapore", "Singapore | Hyderabad"],
)
def test_is_india_accepts_india_and_multi_location(text):
    assert scraper._is_india(text)


# ---------------------------------------------------------------------------
# Workday robustness
# ---------------------------------------------------------------------------

def _run_workday(monkeypatch, postings, details):
    """Run _scrape_api against a mocked Workday; ``details`` maps
    externalPath -> response (dict body, int status, or Exception)."""
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST":
            return httpx.Response(200, json={"jobPostings": postings})
        ext = url[len(DETAIL_BASE):]
        resp = details.get(ext, 404)
        if isinstance(resp, Exception):
            raise resp
        if isinstance(resp, int):
            return httpx.Response(resp)
        return httpx.Response(200, json=resp)

    real_client = httpx.Client
    monkeypatch.setattr(
        scraper.httpx, "Client",
        lambda *a, **kw: real_client(*a, **{**kw, "transport": httpx.MockTransport(handler)}),
    )
    return scraper._scrape_api({"id": "sanofi", "name": "Sanofi", "url": "x"})


def test_workday_malformed_postings_do_not_abort_company(monkeypatch):
    good = {"title": "Graduate Trainee", "locationsText": "Hyderabad", "externalPath": "/job/ok"}
    postings = [
        "not-a-dict",
        {"title": None, "locationsText": None, "externalPath": None},
        {"title": "Graduate Analyst", "locationsText": "Pune", "externalPath": "/job/null-info"},
        {"title": "Graduate Engineer", "locationsText": "Pune", "externalPath": "/job/str-country"},
        {"title": "Graduate Developer", "locationsText": "Pune", "externalPath": "/job/http500"},
        {"title": "Graduate Designer", "locationsText": "Pune", "externalPath": "/job/timeout"},
        good,
    ]
    details = {
        "/job/null-info": {"jobPostingInfo": None},
        "/job/str-country": {"jobPostingInfo": {"jobDescription": None, "location": "Pune",
                                                "additionalLocations": [None, 3], "country": "India"}},
        "/job/http500": 500,
        "/job/timeout": httpx.ReadTimeout("timed out"),
        "/job/ok": {"jobPostingInfo": {"jobDescription": "<p>Open to 2025 graduates.</p>",
                                       "location": "Hyderabad", "country": {"descriptor": "India"}}},
    }
    jobs = _run_workday(monkeypatch, postings, details)
    titles = {j["title"] for j in jobs}
    # malformed postings and failed detail fetches never abort the company —
    # and a posting whose own detail couldn't be read is never alerted
    # (it stays pending and is retried next run)
    assert titles == {"Graduate Trainee"}


def _workday_pages(monkeypatch, pages_by_facet, facets=None):
    """Mock a paginated Workday list API. ``pages_by_facet`` maps
    json.dumps(appliedFacets) -> full posting list; returns request log."""
    import json as _json
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method != "POST":
            return httpx.Response(200, json={"jobPostingInfo": {
                "jobDescription": "<p>Open to 2025 graduates. Experience: 0-1 years.</p>",
                "location": "Hyderabad", "country": {"descriptor": "India"}}})
        body = _json.loads(request.content)
        calls.append(body)
        allp = pages_by_facet.get(_json.dumps(body.get("appliedFacets", {}), sort_keys=True), [])
        page = allp[body["offset"]:body["offset"] + body["limit"]]
        out = {"jobPostings": page, "total": len(allp)}
        if facets is not None:
            out["facets"] = facets
        return httpx.Response(200, json=out)

    real_client = httpx.Client
    monkeypatch.setattr(scraper.httpx, "Client",
                        lambda *a, **kw: real_client(*a, **{**kw, "transport": httpx.MockTransport(handler)}))
    return calls


def test_workday_paginates_and_uses_india_country_facet(monkeypatch):
    import json as _json
    facets = [{"facetParameter": "locationCountry", "values": [
        {"descriptor": "France", "id": "fr1", "count": 500}, {"descriptor": "India", "id": "in1", "count": 45}]}]
    india = [{"title": f"Graduate Trainee {i}", "locationsText": "Hyderabad",
              "externalPath": f"/job/Hyderabad/Graduate-Trainee-{i}_R{1000 + i}"} for i in range(45)]
    everything = [{"title": f"Other {i}", "locationsText": "Paris", "externalPath": f"/job/p{i}"} for i in range(799)]
    calls = _workday_pages(monkeypatch, {
        _json.dumps({}, sort_keys=True): everything,
        _json.dumps({"locationCountry": ["in1"]}, sort_keys=True): india,
    }, facets=facets)
    jobs = scraper._scrape_api({"id": "sanofi", "name": "Sanofi", "url": "x"})
    # all 45 India postings, across 3 pages — not just the first 20
    assert len(jobs) == 45
    assert {c["offset"] for c in calls if c.get("appliedFacets")} == {0, 20, 40}
    assert all(c["limit"] == 20 for c in calls)
    assert calls[0]["appliedFacets"] == {}


def test_workday_india_location_facet_values(monkeypatch):
    """PwC-style tenants have no country facet — only city values."""
    import json as _json
    facets = [{"facetParameter": "locationMainGroup", "values": [{"facetParameter": "locations", "values": [
        {"descriptor": "Bengaluru Millenia", "id": "b1"}, {"descriptor": "Kolkata DN 57", "id": "k1"},
        {"descriptor": "Dublin - One Spencer Dock", "id": "d1"}, {"descriptor": "Hyderabad, Pakistan", "id": "x1"}]}]}]
    india = [{"title": "Graduate Associate", "locationsText": "Kolkata DN 57",
              "externalPath": "/job/Kolkata/Graduate-Associate_R9"}]
    calls = _workday_pages(monkeypatch, {
        _json.dumps({"locations": ["b1", "k1"]}, sort_keys=True): india}, facets=facets)
    jobs = scraper._scrape_api({"id": "sanofi", "name": "Sanofi", "url": "x"})
    assert [j["title"] for j in jobs] == ["Graduate Associate"]
    assert calls[1]["appliedFacets"] == {"locations": ["b1", "k1"]}


def test_workday_without_facets_paginates_everything(monkeypatch):
    import json as _json
    postings = ([{"title": f"Other {i}", "locationsText": "Paris", "externalPath": f"/job/p{i}"} for i in range(60)]
                + [{"title": "Graduate Trainee X", "locationsText": "Pune", "externalPath": "/job/Pune/Graduate-Trainee-X_R77"}])
    calls = _workday_pages(monkeypatch, {_json.dumps({}, sort_keys=True): postings})
    jobs = scraper._scrape_api({"id": "sanofi", "name": "Sanofi", "url": "x"})
    # the only India posting sits on page 4 (offset 60)
    assert [j["title"] for j in jobs] == ["Graduate Trainee X"]
    assert [c["offset"] for c in calls] == [0, 20, 40, 60]


def test_workday_list_request(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            import json as _json
            seen["url"] = str(request.url)
            seen["payload"] = _json.loads(request.content)
        return httpx.Response(200, json={"jobPostings": []})

    real_client = httpx.Client
    monkeypatch.setattr(
        scraper.httpx, "Client",
        lambda *a, **kw: real_client(*a, **{**kw, "transport": httpx.MockTransport(handler)}),
    )
    assert scraper._scrape_api({"id": "sanofi", "name": "Sanofi", "url": "x"}) == []
    assert seen["url"] == LIST_URL
    assert seen["payload"] == {"limit": 20, "offset": 0, "searchText": "", "appliedFacets": {}}


def test_workday_india_in_additional_locations(monkeypatch):
    postings = [{"title": "Graduate Trainee", "locationsText": "2 Locations", "externalPath": "/job/multi"}]
    details = {"/job/multi": {"jobPostingInfo": {
        "jobDescription": "<p>A graduate programme role for the 2025 batch.</p>", "location": "Singapore",
        "additionalLocations": ["Hyderabad"], "country": {"descriptor": "Singapore"}}}}
    jobs = _run_workday(monkeypatch, postings, details)
    assert [j["title"] for j in jobs] == ["Graduate Trainee"]
    # the alert shows the India location(s), not Singapore
    assert jobs[0]["location"] == "Hyderabad"


def test_workday_empty_description_is_not_readable(monkeypatch):
    postings = [{"title": "Graduate Trainee", "locationsText": "Hyderabad", "externalPath": "/job/empty"}]
    details = {"/job/empty": {"jobPostingInfo": {"jobDescription": "", "location": "Hyderabad",
                                                 "country": {"descriptor": "India"}}}}
    assert _run_workday(monkeypatch, postings, details) == []


def test_workday_list_without_location_needs_detail_location(monkeypatch):
    """No India location evidence -> never assume India (the old code used the title)."""
    postings = [{"title": "Graduate Trainee India", "locationsText": "2 Locations", "externalPath": "/job/noloc"}]
    details = {"/job/noloc": {"jobPostingInfo": {"jobDescription": "<p>Open to 2025 graduates, freshers welcome.</p>"}}}
    assert _run_workday(monkeypatch, postings, details) == []


# ---------------------------------------------------------------------------
# JSON-LD helpers
# ---------------------------------------------------------------------------

def test_jsonld_location_country_code_and_remote():
    assert scraper._is_india(scraper._jsonld_location(
        {"jobLocation": {"address": {"addressCountry": "IN"}}}))
    assert scraper._is_india(scraper._jsonld_location(
        {"jobLocationType": "TELECOMMUTE",
         "applicantLocationRequirements": {"@type": "Country", "name": "India"}}))
    assert not scraper._is_india(scraper._jsonld_location(
        {"jobLocation": {"address": {"addressLocality": "Austin", "addressCountry": "US"}}}))


@pytest.mark.parametrize("posting", [
    {"jobLocation": "Hyderabad"}, {"jobLocation": [None, 5, {"address": 7}]},
    {"applicantLocationRequirements": "India"}, {"jobLocation": {"address": None}},
])
def test_jsonld_location_malformed_does_not_crash(posting):
    assert isinstance(scraper._jsonld_location(posting), str)


def test_pick_posting_prefers_matching_title():
    postings = [{"title": "Senior Engineer"}, {"title": "Graduate Engineer"}, "junk"]
    assert scraper._pick_posting(postings, "Graduate Engineer")["title"] == "Graduate Engineer"
    # several postings, none ours: don't guess
    assert scraper._pick_posting(postings, "Other") is None
    # P0: a single posting that is a *different* job is never borrowed
    assert scraper._pick_posting([{"title": "X"}], "Other") is None
    assert scraper._pick_posting([{"title": "Graduate Trainee - Operations"}], "Associate Analyst") is None
    assert scraper._pick_posting(["junk"], "x") is None


@pytest.mark.parametrize("posted, card", [
    ("Associate – Evidence Synthesis", "Associate - Evidence Synthesis"),
    ("ASSOCIATE — EVIDENCE  SYNTHESIS", "Associate – Evidence Synthesis"),
    ("Software Engineer (Java)", "Software Engineer - Java"),
])
def test_pick_posting_normalises_titles(posted, card):
    assert scraper._pick_posting([{"title": posted}, {"title": "Other"}], card)["title"] == posted


def test_pick_posting_by_job_id():
    postings = [{"title": "Different display title", "identifier": {"value": "JR141427"}}, {"title": "Other"}]
    assert scraper._pick_posting(postings, "Store Specialist", "JR141427") is postings[0]
    assert scraper._pick_posting(postings, "Store Specialist", "JR999999") is None


@pytest.mark.parametrize("requested, final, readable", [
    ("https://a.com/jobs/1", "https://a.com/jobs/1", True),
    ("https://a.com/jobs/1#apply", "https://a.com/jobs/1/", True),
    ("http://www.a.com/jobs/1?utm_source=x", "https://a.com/jobs/1", True),
    ("https://a.com/jobs/1", "https://a.com/jobs/2", False),              # different path
    ("https://a.com/job?id=1", "https://a.com/job?id=2", False),          # different job id
    ("https://a.com/jobs/1", "https://a.com/careers", False),             # redirect to a landing page
])
def test_jsonld_detail_rejects_redirects(requested, final, readable):
    posting = {"title": "Graduate Analyst", "description": "Open to 2025 graduates and freshers."}
    detail = scraper.jsonld_detail([posting], "Graduate Analyst", final_url=final, requested_url=requested)
    assert detail.ok is readable


def test_same_page_ignores_fragment_and_trailing_slash():
    assert scraper._same_page("https://a.com/careers#top", "https://a.com/careers/")
    assert not scraper._same_page("https://a.com/careers/job/1", "https://a.com/careers")


def test_workday_ambiguous_titles_decided_by_description(monkeypatch):
    postings = [
        {"title": "Associate Software Engineer", "locationsText": "Hyderabad", "externalPath": "/job/fresh"},
        {"title": "Associate Analyst", "locationsText": "Hyderabad", "externalPath": "/job/mentor"},
        {"title": "Software Engineer I", "locationsText": "Pune", "externalPath": "/job/exp"},
        {"title": "Intern", "locationsText": "Pune", "externalPath": "/job/none"},
    ]
    info = lambda desc: {"jobPostingInfo": {"jobDescription": desc, "location": "Hyderabad",
                                            "country": {"descriptor": "India"}}}
    details = {
        "/job/fresh": info("<p>Fresh graduates are encouraged to apply.</p><p>No prior professional experience required.</p>"),
        "/job/mentor": info("<ul><li>Mentor recent graduates joining the team</li></ul>"),
        "/job/exp": info("<p>Open to recent graduates.</p><p>Requires 2 years of professional experience.</p>"),
        "/job/none": info("<p>Work on exciting projects.</p>"),
    }
    jobs = _run_workday(monkeypatch, postings, details)
    assert [(j["title"], j["category"]) for j in jobs] == [("Associate Software Engineer", "FRESHER")]


# ---------------------------------------------------------------------------
# Location stored on alerted jobs (shown in the email / UI)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("Hyderabad | India", "Hyderabad · India"),
    ("Hyderabad | Pune | India | Hyderabad", "Hyderabad · Pune · India"),
    ("A | B | C | D | E", "A · B · C +2 more"),
    ("", ""),
    ("Bengaluru, India", "Bengaluru, India"),
])
def test_display_location(raw, expected):
    assert scraper._display_location(raw) == expected


def test_card_location_picks_the_india_line():
    assert scraper._card_location("Full time\nHyderabad, India\nPosted today") == "Hyderabad, India"
    assert scraper._card_location("Full time\nRemote") == ""


def test_workday_jobs_carry_location(mock_workday):
    jobs = scraper._scrape_api({"id": "sanofi", "name": "Sanofi", "url": "https://jobs.sanofi.com/en"})
    assert jobs[0]["location"] == "Hyderabad · Pune · India"
    assert set(jobs[0]) == {"title", "url", "company", "location", "category", "reason", "evidence"}
    ev = jobs[0]["evidence"]
    assert ev["detail_read"] is True and ev["detail_match"] and len(ev["checks"]) == 8 and all(ev["checks"].values())
