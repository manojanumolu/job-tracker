from __future__ import annotations

import random
import re
import time
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urldefrag, urljoin

import httpx

from identity import ats_job_id, job_uid, same_page
from job_classifier import (
    Category, Classification, classify_job, has_explicit_zero_experience, normalize_title, not_a_job_reason,
)
from locations import india_segments, is_india
from sources import (
    HEADERS, Detail, Listing, SourceError, html_to_text, read_source, workday_api_from_url, workday_detail,
    workday_listings,
)

logging.basicConfig(level=logging.INFO, format="[scraper] %(message)s")
log = logging.getLogger("scraper")

# Fresher/entry-level classification lives in job_classifier.py (pure text
# logic, unit tested in tests/).
#
# The core safety rule of this module: a job is only ever alerted when its
# own posting was read. If the detail page/record can't be read (HTTP error,
# rate limit, bot challenge, redirect to another page, a different job's
# data, empty description, detail budget exhausted) the job is UNKNOWN — not
# emailed, not saved, retried on the next run. The only card-level evidence
# strong enough on its own is an explicit 0-year experience range.
MAX_DETAIL_FETCHES = 30        # browser detail pages per company (slow)
MAX_API_DETAIL_FETCHES = 400   # HTTP detail requests per company (PwC India needs ~200)
_FAILING_DETAIL_RATIO = 0.5    # more unreadable job pages than this -> "failing"

# ---------------------------------------------------------------------------
# Company -> real job source. Every endpoint below was verified against the
# live site; companies without an entry use their configured URL (Workday
# URLs are recognised automatically, anything else is read with Playwright).
# ---------------------------------------------------------------------------
COMPANY_SOURCES: dict[str, list[dict]] = {
    "sanofi": [{"type": "workday", "api": "https://sanofi.wd3.myworkdayjobs.com/wday/cxs/sanofi/SanofiCareers/jobs"}],
    "pwc": [
        {"type": "workday", "api": "https://pwc.wd3.myworkdayjobs.com/wday/cxs/pwc/Global_Experienced_Careers/jobs"},
        {"type": "workday", "api": "https://pwc.wd3.myworkdayjobs.com/wday/cxs/pwc/Global_Campus_Careers/jobs"},
    ],
    # PokerStars is a Flutter International brand; its jobs live on Flutter's
    # Workday site (named in the careers site's own JobPosting data)
    "pokerstars": [{"type": "workday", "api": "https://flutterbe.wd3.myworkdayjobs.com/wday/cxs/flutterbe/FlutterInt_External/jobs"}],
    "accenture": [{"type": "accenture", "site": "in-en", "country": "India"}],
    "avalara": [{"type": "jibe", "host": "https://careers.avalara.com"}],
    "metlife": [{"type": "avature", "search": "https://www.metlifecareers.com/en_US/ml/SearchJobs"}],
    # NPCI (added from the app as "npcl")
    "c1783687965": [{"type": "zoho", "page": "https://careers.npci.org.in/jobs/Careers"}],
}
# kept for callers that only need "which companies use an API"
COMPANY_API = {cid: {"type": cfgs[0]["type"], **cfgs[0]} for cid, cfgs in COMPANY_SOURCES.items()}

_SOURCE_LABELS = {"workday": "Workday API", "accenture": "Accenture job-search API", "jibe": "Jibe/iCIMS API",
                  "avature": "Avature job search", "zoho": "Zoho Recruit career site"}


def sources_for(company: dict) -> list[dict]:
    if isinstance(company.get("sources"), list) and company["sources"]:
        return company["sources"]
    if company.get("id") in COMPANY_SOURCES:
        return COMPANY_SOURCES[company["id"]]
    api = workday_api_from_url(company.get("url", ""))
    return [{"type": "workday", "api": api}] if api else []


def source_label(company: dict) -> str:
    srcs = sources_for(company)
    if not srcs:
        return "Playwright (headless browser)"
    return " + ".join(dict.fromkeys(_SOURCE_LABELS.get(s.get("type"), s.get("type", "API")) for s in srcs))


def _is_india(text: str) -> bool:
    return is_india(text)


def _html_to_text(html: str) -> str:
    return html_to_text(html)


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


