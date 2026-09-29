"""Hostile career sites from the Sep 2026 audit, served locally.

Every page here tried to get a job emailed that must not be: a redirect to a
different job, failed/blocked detail pages, experience outside JSON-LD,
schema.org URL types, hidden requirements, programme and story pages.
Also checks that a blocked/landing listing page is reported as unhealthy.

Skipped when Chromium isn't available (see test_playwright_scrape.py).
"""
import functools
import http.server
import json
import os
import threading
from pathlib import Path

import pytest

import scraper

pw = pytest.importorskip("playwright.sync_api")

LD = lambda d: f'<script type="application/ld+json">{json.dumps(d)}</script>'
LONG = " Lorem ipsum dolor sit amet, consectetur adipiscing elit." * 5
IND = {"address": {"addressLocality": "Hyderabad", "addressCountry": "IN"}}

CARDS = [
    ("redirect", "Associate Analyst", "Hyderabad, India"),       # 302 -> a different (fresher) job
    ("err500", "Graduate Engineer", "Pune, India"),              # detail HTTP 500
    ("err403", "Graduate Designer", "Pune, India"),              # detail HTTP 403
    ("cf", "Graduate Analyst", "Chennai, India"),                # Cloudflare challenge page
    ("badge", "New\nSenior Data Engineer", "Pune, India"),       # badge on the first line
    ("fieldexp", "Graduate Developer", "Noida, India"),          # JSON-LD lacks exp; visible field has it
    ("schemaurl", "Graduate Tester", "Indore, India"),           # @type as a full schema.org URL
    ("hidden", "Graduate Consultant", "Mumbai, India"),          # requirement in a hidden element
    ("gradprog", "Graduate Program", "Hyderabad, India"),        # programme landing page
    ("story", "Inside our Graduate Program", "Stories from Hyderabad, India"),
    ("otherjob", "Junior Analyst", "Hyderabad, India"),          # page's only JSON-LD is another job
    ("inka", "Graduate Trainee", "Bengaluru, India"),            # JSON-LD country "IN-KA" only
    ("similar", "Associate Engineer", "Pune, India"),            # similar-jobs widget says freshers welcome
    ("good", "Graduate Software Engineer", "Hyderabad, India"),  # control: a genuine graduate role
]
PAGES = {
    "other.html": LD({"@type": "JobPosting", "title": "Graduate Trainee - Operations",
                      "description": "Freshers welcome. No prior experience required.", "jobLocation": IND}),
    "cf.html": "<title>Just a moment...</title><div>Checking your browser before accessing.</div>",
    "badge.html": LD({"@type": "JobPosting", "title": "Senior Data Engineer", "description": "8+ years of experience"}),
    "fieldexp.html": LD({"@type": "JobPosting", "title": "Graduate Developer", "description": "Build APIs for our platform.",
                         "jobLocation": IND}) + "<div class='job-meta'>Experience: 3-5 years</div>",
    "schemaurl.html": LD({"@type": "http://schema.org/JobPosting", "title": "Graduate Tester",
                          "description": "Minimum 4 years of experience in testing.", "jobLocation": IND}),
    "hidden.html": f"<h1>Graduate Consultant</h1><div class='job-description'>Great role for graduates.{LONG}"
                   "<span style='display:none'>Minimum 3 years of experience required</span></div>",
    "gradprog.html": f"<div class='job-description'>Our graduate program welcomes recent graduates.{LONG}</div>",
    "story.html": "<p>blog</p>",
    "otherjob.html": LD({"@type": "JobPosting", "title": "Graduate Trainee (Freshers)",
                         "description": "Freshers welcome, no experience required.", "jobLocation": IND}),
    "inka.html": LD({"@type": "JobPosting", "title": "Graduate Trainee", "description": "Open to 2025 graduates.",
                     "jobLocation": {"address": {"addressCountry": "IN-KA"}}}),
    "similar.html": LD({"@type": "JobPosting", "title": "Associate Engineer", "description": "Design and build services.",
                        "jobLocation": IND})
                    + "<aside class='similar-jobs'><div>Experience: 0-1 years</div><div>Freshers welcome</div></aside>",
    "good.html": LD({"@type": "JobPosting", "title": "Graduate Software Engineer",
                     "description": "Open to 2025 graduates. Experience: 0-1 years.", "jobLocation": IND}),
}


