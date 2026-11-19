"""Radancy career sites (Sanofi, Synchrony) read from their published sitemap
and JobPosting pages; S&P Global on its official Jibe site. Fixtures; no network."""
import json

import httpx
import pytest

import scraper
import sources

SM = "https://jobs.example-radancy.com/sitemap.xml"
JOB = "https://jobs.example-radancy.com/en/job/hyderabad/team-lead-data-and-process-management/2649/47053827072"
DESC = "Graduate role in Hyderabad. Freshers welcome; 0 years of experience required. " * 3


def _sitemap(*locs):
    return ('<?xml version="1.0" encoding="utf-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + "".join(f"<url><loc>{u}</loc></url>" for u in locs) + "</urlset>")


def _client(sitemap_text, page_status=200):
    def handler(request):
        if str(request.url) == SM:
            return httpx.Response(200, text=sitemap_text)
        posting = {"@context": "https://schema.org", "@type": "JobPosting", "title": "Team Lead - Data & Process Management",
                   "url": JOB, "identifier": {"@type": "PropertyValue", "name": "Job ID", "value": "R2879046"},
                   "description": DESC, "datePosted": "2026-11-19",
                   "jobLocation": {"@type": "Place", "address": {"addressLocality": "Hyderabad", "addressCountry": "India"}}}
        return httpx.Response(page_status, text=f'<script type="application/ld+json">{json.dumps(posting)}</script>')
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_only_india_job_pages_of_the_same_site_are_listed():
    text = _sitemap("https://jobs.example-radancy.com/en/category/sales-jobs/2649/1/1", JOB,
                    "https://jobs.example-radancy.com/en/job/hangzhou/x/2649/2", "https://evil.example/en/job/hyderabad/x/1/2")
    res = sources.career_sitemap_listings(_client(text), {"sitemap": SM})
    assert res.seen == 2 and [l.url for l in res.listings] == [JOB] and res.listings[0].location == "Hyderabad"


def test_detail_uses_the_pages_own_posting_title_and_requisition_id():
    listing = sources.career_sitemap_listings(_client(_sitemap(JOB)), {"sitemap": SM}).listings[0]
    d = listing.fetch_detail()
    assert d.ok and d.posted_at == "2026-11-19" and "Freshers welcome" in d.description
    assert listing.title == "Team Lead - Data & Process Management" and listing.job_id == "R2879046"


def test_a_sitemap_without_job_pages_or_an_html_error_page_is_a_failure():
    with pytest.raises(sources.SourceError, match="no job pages"):
        sources.career_sitemap_listings(_client(_sitemap("https://jobs.example-radancy.com/en/about")), {"sitemap": SM})
    with pytest.raises(sources.SourceError, match="not a sitemap"):
        sources.career_sitemap_listings(_client("<html>blocked</html>"), {"sitemap": SM})


def test_a_job_already_recorded_from_workday_is_not_re_alerted():
    seen = [{"title": "Team Lead", "company": "Sanofi", "location": "Hyderabad",
             "url": "https://sanofi.wd3.myworkdayjobs.com/SanofiCareers/job/Hyderabad/Team-Lead_R2879046"}]
    found = {"title": "Team Lead - Data & Process Management", "company": "Sanofi", "location": "Hyderabad, India",
             "url": JOB, "evidence": {"job_id": "id:R2879046"}}
    assert scraper.merge_new_jobs(seen, [found]) == []


def test_sanofi_synchrony_and_sp_global_use_their_official_sites():
    for cid, kind, key, value in (("sanofi", "career_sitemap", "sitemap", "https://jobs.sanofi.com/sitemap.xml"),
                                  ("c1793696374", "career_sitemap", "sitemap", "https://www.synchronycareers.com/sitemap.xml"),
                                  ("c1793696385", "jibe", "host", "https://careers.spglobal.com")):
        src = scraper.sources_for({"id": cid, "url": "https://x.wd3.myworkdayjobs.com/External"})
        assert src[0]["type"] == kind and src[0][key] == value
