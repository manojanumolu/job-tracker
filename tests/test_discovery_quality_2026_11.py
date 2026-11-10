"""Job-discovery fixes from the production scan of 10 Nov 2026 (09:02 UTC).

* Microsoft: its full-time university hire "Software Engineering" (India,
  Multiple Locations) was rejected because "proven experience coding in
  languages including C, C++ ..." was read as a prior-employment
  requirement, and nothing else marked it as entry level. Microsoft tags
  university-hire positions with its "Microsoft Aspire" media block and
  states each position's employment type; a FULL-TIME position with that
  tag now carries an early-careers line. Its interns stay out.
* Eightfold (Microsoft, Qualcomm): the posting date the API states
  (postedTs) is now stored as posted_at.
* A posting the employer places anywhere in India ("Multiple Locations",
  "PAN India") without naming a city matches every city choice, so the
  Hyderabad-tailored shared email no longer drops it.
* IBM: postings in "Multiple Cities" (61 that scan) come from IBM's
  India-filtered search and were rejected as "not in India".
* Workday: a posting repeated across result pages is read once.

Eligibility is not relaxed: years of experience, seniority and levels
reject exactly as before. No network; nothing is emailed."""
import httpx
import pytest

import job_classifier as jc
import job_filters
import scraper
import sources
from job_classifier import Category

# the Microsoft university-hire template, as served on 10 Nov 2026 (abridged)
MS_UNIVERSITY = """Overview
As a Software Engineer, you will help plan, design, develop and test software systems or applications.
Responsibilities
With guidance, you will learn to create and implement code that is extensible and maintainable.
Qualifications
Required Qualifications
Bachelor's Degree in Computer Science, or related technical discipline with proven experience coding in languages including, but not limited to, C, C++, C#, Java, JavaScript, or Python
OR equivalent experience
Preferred Qualifications
Bachelor's Degree (or currently pursuing) in Computer Science or related technical field AND 1+ year(s) technical engineering experience with coding in languages including, but not limited to, C, C++, C#, Java, JavaScript, or Python
OR equivalent experience"""

MS_CFG = scraper.COMPANY_SOURCES["microsoft"][0]
ASPIRE = [{"title": "Microsoft Intern Experience 2024"}, {"title": "Microsoft Aspire Experience"},
          {"title": "Microsoft Aspire MBA Experience"}, {"title": "Our Mission"}]
STANDARD = [{"title": "Our mission"}, {"title": "100% Empowered by Employees"}, {"title": "One Day, One Microsoft"}]


def _record(description=MS_UNIVERSITY, videos=ASPIRE, employment=("Full-Time",), posted=1793257651, name="Software Engineering"):
    return {"data": {"id": 1, "name": name, "jobDescription": description.replace("\n", "<br>"),
                     "postedTs": posted, "efcustomTextEmploymentType": list(employment),
                     "positionExtraDetails": {"videos": videos}}}


def _detail(record, cfg=MS_CFG):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=record)))
    return sources.eightfold_detail(client, cfg, "1", "India, Multiple Locations, Multiple Locations")


# ── "proven experience coding" is a skill ─────────────────────────────────

@pytest.mark.parametrize("text", [
    "Bachelor's degree with proven experience coding in Java or Python",
    "Proven experience programming in C++",
    "proven experience writing code in Go",
])
def test_proven_coding_skill_is_not_a_prior_employment_requirement(text):
    assert jc.prior_experience_requirement(text) == ""


@pytest.mark.parametrize("text", [
    "Proven experience in enterprise sales is required.",
    "A proven track record of leading engineering teams.",
    "Proven industry experience with payments systems.",
    "Proven experience coding in Java. Proven experience managing a team.",   # the second still counts
])
def test_other_proven_experience_still_rejects(text):
    assert jc.prior_experience_requirement(text)


def test_years_still_reject_whatever_the_wording():
    c = jc.classify_job("Software Engineer", "Required: 2+ years of experience, proven experience coding in Java.")
    assert c.category == Category.EXPERIENCED


# ── Microsoft's early-career tag ──────────────────────────────────────────

def test_microsoft_full_time_university_hire_is_entry_level_with_its_posting_date():
    d = _detail(_record())
    assert d.ok and d.posted_at == "2026-10-29"
    assert d.description.endswith("Career site: early careers programme role (university hiring, full-time)")
    c = jc.classify_job("Software Engineering", d.description, url="https://x/careers/job/1", posting_evidence=True)
    assert c.category == Category.ENTRY_LEVEL
    assert not jc.experience_conflict("Software Engineering", d.description)


def test_microsoft_interns_and_ordinary_positions_get_no_tag():
    intern = _detail(_record(employment=("Internship",), name="Applied Sciences INTERN"))
    assert "early careers" not in intern.description
    assert jc.classify_job("Applied Sciences INTERN", intern.description, posting_evidence=True).category == Category.UNKNOWN
    ordinary = _detail(_record(videos=STANDARD))
    assert "early careers" not in ordinary.description
    assert jc.classify_job("Software Engineering", ordinary.description, posting_evidence=True).category == Category.UNKNOWN


def test_the_tag_never_overrides_experience_or_seniority():
    senior = _detail(_record(description=MS_UNIVERSITY + "\nRequired: 5+ years of professional experience."))
    assert jc.classify_job("Software Engineering", senior.description, posting_evidence=True).category == Category.EXPERIENCED
    assert jc.classify_job("Senior Software Engineer", _detail(_record()).description,
                           posting_evidence=True).category == Category.SENIOR


