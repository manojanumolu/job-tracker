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
_PAREN_NUMBER_RE = re.compile(
    r"\b(?:(?:zero|one|two|three|four|five|six|seven|eight|nine|ten)\s*\((?P<d1>\d{1,2})\)"
    r"|(?P<d2>\d{1,2})\s*\((?:zero|one|two|three|four|five|six|seven|eight|nine|ten)\))",
    re.IGNORECASE,
)
_DASHES_RE = re.compile("[‐‑‒–—―−]")
_WS_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    text = _DASHES_RE.sub("-", text or "")
    # "one (1) year" / "1 (one) year" -> "1 year"
    text = _PAREN_NUMBER_RE.sub(lambda m: m.group("d1") or m.group("d2"), text)
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
# Career-site navigation that carries early-career words but links to a
# landing page, not a posting: "Early Careers", "Graduate Programs",
# "Explore Early Careers", "Students & Graduates", "Campus Hiring India".
_LANDING_PAGE_RE = re.compile(
    r"^\s*(?:explore|discover|learn\s+more|join\s+us|view|see|search|find|browse|read\s+more|apply\s+now)\b"
    r"|\b(?:careers|programs|programmes|opportunities|jobs|openings|vacancies)\s*$"
    r"|\bstudents?\s*(?:&|and)\s*graduates?\b"
    r"|^\s*(?:campus|university)\s+(?:hiring|recruitment|placements?|programs?)\s*(?:india|20\d\d)?\s*$",
    re.IGNORECASE,
)


def is_real_job(title: str, href: str = "") -> bool:
    if _NOT_A_JOB_RE.search(title or "") or _LANDING_PAGE_RE.search(title or ""):
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
    r"senior|sr\b\.?|staff|principal|lead(?!\s+generation)|leader|manager|director|head"
    r"|vice[\s-]+president|vp|svp|avp|evp|chief|c[etfo]o|president"
    r"|architect|distinguished|partner|supervisor"
    r")\b",
    re.IGNORECASE,
)

# "Software Engineer II", "SDE III", "Analyst IV" — levelled titles above the
# entry band. Case-sensitive so the pronoun/letter "v"/"i" never matches.
_LEVEL_TITLE_RE = re.compile(r"(?<![\w'])(?:II|III|IV|V|VI)(?![\w'])|\b(?:L|Level\s*)[3-9]\b")
# "SDE-2", "Software Engineer 2", "Analyst 3" (but not "Engineer 2026")
_LEVEL_NUM_TITLE_RE = re.compile(
    r"\b(?:sde|swe|engineer|developer|analyst|associate|consultant|scientist)[\s-]*[2-5]\b(?![\d.])",
    re.IGNORECASE,
)


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
    r"\s*(?P<plus>\+|plus\b|or\s+more\b)?\s*-?\s*"
    # "2Y+" — a bare "y" only counts when followed by "+"
    r"(?P<unit>years?|yrs?|months?|mos?|y(?=\s*\+))\b\.?"
    r"(?P<plus2>\s*(?:\+|\bor\s+(?:more|above)\b|\band\s+(?:above|more)\b|\bplus\b))?"
    # "2 years minimum", "2 years (mandatory)"
    r"(?P<qual2>\s*,?\s*\(?\s*(?:minimum|min\b\.?|at\s+least|mandatory|required)\b)?",
    re.IGNORECASE,
)