def _unknown(reason: str) -> Classification:
    return Classification(Category.UNKNOWN, reason)


# ---------------------------------------------------------------------------
# Per-company scan bookkeeping and health
# ---------------------------------------------------------------------------

@dataclass
class Scan:
    name: str
    jobs: list[dict] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)   # (status, message)
    sources_ok: int = 0
    seen: int = 0
    candidates: int = 0
    details_ok: int = 0
    details_failed: int = 0
    last_detail_error: str = ""
    pending: int = 0
    rejected: int = 0
    unknown: int = 0
    notes: list[str] = field(default_factory=list)

    def fail(self, status: str, message: str) -> None:
        log.warning("%s: %s (%s)", self.name, message, status)
        self.errors.append((status, message))

    def record_detail(self, detail: Detail) -> None:
        if detail.ok:
            self.details_ok += 1
        else:
            self.details_failed += 1
            self.last_detail_error = detail.reason

    @property
    def status(self) -> str:
        if self.errors and not self.sources_ok:
            order = ["needs_config", "broken", "failing"]
            return min((s for s, _ in self.errors), key=lambda s: order.index(s) if s in order else 9)
        if self.errors:
            return "failing"
        attempted = self.details_ok + self.details_failed
        if attempted >= 3 and self.details_failed / attempted > _FAILING_DETAIL_RATIO:
            return "failing"
        return "active"

    @property
    def reason(self) -> str:
        if self.errors:
            return "; ".join(m for _, m in self.errors)
        attempted = self.details_ok + self.details_failed
        if attempted >= 3 and self.details_failed / attempted > _FAILING_DETAIL_RATIO:
            return f"{self.details_failed} of {attempted} job pages could not be read (last: {self.last_detail_error})"
        return "; ".join(self.notes)

    def summary(self) -> dict:
        return {"postings_seen": self.seen, "candidates": self.candidates, "details_read": self.details_ok,
                "details_unreadable": self.details_failed, "pending": self.pending, "accepted": len(self.jobs)}


# ---------------------------------------------------------------------------
# The decision: listing + (maybe) detail -> classification
# ---------------------------------------------------------------------------

def decide(company: str, listing: Listing, detail: Detail | None) -> tuple[Classification, dict | None]:
    """Final verdict for one posting. Never accepts without the posting's own
    detail, unless the card itself states a 0-year experience range."""
    title, card = listing.title, listing.card_text
    if detail is None or not detail.ok:
        why = detail.reason if detail else "no job page"
        if has_explicit_zero_experience(card) and is_india(listing.location):
            result = classify_job(title, card, url=listing.url, posting_evidence=listing.posting_evidence)
            if result.accepted:
                return result, _job(title, listing.url, company, result, listing.location)
            return result, None
        return _unknown(f"job page unreadable ({why}) — not alerted, retried next run"), None

    if detail.location and not is_india(detail.location):
        return _unknown(f"not in India (posting location: {detail.location[:80]})"), None
    location = detail.location if is_india(detail.location) else listing.location
    if not is_india(location):
        return _unknown("no India location evidence — not alerted"), None
    description = f"{card}\n{detail.description}".strip()
    result = classify_job(title, description, url=listing.url, strict_text=detail.strict_text,
                          posting_evidence=detail.posting_evidence or listing.posting_evidence)
    if not result.accepted:
        return result, None
    shown = " | ".join(india_segments(location)) or location
    return result, _job(title, listing.url, company, result, shown if is_india(detail.location) else location)


def _evaluate(scan: Scan, listing: Listing, budget: list[int]) -> None:
    scan.candidates += 1
    # judged as a posting here only to avoid dropping programme titles before
    # their JobPosting evidence has been read; the final verdict uses the real evidence
    pre = classify_job(listing.title, listing.card_text, url=listing.url, posting_evidence=True)
    if pre.category in _HARD_REJECT:
        scan.rejected += 1
        _log_decision(scan.name, listing.title, pre)
        return
    detail = listing.detail
    if detail is None and listing.fetch_detail is not None:
        if budget[0] > 0:
            budget[0] -= 1
            try:
                detail = listing.fetch_detail()
            except Exception as e:  # one broken posting never aborts the company
                detail = Detail.unreadable(f"{type(e).__name__}: {e}")
            scan.record_detail(detail)
        else:
            detail = Detail.unreadable("detail budget reached this run")
            scan.pending += 1
    result, job = decide(scan.name, listing, detail)
    _log_decision(scan.name, listing.title, result)
    if job:
        scan.jobs.append(job)
    elif result.category == Category.UNKNOWN:
        scan.unknown += 1
    else:
        scan.rejected += 1


