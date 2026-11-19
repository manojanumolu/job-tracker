"""Phenom career sites (Cisco, Mastercard, State Street, Evernorth, Thermo
Fisher, WBD): the public, robots-allowed search page's results JSON, paged with
"from"; job pages' schema.org JobPosting; requisition ids that match Workday
records so a source switch never re-alerts. Recorded-shape fixtures; no network."""
import json

import httpx
import pytest

import scraper
import sources

BASE = "https://careers.example-phenom.com/global/en"
DESC = "Graduate engineer role. Freshers welcome, 0 years of experience. " * 3


def _page(jobs, total):
    ddo = {"eagerLoadRefineSearch": {"totalHits": total, "hits": len(jobs), "data": {"jobs": jobs}}}
    return f"<html><script>phApp.ddo = {json.dumps(ddo)}; phApp.experimentData = {{}};</script></html>"


def _job(i, loc="Bangalore, India"):
    return {"jobId": str(2023000 + i), "reqId": str(2023000 + i), "title": f"Software Engineer {i}", "location": loc,
            "descriptionTeaser": "Build things"}


def _client(total=25, fail_at=None, status=200):
    asked = []

    def handler(request):
        asked.append(str(request.url))
        if request.url.path.endswith("/search-results"):
            start = int(request.url.params.get("from", 0))
            if fail_at is not None and start == fail_at:
                return httpx.Response(status)
            assert request.url.params["qcountry"] == "India"
            return httpx.Response(200, text=_page([_job(i) for i in range(start, min(start + 10, total))], total))
        posting = {"@context": "https://schema.org", "@type": "JobPosting", "title": "Software Engineer 0",
                   "description": DESC, "datePosted": "2026-11-13",
                   "jobLocation": [{"@type": "Place", "address": {"addressLocality": "Milpitas", "addressCountry": "US"}}]}
        return httpx.Response(200, text=f'<script type="application/ld+json">{json.dumps(posting)}</script>')
    return httpx.Client(transport=httpx.MockTransport(handler)), asked


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(sources.time, "sleep", lambda s: None)


def test_every_page_is_read_until_the_total_with_requisition_ids():
    client, asked = _client(total=25)
    res = sources.phenom_listings(client, {"base": BASE, "country": "India"})
    assert res.seen == 25 and len(res.listings) == 25 and len(asked) == 3          # 3 pages, nothing else
    first = res.listings[0]
    assert first.url == f"{BASE}/job/2023000" and first.job_id == "2023000" and first.posting_evidence


def test_a_failed_page_fails_the_scan_never_a_partial_healthy_one():
    client, _ = _client(total=25, fail_at=10, status=429)
    with pytest.raises(sources.SourceError, match="HTTP 429"):
        sources.phenom_listings(client, {"base": BASE, "country": "India"})


def test_an_unreadable_search_page_is_a_parse_failure():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>maintenance</html>")))
    with pytest.raises(sources.SourceError, match="unexpected format"):
        sources.phenom_listings(client, {"base": BASE, "country": "India"})


def test_a_valid_empty_india_result_is_healthy_and_empty():
    client, _ = _client(total=0)
    res = sources.phenom_listings(client, {"base": BASE, "country": "India"})
    assert res.listings == [] and res.seen == 0


def test_job_page_detail_keeps_the_india_location_of_a_multi_location_posting():
    client, _ = _client(total=1)
    listing = sources.phenom_listings(client, {"base": BASE, "country": "India"}).listings[0]
    d = listing.fetch_detail()
    assert d.ok and d.location == "Bangalore, India" and d.posted_at == "2026-11-13" and "Freshers welcome" in d.description


def test_the_six_companies_use_their_official_phenom_sites():
    expect = {"cisco": "careers.cisco.com", "c1793696382": "careers.mastercard.com", "c1793696381": "careers.statestreet.com",
              "c1793696380": "jobs.thecignagroup.com", "c1793696389": "jobs.thermofisher.com", "c1793696377": "careers.wbd.com"}
    for cid, host in expect.items():
        src = scraper.sources_for({"id": cid, "url": "https://x.wd5.myworkdayjobs.com/External"})
        assert src[0]["type"] == "phenom" and host in src[0]["base"] and src[0]["country"] == "India"
        assert scraper.source_label({"id": cid, "url": ""}) == "Phenom career site"


@pytest.mark.parametrize("old_url,new_req", [
    ("https://cisco.wd5.myworkdayjobs.com/Cisco_Careers/job/Bangalore/Software-Engineer_2023748", "2023748"),
    ("https://thermofisher.wd5.myworkdayjobs.com/TF/job/Mumbai/Analyst_R-01372950-1", "R-01372950"),
    ("https://statestreet.wd1.myworkdayjobs.com/Global/job/Pune/Apprentice_R-799731", "R-799731"),
])
def test_a_job_already_recorded_from_workday_is_not_re_alerted_after_the_switch(old_url, new_req):
    seen = [{"title": "Old title", "company": "Co", "location": "Somewhere, India", "url": old_url}]
    found = {"title": "Software Engineer", "company": "Co", "location": "Bengaluru, India",
             "url": f"{BASE}/job/{new_req}", "evidence": {"job_id": f"id:{new_req}"}}
    assert scraper.merge_new_jobs(seen, [found]) == []
    other = dict(found, url=f"{BASE}/job/9999999", evidence={"job_id": "id:9999999"})
    assert len(scraper.merge_new_jobs(seen, [other])) == 1
