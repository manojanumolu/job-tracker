from __future__ import annotations

import re
import time
import logging
from datetime import datetime, timezone
from html import unescape
from urllib.parse import urldefrag, urlparse, urljoin

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


def _is_india(text: str) -> bool:
    return bool(_INDIA_RE.search(text or ""))


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


def _display_location(text: str, limit: int = 3) -> str:
    """"Hyderabad | Pune | India" -> "Hyderabad · Pune · India" (deduplicated,
    at most ``limit`` places) for the alert email / UI."""
    parts: list[str] = []
    for part in re.split(r"\s*(?:\||\n)\s*", text or ""):
        part = part.strip(" ,")
        if part and part.lower() not in (p.lower() for p in parts):
            parts.append(part)
    shown = " · ".join(parts[:limit])
    return shown + (f" +{len(parts) - limit} more" if len(parts) > limit else "")


def _card_location(card_rest: str) -> str:
    """The line of a job card that names the India location, if any."""
    for line in (card_rest or "").splitlines():
        if _is_india(line) and len(line) <= 120:
            return line.strip()
    return ""


def _job(title: str, url: str, company: str, result: Classification, location: str = "") -> dict:
    return {
        "title": title,
        "url": url,
        "company": company,
        "location": _display_location(location),
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
        info = r.json().get("jobPostingInfo") or {}
    except Exception as e:
        log.warning("Workday detail fetch failed for %s: %s", url, e)
        return None
    if not isinstance(info, dict) or not info:
        log.warning("Workday detail for %s has no jobPostingInfo", url)
        return None
    locations = [info.get("location")] + list(info.get("additionalLocations") or [])
    country = info.get("country")
    if isinstance(country, dict):
        country = country.get("descriptor")
    location_text = " | ".join(str(x) for x in locations + [country] if x)
    return _html_to_text(str(info.get("jobDescription") or "")), location_text


def _workday_posting(client: httpx.Client, p: dict, base: str, cxs_base: str, name: str) -> dict | None:
    title = p.get("title") or ""
    location = p.get("locationsText") or ""
    ext = p.get("externalPath") or ""
    pre = classify_job(title, url=ext)
    if pre.category in _HARD_REJECT:
        _log_decision(name, title, pre)
        return None
    # a concrete non-India location in the list view is final;
    # "N Locations" needs the detail page to know
    if location and not _WORKDAY_MULTI_LOC_RE.match(location) and not _is_india(location):
        return None
    detail = _workday_detail(client, cxs_base + ext) if ext else None
    description, location_text = detail or ("", "")
    # detail without location data: fall back to the list view (and, as
    # before, the title when the list view has no location either)
    location_text = location_text or location or title
    if not _is_india(location_text):
        return None
    result = classify_job(title, description, url=ext)
    _log_decision(name, title, result)
    shown_location = location_text if _is_india(location_text) and location_text != title else location
    return _job(title, base + ext, name, result, shown_location) if result.accepted else None


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
                # one malformed posting must not abort the rest of the company
                try:
                    job = _workday_posting(client, p, base, cxs_base, name)
                except Exception as e:
                    log.warning("Skipping malformed Workday posting for %s: %s", name, e)
                    continue
                if job:
                    jobs.append(job)
            return jobs
    return []


# Pulls schema.org JobPosting data (description + location) out of a detail
# page's JSON-LD — most ATSs emit it for Google Jobs, JS-rendered or not.
# Malformed blocks are skipped.
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

# Fallback description containers when a page has no JSON-LD. Only
# job-description-specific ones: generic <main>/<article> wrappers also hold
# navigation, cookie banners and "similar jobs" widgets whose text would
# wrongly accept or reject the posting.
_DESCRIPTION_SELECTORS = [
    '[data-automation-id="jobPostingDescription"]',
    '[itemprop="description"]',
    '[class*="job-description" i]',
    '[class*="jobdescription" i]',
    '[id*="job-description" i]',
    '[id*="jobdescription" i]',
]
_MIN_DESCRIPTION_CHARS = 200

# schema.org addressCountry is often an ISO code
_INDIA_COUNTRY_CODES = {"IN", "IND"}

# Detail pages only need their DOM text; skipping heavy assets keeps the
# per-page cost down.
_BLOCKED_RESOURCES = {"image", "media", "font"}


def _jsonld_location(posting: dict) -> str:
    locs = posting.get("jobLocation") or []
    if isinstance(locs, dict):
        locs = [locs]
    parts: list[str] = []
    for loc in locs if isinstance(locs, list) else []:
        addr = loc.get("address") if isinstance(loc, dict) else None
        if isinstance(addr, str):
            parts.append(addr)
            continue
        if not isinstance(addr, dict):
            continue
        for key in ("addressLocality", "addressRegion", "addressCountry"):
            val = addr.get(key)
            if isinstance(val, dict):
                val = val.get("name", "")
            if not val:
                continue
            val = str(val)
            parts.append("India" if val.strip().upper() in _INDIA_COUNTRY_CODES else val)
    # remote postings: "applicantLocationRequirements": {"@type": "Country", "name": "India"}
    reqs = posting.get("applicantLocationRequirements") or []
    for req in [reqs] if isinstance(reqs, dict) else reqs if isinstance(reqs, list) else []:
        name = req.get("name") if isinstance(req, dict) else None
        if name:
            name = str(name)
            parts.append("India" if name.strip().upper() in _INDIA_COUNTRY_CODES else name)
    return " | ".join(parts)


def _pick_posting(postings: list, title: str) -> dict | None:
    """Pages sometimes embed several JobPostings (e.g. related jobs). Use the
    one whose title matches the card; with several and no match we can't tell
    which is ours, so use none rather than risk another job's description."""
    postings = [x for x in postings if isinstance(x, dict)]
    want = title.strip().lower()
    for posting in postings:
        if str(posting.get("title", "")).strip().lower() == want:
            return posting
    return postings[0] if len(postings) == 1 else None


def _same_page(a: str, b: str) -> bool:
    return urldefrag(a)[0].rstrip("/") == urldefrag(b)[0].rstrip("/")


def _playwright_detail(page, url: str, title: str, listing_url: str) -> tuple[str, str] | None:
    """Open a job detail page; return (description, location). ``location``
    is "" when the page has no structured location data. Returns None when
    nothing trustworthy could be read (the caller then relies on the card)."""
    try:
        page.goto(url, timeout=20000, wait_until="domcontentloaded")
        # redirected back to the listing (expired posting, login wall)
        if _same_page(page.url, listing_url):
            return None
        postings = page.evaluate(_JSONLD_JS) or []
        if not postings:
            # JS-rendered ATS — give it a moment to inject content
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            postings = page.evaluate(_JSONLD_JS) or []
        posting = _pick_posting(postings, title)
        if posting:
            description = _html_to_text(str(posting.get("description") or ""))
            extra = posting.get("experienceRequirements")
            if isinstance(extra, str):
                description += "\n" + extra
            elif isinstance(extra, dict):
                months = extra.get("monthsOfExperience")
                if months is not None:
                    description += f"\nExperience: {months} months"
            return description, _jsonld_location(posting)
        for selector in _DESCRIPTION_SELECTORS:
            # e.g. a short "job-description-header" can precede the real block
            for el in page.query_selector_all(selector):
                text = (el.inner_text() or "").strip()
                if len(text) >= _MIN_DESCRIPTION_CHARS:
                    return text, ""
        return None
    except Exception as e:
        log.warning("Detail page failed for %s: %s", url, e)
        return None


def _collect_candidates(page, listing_url: str, name: str) -> list[tuple[str, str, str, Classification]]:
    """Job-card links on the listing page that are in India and not already
    rejectable from the card alone, deduplicated by URL."""
    candidates = []
    seen: set[str] = set()
    for link in page.query_selector_all("a"):
        try:
            raw = (link.inner_text() or "").strip()
            href = link.get_attribute("href") or ""
        except Exception as e:  # element detached by a re-render
            log.debug("Skipping unreadable link on %s: %s", listing_url, e)
            continue
        # card-style links wrap a heading + description (and often the
        # location) in one <a>; the first line is the display title,
        # the rest of the card is extra evidence for the classifier
        title, _, card_rest = raw.partition("\n")
        title = title.strip()
        # India must come from the job's own card, never from the
        # surrounding career page (nav links, country pickers)
        if not title or not is_real_job(title, href) or not _is_india(raw):
            continue
        # urljoin resolves every relative form correctly (root-relative,
        # page-relative, absolute) — the previous manual check fell back
        # to the generic career-page URL for plain "job/123"-style relative
        # hrefs, which is why "Apply" sometimes opened the homepage instead
        # of the specific job posting.
        href = urljoin(listing_url, href) if href else listing_url
        # links without their own page (href="#…", or none) are keyed by title
        key = f"title:{title}" if _same_page(href, listing_url) else urldefrag(href)[0]
        if key in seen:
            continue
        seen.add(key)
        pre = classify_job(title, card_rest, url=href)
        if pre.category in _HARD_REJECT:
            _log_decision(name, title, pre)
            continue
        candidates.append((title, card_rest, href, pre))
    return candidates


def _scrape_playwright(company: dict) -> list[dict]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.warning("Playwright not installed — skipping JS scrape for %s", company["name"])
        return []

    name = company["name"]
    listing_url = company["url"]
    jobs = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(extra_http_headers={"User-Agent": HEADERS["User-Agent"]})
            page.goto(listing_url, timeout=30000)
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass  # page may keep background network activity forever; DOM is usable regardless
            candidates = _collect_candidates(page, listing_url, name)
            # verify the would-be alerts first, so the detail budget is never
            # spent on UNKNOWN cards while a card-accepted job goes unchecked
            candidates.sort(key=lambda c: not c[3].accepted)

            detail_page = browser.new_page(extra_http_headers={"User-Agent": HEADERS["User-Agent"]})
            detail_page.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in _BLOCKED_RESOURCES
                else route.continue_(),
            )
            fetched = 0
            for title, card_rest, href, _pre in candidates:
                detail = None
                if href.startswith("http") and not _same_page(href, listing_url):
                    if fetched < MAX_DETAIL_FETCHES:
                        fetched += 1
                        detail = _playwright_detail(detail_page, href, title, listing_url)
                    elif fetched == MAX_DETAIL_FETCHES:
                        fetched += 1  # warn once
                        log.warning("%s: detail-page budget (%d) reached; remaining cards judged on card text",
                                    name, MAX_DETAIL_FETCHES)
                description = card_rest
                location = _card_location(card_rest)
                if detail:
                    detail_text, detail_location = detail
                    if detail_location and not _is_india(detail_location):
                        log.info("  %s: %s — skipped, detail location %r", name, title, detail_location)
                        continue
                    description = f"{card_rest}\n{detail_text}"
                    location = detail_location or location
                result = classify_job(title, description, url=href)
                _log_decision(name, title, result)
                if result.accepted:
                    jobs.append(_job(title, href, name, result, location))
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
