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


def test_workday_detail_failure_falls_back_to_title(monkeypatch, mock_workday):
    monkeypatch.setattr(scraper, "_workday_detail", lambda client, url: None)
    jobs = scraper._scrape_api({"id": "sanofi", "name": "Sanofi", "url": "https://jobs.sanofi.com/en"})
    # without descriptions: ambiguous "Associate" is not alerted; the graduate
    # role's list location is "2 Locations" so India can't be confirmed
    assert [j["title"] for j in jobs] == ["Entry Level Analyst"]


def test_unknown_company_has_no_api():
    assert scraper._scrape_api({"id": "metlife", "name": "MetLife", "url": "x"}) == []


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
    # every well-formed or recoverable posting survives; failed detail fetches
    # fall back to the list-view title + location
    assert titles == {"Graduate Trainee", "Graduate Analyst", "Graduate Engineer",
                      "Graduate Developer", "Graduate Designer"}


def test_workday_list_request_unchanged(monkeypatch):
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
    assert seen["payload"] == {"limit": 20, "offset": 0, "searchText": "", "locations": []}


def test_workday_india_in_additional_locations(monkeypatch):
    postings = [{"title": "Graduate Trainee", "locationsText": "2 Locations", "externalPath": "/job/multi"}]
    details = {"/job/multi": {"jobPostingInfo": {
        "jobDescription": "", "location": "Singapore", "additionalLocations": ["Hyderabad"],
        "country": {"descriptor": "Singapore"}}}}
    assert [j["title"] for j in _run_workday(monkeypatch, postings, details)] == ["Graduate Trainee"]


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
    assert scraper._pick_posting([{"title": "X"}], "Other")["title"] == "X"
    assert scraper._pick_posting(["junk"], "x") is None


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