# ---------------------------------------------------------------------------
# HTTP sources (Workday, Accenture, Jibe, Avature, Zoho)
# ---------------------------------------------------------------------------

def _scan_sources(company: dict, sources: list[dict]) -> Scan:
    scan = Scan(company["name"])
    budget = [MAX_API_DETAIL_FETCHES]
    with httpx.Client(headers=HEADERS, timeout=25, follow_redirects=True) as client:
        listings: list[Listing] = []
        for cfg in sources:
            try:
                result = read_source(client, cfg)
            except SourceError as e:
                scan.fail(e.status, str(e))
                continue
            except Exception as e:
                scan.fail("failing", f"{type(e).__name__}: {e}")
                continue
            scan.sources_ok += 1
            scan.seen += result.seen
            if result.note:
                scan.notes.append(result.note)
            listings += result.listings
        # more detail pages than the budget: vary the order between runs so
        # the same postings are never the ones left pending every time
        if sum(1 for x in listings if x.detail is None and x.fetch_detail) > budget[0]:
            random.shuffle(listings)
        for listing in listings:
            try:
                _evaluate(scan, listing, budget)
            except Exception as e:
                log.warning("Skipping malformed posting for %s: %s", scan.name, e)
    if scan.pending:
        scan.notes.append(f"{scan.pending} posting(s) left for the next run (detail budget)")
    return scan


def _scrape_api(company: dict) -> list[dict]:
    """Jobs from a company's API sources ([] when it has none)."""
    sources = sources_for(company)
    return _scan_sources(company, sources).jobs if sources else []


# backwards-compatible helpers (tests / callers)
def _workday_detail(client: httpx.Client, url: str, title: str = "") -> Detail:
    return workday_detail(client, url, title)


# ---------------------------------------------------------------------------
# Generic browser path (companies without a known job source)
# ---------------------------------------------------------------------------

# Pulls schema.org JobPosting data out of a detail page's JSON-LD — most ATSs
# emit it for Google Jobs. "@type" may be "JobPosting", "schema:JobPosting"
# or "http://schema.org/JobPosting". Malformed blocks are skipped.
_JSONLD_JS = """
() => {
  const out = [];
  const isJob = (t) => (Array.isArray(t) ? t : [t]).some(
    x => typeof x === 'string' && x.split('/').pop().split(':').pop() === 'JobPosting');
  const visit = (o) => {
    if (!o || typeof o !== 'object') return;
    if (Array.isArray(o)) { o.forEach(visit); return; }
    if (isJob(o['@type'])) out.push(o);
    if (o['@graph']) visit(o['@graph']);
  };
  document.querySelectorAll('script[type="application/ld+json"]').forEach(s => {
    try { visit(JSON.parse(s.textContent)); } catch (e) {}
  });
  return out;
}
"""

# Visible experience / level fields outside the description ("Experience:
# 2-5 years", "Seniority level: Mid-Senior", "Senior Analyst | Full time |
# Experience: 2-5 years"). Skips links and similar/related-job widgets.
# Used as reject-only evidence.
_PAGE_FIELDS_JS = """
() => {
  const LABEL = /^\\s*(?:years?\\s+of\\s+experience|(?:work\\s+|total\\s+|required\\s+)?experience(?:\\s+(?:required|level|range))?|exp\\.?|seniority\\s+level|career\\s+level|management\\s+level|job\\s+level|experience\\s+level)\\s*[:|\\-]/i;
  const INLINE = /\\|\\s*experience\\s*:/i;
  const BAD = /similar|related|recommend|other-?jobs|more-?jobs|carousel|suggest|also|nearby/i;
  const out = [];
  for (const el of document.querySelectorAll('body *')) {
    if (el.children.length > 4 || ['SCRIPT', 'STYLE', 'NOSCRIPT'].includes(el.tagName)) continue;
    const t = (el.innerText || '').trim();
    if (!t || t.length > 200 || !(LABEL.test(t) || INLINE.test(t))) continue;
    let bad = false;
    for (let e = el; e && e !== document.body; e = e.parentElement) {
      const cls = typeof e.className === 'string' ? e.className : '';
      if (e.tagName === 'A' || BAD.test(cls + ' ' + (e.id || ''))) { bad = true; break; }
    }
    if (!bad && !out.includes(t)) out.push(t);
    if (out.length >= 20) break;
  }
  return out.join('\\n');
}
"""

