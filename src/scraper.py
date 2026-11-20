from __future__ import annotations

import contextvars
import os
import random
import re
import time
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from urllib.parse import urldefrag, urljoin, urlparse

import httpx
from workday_http import CURRENT, WorkdayStop

from config_store import monitoring_state
from identity import ats_job_id, canonical_url, job_uid, same_page
from job_classifier import (
    Category, Classification, classify_job, evidence_is_applicant_directed, evidence_quote, experience_conflict,
    normalize_title, not_a_job_reason, parse_experience_requirements,
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
MAX_API_DETAIL_FETCHES = 500   # HTTP detail requests per company (PwC India needs ~200; Hitachi ~450 on
                               # 10 Nov 2026, when 400 left 46 of its postings unread until a later scan)
_FAILING_DETAIL_RATIO = 0.5    # more unreadable job pages than this -> "failing"

# ---------------------------------------------------------------------------
# Company -> real job source. Every endpoint below was verified against the
# live site; companies without an entry use their configured URL (Workday
# URLs are recognised automatically, anything else is read with Playwright).
# ---------------------------------------------------------------------------
COMPANY_SOURCES: dict[str, list[dict]] = {
    # Sanofi: see "sanofi" below (official Radancy sitemap since Nov 2026; Workday CXS is blocked)
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
    # Employers added Nov 2026, each from its official source, checked live.
    # They are monitored whatever they are hiring for today.
    "amazon": [{"type": "amazon", "country_code": "IND"}],
    "microsoft": [{"type": "eightfold", "host": "https://apply.careers.microsoft.com", "domain": "microsoft.com", "location": "India",
                   "early_career_media": "Microsoft Aspire"}],
    "qualcomm": [{"type": "eightfold", "host": "https://careers.qualcomm.com", "domain": "qualcomm.com", "location": "India"}],
    "google": [{"type": "google", "location": "Hyderabad, India"}],
    "deloitte": [{"type": "successfactors", "host": "https://southasiacareers.deloitte.com", "location": "India"}],
    "infosys": [{"type": "infosys", "location": "Hyderabad"}],
    "amd": [{"type": "jibe", "host": "https://careers.amd.com"}],
    "netflix": [{"type": "eightfold_v2", "host": "https://explore.jobs.netflix.net", "domain": "netflix.com", "location": "India"}],
    "oracle": [{"type": "oracle", "api": "https://eeho.fa.us2.oraclecloud.com/hcmRestApi/resources/latest", "site": "CX_45001",
                "location_facet": "300000000106947", "job_page": "https://careers.oracle.com/en/sites/jobsearch/job"}],
    "ibm": [{"type": "ibm", "country": "India"}],
    # ADP's feed is sometimes well-formed but empty or partial (6 Nov 2026: 1,482 -> 0 -> 1,493 -> 788
    # postings, all three readings counted as healthy scans), so it is checked against its own
    # last good size (_feed_size_check). ServiceNow's feed has no such history and is read as before.
    # JioStar's Workday search API answers 403 "Your request has been blocked" since 6 Nov 2026;
    # its robots.txt publishes the sitemap and allows the job pages, which carry a JobPosting
    "jiohotstar": [{"type": "workday_sitemap", "site": "https://jiostar.wd102.myworkdayjobs.com/JioStar"}],
    "adp": [{"type": "job_feed", "url": "https://jobs.adp.com/en/jobs/xml/",
             # settle_scans 8 (was 3): ADP's partial feed (~700-810 postings, no India jobs) came back
             # for 2 scans in a row twice (7 Nov 06:02-09:02, 9 Nov 15:02-18:02), so 3 would soon have
             # accepted a partial feed as "normal". 8 = a day of the same size before it counts as real.
             "size_guard": {"min_ratio": 0.75, "first_scan_min": 1000, "settle_scans": 8, "reread_wait_s": 30}}],
    "servicenow": [{"type": "job_feed", "url": "https://careers.servicenow.com/jobs/xml/"}],
    "swiggy": [{"type": "swiggy"}],
    # Workday CXS is blocked for the GitHub runners by Cloudflare (HTTP 429 on every
    # myworkdayjobs.com host since 16 Nov 2026). These employers' own career sites run
    # on Eightfold; checked live 19 Nov 2026 (Micron 315, NVIDIA 243 India listings; job
    # pages readable; robots.txt allows /careers and /api/apply). Their requisition ids
    # match the Workday ones, so jobs already recorded from Workday are not re-alerted.
    "c1793696369": [{"type": "eightfold", "host": "https://careers.micron.com", "domain": "micron.com", "location": "India"}],
    "c1793696370": [{"type": "eightfold", "host": "https://jobs.nvidia.com", "domain": "nvidia.com", "location": "India"}],
    # Official Phenom career sites (sources.phenom_listings), checked live 19-20 Nov 2026: India
    # postings via the public search (robots.txt allows it), job pages with a schema.org JobPosting,
    # reqId = the Workday requisition id (no re-alerts of jobs already recorded from Workday).
    "cisco": [{"type": "phenom", "base": "https://careers.cisco.com/global/en", "country": "India"}],              # 313
    "c1793696382": [{"type": "phenom", "base": "https://careers.mastercard.com/us/en", "country": "India"}],      # 156
    "c1793696381": [{"type": "phenom", "base": "https://careers.statestreet.com/global/en", "country": "India"}],  # 225
    "c1793696380": [{"type": "phenom", "base": "https://jobs.thecignagroup.com/us/en", "country": "India"}],      # 106 (Evernorth)
    "c1793696389": [{"type": "phenom", "base": "https://jobs.thermofisher.com/global/en", "country": "India"}],   # 318
    "c1793696377": [{"type": "phenom", "base": "https://careers.wbd.com/global/en", "country": "India"}],         # 7
    # Official Radancy career sites (sources.career_sitemap_listings): robots.txt disallows only
    # /search-jobs/; the published sitemap lists every job page by city; each page's JobPosting
    # identifier is the Workday requisition id. Checked live 20 Nov 2026.
    "sanofi": [{"type": "career_sitemap", "sitemap": "https://jobs.sanofi.com/sitemap.xml"}],                    # 82 of 1,451
    "c1793696374": [{"type": "career_sitemap", "sitemap": "https://www.synchronycareers.com/sitemap.xml"}],      # 14 of 29 (Synchrony)
    "c1793696379": [{"type": "career_sitemap", "sitemap": "https://careers.amgen.com/sitemap.xml"}],            # 592 of 1,670 (Amgen)
    "c1793696373": [{"type": "career_sitemap", "sitemap": "https://jobs.carrier.com/sitemap.xml"}],             # 54 of 934 (Carrier, 20 Nov)
    # Bristol Myers Squibb's own careers site jobs.bms.com runs on Eightfold (robots allows /careers, /api/apply): 70 India
    "c1793696378": [{"type": "eightfold", "host": "https://jobs.bms.com", "domain": "bms.com", "location": "India"}],
    # Novartis's own careers search on novartis.com (sources.novartis_listings), India filter,
    # stable title order; full posting from the job page. Checked live 20 Nov 2026: 108 of 111.
    "c1793696372": [{"type": "novartis", "country_code": "LOC_IN"}],
    # S&P Global's official careers site runs on Jibe/iCIMS (the existing adapter): 57 India of 359
    "c1793696385": [{"type": "jibe", "host": "https://careers.spglobal.com"}],
}
# kept for callers that only need "which companies use an API"
COMPANY_API = {cid: {"type": cfgs[0]["type"], **cfgs[0]} for cid, cfgs in COMPANY_SOURCES.items()}

_SOURCE_LABELS = {"workday": "Workday API", "accenture": "Accenture job-search API", "jibe": "Jibe/iCIMS API",
                  "avature": "Avature job search", "zoho": "Zoho Recruit career site", "amazon": "Amazon jobs API",
                  "eightfold": "Eightfold API", "successfactors": "SuccessFactors career site", "infosys": "Infosys jobs API",
                  "google": "Google Careers site", "eightfold_v2": "Eightfold API", "oracle": "Oracle Recruiting API",
                  "ibm": "IBM careers API", "job_feed": "Official job feed", "swiggy": "Swiggy careers site",
                  "workday_sitemap": "Workday sitemap + job pages", "phenom": "Phenom career site",
                  "career_sitemap": "Career-site sitemap + job pages", "novartis": "Novartis careers search"}


def sources_for(company: dict) -> list[dict]:
    if isinstance(company.get("sources"), list) and company["sources"]:
        return company["sources"]
    if company.get("id") in COMPANY_SOURCES:
        return COMPANY_SOURCES[company["id"]]
    api = workday_api_from_url(company.get("url", ""))
    return [{"type": "workday", "api": api}] if api else []


def source_label(company: dict) -> str:
    if monitoring_state(company):
        return "Not scanned: no usable source yet"
    srcs = sources_for(company)
    if not srcs:
        return "Playwright (headless browser)"
    return " + ".join(dict.fromkeys(_SOURCE_LABELS.get(s.get("type"), s.get("type", "API")) for s in srcs))


def _plural(n: int, word: str) -> str:
    return f"{n} {word if n == 1 else word + 's'}"


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
    reasons: Counter = field(default_factory=Counter)   # why postings were not accepted, by kind (_reason_kind)
    feed: dict = field(default_factory=dict)            # a size-guarded feed's baseline / suspect record (_feed_size_check)

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
        return ""

    @property
    def user_note(self) -> str:
        """Plain-language note for a healthy scan (shown in Monitoring)."""
        return f"{_plural(self.pending, 'posting')} left for the next scan (detail-page budget)" if self.pending else ""

    def summary(self) -> dict:
        """Counts for Monitoring. "rejections": the most common reasons a
        posting was not accepted (Nov 2026: so a company that never yields a
        job — Microsoft, S&P Global — can be judged from the data, without
        the run log)."""
        return {"postings_seen": self.seen, "candidates": self.candidates, "details_read": self.details_ok,
                "details_unreadable": self.details_failed, "pending": self.pending, "accepted": len(self.jobs),
                "rejected": self.rejected, "unknown": self.unknown,
                "rejections": dict(self.reasons.most_common(_TOP_REASONS)),
                **({"feed": self.feed} if self.feed else {})}


_TOP_REASONS = 6
_QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"")


