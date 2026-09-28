"""Fresher / entry-level job classification.

Pure text logic with no network access, so it can be unit tested in isolation.
The scraper gathers whatever job information a site exposes (title, location,
card text, detail-page description) and asks ``classify_job`` for a verdict.

Only FRESHER and ENTRY_LEVEL results are alerted on. When the available text
gives no real evidence either way the result is UNKNOWN, which is *not*
alerted: a missed ambiguous posting is cheaper than an irrelevant
experienced-job alert.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Category(str, Enum):
    FRESHER = "FRESHER"
    ENTRY_LEVEL = "ENTRY_LEVEL"
    EXPERIENCED = "EXPERIENCED"
    SENIOR = "SENIOR"
    NOT_A_JOB = "NOT_A_JOB"
    UNKNOWN = "UNKNOWN"


ALERT_CATEGORIES = frozenset({Category.FRESHER, Category.ENTRY_LEVEL})


@dataclass(frozen=True)
class Classification:
    category: Category
    reason: str

    @property
    def accepted(self) -> bool:
        return self.category in ALERT_CATEGORIES

    def __str__(self) -> str:
        verdict = "ACCEPTED" if self.accepted else "REJECTED"
        return f"{verdict} [{self.category.value}] — {self.reason}"


@dataclass(frozen=True)
class ExperienceRequirement:
    min_years: float
    max_years: float | None
    text: str


# ---------------------------------------------------------------------------
# Text normalisation
# ---------------------------------------------------------------------------

# hyphen-like characters used by career sites: ‐ ‑ ‒ – — ― −
_DASHES_RE = re.compile("[‐‑‒–—―−]")
_WS_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    text = _DASHES_RE.sub("-", text or "")
    text = text.replace("’", "'").replace(" ", " ")
    return _WS_RE.sub(" ", text).strip()


# ---------------------------------------------------------------------------
# Not-a-job detection (employee spotlights, blog posts)
# ---------------------------------------------------------------------------

# Career pages mix real postings with employee-spotlight/blog content
# ("Meet Nils Libert, Associate Scientist in R&D") that happens to contain
# fresher keywords but isn't a job listing at all.
_NOT_A_JOB_RE = re.compile(
    r"^\s*meet\b|^\s*[\w'’.-]+\s+[\w'’.-]+\s*:\s", re.IGNORECASE,
)
_NOT_A_JOB_URL_RE = re.compile(r"/(blog|news|stories|insights|article)s?/", re.IGNORECASE)


def is_real_job(title: str, href: str = "") -> bool:
    if _NOT_A_JOB_RE.search(title or ""):
        return False
    if href and _NOT_A_JOB_URL_RE.search(href):
        return False
    return True


# ---------------------------------------------------------------------------
# Seniority (checked against the title only — descriptions routinely say
# things like "reporting to the Senior Manager")
# ---------------------------------------------------------------------------

_SENIOR_TITLE_RE = re.compile(
    r"\b(?:"
    r"senior|sr\b\.?|staff|principal|lead|leader|manager|director|head"
    r"|vice[\s-]+president|vp|svp|avp|evp|chief|c[etfo]o|president"
    r"|architect|distinguished|partner|supervisor"
    r")\b",
    re.IGNORECASE,
)

# "Software Engineer II", "SDE III", "Analyst IV" — levelled titles above the
# entry band. Case-sensitive so the pronoun/letter "v"/"i" never matches.
_LEVEL_TITLE_RE = re.compile(r"(?<![\w'])(?:II|III|IV|V|VI)(?![\w'])|\b(?:L|Level\s*)[3-9]\b")


# ---------------------------------------------------------------------------
# Experience requirement parsing
# ---------------------------------------------------------------------------

_NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "fifteen": 15,
}
_NUM = r"(?:\d{1,2}(?:\.\d+)?|" + "|".join(_NUMBER_WORDS) + r")"

# Any quantity of years/months, optionally a range, optionally "+":
#   2 years, 2+ years, 2 years+, 1-3 yrs, 0 to 1 year, minimum of 2 yrs,
#   at least two years, 6 months
_QUANTITY_RE = re.compile(
    r"(?P<qual>\b(?:minimum|min|at\s*least|atleast|not\s+less\s+than)\b\.?\s*(?:of\s+)?)?"
    r"(?<![\w.])(?P<lo>" + _NUM + r")"
    r"(?:\s*(?:-|to)\s*(?P<hi>" + _NUM + r"))?"
    r"\s*(?P<plus>\+)?\s*"
    r"(?P<unit>years?|yrs?|months?|mos?)\b\.?"
    r"(?P<plus2>\s*\+)?",
    re.IGNORECASE,
)

# what may sit between the quantity and the word "experience":
#   "2 years experience", "3 years' experience", "2 yrs of relevant work exp"
_EXP_AFTER_RE = re.compile(
    r"^\s*'?s?'?\s*(?:\(\s*)?(?:of\s+)?(?:[a-z/&-]+\s+){0,4}?(?:experience|exp)\b",
    re.IGNORECASE,
)
# "experience required: 2 years", "Experience - 2-4 yrs", "exp of min 2 years"
_EXP_BEFORE_RE = re.compile(
    r"\b(?:experience|exp)\.?\s*(?:\((?:in\s+)?years?\))?\s*"
    r"(?:required|requirement|needed|level|range|of)?\s*[:=-]?\s*(?:of\s+)?(?:about\s+|around\s+)?$",
    re.IGNORECASE,
)
# requirements flagged as optional don't make a role experienced
_PREFERRED_RE = re.compile(
    r"\b(?:preferred|preferably|is\s+a\s+plus|a\s+plus|nice\s+to\s+have|desirable|"
    r"advantageous|an\s+advantage|bonus|good\s+to\s+have)\b",
    re.IGNORECASE,
)
# split on sentence ends ("Min. 2 years" stays together: the next char isn't
# a capital), semicolons, bullets and line breaks
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z])|;|\s*[•·▪●]\s*|\n")

# 15+ years is never an entry requirement — it's company boilerplate like
# "with over 150 years of experience in healthcare"
_MAX_PLAUSIBLE_YEARS = 15


def _to_number(token: str) -> float:
    token = token.lower()
    if token in _NUMBER_WORDS:
        return float(_NUMBER_WORDS[token])
    return float(token)


def _sentences(text: str) -> list[str]:
    parts = _SENTENCE_SPLIT_RE.split((text or "").replace("\r", "\n"))
    return [n for n in (_normalize(p) for p in parts) if n]


def parse_experience_requirements(text: str) -> list[ExperienceRequirement]:
    """Return every experience requirement stated in ``text``.

    Quantities are only treated as experience when the wording makes that
    clear: a range/"N+" of years (in job text that is always experience), or a
    single number tied to "experience" or "minimum"/"at least". Durations such
    as "6-month internship" or "2 year contract" are ignored.
    Requirements marked as preferred / nice-to-have are skipped.
    """
    reqs: list[ExperienceRequirement] = []
    for sentence in _sentences(text):
        # "2+ years preferred" is optional; "Freshers or 1-2 years" is an
        # alternative that still admits freshers
        optional = bool(_PREFERRED_RE.search(sentence) or _FRESHER_RE.search(sentence))
        for m in _QUANTITY_RE.finditer(sentence):
            lo = _to_number(m.group("lo"))
            hi = _to_number(m.group("hi")) if m.group("hi") else None
            unit = m.group("unit").lower()
            is_months = unit.startswith("mo")
            plus = bool(m.group("plus") or m.group("plus2"))

            before = sentence[max(0, m.start() - 40):m.start()]
            after = sentence[m.end():m.end() + 60]
            linked_to_experience = bool(
                _EXP_AFTER_RE.search(after) or _EXP_BEFORE_RE.search(before)
            )
            if is_months:
                # "3-6 months internship" is a duration, not a requirement
                is_requirement = linked_to_experience
            else:
                is_requirement = (
                    linked_to_experience
                    or bool(m.group("qual"))
                    or hi is not None
                    or plus
                )
            if not is_requirement or optional:
                continue

            if is_months:
                lo, hi = lo / 12, (hi / 12 if hi is not None else None)
            if lo > _MAX_PLAUSIBLE_YEARS or (hi is not None and hi < lo):
                continue
            reqs.append(ExperienceRequirement(lo, hi, m.group(0).strip()))
    return reqs


# ---------------------------------------------------------------------------
# Positive / negative signals
# ---------------------------------------------------------------------------

# Strong signals that mean "open to people with no experience".
_FRESHER_RE = re.compile(
    r"\b(?:freshers?|fresh\s+graduates?"
    r"|no\s+(?:prior\s+|previous\s+|work\s+)?experience\s+(?:is\s+)?(?:required|needed|necessary)"
    r"|(?:experience|exp)\s*[:-]?\s*(?:0|zero|nil|none)\b(?!\s*(?:-|to)\s*[1-9])"
    r")\b",
    re.IGNORECASE,
)

# Entry-level signals trusted anywhere in the title.
_ENTRY_TITLE_RE = re.compile(
    r"\b(?:graduate|grad|new\s+grads?|recent\s+graduates?|university\s+graduates?"
    r"|entry[\s-]*level|trainee|campus|early[\s-]+careers?|apprentice(?:ship)?"
    r")\b",
    re.IGNORECASE,
)

# Description text needs more specific phrasing: "graduate degree" or
# "campus" alone appear in plenty of experienced postings.
_ENTRY_DESC_RE = re.compile(
    r"\b(?:new\s+grads?|new\s+graduates?|recent\s+graduates?|recently\s+graduated"
    r"|university\s+graduates?|entry[\s-]*level|early[\s-]+careers?"
    r"|graduate\s+(?:program|programme|scheme|trainee|hire|hiring|role|position)"
    r"|campus\s+(?:hire|hiring|recruitment|placement|drive)|trainee|apprenticeship"
    r"|(?:batch|class)\s+of\s+20\d\d|20\d\d\s+(?:batch|pass[\s-]*outs?|graduates?)"
    r")\b",
    re.IGNORECASE,
)

# Words that show up in both entry and experienced titles — never proof on
# their own ("Associate Project Specialist", "Junior Partner", "International").
_AMBIGUOUS_RE = re.compile(r"\b(?:associate|junior|jr|intern|internship)\b", re.IGNORECASE)

_NO_FRESHERS_RE = re.compile(
    r"\b(?:freshers?\s+(?:need\s+not|should\s+not|cannot|can\s*not|may\s+not|are\s+not\s+eligible\s+to)\s+apply"
    r"|not\s+(?:suitable|open|eligible)\s+for\s+freshers?"
    r"|no\s+freshers?)\b",
    re.IGNORECASE,
)


def _fmt_years(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:g}"


def _describe(req: ExperienceRequirement) -> str:
    return f"'{req.text}'"


def classify_job(title: str, description: str = "", url: str = "") -> Classification:
    """Classify a posting from its title and any extra text available.

    ``description`` can be card text, a detail-page description, or both
    concatenated. Seniority is judged from the title; experience requirements
    and entry-level evidence from title + description. Positive signals never
    override an explicit experience requirement.
    """
    title_n = _normalize(title)
    desc_n = _normalize(description)
    full = f"{title_n}\n{desc_n}" if desc_n else title_n

    if not title_n or not is_real_job(title_n, url):
        return Classification(Category.NOT_A_JOB, "not a job posting (spotlight/blog/empty title)")

    m = _SENIOR_TITLE_RE.search(title_n)
    if m:
        return Classification(Category.SENIOR, f"seniority in title: '{m.group(0)}'")

    if _NO_FRESHERS_RE.search(full):
        return Classification(Category.EXPERIENCED, "posting says freshers are not eligible")

    reqs = parse_experience_requirements(f"{title}\n{description or ''}")
    experienced = [r for r in reqs if r.min_years > 0]
    if experienced:
        worst = max(experienced, key=lambda r: r.min_years)
        return Classification(
            Category.EXPERIENCED,
            f"explicit requirement: {_describe(worst)} "
            f"(minimum {_fmt_years(worst.min_years)} yr)",
        )

    m = _LEVEL_TITLE_RE.search(title_n)
    if m:
        return Classification(Category.EXPERIENCED, f"levelled title above entry: '{m.group(0)}'")

    zero_reqs = [r for r in reqs if r.min_years == 0]
    m = _FRESHER_RE.search(full)
    if m:
        return Classification(Category.FRESHER, f"fresher signal: '{m.group(0)}'")
    if zero_reqs:
        return Classification(
            Category.FRESHER, f"experience starts at 0: {_describe(zero_reqs[0])}"
        )

    m = _ENTRY_TITLE_RE.search(title_n) or _ENTRY_DESC_RE.search(desc_n)
    if m:
        return Classification(Category.ENTRY_LEVEL, f"entry-level signal: '{m.group(0)}'")

    m = _AMBIGUOUS_RE.search(title_n)
    if m:
        return Classification(
            Category.UNKNOWN,
            f"'{m.group(0)}' alone is not evidence of an entry-level role",
        )
    return Classification(Category.UNKNOWN, "no fresher/entry-level evidence")
