"""End-to-end test of the Playwright path against a local fake career site.

Skipped when Chromium isn't available (e.g. `playwright install` not run).
Set PLAYWRIGHT_CHROMIUM_EXECUTABLE to use a pre-installed Chromium.
"""
import functools
import http.server
import os
import threading
from pathlib import Path

import pytest

import scraper

pw = pytest.importorskip("playwright.sync_api")

LISTING = """<html><body>
<nav><a href="/india">Careers in India</a> <a href="/early">Early Careers</a></nav>
<div class="cookie">We use cookies. Senior roles need 5+ years. Freshers welcome!</div>
<a href="jobs/exp.html"><h3>Associate Software Engineer</h3><p>Bengaluru, India</p></a>
<a href="jobs/fresher.html"><h3>Associate Analyst</h3><p>Hyderabad, India</p></a>
<a href="jobs/fresher.html"><h3>Associate Analyst</h3><p>Hyderabad, India</p></a>
<a href="/jobs/grad.html#apply"><h3>Graduate Engineer Trainee</h3><p>Pune, India</p></a>
<a href="jobs/senior.html"><h3>Senior Data Engineer</h3><p>Pune, India</p></a>
<a href="jobs/badjson.html"><h3>Graduate Analyst</h3><p>Chennai, India</p></a>
<a href="jobs/abroad.html"><h3>Graduate Developer</h3><p>Hyderabad, India</p></a>
<a href="jobs/noise.html"><h3>Data Analyst</h3><p>Noida, India</p></a>
<a href="jobs/redirect.html"><h3>Graduate Consultant</h3><p>Mumbai, India</p></a>
<a href="#"><h3>Trainee Associate</h3><p>Delhi, India</p></a>
<a href="jobs/london.html"><h3>Graduate Analyst London</h3><p>London</p></a>
<a href="/blog/meet-priya"><h3>Meet Priya: Associate in Pune</h3></a>
</body></html>"""

LONG = " Lorem ipsum dolor sit amet, consectetur adipiscing elit." * 5

PAGES = {
    "index.html": LISTING,
    # experience only in JSON-LD
    "jobs/exp.html": """<script type="application/ld+json">{"@type":"JobPosting","title":"Associate Software Engineer",
        "description":"<ul><li>2&ndash;4 years of experience in Java</li></ul>",
        "jobLocation":{"address":{"addressLocality":"Bengaluru","addressCountry":"IN"}}}</script>""",
    # no JSON-LD, description container; nav text must be ignored
    "jobs/fresher.html": f"""<nav>Senior roles: 5+ years</nav>
        <div class="job-description">This role is open to freshers.{LONG}</div>""",
    # related job in JSON-LD must not be picked over the matching one
    "jobs/grad.html": """<script type="application/ld+json">[{"@type":"JobPosting","title":"Senior Engineer",
        "description":"8+ years of experience"},{"@type":"JobPosting","title":"Graduate Engineer Trainee",
        "description":"Graduate programme for the 2025 batch.",
        "jobLocation":{"address":{"addressLocality":"Pune","addressCountry":"India"}}}]</script>""",
    # malformed JSON-LD, no container -> card only
    "jobs/badjson.html": """<script type="application/ld+json">{not json</script><p>hello</p>""",
    # India on the card but the posting itself is elsewhere
    "jobs/abroad.html": """<script type="application/ld+json">{"@type":"JobPosting","title":"Graduate Developer",
        "description":"New grads welcome","jobLocation":{"address":{"addressLocality":"Dublin","addressCountry":"IE"}}}</script>""",
    # generic <main> with a related-jobs widget: must not be used as description
    "jobs/noise.html": """<main><h1>Data Analyst</h1><aside>Similar: Graduate Trainee, freshers welcome</aside>
        """ + LONG + """</main>""",
    # expired posting bouncing back to the listing
    "jobs/redirect.html": """<script>location.replace('/index.html')</script>""",
}


class _Handler(http.server.SimpleHTTPRequestHandler):
    requested: list = []

    def do_GET(self):
        self.requested.append(self.path)
        super().do_GET()

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    root: Path = tmp_path_factory.mktemp("site")
    for rel, body in PAGES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(f"<html><head></head><body>{body}</body></html>" if rel != "index.html" else body)
    handler = functools.partial(_Handler, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/index.html"
    server.shutdown()


@pytest.fixture(autouse=True)
def chromium(monkeypatch):
    exe = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
    if exe:
        from playwright.sync_api._generated import BrowserType
        orig = BrowserType.launch
        monkeypatch.setattr(BrowserType, "launch", lambda self, **kw: orig(self, executable_path=exe, **kw))
    try:
        with pw.sync_playwright() as p:
            p.chromium.launch(headless=True).close()
    except Exception as e:
        pytest.skip(f"Chromium not available: {e}")


def test_playwright_end_to_end(site):
    _Handler.requested = []
    jobs = scraper._scrape_playwright({"id": "t", "name": "Test", "url": site})
    by_title = {j["title"]: j for j in jobs}
    # exp.html (2-4 yrs in JSON-LD), abroad.html (Dublin), noise.html (related-jobs
    # text in <main>), Senior, London card, blog and nav links are all excluded.
    # No readable detail -> judged on the card title: "#" link, malformed
    # JSON-LD, redirect back to the listing.
    assert set(by_title) == {"Associate Analyst", "Graduate Engineer Trainee", "Graduate Analyst",
                             "Trainee Associate", "Graduate Consultant"}
    assert by_title["Associate Analyst"]["category"] == "FRESHER"
    # relative URLs resolved, fragments preserved on the stored link
    assert by_title["Associate Analyst"]["url"].endswith("/jobs/fresher.html")
    assert by_title["Graduate Engineer Trainee"]["url"].endswith("/jobs/grad.html#apply")
    # duplicate cards fetched once; "#" link and senior/non-India/blog never fetched
    assert _Handler.requested.count("/jobs/fresher.html") == 1
    for path in ("/jobs/senior.html", "/jobs/london.html", "/blog/meet-priya"):
        assert path not in _Handler.requested
    assert _Handler.requested.count("/index.html") >= 1  # listing (+ redirect bounce)


def test_playwright_detail_budget(site, monkeypatch):
    monkeypatch.setattr(scraper, "MAX_DETAIL_FETCHES", 2)
    _Handler.requested = []
    scraper._scrape_playwright({"id": "t", "name": "Test", "url": site})
    detail_hits = [p for p in _Handler.requested if p.startswith("/jobs/")]
    assert len(detail_hits) == 2
    # card-accepted jobs are verified first
    assert "/jobs/grad.html" in detail_hits