# Job links on a listing page. The title comes from the link text, or — for
# cards whose <a> is an empty overlay (Accenture) — from the link's label or
# the card's heading; the card's own text is the extra evidence/location.
_CANDIDATES_JS = """
() => {
  const GENERIC = /^(apply|apply now|view|view job|details|view details|read more|learn more|save|share|more)$/i;
  const HEADING = 'h1,h2,h3,h4,[class*="title" i]';
  const out = [];
  for (const a of document.querySelectorAll('a')) {
    const href = a.getAttribute('href') || '';
    const text = (a.innerText || '').trim();
    // the job card: the nearest ancestor holding a heading, small enough to be one card
    let card = null;
    for (let e = a.parentElement, i = 0; e && e !== document.body && i < 8; e = e.parentElement, i++) {
      if ((e.innerText || '').length > 2000) break;
      if (e.querySelector(HEADING) && e.querySelectorAll('a[href]').length <= 6) { card = e; break; }
    }
    let title = text.split('\\n')[0].trim();
    if (!title || GENERIC.test(title)) {
      title = (a.getAttribute('aria-label') || a.getAttribute('title') || a.dataset.jobtitle || '').trim();
      if (!title && card) {
        const h = card.querySelector(HEADING);
        if (h) title = (h.innerText || '').trim().split('\\n')[0];
      }
    }
    const cardText = card && (card.innerText || '').length > text.length ? card.innerText : text;
    out.push({href, title, text, cardText: cardText || ''});
  }
  return out;
}
"""

# Fallback description containers when a page has no JSON-LD. Only
# job-description-specific ones: generic <main>/<article> wrappers also hold
# navigation, cookie banners and "similar jobs" widgets.
_DESCRIPTION_SELECTORS = [
    '[data-automation-id="jobPostingDescription"]',
    '[itemprop="description"]',
    '[class*="job-description" i]',
    '[class*="jobdescription" i]',
    '[id*="job-description" i]',
    '[id*="jobdescription" i]',
]
_MIN_DESCRIPTION_CHARS = 200

# schema.org addressCountry is often an ISO code (or a subdivision code "IN-KA")
_INDIA_COUNTRY_CODES = {"IN", "IND"}

# Detail pages only need their DOM text; skipping heavy assets keeps the
# per-page cost down.
_BLOCKED_RESOURCES = {"image", "media", "font"}

# URLs that look like an individual posting (anything else needs a real
# JobPosting on its page before it can be accepted)
_JOB_URL_RE = re.compile(
    r"/jobs?/|/job[-_]?details?|/jobdetail|/positions?/|/requisitions?/|/openings?/|/vacanc|/careers?/[^?#]*\d{3,}"
    r"|[?&](?:job|jobid|job_id|id|req|reqid|requisitionid|gh_jid|pid)=|myworkdayjobs\.com|greenhouse\.io"
    r"|lever\.co|smartrecruiters\.com|icims\.com|/jr\d+|_r\d{5,}",
    re.IGNORECASE,
)
_CHALLENGE_TITLE_RE = re.compile(r"just a moment|attention required|access denied|checking your browser|verify you are human|403 forbidden", re.IGNORECASE)


def _india_code(val: str) -> bool:
    val = val.strip().upper()
    return val in _INDIA_COUNTRY_CODES or bool(re.fullmatch(r"IN-[A-Z]{2,3}", val))


