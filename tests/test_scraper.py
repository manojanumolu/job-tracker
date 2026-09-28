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
    assert scraper._jsonld_location(posting) == "Hyderabad | IN | Pune | MH | India"
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


def test_is_fresher_compat_wrapper():
    assert scraper._is_fresher("Graduate Software Engineer\nHyderabad, India")
    assert not scraper._is_fresher("Associate Project Specialist – Medical Communications\nHyderabad")
    assert not scraper._is_fresher("Associate Software Engineer\n2-4 years experience")