def _reason_kind(reason: str) -> str:
    """"seniority in title: 'Senior'" -> "seniority in title"; "not in India
    (posting location: X)" -> "not in India": the kind, never the job's own
    text (titles and quotes are left out)."""
    text = _QUOTED_RE.sub("", reason or "")
    text = re.split(r"[:(]| — | after ", text, maxsplit=1)[0]
    return re.sub(r"\s+", " ", text).strip()[:60] or "other"


# ---------------------------------------------------------------------------
# The decision: listing + (maybe) detail -> classification
# ---------------------------------------------------------------------------

# lines about experience / eligibility ("each year" or "100 years of history" are not)
_EXPERIENCE_LINE_RE = re.compile(
    r"experience|\bexp\b|fresher|graduat|\d\s*(?:\+\s*)?(?:years?|yrs?|months?)\b", re.IGNORECASE)


def safety_gate(listing: Listing, detail: Detail | None, result: Classification, location: str) -> dict[str, bool]:
    """The final checks every alert must pass (checks 9-10 — not previously
    notified, not dismissed — are enforced when the record is merged and
    when the email queue is built: merge_new_jobs / alert_pending)."""
    title, url = listing.title, listing.url
    description = f"{listing.card_text}\n{detail.description}".strip() if detail else listing.card_text
    posting = bool(detail and (detail.posting_evidence or listing.posting_evidence))
    reason = not_a_job_reason(title, url, posting_evidence=posting)
    return {
        "real_job_posting": not reason and (posting or bool(_JOB_URL_RE.search(url))),
        "india_location": is_india(location),
        "detail_read": bool(detail and detail.ok),
        "detail_is_this_job": bool(detail and detail.ok and detail.matched),
        "no_conflicting_experience": not experience_conflict(title, description, detail.strict_text if detail else ""),
        "not_programme_story_talent_recruiter": not reason,
        "evidence_not_staff_context": evidence_is_applicant_directed(result, title, description),
        "fresher_or_entry_evidence": result.accepted and bool(evidence_quote(result)),
    }


