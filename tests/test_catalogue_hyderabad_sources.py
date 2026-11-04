"""The Hyderabad employers added in Nov 2026: their job-source adapters on
canned answers (no network), and the catalogue entries themselves. Account 1's
monitoring is the catalogue in the shared alerts; Account 2's five companies
stay outside them."""
import json
from pathlib import Path

import httpx
import pytest

import scraper
import sources
from config_store import in_shared_alerts

REPO = Path(__file__).resolve().parent.parent
NEW = {"amazon": "amazon", "microsoft": "eightfold", "qualcomm": "eightfold", "google": "google",
       "deloitte": "successfactors", "infosys": "infosys"}
ACCOUNT_2 = {"metlife", "avalara", "pokerstars", "sanofi", "pwc"}
LONG = "Build and run reliable services for customers across the world. " * 2


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _catalogue():
    return {c["id"]: c for c in json.loads((REPO / "data" / "companies.json").read_text("utf-8"))}


# ── the catalogue ─────────────────────────────────────────────────

def test_the_new_companies_are_in_account_1s_monitoring():
    catalogue = _catalogue()
    for cid, kind in NEW.items():
        assert in_shared_alerts(catalogue[cid]), cid
        assert catalogue[cid]["url"].startswith("https://")
        assert [s["type"] for s in scraper.sources_for(catalogue[cid])] == [kind]
        assert "Playwright" not in scraper.source_label(catalogue[cid])


def test_account_2s_companies_stay_out_of_the_shared_alerts():
    catalogue = _catalogue()
    assert ACCOUNT_2 <= set(catalogue)
    assert all(not in_shared_alerts(catalogue[cid]) for cid in ACCOUNT_2)
    assert not (ACCOUNT_2 & set(NEW))


def test_catalogue_ids_and_names_are_unique():
    rows = json.loads((REPO / "data" / "companies.json").read_text("utf-8"))
    assert len({c["id"] for c in rows}) == len(rows)
    assert len({c["name"].lower() for c in rows}) == len(rows)


# ── Amazon ────────────────────────────────────────────────────────

def _amazon_job(**kw):
    return {"title": "Software Development Engineer I", "job_path": "/en/jobs/1/sde", "id_icims": "1", "description": LONG,
            "normalized_location": "Hyderabad, Telangana, IND", "basic_qualifications": "- A degree",
            "preferred_qualifications": "", **kw}


def test_amazon_reads_every_page_and_carries_the_full_description():
    def handler(request):
        offset = int(request.url.params["offset"])
        assert request.url.params["normalized_country_code[]"] == "IND"
        return httpx.Response(200, json={"hits": 101, "jobs": [_amazon_job(job_path=f"/en/jobs/{offset + i}/x") for i in range(100 if offset == 0 else 1)]})
    res = sources.amazon_listings(_client(handler), {})
    assert res.seen == 101 and len(res.listings) == 101
    first = res.listings[0]
    assert first.url == "https://www.amazon.jobs/en/jobs/0/x" and "Hyderabad" in first.location and "India" in first.location
    assert first.detail.ok and "A degree" in first.detail.description


def test_amazon_a_missing_count_or_a_changed_answer_is_a_failure_not_a_quiet_zero():
    with pytest.raises(sources.SourceError):
        sources.amazon_listings(_client(lambda r: httpx.Response(200, json={"jobs": []})), {})
    with pytest.raises(sources.SourceError):
        sources.amazon_listings(_client(lambda r: httpx.Response(200, json={"unexpected": 1})), {})


# ── Eightfold (Microsoft, Qualcomm) ───────────────────────────────

CFG = {"type": "eightfold", "host": "https://careers.example.com", "domain": "example.com", "location": "Hyderabad"}


def _eightfold(request):
    if request.url.path.endswith("/position_details"):
        if request.url.params["position_id"] == "2":
            return httpx.Response(503)
        return httpx.Response(200, json={"data": {"jobDescription": f"<p>{LONG}</p>"}})
    start = int(request.url.params["start"])
    rows = {0: [{"id": 1, "name": "Associate Engineer", "displayJobId": "A1", "positionUrl": "/careers/job/1", "locations": ["Hyderabad, Telangana, India"]},
                {"id": 2, "name": "Engineer", "locations": ["India, Telangana, Hyderabad"]}],
            2: [{"id": 3, "name": "Elsewhere", "locations": ["Austin, Texas, United States"]}]}
    return httpx.Response(200, json={"data": {"count": 3, "positions": rows.get(start, [])}})


def test_eightfold_pages_keep_india_and_read_the_description_on_demand():
    res = sources.eightfold_listings(_client(_eightfold), CFG)
    assert res.seen == 3 and [x.title for x in res.listings] == ["Associate Engineer", "Engineer"]
    first, second = res.listings
    assert first.url == "https://careers.example.com/careers/job/1" and first.job_id == "A1"
    assert first.detail is None and first.fetch_detail().ok
    assert not second.fetch_detail().ok                     # an unreadable page is never accepted


