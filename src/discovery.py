"""Explore beyond your preferences: the Home page's second job list.

It surfaces jobs the scanner ALREADY found and qualified (fresher or entry
level, in India, every safety check passed) that sit outside the viewer's
primary preferences (the locations and job families their alerts are
tailored to) but are still technical roles. Nothing here finds new jobs,
reads another site or sends anything: it only sorts the existing records.
The alert emails never use this module.

Categories are read from the job title, the same way job_filters reads job
families. A title naming a non-technical function (sales, finance, HR,
operations, life science) only counts when it also names engineering or
software work, so "Operations Finance Analyst" is left out while "Sales
Data Engineer" stays. A bare "Analyst" is not a data role.

No Streamlit import here, so this is unit-testable on its own.
"""

from __future__ import annotations

import re

import job_filters

# discovery category -> words in a job title (ordered: the first match is
# the label a card shows, so the most specific categories come first)
_CATEGORY_WORDS = {
    "VLSI & Semiconductor Engineering": r"vlsi|asic|fpga|rtl|soc design|design verification|formal verification|"
                                        r"verification engineer|physical design|dft|analog (?:design|layout)|"
                                        r"layout engineer|semiconductor|silicon|chip design|post[ -]?silicon",
    "Embedded Systems & Firmware": r"embedded|firmware|rtos|micro ?controllers?|bsp|device drivers?|iot|"
                                   r"hardware engineer|electronics engineer",
    "AI & Machine Learning": r"machine learning|ml|ai|artificial intelligence|deep learning|computer vision|nlp|"
                             r"data scientist|data science|gen ?ai|llm",
    "Cybersecurity": job_filters.JOB_FAMILIES["Cybersecurity"],
    "Testing & Quality Assurance": job_filters.JOB_FAMILIES["Testing & QA"],
    "Cloud & DevOps": job_filters.JOB_FAMILIES["Cloud & DevOps"],
    "Data Engineering & Analytics": job_filters.JOB_FAMILIES["Data & analytics"],
    "IT Support & Infrastructure": job_filters.JOB_FAMILIES["IT support & infrastructure"],
    "Software Engineering": job_filters.JOB_FAMILIES["Software engineering"],
    "Other Graduate Engineering Roles": r"engineer|engineering|engg|technologist|r&d",
}
CATEGORIES = tuple(_CATEGORY_WORDS)
_CATEGORY_RE = {name: re.compile(rf"(?<![a-z0-9])(?:{words})(?![a-z0-9])", re.IGNORECASE)
                for name, words in _CATEGORY_WORDS.items()}

# the job_filters families that name a non-technical function, and the words
# that still make such a title an engineering / software role
_NON_TECH_FAMILIES = ("Sales & business development", "Finance & accounting", "HR & recruiting",
                      "Operations & customer service", "Science & healthcare")
_STRONG_TECH = re.compile(r"(?<![a-z0-9])(?:engineer|engineering|developer|software|programmer|data engineer|"
                          r"data scientist|firmware|embedded|vlsi|asic|fpga)(?![a-z0-9])", re.IGNORECASE)
# the categories that are wider than the three usual tech families
_BROADER = {"VLSI & Semiconductor Engineering", "Embedded Systems & Firmware", "Other Graduate Engineering Roles"}

LOCATIONS = tuple(job_filters.LOCATIONS)    # Hyderabad, Bengaluru, Pune, ... Remote


def _title(job: dict) -> str:
    return job.get("title") if isinstance(job.get("title"), str) else ""


def categories(job: dict) -> list[str]:
    """Every discovery category the title names, most specific first; []
    for a non-technical role."""
    title = _title(job)
    found = [name for name, rx in _CATEGORY_RE.items() if rx.search(title)]
    if found and job_filters.job_families(job) & set(_NON_TECH_FAMILIES) and not _STRONG_TECH.search(title):
        return []
    return found


def category(job: dict) -> str:
    """The one category a card shows ("" for a non-technical role)."""
    found = categories(job)
    return found[0] if found else ""


def is_eligible(job: dict) -> bool:
    """Qualified by the scanner exactly as an alert would be (category and
    every safety check), and a technical role."""
    return job_filters.passes_global_gate(job) and bool(categories(job))


def has_preferences(prefs: dict | None) -> bool:
    p = prefs if isinstance(prefs, dict) else {}
    return bool(p.get("locations") or p.get("job_families"))


def is_tailored(job: dict, prefs: dict | None) -> bool:
    """Inside the primary preferences: the same test as the tailored alert
    emails (job_filters.matches_tailoring)."""
    return job_filters.matches_tailoring(job, prefs)


def reasons(job: dict, prefs: dict | None) -> list[str]:
    """Why a job sits outside the primary preferences (short labels)."""
    p = prefs if isinstance(prefs, dict) else {}
    out = []
    locs = {v for v in p.get("locations") or () if v in job_filters.LOCATIONS}
    fams = {v for v in p.get("job_families") or () if v in job_filters.JOB_FAMILIES}
    if locs and not locs & job_filters.job_location_choices(job):
        out.append("Different location")
    if fams and not fams & job_filters.job_families(job):
        out.append("Different job family")
    if category(job) in _BROADER and "Different job family" in out:
        out[out.index("Different job family")] = "Broader technical opportunity"
    return out


def beyond(jobs: list[dict], prefs: dict | None) -> list[dict]:
    """The explore list: eligible technical jobs outside the primary
    preferences, in the given order. Never a job the tailored list shows,
    and nothing at all without preferences to go beyond."""
    if not has_preferences(prefs):
        return []
    return [j for j in jobs if isinstance(j, dict) and not is_tailored(j, prefs) and is_eligible(j)]


def location_choices(job: dict) -> list[str]:
    """The discovery locations a job is in, in LOCATIONS order."""
    found = job_filters.job_location_choices(job)
    return [loc for loc in LOCATIONS if loc in found]