def _evidence(listing: Listing, detail: Detail, result: Classification, checks: dict) -> dict:
    description = f"{listing.card_text}\n{detail.description}"
    reqs = parse_experience_requirements(description) + parse_experience_requirements(detail.strict_text)
    # the lines that decided it (requirement / fresher quote) first, then other
    # experience-related lines; each line once
    decisive = [r.text.lower() for r in reqs] + [evidence_quote(result).lower()]
    lines: list[str] = []
    for ln in description.splitlines():
        ln = ln.strip(" \t·•*-")[:160]
        if ln and _EXPERIENCE_LINE_RE.search(ln) and ln not in lines:
            lines.append(ln)
    lines.sort(key=lambda ln: not any(q and q in ln.lower() for q in decisive))
    return {
        "experience": [r.text.rstrip(" .,;") for r in reqs] or ["no experience requirement stated"],
        "experience_lines": lines[:4],
        "fresher_evidence": evidence_quote(result),
        "detail_read": detail.ok,
        "detail_match": detail.matched,
        "job_id": ats_job_id(listing.url) or (f"id:{listing.job_id}" if listing.job_id else ""),
        "canonical_url": canonical_url(listing.url),
        "checks": checks,
    }


def _posting_skills(description: str, limit: int = 15) -> dict[str, str]:
    """{skill: the posting line that names it} (job_intel's vocabulary), for
    a job that has already been accepted — Milestone 5's match estimates
    quote it. It never affects classification, and a failure here only
    leaves the field out."""
    try:
        import job_intel
        found: dict[str, str] = {}
        for ln in description.splitlines():
            ln = ln.strip(" \t·•*-")[:160]
            for name in job_intel.extract_skills(ln) if ln else ():
                found.setdefault(name, ln)
        ordered = [name for name in job_intel.SKILLS if name in found]           # vocabulary order
        return {name: found[name] for name in ordered[:limit]}
    except Exception:                      # never let a descriptive field cost a job
        return {}


def _posting_requirements(description: str) -> list[dict]:
    """The posting lines M6's matcher needs ({text, importance}: skills,
    experience, education and other stated conditions, each marked
    mandatory / preferred / unspecified), for a job that has already been
    accepted. Never affects classification; a failure only leaves it out."""
    try:
        import job_requirements
        return job_requirements.posting_requirement_lines(description)
    except Exception:                      # never let a descriptive field cost a job
        return []


def decide(company: str, listing: Listing, detail: Detail | None) -> tuple[Classification, dict | None]:
    """Final verdict for one posting. Never accepts without the posting's own
    detail (the safety gate requires it), and every alert must pass all
    safety-gate checks; its evidence is stored on the record."""
    title, card = listing.title, listing.card_text
    if detail is None or not detail.ok:
        why = detail.reason if detail else "no job page"
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
    checks = safety_gate(listing, detail, result, location)
    failed = [k for k, ok in checks.items() if not ok]
    if failed:
        return _unknown(f"safety gate failed ({', '.join(failed)}) after '{result.reason}' — not alerted"), None
    shown = " | ".join(india_segments(location)) or location
    job = _job(title, listing.url, company, result, shown if is_india(detail.location) else location)
    job["evidence"] = _evidence(listing, detail, result, checks)
    skills = _posting_skills(description)
    if skills:                             # descriptive only: added after every gate has passed
        job["evidence"]["skills"] = skills
    reqs = _posting_requirements(description)
    if reqs:                               # M6: the requirement lines, also only after every gate
        job["evidence"]["requirements"] = reqs
    if detail.posted_at:                   # only what the source states — never a guess (sources.posting_date)
        job["posted_at"] = detail.posted_at
    job["evidence"] = _redact_emails(job["evidence"])
    return result, job


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")


def _redact_emails(value):
    """Quoted posting text kept as evidence never carries an email address into
    the public data files (an employer's "report fraud to x@..." line, Nov 2026)."""
    if isinstance(value, str):
        return _EMAIL_RE.sub("[email removed]", value)
    if isinstance(value, list):
        return [_redact_emails(v) for v in value]
    if isinstance(value, dict):
        return {k: _redact_emails(v) for k, v in value.items()}
    return value


# The 72 h detail-verdict cache for companies read from non-Workday sources (Nov 2026):
# the M7 controller in cache-only mode (Recovery.begin_cache), set by run_all per company.
DETAIL_CACHE: contextvars.ContextVar = contextvars.ContextVar("detail_cache", default=None)


