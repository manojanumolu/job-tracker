"""JioHotstar read from its Workday career site's sitemap (Nov 2026).

JioStar's firewall answers the Workday search API (/wday/cxs/...) with 403
"Your request has been blocked" since 6 Nov 2026. Its robots.txt publishes
the sitemap ("Allow: /JioStar/") and each job page carries a schema.org
JobPosting, so the scan reads that public channel instead and never calls
the blocked API. Canned answers modelled on the live site (9 Nov 2026); no
network; nothing is emailed."""
import json

import httpx
import pytest

import scraper
import sources

SITE = "https://jiostar.wd102.myworkdayjobs.com/JioStar"
CFG = scraper.COMPANY_SOURCES["jiohotstar"][0]
DESC = ("<p>Join the viewer experience team. Freshers and recent graduates welcome. "
        + "Build and run reliable services for millions of viewers. " * 3 + "</p>")


def _sitemap(*paths):
    urls = "".join(f'<url><loc>{SITE}/job/{p}</loc><xhtml:link rel="alternate" hreflang="en-US" '
                   f'href="https://jiostar.wd102.myworkdayjobs.com/en-US/JioStar/job/{p}"/></url>' for p in paths)
    return f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'


def _page(title, job_id, locality="Bengaluru- We Work", description=DESC, posted="2026-11-05"):
    posting = {"@context": "http://schema.org", "@type": "JobPosting", "title": title, "description": description,
               "identifier": {"@type": "PropertyValue", "name": title, "value": job_id}, "datePosted": posted,
               "jobLocation": {"@type": "Place", "address": {"@type": "PostalAddress", "addressLocality": locality,
                                                             "addressCountry": "India"}}}
    return f'<html><head><script type="application/ld+json">{json.dumps(posting)}</script></head><body></body></html>'


PAGES = {
    "Bengaluru--We-Work/Software-Development-Engineer-I_JR12321-1": _page("Software Development Engineer I", "JR12321-1"),
    "Mumbai---OUC/Director---Growth-Strategy_JR10100": _page("Director - Growth Strategy", "JR10100", "Mumbai - OUC"),
    "London/Software-Engineer_JR90001": _page("Software Engineer", "JR90001", "London"),
}


@pytest.fixture
def site():
    asked = []

    def handler(request):
        asked.append(request.url.path)
        if "/wday/cxs/" in request.url.path:
            return httpx.Response(403, text="<h1>Your request has been blocked.</h1>")
        if request.url.path.endswith("/siteMap.xml"):
            return httpx.Response(200, text=_sitemap(*PAGES))
        path = request.url.path.split("/JioStar/job/", 1)[1]
        assert request.url.path.startswith("/en-US/JioStar/job/") and request.headers["accept"] == "text/html"
        return httpx.Response(200, text=PAGES[path])
    return handler, asked


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_the_sitemap_lists_india_jobs_with_their_ids_and_never_calls_the_blocked_api(site):
    handler, asked = site
    res = sources.workday_sitemap_listings(_client(handler), CFG)
    assert res.seen == 3 and [x.job_id for x in res.listings] == ["JR12321-1", "JR10100"]      # London left out
    assert res.listings[0].url == "https://jiostar.wd102.myworkdayjobs.com/en-US/JioStar/job/Bengaluru--We-Work/Software-Development-Engineer-I_JR12321-1"
    assert res.listings[0].title == "Software Development Engineer I"                        # from the URL slug, for now
    assert asked == ["/JioStar/siteMap.xml"] and not any("/wday/cxs/" in a for a in asked)


def test_the_job_page_jobposting_is_the_detail(site):
    handler, asked = site
    res = sources.workday_sitemap_listings(_client(handler), CFG)
    d = res.listings[1].fetch_detail()
    assert d.ok and d.posting_evidence and "Freshers and recent graduates" in d.description
    assert d.location == "Mumbai - OUC, India" and d.posted_at == "2026-11-05"
    assert res.listings[1].title == "Director - Growth Strategy"                              # the page's own title


@pytest.mark.parametrize("answer", [
    lambda: httpx.Response(403, text="blocked"), lambda: httpx.Response(503),
    lambda: httpx.Response(200, text="<html>a sign-in page</html>"),
    lambda: httpx.Response(200, text='<urlset xmlns="x"><url><loc>cut off'),
])
def test_a_refused_or_broken_sitemap_is_a_failure_never_zero_jobs(answer):
    with pytest.raises(sources.SourceError):
        sources.workday_sitemap_listings(_client(lambda r: answer()), CFG)


def test_a_page_without_this_jobs_posting_is_unreadable_not_a_job(site):
    handler, _ = site
    listing = sources.Listing(title="Software Development Engineer I", url=f"{SITE}/job/x/Other_JR1", job_id="JR777")

    def other(request):
        return httpx.Response(200, text=_page("Something Else", "JR999"))
    assert not sources.workday_sitemap_detail(_client(other), listing).ok
    assert not sources.workday_sitemap_detail(_client(lambda r: httpx.Response(404)), listing).ok


def test_jiohotstar_scans_end_to_end_and_keeps_its_old_job_identity(site, monkeypatch):
    handler, asked = site
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: real(*a, **{**kw, "transport": httpx.MockTransport(handler)}))
    scan = scraper.scan_company({"id": "jiohotstar", "name": "JioHotstar (JioStar)", "url": SITE})
    assert scan.status == "active" and scan.seen == 3 and not any("/wday/cxs/" in a for a in asked)
    (job,) = scan.jobs
    assert job["title"] == "Software Development Engineer I" and job["posted_at"] == "2026-11-05"
    assert scan.reasons["seniority in title"] == 1                                           # the Director role
    # the job saved from the old Workday API keeps its identity: never a new record, never re-alerted
    old = {"title": "Software Development Engineer I", "company": "JioHotstar (JioStar)",
           "url": f"{SITE}/job/Bengaluru--We-Work/Software-Development-Engineer-I_JR12321-1",
           "uid": "jiohotstar (jiostar)|id:JR12321-1"}
    assert scraper.merge_new_jobs([old], scan.jobs, company_id="jiohotstar") == []
    assert scraper.source_label({"id": "jiohotstar", "url": SITE}) == "Workday sitemap + job pages"