def test_eightfold_a_changed_answer_is_a_failure():
    with pytest.raises(sources.SourceError):
        sources.eightfold_listings(_client(lambda r: httpx.Response(200, json={"data": {}})), CFG)


# ── SuccessFactors (Deloitte) ─────────────────────────────────────

SF_ROW = ('<tr class="data-row"><td><a href="/job/Hyderabad-Analyst/1/" class="jobTitle-link">Analyst &amp; Developer | Hyderabad</a>'
          '</td><td class="colLocation"><span class="jobLocation"> Hyderabad, IN </span></td></tr>')


def test_successfactors_reads_rows_and_the_job_page():
    def handler(request):
        if request.url.path.startswith("/job/"):
            return httpx.Response(200, text=f'<span class="jobdescription"><p>{LONG}</p></span><footer>Cookie notice</footer>')
        return httpx.Response(200, text=f"Results <b>1 – 1</b> of <b>1</b> {SF_ROW}")
    res = sources.successfactors_listings(_client(handler), {"host": "https://careers.example.com", "location": "Hyderabad"})
    assert [x.title for x in res.listings] == ["Analyst & Developer | Hyderabad"]
    only = res.listings[0]
    assert only.url == "https://careers.example.com/job/Hyderabad-Analyst/1/" and "India" in only.location
    detail = only.fetch_detail()
    assert detail.ok and "reliable services" in detail.description and "Cookie notice" not in detail.description


def test_successfactors_a_page_without_rows_means_the_layout_changed():
    with pytest.raises(sources.SourceError):
        sources.successfactors_listings(_client(lambda r: httpx.Response(200, text="<html>nothing</html>")), {"host": "https://x.example"})


# ── Infosys ───────────────────────────────────────────────────────

def _infosys(**kw):
    return {"postingTitle": "Java Developer", "referenceCode": "INFSYS-EXTERNAL-1", "location": "HYDERABAD", "country": "India",
            "minExperienceLevel": "5", "maxExperienceLevel": "9", "rolesResponsibilities": LONG, **kw}


def test_infosys_states_the_required_experience_and_keeps_india_only():
    rows = [_infosys(), _infosys(referenceCode="X", country="USA")]
    res = sources.infosys_listings(_client(lambda r: httpx.Response(200, json=rows)), {})
    assert res.seen == 2 and len(res.listings) == 1
    only = res.listings[0]
    assert only.url.endswith("jobReferenceCode=INFSYS-EXTERNAL-1") and only.location == "Hyderabad | India"
    assert "Required experience: 5 to 9 years." in only.detail.description


def test_infosys_an_empty_or_changed_answer_is_a_failure():
    for answer in ([], {"unexpected": 1}):
        with pytest.raises(sources.SourceError):
            sources.infosys_listings(_client(lambda r, a=answer: httpx.Response(200, json=a)), {})


# ── Google ────────────────────────────────────────────────────────

GOOGLE_PAGE = ('<h3>Job types</h3><h3>Organizations</h3><h3 class="x">Software Engineer, Example</h3><div>Hyderabad</div>'
               '<a href="jobs/results/1234567-software-engineer-example?location=Hyderabad">Learn more</a>')


def test_google_reads_jobs_not_filter_headings_and_stops_when_nothing_is_new():
    pages = []

    def handler(request):
        if "jobs/results/1234567" in str(request.url):
            return httpx.Response(200, text=f"<p>Minimum qualifications:</p><p>{LONG}</p><script>tracking()</script>")
        pages.append(request.url.params["page"])
        assert "target_level" not in request.url.params
        return httpx.Response(200, text=GOOGLE_PAGE)
    res = sources.google_listings(_client(handler), {})
    assert pages == ["1", "2"] and [x.title for x in res.listings] == ["Software Engineer, Example"]
    detail = res.listings[0].fetch_detail()
    assert detail.ok and "reliable services" in detail.description and "tracking" not in detail.description


def test_google_an_unrecognised_page_is_a_failure_not_a_quiet_zero():
    with pytest.raises(sources.SourceError):
        sources.google_listings(_client(lambda r: httpx.Response(200, text="<html>a sign-in page</html>")), {})


# ── the rules are the existing ones ───────────────────────────────

@pytest.mark.parametrize("title, qualifications, accepted", [
    ("Software Development Engineer I", "- 1+ years of non-internship professional software development experience", False),
    ("Software Development Engineer, Campus Hire", "- Bachelor's degree. Freshers and new graduates welcome. 0 years of experience required.", True),
])
def test_new_sources_go_through_the_scanners_existing_experience_rule(monkeypatch, title, qualifications, accepted):
    """A stated minimum above 0 years is "experienced" today. This pins that
    the adapters changed nothing; widening it is a separate decision."""
    def handler(request):
        return httpx.Response(200, json={"hits": 1, "jobs": [_amazon_job(title=title, basic_qualifications=qualifications)]})
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: real(*a, **{**kw, "transport": httpx.MockTransport(handler)}))
    scan = scraper.scan_company({"id": "amazon", "name": "Amazon", "url": "https://www.amazon.jobs/"})
    assert bool(scan.jobs) is accepted
