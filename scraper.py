import json
import re
import time
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse, urljoin

import httpx

logging.basicConfig(level=logging.INFO, format="[scraper] %(message)s")
log = logging.getLogger(__name__)

FRESHER_KEYWORDS = [
    "graduate", "entry level", "entry-level", "associate", "0-1 year",
    "0-2 year", "new grad", "trainee", "fresher", "junior", "internship",
    "campus", "early career", "recent graduate",
]

# word-boundary match — plain substring matching let "intern" match inside
# "international"/"internal", flooding results with unrelated nav links
_FRESHER_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(k) for k in FRESHER_KEYWORDS) + r")\b",
    re.IGNORECASE,
)

# "Associate" alone also matches senior titles like "Associate Director" —
# reject anything carrying a seniority/experience signal even if a fresher
# keyword matched too.
SENIOR_EXCLUDE_KEYWORDS = [
    "director", "senior", "sr.", "sr ", "staff", "principal", "lead",
    "manager", "head of", "vice president", "vp,", "vp ", "chief",
    "president", "executive", "years of experience", "years experience",
    "3+ year", "5+ year", "7+ year", "10+ year",
]
_SENIOR_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(k) for k in SENIOR_EXCLUDE_KEYWORDS) + r")\b",
    re.IGNORECASE,
)

# Career pages mix real postings with employee-spotlight/blog content
# ("Meet Nils Libert, Associate Scientist in R&D") that happens to contain
# fresher keywords but isn't a job listing at all.
_NOT_A_JOB_RE = re.compile(
    r"^\s*meet\b|^\s*[\w'’.-]+\s+[\w'’.-]+\s*:\s", re.IGNORECASE,
)
_NOT_A_JOB_URL_RE = re.compile(r"/(blog|news|stories|insights|article)s?/", re.IGNORECASE)

INDIA_KEYWORDS = [
    "india", "bengaluru", "bangalore", "hyderabad", "pune", "chennai",
    "mumbai", "gurgaon", "gurugram", "noida", "delhi", "kolkata",
    "ahmedabad", "kochi", "coimbatore", "indore", "navi mumbai",
]
_INDIA_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(k) for k in INDIA_KEYWORDS) + r")\b",
    re.IGNORECASE,
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; FresherJobTracker/1.0)",
    "Accept": "application/json, text/html, */*",
}

# Known JSON/API endpoints for each company id
API_ENDPOINTS: dict[str, str] = {
    "workday": "https://{domain}/wday/cxs/{tenant}/jobs",
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{tenant}/jobs",
    "lever": "https://api.lever.co/v0/postings/{tenant}?mode=json",
}

# Per-company API config (id -> dict)
# NOTE: Avalara runs on a custom ATS (not Workday) and Accenture has no public
# careers API — both previously pointed at guessed endpoints that returned
# 401/404 on every run. They now fall back to the Playwright scraper below.
COMPANY_API: dict[str, dict] = {
    "sanofi": {
        "type": "workday",
        "url": "https://sanofi.wd3.myworkdayjobs.com/wday/cxs/sanofi/SanofiCareers/jobs",
    },
}


def _is_fresher(text: str) -> bool:
    return bool(_FRESHER_RE.search(text)) and not _SENIOR_RE.search(text)


def _is_india(text: str) -> bool:
    return bool(_INDIA_RE.search(text))


def _is_real_job(title: str, href: str) -> bool:
    if _NOT_A_JOB_RE.search(title):
        return False
    if href and _NOT_A_JOB_URL_RE.search(href):
        return False
    return True


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _scrape_api(company: dict) -> list[dict]:
    cid = company["id"]
    cfg = COMPANY_API.get(cid)
    if not cfg:
        return []
    url = cfg["url"]
    with httpx.Client(headers=HEADERS, timeout=20, follow_redirects=True) as client:
        if cfg.get("type") == "workday":
            payload = {"limit": 20, "offset": 0, "searchText": "", "locations": []}
            r = client.post(url, json=payload)
            r.raise_for_status()
            data = r.json()
            postings = data.get("jobPostings", [])
            # https://{host}/wday/cxs/{tenant}/{site}/jobs -> https://{host}/{site}
            parsed = urlparse(url)
            site = parsed.path.rstrip("/").split("/")[-2]
            base = f"{parsed.scheme}://{parsed.netloc}/{site}"
            jobs = []
            for p in postings:
                title = p.get("title", "")
                location = p.get("locationsText", "")
                ext = p.get("externalPath", "")
                if not _is_real_job(title, ext):
                    continue
                if not _is_fresher(title + " " + location):
                    continue
                if not _is_india(location or title):
                    continue
                jobs.append({"title": title, "url": base + ext, "company": company["name"]})
            return jobs
    return []