def _evaluate(scan: Scan, listing: Listing, budget: list[int]) -> None:
    recovery = CURRENT.get()
    cache = recovery or DETAIL_CACHE.get()
    fingerprint = cache.fingerprint(listing) if cache else ""
    scan.candidates += 1
    # judged as a posting here only to avoid dropping programme titles before
    # their JobPosting evidence has been read; the final verdict uses the real evidence
    pre = classify_job(listing.title, listing.card_text, url=listing.url, posting_evidence=True)
    if pre.category in _HARD_REJECT:
        if recovery:
            recovery.pending.pop(recovery.key(listing), None)
        scan.rejected += 1
        scan.reasons[_reason_kind(pre.reason)] += 1
        _log_decision(scan.name, listing.title, pre)
        return
    detail = listing.detail
    hit = cache.lookup(listing) if cache else None
    if hit:
        cache.stats["cache_hits"] += 1
        if hit["verdict"] == "rejected" and not hit["detail"].get("description"):
            # same posting (fingerprint), same rules: the stored rejection, no request
            if recovery:
                recovery.pending.pop(recovery.key(listing), None)
            listing.title = hit["title"]
            scan.rejected += 1
            reason = hit["detail"].get("reason") or "rejected"
            scan.reasons[_reason_kind(reason)] += 1
            log.info("  %s: %s — REJECTED (cached verdict) — %s", scan.name, listing.title, reason)
            return
        detail = Detail(**hit["detail"])
        listing.title = hit["title"]
    fetched = False
    if detail is None and listing.fetch_detail is not None:
        fetched = True
        if budget[0] > 0:
            budget[0] -= 1
            if cache:
                cache.stats["cache_misses"] += 1
            try:
                detail = listing.fetch_detail()
            except Exception as e:  # one broken posting never aborts the company
                detail = Detail.unreadable(f"{type(e).__name__}: {e}")
            scan.record_detail(detail)
        else:
            detail = Detail.unreadable("detail budget reached this run")
            scan.pending += 1
    if recovery and detail and not detail.ok:
        diagnostic = detail.diagnostic
        category = diagnostic.get("failure_category", "incomplete")
        recovery.defer_listing(listing, category)
        if diagnostic:
            # Keep the actual throttle, not subsequent no-request cooldown outcomes.
            if not recovery.last_stop or diagnostic.get("attempted"):
                recovery.last_stop = diagnostic
            if diagnostic.get("attempted") and category not in ("not_found",):
                scan.fail("failing", f"Workday {category}")
    result, job = decide(scan.name, listing, detail)
    if recovery:
        if result.category == Category.UNKNOWN:
            if not detail or not detail.ok:
                recovery.defer_listing(listing)
            else:
                recovery.pending.pop(recovery.key(listing), None)
        elif hit:
            recovery.pending.pop(recovery.key(listing), None)  # hits never extend TTL
        else:
            recovery.cache_detail(listing, detail, "accepted" if job else "rejected", fingerprint, result.reason)
    elif cache and not hit and fetched and result.category != Category.UNKNOWN:
        # a real verdict from a page this scan had to read (a listing that already carries its
        # detail costs no request, so caching it would only grow the file); failures never cached
        cache.cache_detail(listing, detail, "accepted" if job else "rejected", fingerprint, result.reason)
    _log_decision(scan.name, listing.title, result)
    if job:
        scan.jobs.append(job)
        return
    scan.reasons[_reason_kind(result.reason)] += 1
    if result.category == Category.UNKNOWN:
        scan.unknown += 1
    else:
        scan.rejected += 1


# ---------------------------------------------------------------------------
# HTTP sources (Workday, Accenture, Jibe, Avature, Zoho)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Feed-size guard (only for a source configured with "size_guard": ADP)
# ---------------------------------------------------------------------------
# A feed can be well-formed XML and still be empty or cut short at the
# source: ADP's went 1,482 -> 0 -> 1,493 -> 788 postings on 6 Nov 2026, and
# every reading was a "healthy" scan, so its vacancies silently went unseen.
# The guard compares each reading with the last GOOD size of the same feed,
# kept in the company's own scan record (the only part of companies.json a
# scan publishes; repo_sync.SCAN_FIELDS):
#   * at least min_ratio of the last good size -> good; it becomes the new
#     baseline (so a slow genuine change is followed);
#   * smaller (or empty) -> a failed scan with the reason: nothing is read
#     from it, the baseline and last_success are kept, the next scan retries;
#   * no baseline yet -> good if at least first_scan_min postings, so a first
#     scan is judged on its own and never rejected by default;
#   * the same smaller, non-empty size settle_scans times in a row (within
#     10%) -> taken as the feed's new normal: a real, lasting change is not
#     treated as a failure for ever. An empty feed never becomes "normal".
_SETTLE_TOLERANCE = 0.10


def _feed_size_check(postings: int, guard: dict, previous: dict | None) -> tuple[bool, str, dict]:
    """(accept, reason or note, the scan's new "feed" record). ``previous``
    is the company's last "feed" record ({"baseline": {...}, "suspect": {...}})."""
    previous = previous if isinstance(previous, dict) else {}
    base = previous.get("baseline") if isinstance(previous.get("baseline"), dict) else None
    base_n = base.get("postings") if base and isinstance(base.get("postings"), int) and base["postings"] > 0 else None
    now = _now_iso()
    good = {"baseline": {"postings": postings, "at": now}}
    if base_n is not None and postings >= guard["min_ratio"] * base_n:
        return True, "", good
    if base_n is None and postings >= guard["first_scan_min"]:
        return True, "", good
    expected = (f"{base_n:,} at the last good scan" if base_n is not None
                else f"at least {guard['first_scan_min']:,} expected (no good scan recorded yet)")
    if postings == 0:
        reason = f"feed size anomaly: the job feed listed no postings ({expected})"
    else:
        reason = f"feed size anomaly: the job feed listed {postings:,} postings ({expected})"
    sus = previous.get("suspect") if isinstance(previous.get("suspect"), dict) else None
    same = (postings > 0 and sus and isinstance(sus.get("postings"), int) and sus["postings"] > 0
            and abs(postings - sus["postings"]) <= _SETTLE_TOLERANCE * sus["postings"])
    streak = (int(sus.get("streak", 0)) + 1) if same else 1
    if postings > 0 and streak >= guard["settle_scans"]:
        return True, (f"feed size changed to {postings:,} postings and held for {streak} scans: "
                      f"accepted as the feed's new normal"), good
    record = {"suspect": {"postings": postings, "streak": streak, "at": now}}
    if base:
        record["baseline"] = base                     # the last good size, kept untouched
    return False, reason + " — not read as no jobs; retried next scan", record