def _jsonld_location(posting: dict) -> str:
    locs = posting.get("jobLocation") or []
    if isinstance(locs, dict):
        locs = [locs]
    parts: list[str] = []
    for loc in locs if isinstance(locs, list) else []:
        if not isinstance(loc, dict):
            continue
        addr = loc.get("address")
        if isinstance(addr, str):
            parts.append(addr)
            continue
        if isinstance(addr, dict):
            for key in ("addressLocality", "addressRegion", "addressCountry"):
                val = addr.get(key)
                if isinstance(val, dict):
                    val = val.get("name", "")
                if not val:
                    continue
                val = str(val)
                parts.append("India" if _india_code(val) else val)
        elif loc.get("name"):
            # {"@type": "Place", "name": "Hyderabad"}
            parts.append(str(loc["name"]))
    # remote postings: "applicantLocationRequirements": {"@type": "Country", "name": "India"}
    reqs = posting.get("applicantLocationRequirements") or []
    for req in [reqs] if isinstance(reqs, dict) else reqs if isinstance(reqs, list) else []:
        name = req.get("name") if isinstance(req, dict) else None
        if name:
            name = str(name)
            parts.append("India" if _india_code(name) else name)
    return " | ".join(parts)


def _posting_id(posting: dict) -> str:
    ident = posting.get("identifier")
    if isinstance(ident, dict):
        ident = ident.get("value")
    return str(ident or "")


def _pick_posting(postings: list, title: str, job_id: str = "") -> dict | None:
    """The JobPosting that is *this* job: same normalised title, or the same
    job ID. Pages often embed related jobs; a posting that is not ours is
    never used — not even when it is the only one on the page."""
    postings = [x for x in postings if isinstance(x, dict)]
    want = normalize_title(title)
    for posting in postings:
        if want and normalize_title(str(posting.get("title", ""))) == want:
            return posting
    if job_id:
        jid = job_id.lower()
        for posting in postings:
            pid = _posting_id(posting).lower()
            if pid and (pid == jid or pid.endswith(jid) or jid.endswith(pid)):
                return posting
    return None


def jsonld_detail(postings: list, title: str, job_id: str = "", *, final_url: str = "", requested_url: str = "",
                  extra_strict: str = "") -> Detail:
    if requested_url and final_url and not same_page(final_url, requested_url):
        return Detail.unreadable(f"redirected to {final_url}")
    posting = _pick_posting(postings, title, job_id)
    if posting is None:
        return Detail.unreadable("structured data is for a different job" if postings else "no structured job data")
    description = _html_to_text(str(posting.get("description") or ""))
    extra = posting.get("experienceRequirements")
    if isinstance(extra, str):
        description += "\n" + extra
    elif isinstance(extra, dict):
        months = extra.get("monthsOfExperience")
        if months is not None:
            description += f"\nExperience: {months} months"
        elif extra.get("description"):
            description += "\n" + str(extra["description"])
    if len(description.strip()) < 20:
        return Detail.unreadable("posting has no description")
    return Detail(ok=True, description=description.strip(), location=_jsonld_location(posting),
                  strict_text=extra_strict, posting_evidence=True)


def _same_page(a: str, b: str) -> bool:
    return urldefrag(a)[0].rstrip("/") == urldefrag(b)[0].rstrip("/")


def _is_challenge(page) -> bool:
    try:
        if _CHALLENGE_TITLE_RE.search(page.title() or ""):
            return True
        return bool(page.evaluate(
            "() => !!document.querySelector('#challenge-form, #cf-challenge-running, [name=\"cf-turnstile-response\"], script[src*=\"challenge-platform\"]')"
        ))
    except Exception:
        return False


