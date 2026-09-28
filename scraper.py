import json
import re
import time
import logging
from datetime import datetime, timezone
from html import unescape
from urllib.parse import urlparse, urljoin

import httpx

from job_classifier import Category, Classification, classify_job, is_real_job

logging.basicConfig(level=logging.INFO, format="[scraper] %(message)s")
log = logging.getLogger(__name__)

# Fresher/entry-level classification lives in job_classifier.py (pure text
# logic, unit tested in tests/). The old keyword list treated words such as
# "associate", "junior" and "internship" as proof of a fresher role and missed
# most experience requirements ("2-4 years", "minimum 2 yrs", ...).
MAX_DETAIL_FETCHES = 30  # per company, bounds run time on pages with many cards

INDIA_KEYWORDS = [
    "india", "bengaluru", "bangalore", "hyderabad", "pune", "chennai",
    "mumbai", "gurgaon", "gurugram", "noida", "delhi", "kolkata",
    "ahmedabad", "kochi", "coimbatore", "indore", "navi mumbai",
    "thiruvananthapuram", "trivandrum", "mysuru", "mysore", "jaipur",
    "chandigarh", "mohali", "vadodara", "nagpur", "visakhapatnam",
    "bhubaneswar", "gandhinagar",
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
    """Backward-compatible boolean wrapper around classify_job."""
    title, _, rest = (text or "").strip().partition("\n")
    return classify_job(title, rest).accepted


def _is_india(text: str) -> bool:
    return bool(_INDIA_RE.search(text or ""))


def _is_real_job(title: str, href: str) -> bool:
    return is_real_job(title, href)


_BLOCK_TAG_RE = re.compile(r"<\s*(?:br|/p|/li|/div|/h[1-6]|/tr|li)\b[^>]*>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


def _html_to_text(html: str) -> str:
    """Strip tags but keep block boundaries as newlines, so the classifier can
    tell bullet points / sentences apart."""
    text = _BLOCK_TAG_RE.sub("\n", html or "")
    text = unescape(_TAG_RE.sub(" ", text))
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def _log_decision(company: str, title: str, result: Classification) -> None:
    log.info("  %s: %s — %s", company, title, result)


def _job(title: str, url: str, company: str, result: Classification) -> dict:
    return {
        "title": title,
        "url": url,
        "company": company,
        "category": result.category.value,
        "reason": result.reason,
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Classifications that are final from the title/card alone — no point
# fetching a detail page for them.
_HARD_REJECT = {Category.NOT_A_JOB, Category.SENIOR, Category.EXPERIENCED}

# Workday's list API reports multi-location postings as "3 Locations"
_WORKDAY_MULTI_LOC_RE = re.compile(r"^\s*\d+\s+locations?\s*$", re.IGNORECASE)


def _workday_detail(client: httpx.Client, url: str) -> tuple[str, str] | None:
    """Fetch a Workday posting's description and its full location list."""
    try:
        r = client.get(url)
        r.raise_for_status()
        info = r.json().get("jobPostingInfo", {})
    except Exception as e:
        log.warning("Workday detail fetch failed for %s: %s", url, e)
        return None
    locations = [info.get("location") or ""] + list(info.get("additionalLocations") or [])
    country = (info.get("country") or {}).get("descriptor", "")
    location_text = " | ".join(x for x in locations + [country] if x)
    return _html_to_text(info.get("jobDescription", "")), location_text


def _scrape_api(company: dict) -> list[dict]:
    cid = company["id"]
    cfg = COMPANY_API.get(cid)
    if not cfg:
        return []
    url = cfg["url"]
    name = company["name"]
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
            # detail API: https://{host}/wday/cxs/{tenant}/{site}{externalPath}
            cxs_base = url.rstrip("/").rsplit("/", 1)[0]
            jobs = []
            for p in postings:
                title = p.get("title", "")
                location = p.get("locationsText", "")
                ext = p.get("externalPath", "")
                pre = classify_job(title, url=ext)
                if pre.category in _HARD_REJECT:
                    _log_decision(name, title, pre)
                    continue
                # a concrete non-India location in the list view is final;
                # "N Locations" needs the detail page to know
                if location and not _WORKDAY_MULTI_LOC_RE.match(location) and not _is_india(location):
                    continue
                detail = _workday_detail(client, cxs_base + ext) if ext else None
                if detail:
                    description, location_text = detail
                else:
                    description, location_text = "", location or title
                if not _is_india(location_text):
                    continue
                result = classify_job(title, description, url=ext)
                _log_decision(name, title, result)
                if result.accepted:
                    jobs.append(_job(title, base + ext, name, result))
            return jobs
    return []


# Pulls schema.org JobPosting data (description + location) out of a detail
# page's JSON-LD — most ATSs emit it for Google Jobs, JS-rendered or not.
_JSONLD_JS = """
() => {
  const out = [];
  const visit = (o) => {
    if (!o || typeof o !== 'object') return;
    if (Array.isArray(o)) { o.forEach(visit); return; }
    const t = o['@type'];
    if (t === 'JobPosting' || (Array.isArray(t) && t.includes('JobPosting'))) out.push(o);
    if (o['@graph']) visit(o['@graph']);
  };
  document.querySelectorAll('script[type="application/ld+json"]').forEach(s => {
    try { visit(JSON.parse(s.textContent)); } catch (e) {}
  });
  return out;
}
"""

# Fallback containers for the description when a page has no JSON-LD.
_DESCRIPTION_SELECTORS = [
    '[data-automation-id="jobPostingDescription"]',
    '[itemprop="description"]',
    '[class*="job-description" i]',
    '[class*="jobdescription" i]',
    '[class*="job-details" i]',
    '[class*="description" i]',
    "article",
    "main",
]


def _jsonld_location(posting: dict) -> str:
    locs = posting.get("jobLocation") or []
    if isinstance(locs, dict):
        locs = [locs]
    parts: list[str] = []
    for loc in locs:
        addr = (loc or {}).get("address") or {}
        if isinstance(addr, str):
            parts.append(addr)
            continue
        for key in ("addressLocality", "addressRegion", "addressCountry"):
            val = addr.get(key)
            if isinstance(val, dict):
                val = val.get("name", "")
            if val:
                parts.append(str(val))
    return " | ".join(parts)


def _playwright_detail(page, url: str) -> tuple[str, str] | None:
    """Open a job detail page; return (description, location). ``location``
    is "" when the page has no structured location data."""
    try:
        page.goto(url, timeout=20000)
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        postings = page.evaluate(_JSONLD_JS) or []
        if postings:
            posting = postings[0]
            description = _html_to_text(str(posting.get("description", "")))
            extra = posting.get("experienceRequirements")
            if isinstance(extra, str):
                description += "\n" + extra
            elif isinstance(extra, dict):
                months = extra.get("monthsOfExperience")
                if months is not None:
                    description += f"\nExperience: {months} months"
            return description, _jsonld_location(posting)
        for selector in _DESCRIPTION_SELECTORS:
            el = page.query_selector(selector)
            if el:
                text = (el.inner_text() or "").strip()
                if len(text) > 200:
                    return text, ""
        return None
    except Exception as e:
        log.warning("Detail page failed for %s: %s", url, e)
        return None


def _scrape_playwright(company: dict) -> list[dict]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.warning("Playwright not installed — skipping JS scrape for %s", company["name"])
        return []

    name = company["name"]
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
            candidates: list[tuple[str, str, str]] = []
            seen: set[tuple[str, str]] = set()
            for link in page.query_selector_all("a"):
                raw = (link.inner_text() or "").strip()
                # card-style links wrap a heading + description (and often the
                # location) in one <a>; the first line is the display title,
                # the rest of the card is extra evidence for the classifier
                title, _, card_rest = raw.partition("\n")
                title = title.strip()
                href = link.get_attribute("href") or ""
                # India must come from the job's own card, never from the
                # surrounding career page (nav links, country pickers)
                if not title or not is_real_job(title, href) or not _is_india(raw):
                    continue
                # urljoin resolves every relative form correctly (root-relative,
                # page-relative, absolute) — the previous manual check fell back
                # to the generic career-page URL for plain "job/123"-style relative
                # hrefs, which is why "Apply" sometimes opened the homepage instead
                # of the specific job posting.
                href = urljoin(company["url"], href) if href else company["url"]
                if (title, href) in seen:
                    continue
                seen.add((title, href))
                pre = classify_job(title, card_rest, url=href)
                if pre.category in _HARD_REJECT:
                    _log_decision(name, title, pre)
                    continue
                candidates.append((title, card_rest, href))

            detail_page = browser.new_page(extra_http_headers={"User-Agent": HEADERS["User-Agent"]})
            for i, (title, card_rest, href) in enumerate(candidates):
                detail = None
                fetchable = href.startswith("http") and href.rstrip("/") != company["url"].rstrip("/")
                if fetchable and i < MAX_DETAIL_FETCHES:
                    detail = _playwright_detail(detail_page, href)
                description = card_rest
                if detail:
                    detail_text, detail_location = detail
                    if detail_location and not _is_india(detail_location):
                        log.info("  %s: %s — skipped, detail location %r", name, title, detail_location)
                        continue
                    description = f"{card_rest}\n{detail_text}"
                result = classify_job(title, description, url=href)
                _log_decision(name, title, result)
                if result.accepted:
                    jobs.append(_job(title, href, name, result))
        except Exception as e:
            log.warning("Playwright error for %s: %s", name, e)
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
        # reflect what this scrape actually sees — only updating when jobs
        # were found left titles from removed postings (and pre-filter junk)
        # stuck in the UI forever
        company["last_job"] = jobs[0]["title"] if jobs else ""
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
