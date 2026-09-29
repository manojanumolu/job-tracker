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
<a href="jobs/gradexp.html"><h3>Graduate Engineer</h3><p>Kochi, India</p></a>
<a href="jobs/cardexp.html"><h3>Associate Engineer</h3><p>Pune, India</p><p>2-5 Yrs</p></a>
<a href="jobs/assoc.html"><h3>Associate Developer</h3><p>Indore, India</p></a>
<a href="jobs/related.html"><h3>Associate Tester</h3><p>Jaipur, India</p></a>
<a href="jobs/london.html"><h3>Graduate Analyst London</h3><p>London</p></a>
<a href="/blog/meet-priya"><h3>Meet Priya: Associate in Pune</h3></a>
<!-- Accenture's real card structure: the <a> is an empty overlay; title, location
     and experience sit elsewhere in the card -->
<div class="rad-filters-vertical__job-card">
  <div class="rad-filters-vertical__job-card-header">
    <h3 class="rad-filters-vertical__job-card-title">Trust &amp; Safety New Associate</h3>
    <div class="rad-filters-vertical__job-card-details"><span>Various locations</span><span>Full time</span>
      <span>Experience: 0-2 years</span></div></div>
  <div class="rad-filters-vertical__job-card-content"><div>Location: Hyderabad</div>
    <div class="rad-filters-vertical__job-card-content-buttons">
      <a class="rad-button" href="jobs/accfresh.html?id=AIOC-S0001_en&amp;title=Trust"></a></div></div>
</div>
<div class="rad-filters-vertical__job-card">
  <div class="rad-filters-vertical__job-card-header">
    <h3 class="rad-filters-vertical__job-card-title">Custom Software Engineer</h3>
    <div class="rad-filters-vertical__job-card-details"><span>Bengaluru</span><span>Full time</span>
      <span>Experience: 2-5 years</span></div></div>
  <div class="rad-filters-vertical__job-card-content-buttons">
    <a class="rad-button" href="jobs/accexp.html?id=ATCI-1_en&amp;title=Custom"></a></div>
</div>
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
    # card says Graduate, detail says 2 years: detail wins
    "jobs/gradexp.html": """<script type="application/ld+json">{"@type":"JobPosting","title":"Graduate Engineer",
        "description":"<p>Minimum 2 years of experience in embedded C.</p>",
        "jobLocation":{"address":{"addressLocality":"Kochi","addressCountry":"IN"}}}</script>""",
    # experience on the card itself: rejected without fetching the detail page
    "jobs/cardexp.html": "<p>unused</p>",
    # ambiguous title, evidence only in the (header-then-body) description container
    "jobs/assoc.html": f"""<div class="job-description-header">Job description</div>
        <div class="job-description-body">Fresh graduates are encouraged to apply.
        No prior professional experience required.{LONG}</div>""",
    # two unrelated JSON-LD postings, neither ours: must not borrow their text
    "jobs/related.html": """<script type="application/ld+json">[{"@type":"JobPosting","title":"Graduate Trainee",
        "description":"Freshers welcome"},{"@type":"JobPosting","title":"Graduate Analyst",
        "description":"New grads welcome"}]</script>""",
    # expired posting bouncing back to the listing
    "jobs/redirect.html": """<script>location.replace('/index.html')</script>""",
    # Accenture-style detail: no JSON-LD, a job-description block
    "jobs/accfresh.html": f"""<h1>Trust &amp; Safety New Associate</h1><div class="job-description">
        Designation: Trust &amp; Safety New Associate. Qualifications: Any Graduation.
        Years of Experience: 0 to 1 years.{LONG}</div>""",
    "jobs/accexp.html": "<p>unused</p>",
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
    # P0: no readable detail -> never alerted on the card title alone: "#" link
    # (Trainee Associate), malformed JSON-LD (Graduate Analyst), redirect back
    # to the listing (Graduate Consultant).
    assert set(by_title) == {"Associate Analyst", "Graduate Engineer Trainee", "Associate Developer",
                             "Trust & Safety New Associate"}
    for title in ("Graduate Analyst", "Trainee Associate", "Graduate Consultant"):
        assert title not in by_title
    # Accenture-style card: empty <a>, title from the card heading, India from the card
    assert by_title["Trust & Safety New Associate"]["category"] == "FRESHER"
    assert by_title["Trust & Safety New Associate"]["url"].endswith("jobs/accfresh.html?id=AIOC-S0001_en&title=Trust")
    assert by_title["Trust & Safety New Associate"]["location"] == "Location: Hyderabad"
    # its experienced sibling is rejected from the card ("Experience: 2-5 years") without a fetch
    assert "Custom Software Engineer" not in by_title and "/jobs/accexp.html" not in " ".join(_Handler.requested)
    # detail requirement overrides the card's "Graduate"; card "2-5 Yrs" is final;
    # unrelated JSON-LD postings are not used as this job's description
    assert "Graduate Engineer" not in by_title
    assert "Associate Engineer" not in by_title and "/jobs/cardexp.html" not in _Handler.requested
    assert "Associate Tester" not in by_title and "/jobs/related.html" in _Handler.requested
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


def test_playwright_jobs_carry_location(site):
    jobs = {j["title"]: j for j in scraper._scrape_playwright({"id": "t", "name": "Test", "url": site})}
    assert jobs["Graduate Engineer Trainee"]["location"] == "Pune · India"    # JSON-LD
    assert jobs["Associate Analyst"]["location"] == "Hyderabad, India"         # card fallback