# Nouns that make a number of years a duration/commitment rather than an
# experience requirement: "2-year contract", "minimum 2 years service
# agreement", "2 year graduate programme".
_DURATION_NOUNS = (
    r"(?:bond|agreement|contract|commitment|service|tenure|training|internship|"
    r"program|programme|course|degree|diploma|warranty|lock[\s-]?in|notice|"
    r"probation|stay|stipend|period|term|fixed[\s-]term|with)"
)
_DURATION_AFTER_RE = re.compile(
    r"^\s*'?s?'?\s*(?:of\s+)?(?:[\w-]+\s+){0,2}?" + _DURATION_NOUNS + r"\b",
    re.IGNORECASE,
)
# what may sit between the quantity and the word "experience":
#   "2 years experience", "3 years' experience", "2 yrs of relevant work exp",
#   "2 years C++ experience", "3 years .NET experience"
# ("1 year of internship experience" is still experience, so only the
# commitment-type nouns break the link)
_FILLER_STOP = r"(?:bond|agreement|contract|commitment|with|program|programme|course|degree|term)"
_EXP_AFTER_RE = re.compile(
    r"^\s*'?s?'?\s*(?:\(\s*)?(?:of\s+)?"
    r"(?:(?!" + _FILLER_STOP + r"\b)[\w/&+#.'-]+\s+){0,4}?(?:experience|exp)\b",
    re.IGNORECASE,
)
# "You bring 3 years in backend engineering", "2 years working in a similar
# role", "3 years as a data analyst" — applicant experience without the word.
_EXP_DOING_AFTER_RE = re.compile(
    r"^\s*(?:of\s+)?(?:(?:professional|industry|relevant|hands[\s-]on|proven)\s+)?"
    r"(?:working|work|in|as|building|developing|designing|coding|programming|managing|handling)\b",
    re.IGNORECASE,
)
# ...unless it's a duration: "for 2 years in the programme", "after 1 year in
# the role", "the past 3 years in India", "bond of 2 years in Pune"
_DURATION_BEFORE_RE = re.compile(
    r"\b(?:for|after|within|next|past|last|first|during|of|every|per|about|over\s+the|in\s+the)\s*$",
    re.IGNORECASE,
)
_DURATION_NOUN_NEAR_RE = re.compile(
    r"^.{0,40}?\b(?:program|programme|rotation|course|contract|bond|agreement|training|internship|"
    r"tenure|probation|scheme|residency)\b",
    re.IGNORECASE,
)
# "experience required: 2 years", "Experience - 2-4 yrs", "exp of min 2 years"
_EXP_BEFORE_RE = re.compile(
    r"\b(?:experience|exp)\.?\s*(?:\((?:in\s+)?years?\))?\s*"
    r"(?:required|requirement|needed|level|range|of)?\s*[:=-]?\s*(?:of\s+)?(?:about\s+|around\s+)?$",
    re.IGNORECASE,
)
# Unit-less form common on Indian portals: "Years of Experience: 2-4",
# "Experience: 3+", "Exp (yrs): 0-1"
_UNITLESS_RE = re.compile(
    r"\b(?:years?\s+of\s+(?:relevant\s+|work\s+|total\s+)?experience"
    r"|(?:total\s+|work\s+|relevant\s+|required\s+|min(?:imum)?\.?\s+)?(?:experience|exp)\.?"
    r"\s*(?:\(\s*(?:in\s+)?(?:years|yrs)\s*\)|in\s+(?:years|yrs))?"
    r"(?:\s+required)?)"
    r"\s*[:=-]\s*(?P<lo>" + _NUM + r")(?:\s*(?:-|to)\s*(?P<hi>" + _NUM + r"))?\s*(?P<plus>\+)?"
    # a following unit belongs to _QUANTITY_RE; anything else (end of text,
    # a new sentence without a full stop) is fine
    r"(?!\s*(?:\d|\.\d|[-+%/]|(?:years?|yrs?|months?|mos?|days?|weeks?|lpa|lakhs?|k)\b))",
    re.IGNORECASE,
)
# "We have 10+ years of experience", "with over 12 years of experience" —
# the company describing itself, not a requirement on the applicant
_COMPANY_CLAIM_BEFORE_RE = re.compile(
    r"\b(?:we|our\s+\w+|the\s+company|company|firm|team)\s+(?:have|has|bring|brings|boasts?)\s+"
    r"(?:over\s+|more\s+than\s+|nearly\s+|almost\s+)?$"
    r"|\bwith\s+(?:over|more\s+than|nearly|almost)\s+$",
    re.IGNORECASE,
)
# requirements flagged as optional don't make a role experienced
_PREFERRED_RE = re.compile(
    r"\b(?:preferred|preferably|is\s+a\s+plus|a\s+plus|nice\s+to\s+have|desirable|"
    r"advantageous|an\s+advantage|bonus|good\s+to\s+have)\b",
    re.IGNORECASE,
)
_MANDATORY_RE = re.compile(r"\b(?:required|must|mandatory|essential|minimum|at\s+least)\b", re.IGNORECASE)
# Section headings that switch preferred-mode on/off for the lines below them
_REQUIRED_HEADING_RE = re.compile(
    r"\b(?:minimum|basic|required|requirements?|must[\s-]have|qualifications|eligibility|"
    r"responsibilities|what\s+you(?:'ll)?\s+(?:need|bring|do)|who\s+you\s+are|about)\b",
    re.IGNORECASE,
)
_HEADING_MAX_LEN = 60
_CLAUSE_SPLIT_RE = re.compile(r",|\s+\band\b\s+|\s+\bbut\b\s+")
# "Freshers or candidates with 1-2 years", "Fresher / 0-2 years",
# "1-2 years or freshers" — experience offered as an alternative to freshers
_FRESHER_ALTERNATIVE_RE = re.compile(
    r"\bfreshers?\s*(?:/|\bor\b|&|\()"
    r"|\bfreshers?\s+and\s+(?:experienced|candidates|professionals)\b"
    r"|(?:/|\bor\b)\s*freshers?\b"
    r"|\bfreshers?\s+(?:can|may|are)\s+(?:also\s+)?(?:apply|welcome|eligible)",
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


def _clause_is_optional(sentence: str, start: int, end: int) -> bool:
    """True when the clause holding sentence[start:end] marks it as optional
    ("2+ years of Python is a plus") — but not when a *neighbouring* clause
    is the optional one ("2+ years required, Master's preferred")."""
    bounds = [0] + [m.end() for m in _CLAUSE_SPLIT_RE.finditer(sentence)] + [len(sentence) + 1]
    clauses = [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]
    for i, (c_start, c_end) in enumerate(clauses):
        if c_start <= start < c_end:
            clause = sentence[c_start:c_end]
            # ", preferred" / ", is a plus" trailing clause qualifies this one
            if i + 1 < len(clauses):
                nxt = sentence[clauses[i + 1][0]:clauses[i + 1][1]].strip(" .")
                if len(nxt) <= 25 and _PREFERRED_RE.search(nxt):
                    clause += " " + nxt
            return bool(_PREFERRED_RE.search(clause)) and not _MANDATORY_RE.search(clause)
    return False


def _is_heading(sentence: str) -> bool:
    return len(sentence) <= _HEADING_MAX_LEN and not _QUANTITY_RE.search(sentence)


def parse_experience_requirements(text: str, *, title: bool = False) -> list[ExperienceRequirement]:
    """Return every experience requirement stated in ``text``.

    Quantities are only treated as experience when the wording makes that
    clear: a range/"N+" of years (in job text that is always experience), a
    single number tied to "experience" or "minimum"/"at least", or the
    unit-less "Experience: 2-4" form. Durations such as "6-month internship",
    "2 year contract" or "minimum 2 years service agreement" are ignored.

    With ``title=True`` any number of years counts ("Engineer — 2 years").

    Requirements stated as preferred / nice-to-have — inline or under a
    "Preferred qualifications" heading — are skipped, as are ones offered as
    an alternative to freshers ("Freshers or 1-2 years").
    """
    reqs: list[ExperienceRequirement] = []
    in_preferred_section = False
    for sentence in _sentences(text):
        if _is_heading(sentence):
            if _PREFERRED_RE.search(sentence):
                in_preferred_section = True
            elif _REQUIRED_HEADING_RE.search(sentence):
                in_preferred_section = False
        fresher_alternative = bool(_FRESHER_ALTERNATIVE_RE.search(sentence))

        found: list[tuple[re.Match, float, float | None]] = []
        for m in _QUANTITY_RE.finditer(sentence):
            lo = _to_number(m.group("lo"))
            hi = _to_number(m.group("hi")) if m.group("hi") else None
            is_months = m.group("unit").lower().startswith("mo")
            plus = bool(m.group("plus") or m.group("plus2"))

            before = sentence[max(0, m.start() - 40):m.start()]
            after = sentence[m.end():m.end() + 60]
            linked_to_experience = bool(
                _EXP_AFTER_RE.search(after)
                or _EXP_BEFORE_RE.search(before)
                or (
                    _EXP_DOING_AFTER_RE.search(after)
                    and not _DURATION_BEFORE_RE.search(before)
                    and not _DURATION_NOUN_NEAR_RE.search(after)
                )
            )
            # the quantity is the whole line: a card/field like "2-5 Yrs"
            standalone = not (sentence[:m.start()] + sentence[m.end():]).strip(" .:-()[]|,")
            if _COMPANY_CLAIM_BEFORE_RE.search(before):
                continue
            if is_months:
                # "3-6 months internship" is a duration, not a requirement;
                # "18+ months" / "6 months minimum" are requirements
                is_requirement = linked_to_experience or plus or bool(m.group("qual2"))
            elif _DURATION_AFTER_RE.search(after) and not linked_to_experience:
                is_requirement = False
            else:
                is_requirement = (
                    linked_to_experience
                    or title
                    or standalone
                    or bool(m.group("qual") or m.group("qual2"))
                    or hi is not None
                    or plus
                )
            if not is_requirement:
                continue
            if is_months:
                lo, hi = lo / 12, (hi / 12 if hi is not None else None)
            found.append((m, lo, hi))

        covered = [(m.start(), m.end()) for m, _, _ in found]
        for m in _UNITLESS_RE.finditer(sentence):
            lo_start = m.start("lo")
            if any(a <= lo_start < b for a, b in covered):
                continue
            lo = _to_number(m.group("lo"))
            hi = _to_number(m.group("hi")) if m.group("hi") else None
            found.append((m, lo, hi))

        if in_preferred_section or fresher_alternative:
            continue
        for m, lo, hi in found:
            if _clause_is_optional(sentence, m.start(), m.end()):
                continue
            if lo > _MAX_PLAUSIBLE_YEARS or (hi is not None and hi < lo):
                continue
            reqs.append(ExperienceRequirement(lo, hi, m.group(0).strip()))
    return reqs


# ---------------------------------------------------------------------------
# Positive / negative signals
# ---------------------------------------------------------------------------

# Strong signals that mean "open to people with no experience". In the
# title a bare "Fresher" is enough; in the description the word must be used
# about the applicant ("open to freshers"), not the team ("mentor freshers").
_NO_EXPERIENCE_RE = (
    r"no\s+(?:(?:prior|previous|work|professional|relevant|industry)\s+){0,2}experience\s+(?:is\s+)?(?:required|needed|necessary)"
    r"|(?:does\s+not|doesn't|do\s+not|don't)\s+(?:require|need)\s+(?:any\s+)?"
    r"(?:(?:prior|previous|work|professional|relevant|industry)\s+){0,2}experience"
    r"|(?:experience|exp)\s*[:-]?\s*(?:0|zero|nil|none|fresher)\b(?!\s*(?:-|to)\s*[1-9])"
)
_FRESHER_TITLE_RE = re.compile(
    r"\b(?:freshers?|fresh\s+graduates?|" + _NO_EXPERIENCE_RE + r")\b", re.IGNORECASE,
)
_FRESHER_DESC_RE = re.compile(
    r"\b(?:fresh\s+graduates?|" + _NO_EXPERIENCE_RE +
    r"|freshers?\s+(?:can|may|are|is)\s+(?:also\s+)?(?:apply|welcome|eligible|encouraged)"
    r"|freshers?\s+(?:welcome|eligible|only|with)"
    r"|(?:open\s+to|hiring|for|welcom\w*|inviting|recruiting|looking\s+for)\s+(?:all\s+|the\s+|bright\s+)?freshers?"
    r"|freshers?\s*(?:/|\bor\b|&|\()"
    r"|freshers?\s+and\s+(?:experienced|candidates|professionals)"
    r"|(?:/|\bor\b)\s*freshers?"
    r")\b",
    re.IGNORECASE,
)

# Entry-level signals trusted anywhere in the title ("Post Graduate Teacher"
# is an experienced teaching grade, not a graduate role).
_ENTRY_TITLE_RE = re.compile(
    r"\b(?:(?<!post\s)(?<!post-)graduate|grad|new\s+grads?|recent\s+graduates?|university\s+graduates?"
    r"|entry[\s-]*level|trainee|campus|early[\s-]+careers?|apprentice(?:ship)?"
    r")\b",
    re.IGNORECASE,
)
# "Campus Recruiter", "Early Careers Talent Acquisition Partner" — the
# early-career words describe who they hire, not the role itself.
_RECRUITER_TITLE_RE = re.compile(
    r"\b(?:recruit\w*|talent\s+acquisition|sourcer|hiring\s+(?:specialist|coordinator))\b",
    re.IGNORECASE,
)

# Description text needs more specific phrasing: "graduate degree",
# "mentor trainees" or "support entry-level staff" appear in plenty of
# experienced postings.
_ENTRY_DESC_RE = re.compile(
    r"\b(?:new\s+grads?|new\s+graduates?|recent\s+graduates?|recently\s+graduated"
    r"|university\s+graduates?"
    r"|entry[\s-]*level\s+(?:role|position|opportunity|job|opening|program|programme|hiring)"
    r"|(?:this|an|is)\s+(?:is\s+)?(?:an\s+)?entry[\s-]*level"
    r"|early[\s-]+careers?\s+(?:program|programme|role|position|opportunity|hire|hiring)"
    r"|graduate\s+(?:program|programme|scheme|trainee|hire|hiring|role|position)"
    r"|campus\s+(?:hire|hiring|recruitment|placement|drive)"
    r"|(?:graduate|management|engineer|engineering|executive)\s+trainee"
    r"|(?:as\s+a|this)\s+trainee|trainee\s+(?:program|programme|role|position)"
    r"|apprenticeship\s+(?:program|programme|scheme)"
    r"|(?:batch|class)\s+of\s+20\d\d|20\d\d\s+(?:batch|pass[\s-]*outs?|graduates?)"
    r"|(?:open\s+to|suitable\s+for|ideal\s+for|designed\s+for|aimed\s+at|targeted\s+at|we\s+welcome)"
    r"\s+(?:all\s+)?(?:recent\s+|fresh\s+|new\s+)?graduates?"
    r"|graduates?\s+(?:are\s+)?(?:welcome|encouraged|eligible|invited)|graduates?\s+(?:can|may)\s+apply"
    r"|(?:this\s+is|is)\s+an?\s+(?:apprenticeship|traineeship)"
    r")\b",
    re.IGNORECASE,
)

# A description signal about people the role works *with* rather than the
# applicant: "mentor recent graduates", "onboard campus hires", "support our
# apprenticeship scheme".
_STAFF_CONTEXT_RE = re.compile(
    r"\b(?:mentor\w*|coach\w*|onboard\w*|guid(?:e|es|ing)|supervis\w*|manag\w*|support(?:s|ing)?|"
    r"administ\w*|coordinat\w*|oversee\w*|overseeing|lead(?:s|ing)?|run(?:s|ning)?|teach\w*|"
    r"work(?:s|ing)?\s+(?:with|alongside)|alumni)\b[^.,;:\n]{0,40}\Z",
    re.IGNORECASE,
)


def _applicant_signal(pattern: re.Pattern, text: str) -> re.Match | None:
    """First match of ``pattern`` in ``text`` that is about the applicant."""
    for m in pattern.finditer(text):
        if not _STAFF_CONTEXT_RE.search(text[max(0, m.start() - 60):m.start()]):
            return m
    return None

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
    # line breaks kept so a signal's context never spans two bullet points
    desc_lines = "\n".join(n for n in (_normalize(x) for x in (description or "").splitlines()) if n)
    full = f"{title_n}\n{desc_n}" if desc_n else title_n

    if not title_n or not is_real_job(title_n, url):
        return Classification(Category.NOT_A_JOB, "not a job posting (spotlight/blog/empty title)")

    m = _SENIOR_TITLE_RE.search(title_n)
    if m:
        return Classification(Category.SENIOR, f"seniority in title: '{m.group(0)}'")

    if _NO_FRESHERS_RE.search(full):
        return Classification(Category.EXPERIENCED, "posting says freshers are not eligible")

    reqs = parse_experience_requirements(title or "", title=True)
    reqs += parse_experience_requirements(description or "")
    experienced = [r for r in reqs if r.min_years > 0]
    if experienced:
        worst = max(experienced, key=lambda r: r.min_years)
        return Classification(
            Category.EXPERIENCED,
            f"explicit requirement: {_describe(worst)} "
            f"(minimum {_fmt_years(worst.min_years)} yr)",
        )

    m = _LEVEL_TITLE_RE.search(title_n) or _LEVEL_NUM_TITLE_RE.search(title_n)
    if m:
        return Classification(Category.EXPERIENCED, f"levelled title above entry: '{m.group(0)}'")

    zero_reqs = [r for r in reqs if r.min_years == 0]
    m = _FRESHER_TITLE_RE.search(title_n) or _applicant_signal(_FRESHER_DESC_RE, desc_lines)
    if m:
        return Classification(Category.FRESHER, f"fresher signal: '{m.group(0)}'")
    if zero_reqs:
        return Classification(
            Category.FRESHER, f"experience starts at 0: {_describe(zero_reqs[0])}"
        )

    title_signal = None if _RECRUITER_TITLE_RE.search(title_n) else _ENTRY_TITLE_RE.search(title_n)
    m = title_signal or _applicant_signal(_ENTRY_DESC_RE, desc_lines)
    if m:
        return Classification(Category.ENTRY_LEVEL, f"entry-level signal: '{m.group(0)}'")

    m = _AMBIGUOUS_RE.search(title_n)
    if m:
        return Classification(
            Category.UNKNOWN,
            f"'{m.group(0)}' alone is not evidence of an entry-level role",
        )
    return Classification(Category.UNKNOWN, "no fresher/entry-level evidence")
