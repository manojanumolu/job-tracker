"""Personal job filters — applied AFTER the shared eligibility gate.

The scanner's classifier decides which jobs qualify at all (fresher or
entry level, in India, every safety check passed). Nothing here can let in
a job the gate rejected; it only narrows the qualifying jobs down to what
one person asked for:

  General   qualifying jobs from the companies the person monitors
  Tailored  the same, also matching their job families, locations and
            experience levels (an empty choice means "any")

Company scope: all tracked companies, or only the companies they follow
(IDs from the shared catalogue). A new person follows nothing yet: their
General alerts start empty, while Tailored alerts and their job lists use
their preferences across every company until they choose some.

Job type and work mode are saved preferences only. The scanner doesn't
extract either from postings (no employment-type field; no posting says
remote or hybrid), so nothing is matched on them — they are kept for when
the data exists. Matching uses only what the scanner actually reads:
location text, title (job family), the classifier's category (experience)
and the company.

No Streamlit import here, so this is unit-testable on its own.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from config_store import ALERT_CATEGORIES

# experience level -> the classifier's category
EXPERIENCE = {"fresher": "FRESHER", "entry_level": "ENTRY_LEVEL"}
EXPERIENCE_LABELS = {"fresher": "Fresher", "entry_level": "Entry level"}

# location choice -> words that name it in a job's location text
LOCATIONS = {
    "Bengaluru": ("bengaluru", "bangalore", "whitefield", "electronic city"),
    "Hyderabad": ("hyderabad", "secunderabad", "gachibowli", "hitec city", "hitech city"),
    "Pune": ("pune",),
    "Chennai": ("chennai", "sriperumbudur", "chengalpattu"),
    "Mumbai": ("mumbai", "navi mumbai", "thane"),
    "Delhi NCR": ("delhi", "new delhi", "delhi ncr", "ncr", "gurgaon", "gurugram", "noida", "greater noida",
                  "faridabad", "ghaziabad", "manesar"),
    "Kolkata": ("kolkata",),
    "Ahmedabad": ("ahmedabad", "gandhinagar"),
    "Kochi": ("kochi", "cochin"),
    "Coimbatore": ("coimbatore",),
    "Remote": ("remote", "work from home", "wfh"),
}

# job family -> words in a job title
JOB_FAMILIES = {
    "Software engineering": r"software|developer|programmer|sde|full[ -]?stack|front[ -]?end|back[ -]?end|web|mobile|"
                            r"android|ios|java|python|devops|cloud|application|apptech|salesforce|sap",
    "Data & analytics": r"data|analytics?|analyst|machine learning|ml|ai|artificial intelligence|business intelligence|"
                        r"bi|statistics?|statistician",
    "Testing & QA": r"test|tester|testing|qa|quality assurance|automation",
    "IT support & infrastructure": r"support|infra|infrastructure|helpdesk|help desk|service desk|network|networking|"
                                   r"system administrator|sysadmin|it operations",
    "Sales & business development": r"sales|business development|bde|account executive|pre-?sales",
    "Finance & accounting": r"finance|financial|accounting|accountant|accounts|audit|auditor|tax|treasury|payroll",
    "HR & recruiting": r"hr|human resources?|recruiter|recruiting|recruitment|talent acquisition",
    "Design": r"design|designer|ui|ux",
    "Science & healthcare": r"scientific|science|scientist|research|researcher|clinical|lab|laboratory|pharma|"
                            r"pharmaceutical|medical|healthcare|biology|chemist|chemistry",
    "Operations & customer service": r"operations|customer service|customer support|process associate|back office",
}
_FAMILY_RE = {name: re.compile(rf"(?<![a-z0-9])(?:{words})(?![a-z0-9])", re.IGNORECASE)
              for name, words in JOB_FAMILIES.items()}
_LOCATION_RE = {name: re.compile(r"(?<![a-z])(?:" + "|".join(re.escape(w) for w in words) + r")(?![a-z])", re.IGNORECASE)
                for name, words in LOCATIONS.items()}

# saved for future matching only (see the module note)
JOB_TYPES = {"full_time": "Full-time", "internship": "Internship", "contract": "Contract",
             "apprenticeship": "Apprenticeship"}
WORK_MODES = {"onsite": "On-site", "hybrid": "Hybrid", "remote": "Remote"}

VOCABULARY = {"job_families": tuple(JOB_FAMILIES), "locations": tuple(LOCATIONS), "experience": tuple(EXPERIENCE),
              "job_types": tuple(JOB_TYPES), "work_modes": tuple(WORK_MODES)}


def passes_global_gate(job: dict) -> bool:
    """The shared eligibility gate, exactly as for today's alert emails:
    a qualifying category and every safety-gate check passed. Who was
    emailed or what the admin dismissed is NOT part of it — that is
    per-person state."""
    if not isinstance(job, dict):
        return False
    evidence = job.get("evidence")
    checks = evidence.get("checks") if isinstance(evidence, dict) else None
    return (job.get("category") in ALERT_CATEGORIES and isinstance(checks, dict) and bool(checks)
            and all(checks.values()))


def job_families(job: dict) -> set[str]:
    title = job.get("title") if isinstance(job.get("title"), str) else ""
    return {name for name, rx in _FAMILY_RE.items() if rx.search(title)}


def job_location_choices(job: dict) -> set[str]:
    text = job.get("location") if isinstance(job.get("location"), str) else ""
    return {name for name, rx in _LOCATION_RE.items() if rx.search(text)}


def in_scope(job: dict, watch_all: bool, watchlist: Iterable[str]) -> bool:
    """From a company this person monitors?"""
    return watch_all or (isinstance(job.get("company_id"), str) and job["company_id"] in set(watchlist))


def matches_preferences(job: dict, prefs: dict) -> bool:
    """Tailored mode: every dimension the person restricted must match."""
    fams = set(prefs.get("job_families") or ())
    locs = set(prefs.get("locations") or ())
    exp = set(prefs.get("experience") or ())
    if fams and not fams & job_families(job):
        return False
    if locs and not locs & job_location_choices(job):
        return False
    if exp and job.get("category") not in {EXPERIENCE[e] for e in exp if e in EXPERIENCE}:
        return False
    return True


def for_person(job: dict, *, mode: str, prefs: dict, watch_all: bool, watchlist: Iterable[str]) -> bool:
    """General: in the person's company scope. Tailored: also matching
    their preferences. (The global gate is checked separately.)
    With no companies chosen yet (and not "all companies"), General has
    nothing to cover, while Tailored follows the preferences everywhere."""
    watchlist = set(watchlist)
    if not watch_all and not watchlist:
        return mode == "tailored" and matches_preferences(job, prefs)
    if not in_scope(job, watch_all, watchlist):
        return False
    return mode != "tailored" or matches_preferences(job, prefs)


def matches_view(job: dict, *, locations=(), families=(), experience=(), companies=None) -> bool:
    """The job lists' filters (preferences are the defaults; the viewer can
    change them while browsing): empty means "any"; ``companies`` None
    means every company."""
    if companies is not None and job.get("company_id") not in set(companies):
        return False
    return matches_preferences(job, {"locations": list(locations), "job_families": list(families),
                                     "experience": list(experience)})