def _scan_sources(company: dict, sources: list[dict]) -> Scan:
    scan = Scan(company["name"])
    budget = [MAX_API_DETAIL_FETCHES]
    previous_feed = (company.get("scan") or {}).get("feed") if isinstance(company.get("scan"), dict) else None
    with httpx.Client(headers=HEADERS, timeout=25, follow_redirects=True) as client:
        listings: list[Listing] = []
        for cfg in sources:
            try:
                result = read_source(client, cfg)
            except WorkdayStop as e:
                # Keep the real throttle: a later no-request stop on the same host
                # (PwC's second site after its first site's 429) must not replace it.
                run = CURRENT.get()
                if not run.last_stop.get("attempted") or e.diagnostic.get("attempted"):
                    run.last_stop = e.diagnostic
                scan.fail("failing", str(e))
                continue
            except SourceError as e:
                scan.fail(e.status, str(e))
                if CURRENT.get():
                    CURRENT.get().last_stop = {"failure_category": "invalid_response", "http_status": None,
                                               "host": urlparse(cfg.get("api") or cfg.get("site") or "").hostname or ""}
                if isinstance(cfg.get("size_guard"), dict) and isinstance(previous_feed, dict):
                    scan.feed = dict(previous_feed)       # an unreadable feed keeps the last good size too
                continue
            except Exception as e:
                scan.fail("failing", f"{type(e).__name__}: {e}")
                if isinstance(cfg.get("size_guard"), dict) and isinstance(previous_feed, dict):
                    scan.feed = dict(previous_feed)
                continue
            if isinstance(cfg.get("size_guard"), dict):
                accept, why, scan.feed = _feed_size_check(result.seen, cfg["size_guard"], previous_feed)
                wait = cfg["size_guard"].get("reread_wait_s")
                if not accept and wait:
                    # the feed answered 200 with too few postings: read it once more after a pause
                    # (ADP's partial feed is intermittent) — never more than once per scan
                    log.info("%s: %s; reading the feed once more in %s s", scan.name, why, wait)
                    time.sleep(wait)
                    try:
                        again = read_source(client, cfg)
                    except Exception as e:      # the re-read failing changes nothing: the first verdict stands
                        log.info("%s: re-read failed (%s)", scan.name, type(e).__name__)
                    else:
                        ok2, why2, feed2 = _feed_size_check(again.seen, cfg["size_guard"], previous_feed)
                        if ok2:
                            result, accept, why, scan.feed = again, True, why2, feed2
                            scan.notes.append(f"first reading was short; the re-read listed {again.seen:,} postings")
                if not accept:
                    scan.seen += result.seen               # what the feed said, for the record
                    scan.fail("failing", why)
                    continue
                if why:
                    scan.notes.append(why)
                    log.info("%s: %s", scan.name, why)
            if result.coverage_error:
                scan.fail("failing", result.coverage_error)
            scan.sources_ok += 1
            scan.seen += result.seen
            if result.note:
                scan.notes.append(result.note)   # technical detail: logged, not shown to users
            listings += result.listings
        # more detail pages than the budget: vary the order between runs so
        # the same postings are never the ones left pending every time
        if CURRENT.get():
            if not scan.errors and not CURRENT.get().last_stop:
                keys = {CURRENT.get().key(item) for item in listings}
                CURRENT.get().pending = {k: v for k, v in CURRENT.get().pending.items() if k in keys}
            listings = CURRENT.get().order(listings)
        elif sum(1 for x in listings if x.detail is None and x.fetch_detail) > budget[0]:
            random.shuffle(listings)
        for listing in listings:
            try:
                _evaluate(scan, listing, budget)
            except Exception as e:
                log.warning("Skipping malformed posting for %s: %s", scan.name, e)
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
    same_title = normalize_title(str(posting.get("title", ""))) == normalize_title(title)
    return Detail(ok=True, description=description.strip(), location=_jsonld_location(posting),
                  strict_text=extra_strict, posting_evidence=True,
                  matched="JobPosting with the same title" if same_title else "JobPosting with the same job ID")


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
                    return Detail(ok=True, description=text, strict_text=f"{hidden}\n{fields}",
                                  matched="the posting's own URL (no redirect)")
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
    log.info("%s → %d fresher job(s) found [%s%s]%s", scan.name, len(scan.jobs), scan.status,
             f": {scan.reason}" if scan.reason else "", f" ({'; '.join(scan.notes)})" if scan.notes else "")
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


# ---------------------------------------------------------------------------
# Time-bound repost suppression (Nov 2026)
# ---------------------------------------------------------------------------
# Until now a new posting matching the title+location of ANY earlier record
# was suppressed for ever: a role that closed and reopened months later (a
# yearly graduate intake, say) could never be alerted again.
#
# Now a same-title+location posting under a new ID is suppressed only while
# the key was SIGHTED within REPOST_WINDOW_DAYS. A sighting is:
#   * the matching record's first_seen, and
#   * repost_last_seen (a UTC date, day granularity): stamped on a record
#     whenever a scan finds its own posting (same uid) still listed and still
#     qualifying — never stamped on a suppressed candidate, or suppression
#     would renew itself and the window could never expire.
# Deliberately unchanged: while the recorded posting is still listed, its key
# stays fresh, so a CONCURRENT same-title+location requisition stays
# suppressed. That is the steady 36-39 "reposts_suppressed" of every scan
# since 6 Nov 2026 (e.g. Cisco's several "Software Engineer" openings in
# Bengaluru): the same open postings judged again each scan, not new ones.
# A key not sighted for longer than the window is a vacancy that closed and
# came back: it is alerted as a new job (counted as reposts_window_realerted)
# and gets its own record. Eligibility rules are untouched: this only decides
# new-vs-repost for jobs the classifier already accepted.
#
# Dates are whole UTC days. A key last sighted on day D is suppressed through
# D + REPOST_WINDOW_DAYS (inclusive) and can be alerted from the day after.
#
# Deployment (Nov 2026 review): records saved before this code have no
# repost_last_seen, and their first_seen can be weeks old — judged by
# first_seen alone, keys would fall outside the window on the first scan and
# a duplicate suppressed today could be emailed just because history was
# missing. So any record without a reliable stamp (missing, malformed or in
# the future) is stamped with the day of the first scan that sees it, once:
# in production that is the actual deployment day, and its 45 days start
# there. New records are stamped when created, so "no stamp" only ever means
# "saved before deployment". Counted as repost_dates_backfilled.
REPOST_WINDOW_DAYS = 45
REPOST_LAST_SEEN = "repost_last_seen"
_STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _utc_today(now: datetime) -> date:
    return (now if now.tzinfo else now.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).date()


