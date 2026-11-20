"""Novartis's own careers search (India filter, stable order, every page the
total implies) and full job pages. Recorded-shape fixtures; no network."""
import json

import httpx
import pytest

import scraper
import sources

DESC = "<p>Essential Requirements: graduate degree, 0-1 years of experience; freshers welcome.</p>" * 4


def _search(start, total, per=10):
    rows = "".join(f'<div><a href="/careers/career-search/job/details/req-{10088000 + i}-analyst-{i}">Analyst {i}</a>'
                   f"<span>India Hyderabad (Office)</span></div>" for i in range(start, min(start + per, total)))
    return f"<html><p>{total} results</p>{rows}</html>"


def _client(total=25, fail_page=None):
    asked = []

    def handler(request):
        asked.append(request.url)
        if request.url.path == "/careers/career-search":
            assert request.url.params["country[]"] == "LOC_IN" and request.url.params["order"] == "Job Title"
            page = int(request.url.params["page"])
            if page == fail_page:
                return httpx.Response(503)
            return httpx.Response(200, text=_search(page * 10, total))
        posting = {"@context": "https://schema.org", "@type": "JobPosting", "title": "Analyst 0",
                   "description": "Short summary of the role.", "datePosted": "2026-11-18",
                   "identifier": {"@type": "PropertyValue", "value": "REQ-10088000"},
                   "jobLocation": {"@type": "Place", "address": {"addressLocality": "Hyderabad (Office)", "addressCountry": "IN"}}}
        page = (f'<script type="application/ld+json">{json.dumps(posting)}</script>'
                f'<div class="field_job_description ">{DESC}</div><div class="field_job_snapshot">x</div>')
        return httpx.Response(200, text=page)
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="https://www.novartis.com"), asked


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(sources.time, "sleep", lambda s: None)


def test_every_india_page_is_read_with_requisition_ids():
    client, asked = _client(total=25)
    res = sources.novartis_listings(client, {"country_code": "LOC_IN"})
    assert res.seen == 25 and len(res.listings) == 25
    first = res.listings[0]
    assert first.job_id == "REQ-10088000" and first.title == "Analyst 0" and first.url.startswith("https://www.novartis.com/careers/")


def test_a_failed_page_or_a_short_result_never_counts_as_healthy(monkeypatch):
    client, _ = _client(total=25, fail_page=1)
    with pytest.raises(sources.SourceError, match="HTTP 503"):
        sources.novartis_listings(client, {"country_code": "LOC_IN"})
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=_search(0, 25) .replace("25 results", "60 results"))))
    with pytest.raises(sources.SourceError, match="incomplete coverage"):
        sources.novartis_listings(client, {"country_code": "LOC_IN"})


def test_the_job_page_gives_the_full_posting_not_the_summary():
    client, _ = _client(total=1)
    listing = sources.novartis_listings(client, {"country_code": "LOC_IN"}).listings[0]
    d = listing.fetch_detail()
    assert d.ok and "Essential Requirements" in d.description and d.posted_at == "2026-11-18" and "Hyderabad" in d.location


def test_novartis_uses_its_official_search_and_req_ids_match_workday():
    src = scraper.sources_for({"id": "c1793696372", "url": "https://novartis.wd3.myworkdayjobs.com/Novartis_Careers"})
    assert src == [{"type": "novartis", "country_code": "LOC_IN"}]
    seen = [{"title": "x", "company": "Novartis", "location": "Hyderabad",
             "url": "https://novartis.wd3.myworkdayjobs.com/Novartis_Careers/job/Hyderabad/Analyst_REQ-10088000"}]
    found = {"title": "Analyst 0", "company": "Novartis", "location": "Hyderabad, IN",
             "url": "https://www.novartis.com/careers/career-search/job/details/req-10088000-analyst-0",
             "evidence": {"job_id": "id:REQ-10088000"}}
    assert scraper.merge_new_jobs(seen, [found]) == []