def test_only_a_company_whose_config_names_a_marker_gets_a_tag():
    qualcomm = scraper.COMPANY_SOURCES["qualcomm"][0]
    assert "early_career_media" not in qualcomm
    d = _detail(_record(), cfg=qualcomm)
    assert "early careers" not in d.description and d.posted_at == "2026-10-29"     # the date is read for every Eightfold site


@pytest.mark.parametrize("record", [
    {"data": {"jobDescription": MS_UNIVERSITY, "positionExtraDetails": "x", "efcustomTextEmploymentType": None}},
    {"data": {"jobDescription": MS_UNIVERSITY, "positionExtraDetails": {"videos": [None, 3, {"title": None}]}}},
    {"data": {"jobDescription": MS_UNIVERSITY, "postedTs": "not a time"}},
    {"data": {"jobDescription": MS_UNIVERSITY, "postedTs": 99999999999999}},
])
def test_malformed_eightfold_records_never_crash_or_invent(record):
    d = _detail(record)
    assert d.ok and "early careers" not in d.description and d.posted_at == ""


def test_microsoft_scans_end_to_end(monkeypatch):
    """Search -> detail -> verdict, as run_all does it (one university hire, one intern, one experienced role)."""
    positions = [{"id": 1, "name": "Software Engineering", "locations": ["India, Multiple Locations, Multiple Locations"]},
                 {"id": 2, "name": "Applied Sciences INTERN", "locations": ["India, Hyderabad, Telangana"]},
                 {"id": 3, "name": "Software Engineer II", "locations": ["India, Hyderabad, Telangana"]}]
    details = {"1": _record(), "2": _record(employment=("Internship",), name="Applied Sciences INTERN"),
               "3": _record(videos=STANDARD, name="Software Engineer II")}

    def handler(request):
        if request.url.path.endswith("/search"):
            start = int(request.url.params.get("start", "0"))
            return httpx.Response(200, json={"data": {"positions": positions if start == 0 else [], "count": 3}})
        return httpx.Response(200, json=details[request.url.params["position_id"]])
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: real(*a, **{**kw, "transport": httpx.MockTransport(handler)}))
    monkeypatch.setattr(sources.time, "sleep", lambda s: None)
    scan = scraper.scan_company({"id": "microsoft", "name": "Microsoft",
                                 "url": "https://apply.careers.microsoft.com/careers?domain=microsoft.com"})
    assert scan.status == "active" and [j["title"] for j in scan.jobs] == ["Software Engineering"]
    (job,) = scan.jobs
    assert job["posted_at"] == "2026-10-29" and job["category"] == "ENTRY_LEVEL"
    assert job_filters.matches_tailoring(job, {"locations": ["Hyderabad"], "job_families": ["Software engineering"]})


# ── India-wide locations ──────────────────────────────────────────────────

@pytest.mark.parametrize("location", ["India, Multiple Locations, Multiple Locations", "PAN India",
                                      "Multiple Cities | India", "Anywhere in India"])
def test_an_india_wide_posting_matches_every_city_choice(location):
    choices = job_filters.job_location_choices({"location": location})
    assert "Hyderabad" in choices and "Bengaluru" in choices and "Remote" not in choices


@pytest.mark.parametrize("location, expected", [
    ("Bangalore, India · Multiple Locations", {"Bengaluru"}),   # names a city: only that city
    ("Hyderabad, India", {"Hyderabad"}),
    ("Multiple Locations", set()),                               # not said to be India
    ("Multiple Locations, United States", set()),
    ("Remote, India", {"Remote"}),
])
def test_named_cities_and_non_india_places_are_unchanged(location, expected):
    assert job_filters.job_location_choices({"location": location}) == expected


# ── IBM "Multiple Cities" ─────────────────────────────────────────────────

def _ibm(place):
    body = "Graduate role. Freshers welcome. " + "Build reliable software for clients. " * 6
    rows = [{"_source": {"title": "Associate Developer", "url": "https://careers.ibm.com/careers/JobDetail?jobId=1",
                         "body": body, "field_keyword_19": place}}]
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"hits": {"total": {"value": 1}, "hits": rows}})))
    return sources.ibm_listings(client, {"country": "India"}).listings[0]


@pytest.mark.parametrize("place, location", [
    ("Multiple Cities", "Multiple Cities | India"),
    ("Bangalore, IN", "Bangalore, IN | India"),
    ("Austin, US", "Austin, US"),                                # a foreign place is never relabelled
    ("", ""),
])
def test_ibm_places_from_its_india_search(place, location):
    assert _ibm(place).location == location


# ── Workday: a posting repeated across pages ──────────────────────────────

def test_a_workday_posting_repeated_across_pages_is_read_once(monkeypatch):
    monkeypatch.setattr(sources, "WORKDAY_PAGE", 2)
    pages = [[("Intern", "/job/a"), ("Analyst", "/job/b")], [("Analyst", "/job/b"), ("Engineer", "/job/c")]]

    def handler(request):
        offset = int(__import__("json").loads(request.content).get("offset", 0))
        rows = pages[offset // 2] if offset // 2 < len(pages) else []
        return httpx.Response(200, json={"total": 4, "facets": [], "jobPostings": [
            {"title": t, "externalPath": p, "locationsText": "Hyderabad, India"} for t, p in rows]})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    res = sources.workday_listings(client, "https://x.wd1.myworkdayjobs.com/wday/cxs/x/Site/jobs")
    assert [x.url.rsplit("/", 1)[1] for x in res.listings] == ["a", "b", "c"] and res.seen == 4


def test_the_detail_budget_covers_hitachis_india_postings():
    assert scraper.MAX_API_DETAIL_FETCHES == 500 and scraper.MAX_DETAIL_FETCHES == 30