def _scrape_playwright(company: dict) -> list[dict]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.warning("Playwright not installed — skipping JS scrape for %s", company["name"])
        return []

    jobs = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(extra_http_headers={"User-Agent": HEADERS["User-Agent"]})
        try:
            page.goto(company["url"], timeout=30000)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass  # page may keep background network activity forever; DOM is usable regardless
            text = page.inner_text("body")
            links = page.query_selector_all("a")
            for link in links:
                raw = (link.inner_text() or "").strip()
                # card-style links wrap a heading + description (and often the
                # location) in one <a>; the first line is the display title,
                # but fresher/seniority/location checks run over the whole card
                title = raw.splitlines()[0].strip() if raw else ""
                href = link.get_attribute("href") or ""
                if (
                    title
                    and _is_real_job(title, href)
                    and _is_fresher(raw)
                    and _is_india(raw)
                ):
                    # urljoin resolves every relative form correctly (root-relative,
                    # page-relative, absolute) — the previous manual check fell back
                    # to the generic career-page URL for plain "job/123"-style relative
                    # hrefs, which is why "Apply" sometimes opened the homepage instead
                    # of the specific job posting.
                    href = urljoin(company["url"], href) if href else company["url"]
                    jobs.append({"title": title, "url": href, "company": company["name"]})
        except Exception as e:
            log.warning("Playwright error for %s: %s", company["name"], e)
        finally:
            browser.close()
    return jobs


def scrape_company(company: dict) -> tuple[list[dict], str]:
    """Returns (jobs, status) where status is 'active' or 'broken'."""
    name = company["name"]
    try:
        jobs = _scrape_api(company)
        if not jobs:
            jobs = _scrape_playwright(company)
        log.info("%s → %d fresher job(s) found", name, len(jobs))
        return jobs, "active"
    except Exception as e:
        log.error("FAILED %s: %s", name, e)
        return [], "broken"


def run_all() -> list[dict]:
    from config_store import load_companies, save_companies, load_seen_jobs, save_seen_jobs

    companies = load_companies()
    seen = load_seen_jobs()
    seen_keys = {(j["company"], j["title"]) for j in seen}

    new_jobs: list[dict] = []

    for company in companies:
        jobs, status = scrape_company(company)
        company["status"] = status
        company["last_checked"] = _now_iso()
        if jobs:
            company["last_job"] = jobs[0]["title"]
        for job in jobs:
            key = (job["company"], job["title"])
            if key not in seen_keys:
                now = datetime.now(timezone.utc)
                job["date"] = f"{now:%b} {now.day}, {now:%H:%M}"
                job["id"] = f"{job['company']}_{job['title'][:40]}".replace(" ", "_")
                new_jobs.append(job)
                seen_keys.add(key)
        time.sleep(1)

    # commit=False: the GitHub Actions workflow itself does one git commit+push
    # at the end of the run. Letting this also push via the API caused a second,
    # independent commit on every run — the workflow's later `git push` would
    # then be rejected as non-fast-forward (remote had already moved), failing
    # the job even though the scrape itself succeeded.
    save_companies(companies, commit=False)
    if new_jobs:
        save_seen_jobs(seen + new_jobs, commit=False)
    return new_jobs


if __name__ == "__main__":
    new = run_all()
    print(f"[scraper] {len(new)} new job(s) found")
    if new:
        for j in new:
            print(f"  - {j['title']} @ {j['company']}")