def _playwright_detail(page, url: str, title: str, listing_url: str) -> Detail:
    """Open a job detail page and read *this* job's description/location.
    Anything untrustworthy yields an unreadable Detail (never accepted)."""
    try:
        response = page.goto(url, timeout=20000, wait_until="domcontentloaded")
        if response is not None and response.status >= 400:
            return Detail.unreadable(f"HTTP {response.status}")
        if _is_challenge(page):
            return Detail.unreadable("bot challenge page")
        postings = page.evaluate(_JSONLD_JS) or []
        if not postings:
            # JS-rendered ATS — give it a moment to inject content
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            postings = page.evaluate(_JSONLD_JS) or []
        # redirected (expired posting, login wall, a different job)
        if _same_page(page.url, listing_url) or not same_page(page.url, url):
            return Detail.unreadable(f"redirected to {page.url}")
        fields = page.evaluate(_PAGE_FIELDS_JS) or ""
        job_id = ats_job_id(url).split(":", 1)[-1]
        if postings:
            return jsonld_detail(postings, title, job_id, final_url=page.url, requested_url=url, extra_strict=fields)
        for selector in _DESCRIPTION_SELECTORS:
            # e.g. a short "job-description-header" can precede the real block
            for el in page.query_selector_all(selector):
                text = (el.inner_text() or "").strip()
                if len(text) >= _MIN_DESCRIPTION_CHARS:
                    hidden = el.evaluate("e => e.textContent") or ""
                    return Detail(ok=True, description=text, strict_text=f"{hidden}\n{fields}")
        return Detail.unreadable("no job description found on the page")
    except Exception as e:
        log.warning("Detail page failed for %s: %s", url, e)
        return Detail.unreadable(f"{type(e).__name__}")


def _collect_candidates(page, listing_url: str, name: str) -> tuple[list[tuple[str, str, str, Classification]], int]:
    """Job-card links on the listing page that are in India and not already
    rejectable from the card alone, deduplicated by URL. Also returns how
    many links look like individual postings (0 -> not a job listing page)."""
    candidates = []
    seen: set[str] = set()
    job_links = 0
    try:
        links = page.evaluate(_CANDIDATES_JS) or []
    except Exception as e:
        log.warning("Could not read links on %s: %s", listing_url, e)
        return [], 0
    for link in links:
        href = link.get("href") or ""
        title = (link.get("title") or "").strip()
        text = link.get("text") or ""
        card = link.get("cardText") or text
        abs_href = urljoin(listing_url, href) if href else listing_url
        if _JOB_URL_RE.search(abs_href) and not _same_page(abs_href, listing_url):
            job_links += 1
        # the rest of the card (everything but the title line) is extra evidence
        card_rest = "\n".join(line for line in card.splitlines() if line.strip() and line.strip() != title)
        # India must come from the job's own card, never from the
        # surrounding career page (nav links, country pickers)
        if not title or not_a_job_reason(title, abs_href, posting_evidence=True) or not _is_india(card):
            continue
        # links without their own page (href="#…", or none) are keyed by title
        key = f"title:{title}" if _same_page(abs_href, listing_url) else urldefrag(abs_href)[0]
        if key in seen:
            continue
        seen.add(key)
        pre = classify_job(title, card_rest, url=abs_href, posting_evidence=True)
        if pre.category in _HARD_REJECT:
            _log_decision(name, title, pre)
            continue
        candidates.append((title, card_rest, abs_href, pre))
    return candidates, job_links


def _scan_playwright(company: dict) -> Scan:
    scan = Scan(company["name"])
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        scan.fail("failing", "Playwright not installed")
        return scan

    listing_url = company["url"]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(extra_http_headers={"User-Agent": HEADERS["User-Agent"]})
            try:
                response = page.goto(listing_url, timeout=30000)
            except Exception as e:
                scan.fail("failing", f"career page did not load ({type(e).__name__})")
                return scan
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass  # page may keep background network activity forever; DOM is usable regardless
            if response is not None and response.status >= 400:
                scan.fail("broken", f"career page returned HTTP {response.status}")
                return scan
            if _is_challenge(page):
                scan.fail("broken", "career page shows a bot challenge (e.g. Cloudflare)")
                return scan
            candidates, job_links = _collect_candidates(page, listing_url, scan.name)
            if job_links == 0 and not candidates:
                scan.fail("needs_config", "no job postings found on this page — set the company's job search page URL")
                return scan
            scan.sources_ok += 1
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
            budget = [MAX_DETAIL_FETCHES]
            for title, card_rest, href, _pre in candidates:
                has_page = href.startswith("http") and not _same_page(href, listing_url)
                listing = Listing(title=title, url=href, location=_card_location(card_rest), card_text=card_rest,
                                  fetch_detail=(lambda h=href, t=title: _playwright_detail(detail_page, h, t, listing_url))
                                  if has_page else None)
                # a link that doesn't look like a posting must prove it with a JobPosting
                if has_page and not _JOB_URL_RE.search(href):
                    listing.fetch_detail = (lambda h=href, t=title: _require_posting(
                        _playwright_detail(detail_page, h, t, listing_url)))
                _evaluate(scan, listing, budget)
        except Exception as e:
            scan.fail("failing", f"browser error: {type(e).__name__}: {e}")
        finally:
            browser.close()
    if scan.pending:
        scan.notes.append(f"{scan.pending} posting(s) left for the next run (detail budget)")
    return scan


