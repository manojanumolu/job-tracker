"""Canonical job identity, used to deduplicate alerts.

A company can have several open jobs with the same title (different cities,
teams or requisitions), so identity is the ATS job ID where one is visible in
the URL, otherwise the canonical URL. Title + location is only a secondary
repost check.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_PARAMS = re.compile(
    r"^(?:utm_\w+|gclid|fbclid|msclkid|mc_cid|mc_eid|_ga|igshid|trk|trkid|trackingid|tracking_id|"
    r"source|src|ref|referrer|refid|codes|sid|previouslocale|jobboard|feed)$",
    re.IGNORECASE,
)

# ATS job IDs recognisable from the posting URL
_ID_PATTERNS = [
    # Workday: .../job/Hyderabad/Some-Title_R2868429, _JR141427, _R2849882-1, PwC's _570925WD-1
    ("workday", re.compile(r"myworkdayjobs\.com/.*_([A-Za-z]{0,4}\d{4,}[A-Za-z]{0,4}(?:-\d+)?)(?:/apply)?/?$", re.IGNORECASE)),
    # Accenture: jobdetails?id=ATCI-5780951-S2070513_en
    ("accenture", re.compile(r"accenture\.com/.*jobdetails\?(?:.*&)?id=([^&#]+)", re.IGNORECASE)),
    # Flutter careers site: /jobs/jr141427/slug/  (the Workday JR id)
    ("jr", re.compile(r"/jobs/(jr\d+)(?:/|$)", re.IGNORECASE)),
    # Zoho Recruit: /jobs/Careers/190737000005689329/Slug
    ("zoho", re.compile(r"/jobs/careers/(\d{8,})(?:/|$)", re.IGNORECASE)),
    # Avature: /JobDetail/Slug/19919
    ("avature", re.compile(r"/jobdetail/[^/]+/(\d+)(?:/|$)", re.IGNORECASE)),
    # Jibe / iCIMS: /careers-home/jobs/17498 or icims.com/jobs/17498/
    ("jibe", re.compile(r"(?:/careers-home/jobs/|icims\.com/jobs/)(\d+)(?:[/?]|$)", re.IGNORECASE)),
    # Greenhouse / Lever / SmartRecruiters
    ("greenhouse", re.compile(r"greenhouse\.io/.*/jobs/(\d+)", re.IGNORECASE)),
    ("lever", re.compile(r"jobs\.lever\.co/[^/]+/([0-9a-f-]{36})", re.IGNORECASE)),
    ("smartrecruiters", re.compile(r"smartrecruiters\.com/[^/]+/(\d{6,})", re.IGNORECASE)),
]


def canonical_url(url: str) -> str:
    """Lower-cased scheme/host, no "www.", no fragment, no tracking
    parameters, sorted query, no trailing slash."""
    url = (url or "").strip()
    if not url:
        return ""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    query = sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                   if not _TRACKING_PARAMS.match(k))
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https" if parts.scheme in ("http", "https", "") else parts.scheme.lower(),
                       host, path, urlencode(query), ""))


def ats_job_id(url: str) -> str:
    """"workday:R2868429", "accenture:ATCI-5780951-S2070513_en", ... or ""."""
    for kind, pattern in _ID_PATTERNS:
        m = pattern.search(url or "")
        if m:
            value = m.group(1)
            return f"{kind}:{value.upper() if kind in ('workday', 'jr') else value}"
    return ""


def job_uid(company: str, url: str) -> str:
    """Stable identity of a job posting within a company."""
    company = (company or "").strip().lower()
    ident = ats_job_id(url)
    if ident:
        # the Flutter careers site shows the Workday JR id — same job either way
        ident = ident.replace("workday:JR", "jr:JR").replace("jr:JR", "id:JR")
        return f"{company}|{ident}"
    return f"{company}|url:{canonical_url(url)}"


def same_page(a: str, b: str) -> bool:
    """True when two URLs point at the same page (ignoring fragment,
    tracking parameters, trailing slash, scheme and "www.")."""
    return canonical_url(a) == canonical_url(b)