class _Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path.startswith("/jobs/redirect"):
            self.send_response(302)
            self.send_header("Location", "/jobs/other.html")
            self.end_headers()
            return
        if path.startswith("/jobs/err500") or path.startswith("/jobs/err403"):
            self.send_response(500 if "500" in path else 403)
            self.end_headers()
            self.wfile.write(b"<h1>Error</h1>")
            return
        if path.startswith("/blocked"):
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"<html><head><title>Just a moment...</title></head><body>cf</body></html>")
            return
        super().do_GET()

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    root: Path = tmp_path_factory.mktemp("hostile")
    (root / "jobs").mkdir()
    listing = "<html><body>" + "".join(
        f'<a href="/jobs/{k}.html"><h3>{t.replace(chr(10), "</h3><h3>")}</h3><p>{loc}</p></a>' for k, t, loc in CARDS
    ) + "</body></html>"
    (root / "index.html").write_text(listing, encoding="utf-8")
    (root / "landing.html").write_text(
        "<html><body><nav><a href='/about'>About us</a><a href='/india'>India (English)</a>"
        "<a href='/early'>Early Careers</a></nav><p>Freshers welcome! Graduate programme in Hyderabad, India.</p></body></html>",
        encoding="utf-8")
    for rel, body in PAGES.items():
        (root / "jobs" / rel).write_text(f"<html><head></head><body>{body}</body></html>", encoding="utf-8")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Handler, directory=str(root)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
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


@pytest.fixture(scope="module")
def scan(site):
    return scraper._scan_playwright({"id": "hostile", "name": "Hostile", "url": f"{site}/index.html"})


def test_only_the_genuine_roles_are_emailed(scan):
    # the control role, and the graduate role whose only location is the ISO
    # subdivision "IN-KA" (Karnataka) — previously skipped as "not India"
    assert sorted((j["title"], j["category"]) for j in scan.jobs) == [
        ("Graduate Software Engineer", "FRESHER"), ("Graduate Trainee", "ENTRY_LEVEL")]


def test_redirect_to_another_job_never_borrows_its_details(scan):
    assert "Associate Analyst" not in {j["title"] for j in scan.jobs}


@pytest.mark.parametrize("title", ["Graduate Engineer", "Graduate Designer", "Graduate Analyst"])
def test_failed_or_blocked_detail_never_emails(scan, title):
    assert title not in {j["title"] for j in scan.jobs}


@pytest.mark.parametrize("title", ["Graduate Developer", "Graduate Tester", "Graduate Consultant"])
def test_experience_outside_the_description_rejects(scan, title):
    assert title not in {j["title"] for j in scan.jobs}


@pytest.mark.parametrize("title", ["Graduate Program", "Inside our Graduate Program", "Junior Analyst",
                                   "New", "Senior Data Engineer", "Associate Engineer"])
def test_non_jobs_and_mismatches_never_email(scan, title):
    assert title not in {j["title"] for j in scan.jobs}


def test_scan_reports_unreadable_details(scan):
    assert scan.details_failed >= 4
    assert scan.status in {"active", "failing"}


def test_blocked_listing_is_broken_not_healthy(site):
    scan = scraper._scan_playwright({"id": "b", "name": "Blocked", "url": f"{site}/blocked"})
    assert scan.jobs == [] and scan.status == "broken"
    assert "403" in scan.reason


def test_landing_page_without_job_links_needs_configuration(site):
    scan = scraper._scan_playwright({"id": "l", "name": "Landing", "url": f"{site}/landing.html"})
    assert scan.jobs == [] and scan.status == "needs_config"
    assert "job search page" in scan.reason


def test_unreachable_listing_is_failing():
    scan = scraper._scan_playwright({"id": "u", "name": "Down", "url": "http://127.0.0.1:9/careers"})
    assert scan.jobs == [] and scan.status in {"failing", "broken"}