def _require_posting(detail: Detail) -> Detail:
    if detail.ok and not detail.posting_evidence:
        return Detail.unreadable("not a job-posting URL and no JobPosting data on the page")
    return detail


def _scrape_playwright(company: dict) -> list[dict]:
    return _scan_playwright(company).jobs


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def scan_company(company: dict) -> Scan:
    """Scan one company. A company with a known job source is read only
    through it — its marketing/landing page is never scraped as a fallback
    (API success with zero matches is a normal, healthy result)."""
    try:
        sources = sources_for(company)
        scan = _scan_sources(company, sources) if sources else _scan_playwright(company)
    except Exception as e:
        scan = Scan(company.get("name", "?"))
        scan.fail("failing", f"{type(e).__name__}: {e}")
    log.info("%s → %d fresher job(s) found [%s%s]", scan.name, len(scan.jobs), scan.status,
             f": {scan.reason}" if scan.reason else "")
    return scan


def scrape_company(company: dict) -> tuple[list[dict], str]:
    """Returns (jobs, status) — status is 'active', 'failing', 'broken' or 'needs_config'."""
    scan = scan_company(company)
    return scan.jobs, scan.status


def record_uid(job: dict) -> str:
    return job.get("uid") or job_uid(job.get("company", ""), job.get("url", ""))


def _repost_key(job: dict) -> tuple | None:
    location = (job.get("location") or "").strip().lower()
    if not location:
        return None
    return ((job.get("company") or "").lower(), normalize_title(job.get("title", "")), location)


def merge_new_jobs(seen: list[dict], found: list[dict], now: datetime | None = None) -> list[dict]:
    """New records for ``found`` jobs that aren't already known. Identity is
    the ATS job ID / canonical URL, so two different jobs with the same title
    both get through; the same title *and* location under a new ID is treated
    as a repost of a job already alerted."""
    now = now or datetime.now(timezone.utc)
    known = {record_uid(j) for j in seen if isinstance(j, dict)}
    reposts = {k for k in (_repost_key(j) for j in seen if isinstance(j, dict)) if k}
    new: list[dict] = []
    for job in found:
        uid = job_uid(job["company"], job["url"])
        rp = _repost_key(job)
        if uid in known:
            continue
        if rp and rp in reposts:
            log.info("  %s: %s — repost of a job already alerted (same title and location)", job["company"], job["title"])
            continue
        record = dict(job)
        record.update({
            "uid": uid,
            "date": f"{now:%b} {now.day}, {now:%H:%M}",
            "first_seen": now.isoformat(),
            "id": f"{job['company']}_{job['title'][:40]}".replace(" ", "_"),
            "notified": False,
        })
        new.append(record)
        known.add(uid)
        if rp:
            reposts.add(rp)
    return new


def run_all() -> list[dict]:
    from config_store import load_companies, save_companies, load_seen_jobs, save_seen_jobs

    companies = load_companies()
    seen = load_seen_jobs()
    new_jobs: list[dict] = []

    for company in companies:
        scan = scan_company(company)
        company["status"] = scan.status
        company["status_reason"] = scan.reason
        company["last_checked"] = _now_iso()
        company["scan"] = scan.summary()
        company["source"] = source_label(company)
        # reflect what this scrape actually sees
        company["last_job"] = scan.jobs[0]["title"] if scan.jobs else ""
        new_jobs += merge_new_jobs(seen + new_jobs, scan.jobs)
        time.sleep(1)

    # commit=False: alerts.py publishes the data (merge-safe) before and
    # after emailing — see alerts.py
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
