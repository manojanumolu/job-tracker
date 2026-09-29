"""Job sources: how each company's real postings are discovered.

Every adapter returns ``Listing`` objects for India postings. An adapter
either fills ``detail`` itself (its API already returns the full posting) or
provides ``fetch_detail`` for the scraper to call within its budget.

A failure to read the job list raises ``SourceError`` — the company is then
reported as broken/failing, never as healthy with zero jobs.

Endpoints were verified against the live sites (Sep 2026); none is guessed.
"""

from __future__ import annotations

import html as html_lib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlparse

import httpx

from job_classifier import normalize_title
from locations import is_india

log = logging.getLogger("scraper")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; FresherJobTracker/1.0)",
    "Accept": "application/json, text/html, */*",
}

_BLOCK_TAG_RE = re.compile(r"<\s*(?:br|/p|/li|/div|/h[1-6]|/tr|li)\b[^>]*>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
# shorter than this is an empty/placeholder description ("", "TBD", "See PDF")
_MIN_DESCRIPTION_CHARS = 20


def html_to_text(html: str) -> str:
    """Strip tags but keep block boundaries as newlines, so the classifier can
    tell bullet points / sentences apart."""
    text = _BLOCK_TAG_RE.sub("\n", html or "")
    text = html_lib.unescape(_TAG_RE.sub(" ", text))
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


@dataclass
class Detail:
    """What a job's own page/record says. ``ok`` is False when nothing
    trustworthy could be read — the scraper then never accepts the job."""
    ok: bool
    description: str = ""
    location: str = ""
    strict_text: str = ""          # reject-only evidence (fields outside the description)
    posting_evidence: bool = False  # a real JobPosting (ATS record / schema.org)
    reason: str = ""               # why it is not ok

    @classmethod
    def unreadable(cls, reason: str) -> "Detail":
        return cls(ok=False, reason=reason)


@dataclass
class Listing:
    title: str
    url: str
    location: str = ""
    card_text: str = ""
    job_id: str = ""
    posting_evidence: bool = False   # the listing itself comes from an ATS
    detail: Detail | None = None
    fetch_detail: Callable[[], Detail] | None = field(default=None, repr=False)


class SourceError(Exception):
    """The job list itself could not be read."""
    def __init__(self, message: str, status: str = "broken"):
        super().__init__(message)
        self.status = status


@dataclass
class SourceResult:
    listings: list[Listing]
    seen: int = 0          # postings looked at before the India filter
    note: str = ""


def _get_json(client: httpx.Client, method: str, url: str, **kw):
    try:
        r = client.request(method, url, **kw)
    except httpx.TimeoutException as e:
        raise SourceError(f"timeout reading {urlparse(url).netloc}", "failing") from e
    except httpx.HTTPError as e:
        raise SourceError(f"network error reading {urlparse(url).netloc}: {type(e).__name__}", "failing") from e
    if r.status_code in (429, 500, 502, 503, 504):
        raise SourceError(f"HTTP {r.status_code} from {urlparse(url).netloc}", "failing")
    if r.status_code >= 400:
        raise SourceError(f"HTTP {r.status_code} from {urlparse(url).netloc}")
    try:
        return r.json()
    except ValueError as e:
        blocked = "Just a moment" in r.text[:2000] or "challenge" in r.text[:2000].lower()
        raise SourceError("bot challenge instead of data" if blocked else "response was not JSON") from e


# ---------------------------------------------------------------------------
# Workday (cxs JSON API) — Sanofi, PwC, Flutter International
# ---------------------------------------------------------------------------

WORKDAY_PAGE = 20              # the cxs API's maximum page size
MAX_WORKDAY_POSTINGS = 2000    # safety cap per site
_WORKDAY_MULTI_LOC_RE = re.compile(r"^\s*\d+\s+locations?\s*$", re.IGNORECASE)
_COUNTRY_FACET_RE = re.compile(r"country", re.IGNORECASE)


def workday_urls(api: str) -> tuple[str, str]:
    """(public job base, cxs base) for a .../wday/cxs/{tenant}/{site}/jobs URL."""
    parsed = urlparse(api)
    site = parsed.path.rstrip("/").split("/")[-2]
    return f"{parsed.scheme}://{parsed.netloc}/{site}", api.rstrip("/").rsplit("/", 1)[0]


def workday_api_from_url(url: str) -> str:
    """https://x.wd3.myworkdayjobs.com/en-US/Site -> the site's cxs jobs API ("" if not Workday)."""
    p = urlparse(url or "")
    if not p.netloc.endswith("myworkdayjobs.com"):
        return ""
    tenant = p.netloc.split(".")[0]
    segs = [s for s in p.path.split("/") if s and not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", s)]
    if not segs or segs[0] == "wday":
        return url if "/wday/cxs/" in url else ""
    return f"{p.scheme}://{p.netloc}/wday/cxs/{tenant}/{segs[0]}/jobs"


def _india_facets(facets: list) -> dict[str, list[str]]:
    """Facet values that select India: a country facet's "India", otherwise
    every location facet value that is an Indian place ("Bengaluru Millenia")."""
    country: dict[str, list[str]] = {}
    places: dict[str, list[str]] = {}

    def walk(values, param):
        for v in values or []:
            if not isinstance(v, dict):
                continue
            if "values" in v:
                walk(v["values"], v.get("facetParameter") or param)
                continue
            desc = str(v.get("descriptor") or "")
            if not v.get("id") or not is_india(desc):
                continue
            if _COUNTRY_FACET_RE.search(param) and re.fullmatch(r"\s*india\s*", desc, re.IGNORECASE):
                country.setdefault(param, []).append(v["id"])
            elif not _COUNTRY_FACET_RE.search(param):
                places.setdefault(param, []).append(v["id"])

    for f in facets or []:
        if isinstance(f, dict):
            walk(f.get("values"), f.get("facetParameter") or "")
    if country:
        return country
    # one location parameter only — combining parameters would AND them
    if places:
        param = max(places, key=lambda k: len(places[k]))
        return {param: places[param]}
    return {}


def workday_detail(client: httpx.Client, url: str, title: str) -> Detail:
    try:
        r = client.get(url)
    except httpx.HTTPError as e:
        return Detail.unreadable(f"{type(e).__name__}")
    if r.status_code != 200:
        return Detail.unreadable(f"HTTP {r.status_code}")
    try:
        info = r.json().get("jobPostingInfo") or {}
    except (ValueError, AttributeError):
        return Detail.unreadable("detail was not JSON (bot challenge?)")
    if not isinstance(info, dict) or not info:
        return Detail.unreadable("no jobPostingInfo")
    # the detail is fetched by the listing's own path; a title that disagrees
    # means Workday served a different posting
    got = info.get("title")
    if got and normalize_title(str(got)) != normalize_title(title):
        return Detail.unreadable(f"detail is for a different job ({got!r})")
    description = html_to_text(str(info.get("jobDescription") or ""))
    if len(description) < _MIN_DESCRIPTION_CHARS:
        return Detail.unreadable("empty job description")
    locations = [info.get("location")] + list(info.get("additionalLocations") or [])
    country = info.get("country")
    if isinstance(country, dict):
        country = country.get("descriptor")
    location = " | ".join(str(x) for x in locations + [country] if x)
    return Detail(ok=True, description=description, location=location, posting_evidence=True)


def workday_listings(client: httpx.Client, api: str) -> SourceResult:
    base, cxs_base = workday_urls(api)
    first = _get_json(client, "POST", api, json={"limit": WORKDAY_PAGE, "offset": 0, "searchText": "", "appliedFacets": {}})
    if not isinstance(first, dict):
        raise SourceError("unexpected Workday response")
    applied = _india_facets(first.get("facets") or [])
    if applied:
        page = _get_json(client, "POST", api, json={"limit": WORKDAY_PAGE, "offset": 0, "searchText": "", "appliedFacets": applied})
    else:
        page = first
    total = page.get("total")
    postings: list = []
    offset = 0
    while True:
        batch = [p for p in (page.get("jobPostings") or []) if isinstance(p, dict)]
        postings += batch
        offset += WORKDAY_PAGE
        if len(batch) < WORKDAY_PAGE or offset >= MAX_WORKDAY_POSTINGS or (isinstance(total, int) and offset >= total):
            break
        page = _get_json(client, "POST", api, json={"limit": WORKDAY_PAGE, "offset": offset, "searchText": "",
                                                     "appliedFacets": applied})
    listings = []
    for p in postings:
        title = str(p.get("title") or "").strip()
        ext = str(p.get("externalPath") or "")
        loc = str(p.get("locationsText") or "")
        if not title or not ext:
            continue
        multi = bool(_WORKDAY_MULTI_LOC_RE.match(loc))
        # without an India facet, keep India and "N Locations" postings only
        if not applied and loc and not multi and not is_india(loc):
            continue
        detail_url = cxs_base + ext
        listings.append(Listing(
            title=title, url=base + ext, location="" if multi else loc, posting_evidence=True,
            fetch_detail=lambda u=detail_url, t=title: workday_detail(client, u, t),
        ))
    note = f"India facet {sorted(applied)}" if applied else "no India facet; filtered by location text"
    return SourceResult(listings, seen=len(postings), note=note)


# ---------------------------------------------------------------------------
# Accenture (findjobs search API)
# ---------------------------------------------------------------------------

ACCENTURE_API = "https://www.accenture.com/api/accenture/elastic/findjobs"
ACCENTURE_PAGE = 100
ACCENTURE_MAX = 300     # newest 300 in the 0-2 years bucket ≈ 6 days of postings
# Accenture's own experience buckets; only 0-2 years can be a fresher role
ACCENTURE_BUCKET = "Experience: 0-2 years"


def accenture_listings(client: httpx.Client, cfg: dict) -> SourceResult:
    site = cfg.get("site", "in-en")
    listings: list[Listing] = []
    seen = 0
    for start in range(0, cfg.get("max", ACCENTURE_MAX), ACCENTURE_PAGE):
        form = {
            "startIndex": str(start), "maxResultSize": str(ACCENTURE_PAGE), "jobKeyword": "",
            "jobCountry": cfg.get("country", "India"), "jobLanguage": "en", "countrySite": site,
            "sortBy": "2",  # newest first
            "searchType": "vectorSearch", "enableQueryBoost": "true", "minScore": "0.6",
            "getFeedbackJudgmentEnabled": "true", "useCleanEmbedding": "true", "score": "true",
            "totalHits": "true", "debugQuery": "false",
            "jobFilters": json.dumps([{"fieldName": "yearsOfExperience.keyword", "items": [ACCENTURE_BUCKET]}]),
        }
        data = _get_json(client, "POST", ACCENTURE_API, files={k: (None, v) for k, v in form.items()},
                         headers={"Referer": f"https://www.accenture.com/{site}/careers/jobsearch"})
        rows = data.get("data") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise SourceError("Accenture search API changed shape (no 'data' list)")
        if start == 0 and not rows:
            # a wrong/changed filter silently returns nothing
            raise SourceError("Accenture search returned no postings — filter or API may have changed", "failing")
        for row in rows:
            if not isinstance(row, dict):
                continue
            seen += 1
            listing = accenture_listing(row, site)
            if listing:
                listings.append(listing)
        if len(rows) < ACCENTURE_PAGE:
            break
    return SourceResult(listings, seen=seen, note=f"{ACCENTURE_BUCKET!r} bucket, newest {seen}")


def accenture_listing(row: dict, site: str = "in-en") -> Listing | None:
    title = str(row.get("title") or "").strip()
    url_t = str(row.get("jobDetailUrl") or "")
    if not title or "{0}" not in url_t and not url_t.startswith("http"):
        return None
    url = url_t.replace("{0}", site)
    places = row.get("location") if isinstance(row.get("location"), list) else [row.get("location")]
    location = " | ".join([str(p) for p in places if p] + ([str(row["country"])] if row.get("country") else []))
    parts = [html_to_text(str(row.get("jobDescription") or ""))]
    qual = html_to_text(str(row.get("qualification") or ""))
    if qual and qual.strip().upper() != "TBD":
        parts.append(qual)
    description = "\n".join(p for p in parts if p)
    # structured level/experience fields: evidence that can only reject
    strict = "\n".join(x for x in (
        f"Career level: {row['careerLevel']}" if row.get("careerLevel") else "",
        f"Career level: {row['jobProfile']}" if row.get("jobProfile") else "",
        str(row.get("yearsOfExperience") or ""),
    ) if x)
    detail = (Detail(ok=True, description=description, location=location, strict_text=strict, posting_evidence=True)
              if len(description) >= _MIN_DESCRIPTION_CHARS else Detail.unreadable("empty job description"))
    return Listing(title=title, url=url, location=location, job_id=str(row.get("requisitionId") or ""),
                   posting_evidence=True, detail=detail)


# ---------------------------------------------------------------------------
# Jibe (iCIMS career sites) — Avalara
# ---------------------------------------------------------------------------

JIBE_MAX_PAGES = 60


def jibe_listings(client: httpx.Client, cfg: dict) -> SourceResult:
    host = cfg["host"].rstrip("/")
    listings, seen, total = [], 0, None
    for page in range(1, JIBE_MAX_PAGES + 1):
        data = _get_json(client, "GET", f"{host}/api/jobs",
                         params={"page": page, "sortBy": "posted_date", "descending": "true", "internal": "false"})
        jobs = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(jobs, list):
            raise SourceError("job API changed shape (no 'jobs' list)")
        total = data.get("totalCount", total)
        if page == 1 and not jobs:
            raise SourceError("job API returned no postings", "failing")
        for j in jobs:
            d = j.get("data") if isinstance(j, dict) else None
            if not isinstance(d, dict):
                continue
            seen += 1
            location = " | ".join(str(x) for x in (d.get("full_location"), d.get("country")) if x)
            if not (d.get("country") == "India" or is_india(location)):
                continue
            title = str(d.get("title") or "").strip()
            slug = str(d.get("slug") or d.get("req_id") or "")
            if not title or not slug:
                continue
            description = "\n".join(html_to_text(str(d.get(k) or "")) for k in ("description", "responsibilities", "qualifications"))
            detail = (Detail(ok=True, description=description.strip(), location=location, posting_evidence=True)
                      if len(description.strip()) >= _MIN_DESCRIPTION_CHARS else Detail.unreadable("empty job description"))
            listings.append(Listing(title=title, url=f"{host}/careers-home/jobs/{slug}?lang=en-us",
                                    location=location, job_id=str(d.get("req_id") or ""),
                                    posting_evidence=True, detail=detail))
        if not jobs or (isinstance(total, int) and seen >= total):
            break
    return SourceResult(listings, seen=seen)


# ---------------------------------------------------------------------------
# Avature (server-rendered search + JSON-LD detail) — MetLife
# ---------------------------------------------------------------------------

AVATURE_MAX_PAGES = 250
_AVATURE_ARTICLE_RE = re.compile(r'<article class="article article--result".*?</article>', re.S)
_AVATURE_COUNT_RE = re.compile(r"(\d[\d,]*)\s*results")
_JSONLD_BLOCK_RE = re.compile(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', re.S | re.IGNORECASE)


def _is_jobposting_type(t) -> bool:
    types = t if isinstance(t, list) else [t]
    return any(isinstance(x, str) and x.rsplit("/", 1)[-1].rsplit(":", 1)[-1] == "JobPosting" for x in types)


def jsonld_postings(page_html: str) -> list[dict]:
    out: list[dict] = []

    def visit(o):
        if isinstance(o, list):
            for x in o:
                visit(x)
        elif isinstance(o, dict):
            if _is_jobposting_type(o.get("@type")):
                out.append(o)
            if "@graph" in o:
                visit(o["@graph"])

    for block in _JSONLD_BLOCK_RE.findall(page_html or ""):
        try:
            visit(json.loads(block))
        except ValueError:
            continue
    return out


_AVATURE_FIELD_SPLIT_RE = re.compile(r'<div class="article__content__view__field(?:\s[^"]*)?"\s*>')
_AVATURE_LABEL_RE = re.compile(r'field__label"\s*>(.*?)</div>', re.S)


def avature_fields(page_html: str) -> list[tuple[str, str]]:
    """(label, text) of each field block on an Avature job page. MetLife's
    JSON-LD carries only the title, so the description lives here."""
    out = []
    for seg in _AVATURE_FIELD_SPLIT_RE.split(page_html or "")[1:]:
        seg = seg.split("</article>", 1)[0]
        m = _AVATURE_LABEL_RE.search(seg)
        label = html_to_text(m.group(1)) if m else ""
        body = seg[m.end():] if m else seg
        text = html_to_text(body)
        if text:
            out.append((label, text))
    return out


def avature_detail(client: httpx.Client, url: str, title: str, job_id: str) -> Detail:
    from identity import same_page
    from scraper import _pick_posting
    try:
        r = client.get(url)
    except httpx.HTTPError as e:
        return Detail.unreadable(type(e).__name__)
    if r.status_code != 200:
        return Detail.unreadable(f"HTTP {r.status_code}")
    if not same_page(str(r.url), url):
        return Detail.unreadable(f"redirected to {r.url}")
    # the page must be this job: its JobPosting title matches the listing, or
    # the page title names the same job ID / title (MetLife keeps a stale
    # JobPosting title after a rename: "Actuarial Analyst" vs "Actuarial
    # Specialist - Noida, India - 20761")
    if _pick_posting(jsonld_postings(r.text), title, job_id) is None:
        m = re.search(r"<title>(.*?)</title>", r.text, re.S | re.IGNORECASE)
        page_title = html_lib.unescape(m.group(1)) if m else ""
        id_match = bool(job_id) and re.search(rf"(?<!\d){re.escape(job_id)}(?!\d)", page_title)
        if not id_match and normalize_title(title) not in normalize_title(page_title):
            return Detail.unreadable("page is not this job (JobPosting title/ID mismatch)")
    fields = avature_fields(r.text)
    location = ""
    parts = []
    for label, text in fields:
        if label.lower() == "location" or (not label and text.lower().startswith("location ")):
            location = re.sub(r"^location\s+", "", text, flags=re.IGNORECASE)
        elif label:
            parts.append(f"{label}: {text}")
        else:
            parts.append(text)
    description = "\n".join(parts)
    if len(description) < _MIN_DESCRIPTION_CHARS:
        return Detail.unreadable("posting has no description")
    return Detail(ok=True, description=description, location=location, posting_evidence=True)


def avature_listings(client: httpx.Client, cfg: dict) -> SourceResult:
    search = cfg["search"].rstrip("/")
    listings, seen, total, offset = [], 0, None, 0
    for _ in range(AVATURE_MAX_PAGES):
        try:
            r = client.get(f"{search}/?jobOffset={offset}")
        except httpx.HTTPError as e:
            raise SourceError(f"network error reading job search: {type(e).__name__}", "failing") from e
        if r.status_code >= 400:
            raise SourceError(f"HTTP {r.status_code} from job search")
        articles = _AVATURE_ARTICLE_RE.findall(r.text)
        if total is None:
            m = _AVATURE_COUNT_RE.search(r.text)
            total = int(m.group(1).replace(",", "")) if m else None
            if not articles:
                raise SourceError("job search page has no results — layout may have changed", "failing")
        for a in articles:
            seen += 1
            href = re.search(r'href="([^"]*JobDetail[^"]*)"', a)
            name = re.search(r'<a class="link"[^>]*>(.*?)</a>', a, re.S)
            loc = re.search(r'list-item-location">(.*?)</span>', a, re.S)
            ref = re.search(r"Job ID:\s*(\d+)", a)
            if not href or not name:
                continue
            title = html_lib.unescape(_TAG_RE.sub("", name.group(1))).strip()
            location = html_lib.unescape(_TAG_RE.sub(" ", loc.group(1))).strip() if loc else ""
            if not is_india(location):
                continue
            url, job_id = html_lib.unescape(href.group(1)), ref.group(1) if ref else ""
            listings.append(Listing(title=title, url=url, location=location, job_id=job_id, posting_evidence=True,
                                    fetch_detail=lambda u=url, t=title, i=job_id: avature_detail(client, u, t, i)))
        offset += len(articles)
        if not articles or (total is not None and offset >= total):
            break
        time.sleep(cfg.get("delay", 0.3))
    return SourceResult(listings, seen=seen)


# ---------------------------------------------------------------------------
# Zoho Recruit career site (job list embedded in the page) — NPCI
# ---------------------------------------------------------------------------

_ZOHO_JOBS_RE = re.compile(r'<input type="hidden" value="(\[[^"]*)" id="jobs"')


def zoho_listings(client: httpx.Client, cfg: dict) -> SourceResult:
    page_url = cfg["page"]
    try:
        r = client.get(page_url)
    except httpx.HTTPError as e:
        raise SourceError(f"network error reading career site: {type(e).__name__}", "failing") from e
    if r.status_code >= 400:
        raise SourceError(f"HTTP {r.status_code} from career site")
    m = _ZOHO_JOBS_RE.search(r.text)
    if not m:
        raise SourceError("job data not found on the career site — layout may have changed")
    try:
        jobs = json.loads(html_lib.unescape(m.group(1)))
    except ValueError as e:
        raise SourceError("embedded job data is not valid JSON") from e
    base = page_url.rstrip("/")
    listings = []
    for j in jobs if isinstance(jobs, list) else []:
        if not isinstance(j, dict) or j.get("Publish") is False:
            continue
        title = str(j.get("Posting_Title") or j.get("Job_Opening_Name") or "").strip()
        jid = str(j.get("id") or "")
        if not title or not jid:
            continue
        location = ", ".join(str(j[k]) for k in ("City", "State", "Country") if j.get(k))
        if j.get("Remote_Job") and not location:
            location = cfg.get("country", "")
        description = html_to_text(str(j.get("Job_Description") or ""))
        slug = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-")
        detail = (Detail(ok=True, description=description, location=location, posting_evidence=True)
                  if len(description) >= _MIN_DESCRIPTION_CHARS else Detail.unreadable("empty job description"))
        listings.append(Listing(title=title, url=f"{base}/{jid}/{slug}", location=location, job_id=jid,
                                posting_evidence=True, detail=detail))
    return SourceResult(listings, seen=len(listings))


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def read_source(client: httpx.Client, cfg: dict) -> SourceResult:
    kind = cfg.get("type")
    if kind == "workday":
        return workday_listings(client, cfg["api"])
    if kind == "accenture":
        return accenture_listings(client, cfg)
    if kind == "jibe":
        return jibe_listings(client, cfg)
    if kind == "avature":
        return avature_listings(client, cfg)
    if kind == "zoho":
        return zoho_listings(client, cfg)
    raise SourceError(f"unknown source type {kind!r}", "needs_config")