def _first_seen_day(value) -> date | None:
    """A record's first_seen (an ISO time, any UTC offset) as its UTC day;
    None when unreadable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).date()


def _stamp_day(value, today: date) -> date | None:
    """A reliable repost_last_seen: exactly YYYY-MM-DD, a real date, not
    after ``today``. Anything else counts as no stamp."""
    if not isinstance(value, str) or not _STAMP_RE.match(value):
        return None
    try:
        day = date.fromisoformat(value)
    except ValueError:
        return None
    return day if day <= today else None


def _last_sighting_day(records: list[dict], today: date) -> date | None:
    """The latest UTC day any of these records' postings is known to have
    been listed: the newest first_seen (not after today) or reliable stamp."""
    days = [d for r in records for d in (_first_seen_day(r.get("first_seen")), _stamp_day(r.get(REPOST_LAST_SEEN), today))
            if d is not None and d <= today]
    return max(days) if days else None


def repost_suppressed(last_day: date | None, today: date) -> bool:
    """Still within the window? An unknown sighting stays suppressed."""
    return last_day is None or (today - last_day).days <= REPOST_WINDOW_DAYS


def backfill_repost_dates(seen: list[dict], now: datetime, stats: Counter | None = None) -> int:
    """Stamp every record without a reliable repost_last_seen with today's
    UTC day (see the deployment note above). Idempotent; returns how many
    records it stamped."""
    today = _utc_today(now)
    n = 0
    for j in seen:
        if isinstance(j, dict) and _stamp_day(j.get(REPOST_LAST_SEEN), today) is None:
            j[REPOST_LAST_SEEN] = today.isoformat()
            n += 1
    if stats is not None and n:
        stats["repost_dates_backfilled"] += n
    return n


def _posting_key(url: str) -> str:
    """Company-independent identity: the posting's host + ATS job ID. It keeps
    a renamed company ("npcl" -> "NPCI") from re-alerting its old jobs."""
    ident = ats_job_id(url)
    return f"{urlparse(url).netloc.lower().removeprefix('www.')}|{ident}" if ident else ""


_WD_REQ_RE = re.compile(r"_((?:JR|REQ|R)-?\d{4,}(?:-\d{1,2})?|\d{6,}(?:-\d{1,2})?)/?$", re.IGNORECASE)
_REQ_RE = re.compile(r"(?:JR|REQ|R)-?\d{4,}|\d{6,}")


def _requisition(job: dict) -> str:
    """The employer's requisition id ("JR112672", "R-01372949", "2023748"),
    whichever official career site the posting was read from: a Workday URL
    ends with "_<id>" (sometimes "-1" for a posting copy), and the Eightfold
    and Phenom sites that replaced blocked Workday tenants (Nov 2026) report
    the same id, which the record keeps as "id:<id>". "" when there is none."""
    values = []
    ident = str((job.get("evidence") or {}).get("job_id") or "")
    if ident.split(":", 1)[0] in ("id", "workday", "jr"):
        values.append(ident.split(":", 1)[-1])
    url = job.get("url", "") or ""
    if "myworkdayjobs.com" in url:
        m = _WD_REQ_RE.search(url.split("?", 1)[0])
        if m:
            values.append(m.group(1))
    for value in values:
        value = value.strip().upper()
        base = re.sub(r"-\d{1,2}$", "", value) if re.fullmatch(r"(?:(?:JR|REQ|R)-?\d{4,}|\d{6,})-\d{1,2}", value) else value
        if _REQ_RE.fullmatch(base):
            return base
    return ""


def merge_new_jobs(seen: list[dict], found: list[dict], now: datetime | None = None,
                   company_id: str | None = None, stats: Counter | None = None) -> list[dict]:
    """New records for ``found`` jobs that aren't already known. Identity is
    the ATS job ID / canonical URL, so two different jobs with the same title
    both get through; the same title *and* location under a new ID is treated
    as a repost of a job already alerted — but only while that key was
    sighted within REPOST_WINDOW_DAYS (see the notes above): a key quiet for
    longer is a vacancy that came back, alerted as new.
    Side effects on ``seen``, in place: records without a reliable
    repost_last_seen are backfilled with today (backfill_repost_dates), and
    records whose own posting is in ``found`` (same uid) are stamped today;
    the caller must save ``seen`` when stats says either happened.
    ``company_id`` (the company's immutable id) is stored on new records."""
    now = now or datetime.now(timezone.utc)
    today = _utc_today(now)
    stamp = today.isoformat()
    backfill_repost_dates(seen, now, stats)
    known = {record_uid(j) for j in seen if isinstance(j, dict)}
    by_uid = {record_uid(j): j for j in seen if isinstance(j, dict)}
    known_postings = {k for k in (_posting_key(j.get("url", "")) for j in seen if isinstance(j, dict)) if k}
    # the same requisition read from another official site of the same company is not a new job
    known_reqs = {((j.get("company") or "").strip().lower(), _requisition(j)) for j in seen
                  if isinstance(j, dict) and _requisition(j)}
    by_repost_key: dict[tuple, list[dict]] = {}
    for j in seen:
        if isinstance(j, dict):
            k = _repost_key(j)
            if k:
                by_repost_key.setdefault(k, []).append(j)
    # stamp still-listed recorded postings FIRST, so a same-key candidate
    # later in this very scan is judged against a fresh sighting
    for job in found:
        record = by_uid.get(job_uid(job["company"], job["url"]))
        if record is not None and record.get(REPOST_LAST_SEEN) != stamp:
            record[REPOST_LAST_SEEN] = stamp
            if stats is not None:
                stats["repost_stamps_updated"] += 1
    reposts = {k for k in by_repost_key}
    new: list[dict] = []
    for job in found:
        uid = job_uid(job["company"], job["url"])
        rp = _repost_key(job)
        pk = _posting_key(job["url"])
        if uid in known or (pk and pk in known_postings):
            continue
        req = _requisition(job)
        if req and ((job.get("company") or "").strip().lower(), req) in known_reqs:
            log.info("  %s: %s — requisition %s already recorded from another official site",
                     job["company"], job["title"], req)
            continue
        if rp and rp in reposts:
            last = _last_sighting_day(by_repost_key.get(rp, []), today)
            if repost_suppressed(last, today):
                log.info("  %s: %s — repost of a job already alerted (same title and location)",
                         job["company"], job["title"])
                if stats is not None:      # measured (Nov 2026) before any change to how reposts are judged
                    stats["reposts_suppressed"] += 1
                continue
            log.info("  %s: %s — same title and location as a record last sighted %s (over %d days): a vacancy "
                     "that came back, treated as new", job["company"], job["title"], last.isoformat(),
                     REPOST_WINDOW_DAYS)
            if stats is not None:
                stats["reposts_window_realerted"] += 1
        record = dict(job)
        record.update({
            "uid": uid,
            "date": f"{now:%b} {now.day}, {now:%H:%M}",
            "first_seen": now.isoformat(),
            "id": f"{job['company']}_{job['title'][:40]}".replace(" ", "_"),
            "notified": False,
            REPOST_LAST_SEEN: stamp,
        })
        if company_id:
            record["company_id"] = company_id
        new.append(record)
        known.add(uid)
        if pk:
            known_postings.add(pk)
        if rp:
            # the new record is a fresh sighting of its key: a second
            # same-key candidate in this very scan is a repost of IT
            reposts.add(rp)
            by_repost_key.setdefault(rp, []).append(record)
    return new


def _failure_streak(scan: Scan, previous: dict, was_failing: bool, now: str) -> dict:
    """{"failing_since", "failed_scans"} for a failed scan (counting on from
    the last scan's record when that one failed too); {} for a healthy one.
    Diagnostics only: it never changes a status."""
    if scan.status == "active":
        return {}
    if was_failing and isinstance(previous.get("failed_scans"), int) and previous.get("failing_since"):
        return {"failing_since": previous["failing_since"], "failed_scans": previous["failed_scans"] + 1}
    return {"failing_since": now, "failed_scans": 1}


def _blocked_streak(previous: dict, failing: bool, full: bool, now: str) -> dict:
    """Consecutive SCHEDULED FULL scans (scope "all") this company ended without a
    healthy scan - attempted and failed, or held back while still failing (a
    rate-limited Workday data centre). Targeted Workday-only runs never advance
    it; a healthy scan clears it. Feeds the monitoring-failure email
    (monitoring_alerts.py): one company, one count per scheduled scan, never
    one per HTTP retry. A valid empty result is healthy and never counts."""
    if not failing:
        return {}
    old = {k: previous[k] for k in ("blocked_scans", "blocked_since", "last_failed_at") if k in previous}
    if not full:
        return {**old, "last_failed_at": now}
    n = previous.get("blocked_scans") if isinstance(previous.get("blocked_scans"), int) else 0
    return {"blocked_scans": n + 1, "blocked_since": previous.get("blocked_since") or now, "last_failed_at": now}


def _streak_label(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return str(iso)
    return f"{dt.day} {dt:%b} {dt:%H:%M} UTC"


SCOPES = ("all", "workday")


def run_all(recovery=None, scope: str = "all") -> list[dict]:
    """scope "all": the regular scan. scope "workday": a targeted Workday recovery
    run - only Workday API companies are scanned; every other company is left
    exactly as it is, and the run marker carries scope="workday" so it never
    counts as the window's full scan (scan_schedule.last_scan ignores it)."""
    if scope not in SCOPES:
        raise ValueError(f"unknown scan scope {scope!r}")
    from config_store import load_companies, save_companies, load_seen_jobs, save_seen_jobs

    companies = load_companies()
    if recovery is True:
        from config_store import DATA
        from workday_runtime import Recovery
        recovery = Recovery(DATA, companies, sources_for)
    seen = load_seen_jobs()
    new_jobs: list[dict] = []
    started_at = _now_iso()
    jobs_found = portals_failed = 0
    merge_stats: Counter = Counter()
    scanned = [c for c in companies if not monitoring_state(c)]   # the others have no usable source yet: left as they are
    if scope == "workday":
        if not recovery:
            raise ValueError("a Workday-only run needs the M7 recovery controller")
        from workday_runtime import is_api
        scanned = [c for c in scanned if is_api(sources_for(c))]
    if recovery:
        from workday_runtime import is_api
        # Healthy independent sources (including JioStar) precede the API budget.
        # ... and each data centre's recovery probe goes first among the API companies,
        # so a healthy probe releases the rest of its data centre in the same run.
        scanned.sort(key=lambda c: (is_api(sources_for(c)), recovery.order_key(c)))

    cache_totals: Counter = Counter()
    for company in scanned:
        previous = company.get("scan") if isinstance(company.get("scan"), dict) else {}
        from workday_runtime import is_workday
        managed = recovery and is_workday(sources_for(company))
        if managed:
            reason = recovery.defer_reason(company)
            if reason:
                from workday_alternatives import permitted
                unverified = any(c.get("type") == "workday_sitemap" and not permitted(c) for c in sources_for(company))
                from workday_runtime import hosts
                unverified = unverified or any(recovery.state["hosts"].get(h, {}).get("failure_category") in
                                              ("forbidden", "authentication", "redirect") for h in hosts(sources_for(company)))
                outcome = "needs_config" if unverified else "cooling_down" if reason == "cooldown" else "deferred"
                last = (previous.get("recovery") or {}).get("outcome")
                if (outcome == "deferred" and company["id"] in recovery.not_due and company.get("status") == "active"
                        and last in ("healthy", "degraded")):
                    outcome, reason = last, ""   # checked successfully within the revisit interval: not a deferral
                failing = company.get("status") in ("failing", "broken", "needs_config")
                base = {k: v for k, v in previous.items() if k not in ("blocked_scans", "blocked_since", "last_failed_at")}
                company["scan"] = {**base, **_blocked_streak(previous, failing, scope == "all", _now_iso()),
                                   "recovery": recovery.projection(company, outcome, reason)}
                portals_failed += company.get("status") in ("failing", "broken", "needs_config")
                continue  # No attempt, timestamp or failure-streak advancement.
            recovery.begin(company)
        was_failing = company.get("status") not in (None, "", "active", "unknown")
        cache_token = None
        if recovery and not managed:
            recovery.begin_cache(company)                 # 72 h detail-verdict cache for every source
            cache_token = DETAIL_CACHE.set(recovery)
        try:
            scan = scan_company(company)
        finally:
            if managed:
                recovery.end()
            if cache_token is not None:
                DETAIL_CACHE.reset(cache_token)
        if recovery:
            for k in ("cache_hits", "cache_misses"):
                cache_totals[k] += recovery.stats.get(k, 0)
        if managed and len(recovery.http.events) == recovery.event_start:
            category = recovery.last_stop.get("failure_category", "budget")
            failing = company.get("status") in ("failing", "broken", "needs_config")
            base = {k: v for k, v in previous.items() if k not in ("blocked_scans", "blocked_since", "last_failed_at")}
            company["scan"] = {**base, **_blocked_streak(previous, failing, scope == "all", _now_iso()),
                               "recovery": recovery.projection(company, "deferred", category)}
            portals_failed += company.get("status") in ("failing", "broken", "needs_config")
            continue
        # A Workday stop mid-scan (throttle, error) is a failure. Detail work left for the
        # next visit by our own limits (request budget, cross-host circuit), a vanished
        # posting (404) or the pagination cap is not: the listing was read and new postings
        # went first, so - as before M7 - the scan stays active with a pending count
        # and the projection reports it as "degraded".
        stopped = managed and recovery.last_stop and recovery.last_stop.get("failure_category") not in ("incomplete", "not_found", "budget", "circuit")
        if stopped and scan.status == "active":
            scan.fail("failing", f"Workday {recovery.last_stop.get('failure_category') or 'stop'} during scan; pending work retained")
        jobs_found += len(scan.jobs)
        portals_failed += scan.status != "active"
        company["status"] = scan.status
        company["status_reason"] = scan.reason      # problems only ("" when healthy)
        streak = _failure_streak(scan, previous, was_failing, _now_iso())
        if streak and streak["failed_scans"] >= 2:
            company["status_reason"] += (f" — failing since {_streak_label(streak['failing_since'])} "
                                         f"({streak['failed_scans']} scans in a row)")
        company["scan_note"] = scan.user_note
        company["last_checked"] = _now_iso()          # this attempt, whatever its outcome
        if scan.status == "active":
            company["last_success"] = company["last_checked"]   # a failed scan keeps the older one
        company["scan"] = {**scan.summary(), **(streak or {}),
                           **_blocked_streak(previous, scan.status != "active", scope == "all", _now_iso())}
        if managed:
            outcome = "healthy" if scan.status == "active" else ("rate_limited" if recovery.last_stop.get("http_status") == 429 else "failed")
            company["scan"]["recovery"] = recovery.projection(company, outcome, attempted=True)
        company["source"] = source_label(company)
        # reflect what this scrape actually sees — a failed scan saw nothing, so it keeps the last one
        if scan.status == "active":
            company["last_job"] = scan.jobs[0]["title"] if scan.jobs else ""
        new_jobs += merge_new_jobs(seen + new_jobs, scan.jobs, company_id=company.get("id"), stats=merge_stats)
        time.sleep(1)

    if recovery:
        log.info("Detail-verdict cache (all sources): %d hits (job pages not re-read), %d misses (pages read)",
                 cache_totals["cache_hits"], cache_totals["cache_misses"])

    # This scan as a whole, recorded on every company it checked (inside the
    # "scan" field, which already travels merge-safely with each company's
    # results — no new file or field): when it started and finished, and the
    # eligible jobs it found across all portals (the same jobs as each
    # portal's "accepted", already-known ones included; "new_jobs" = first
    # seen in this scan). It only reaches GitHub once the whole scan is
    # done, so it always describes a completed scan. Read by the admin's
    # Settings → Scan details (scan_schedule.latest_run).
    run = {"started_at": started_at, "finished_at": _now_iso(), "jobs_found": jobs_found,
           "new_jobs": len(new_jobs), "portals": len(scanned), "portals_failed": portals_failed,
           "reposts_suppressed": merge_stats["reposts_suppressed"],
           "reposts_window_realerted": merge_stats["reposts_window_realerted"],
           "repost_dates_backfilled": merge_stats["repost_dates_backfilled"]}
    if recovery:
        run.update(recovery_version=1, run_id=recovery.run_id)
        if scope != "all":
            run["scope"] = scope
        recovery.save(started_at, run["finished_at"])
    for company in scanned:
        company["scan"]["run"] = dict(run)
    if recovery:
        from monitoring_health import report_actions, report_workday
        report_actions(companies)
        report_workday(recovery.mode, recovery.http.requests, len(recovery.selected), len(recovery.not_due),
                       len(recovery.http.throttled), sum(recovery.http.cooling(h) for h in recovery.state["hosts"]),
                       recovery.cluster_summary())

    # commit=False: alerts.py publishes the data (merge-safe) before and
    # after emailing — see alerts.py
    save_companies(companies, commit=False)
    # repost_last_seen stamps mutate existing records in place, so the file
    # must be written even when no new job was found (repo_sync merges them)
    if new_jobs or merge_stats["repost_stamps_updated"] or merge_stats["repost_dates_backfilled"]:
        save_seen_jobs(seen + new_jobs, commit=False)
    return new_jobs


if __name__ == "__main__":
    new = run_all(recovery=True, scope=os.environ.get("JT_SCAN_SCOPE", "").strip() or "all")
    print(f"[scraper] {len(new)} new job(s) found")
    if new:
        for j in new:
            print(f"  - {j['title']} @ {j['company']}")
