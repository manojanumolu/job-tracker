"""Fresher / entry-level job classification.

Pure text logic with no network access, so it can be unit tested in isolation.
The scraper gathers whatever job information a site exposes (title, location,
card text, detail-page description) and asks ``classify_job`` for a verdict.

Only FRESHER and ENTRY_LEVEL results are alerted on. When the available text
gives no real evidence either way the result is UNKNOWN, which is *not*
alerted: a missed ambiguous posting is cheaper than an irrelevant
experienced-job alert.

Rules, in order of precedence:
  1. Pages that aren't job postings (stories, programme/landing pages, talent
     networks, country pickers, recruiter roles for graduates) -> NOT_A_JOB.
  2. Seniority / mid-level in the title -> SENIOR / EXPERIENCED.
  3. Any explicit experience requirement (a number of years, "prior
     experience required", a mid/senior level field) -> EXPERIENCED. Positive
     wording such as "freshers welcome" never overrides it.
  4. Applicant-directed fresher / entry-level evidence -> FRESHER / ENTRY_LEVEL.
  5. Otherwise UNKNOWN.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from locations import is_location_only


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
    # "IN_Senior Associate_GenAI" (PwC) — "_" is a word character, so
    # without this "\bsenior" never matches
    text = text.replace("_", " ")
    # "one (1) year" / "1 (one) year" -> "1 year"
    text = _PAREN_NUMBER_RE.sub(lambda m: m.group("d1") or m.group("d2"), text)
    text = text.replace("’", "'").replace(" ", " ")
    return _WS_RE.sub(" ", text).strip()


def normalize_title(title: str) -> str:
    """Title key for matching a listing to its detail page and for
    duplicate checks: case, dashes, whitespace and harmless punctuation
    don't matter ("Associate – Evidence Synthesis" == "associate - evidence synthesis")."""
    text = _normalize(title).lower()
    text = re.sub(r"[^\w]+", " ", text)
    return _WS_RE.sub(" ", text).strip()


# ---------------------------------------------------------------------------
# Not-a-job detection
# ---------------------------------------------------------------------------

# Career pages mix real postings with employee-spotlight/blog content
# ("Meet Nils Libert, Associate Scientist in R&D") that happens to contain
# fresher keywords but isn't a job listing at all.
_NOT_A_JOB_RE = re.compile(
    r"^\s*meet\b|^\s*[\w'’.-]+\s+[\w'’.-]+\s*:\s", re.IGNORECASE,
)
_NOT_A_JOB_URL_RE = re.compile(r"/(blog|news|stories|insights|article|life|events?|people|culture)s?/", re.IGNORECASE)
# Career-site navigation that carries early-career words but links to a
# landing page, not a posting: "Early Careers", "Graduate Programs",
# "Explore Early Careers", "Students & Graduates", "Campus Hiring India".
# (plural / collective forms only: "Intern" or "Graduate" alone may be a posting)
_AUDIENCE = (
    r"(?:early[\s-]+(?:careers?|talent)|emerging\s+talent|students|graduates|campus|university|universities"
    r"|internships|apprenticeships|young\s+professionals|school\s+leavers|freshers)"
)
_LANDING_PAGE_RE = re.compile(
    r"^\s*(?:explore|discover|learn\s+more|join\s+us|view|see|search|find|browse|read\s+more|apply\s+now)\b"
    r"|\b(?:careers|programs|programmes|opportunities|jobs|openings|vacancies)\s*$"
    r"|\bstudents?\s*(?:&|and)\s*graduates?\b"
    r"|^\s*(?:campus|university)\s+(?:hiring|recruitment|placements?|programs?)\s*(?:india|20\d\d)?\s*$"
    # a title made only of audience words: "Early Talent", "Students & Graduates 2026"
    r"|^\s*" + _AUDIENCE + r"(?:\s*(?:&|and|,|/|\+)\s*" + _AUDIENCE + r")*"
    r"(?:\s+(?:hiring|recruitment|careers?|opportunities|jobs|india|20\d\d))*\s*$",
    re.IGNORECASE,
)
# Stories, events, talent networks and site chrome
_INFO_PAGE_RE = re.compile(
    # "Inside Sanofi's Graduate Program" (but "Inside Sales Representative" is a job)
    r"^\s*(?:inside\s+(?:our|the|life|\w+'s)|how|why|what|when|where|who|meet|introducing|celebrating|welcome\s+to"
    r"|life\s+at|a\s+day\s+in|day\s+in\s+the\s+life|behind\s+the|spotlight|career\s+stories"
    r"|our\s+(?:people|stories|story|culture|values|benefits)|working\s+at)\b"
    # site chrome — only as the whole title ("Benefits Analyst" is a job)
    r"|^\s*(?:our\s+)?(?:benefits|cookies?(?:\s+(?:policy|settings|preferences))?|privacy(?:\s+(?:policy|notice|statement))?"
    r"|terms(?:\s+(?:of\s+use|and\s+conditions|&\s+conditions))?|accessibility(?:\s+statement)?|sitemap|faqs?"
    r"|contact\s+us|about\s+us|search\s+jobs|similar\s+jobs|saved\s+jobs|all\s+jobs|job\s+search|view\s+all(?:\s+jobs)?)\s*$"
    r"|\?\s*$"
    r"|\b(?:stories|blog|podcast)\b"
    r"|\b(?:careers?|jobs?|recruitment|recruiting|hiring|graduate|campus|university|virtual)\s+(?:fairs?|events?|days?|expos?|sessions?)\b"
    r"|\bopen\s+days?\b|\binfo(?:rmation)?\s+sessions?\b"
    r"|\btalent\s+(?:network|community|communities|pool|pipeline)\b|\bjoin\s+our\s+talent\b"
    r"|\bregister\s+(?:your\s+)?interest\b|\bjob\s+alerts?\b|\bstay\s+connected\b"
    r"|^\s*(?:early[\s-]+careers?|careers?|graduates?|students?|internships?|life|working|jobs|opportunities)\s+(?:at|with)\s+\S",
    re.IGNORECASE,
)
# Language / country pickers: "India (English)", "Deutschland (Deutsch)"
_LANGUAGE_PICKER_RE = re.compile(
    r"^\s*[^\W\d_][\w .'’-]{1,40}\s*\(\s*(?:english|en|français|french|deutsch|german|español|spanish|"
    r"português|portuguese|italiano|italian|nederlands|dutch|polski|日本語|中文|한국어|简体中文|繁體中文)\s*\)\s*$",
    re.IGNORECASE,
)
# A programme title is an application landing page unless it names a role
# ("Early Career Program - Analyst") or the scraper has a real JobPosting.
_PROGRAMME_RE = re.compile(r"\b(?:programs?|programmes?|schemes?|academy|academies|leadership\s+development)\b", re.IGNORECASE)
_ROLE_NOUN_RE = re.compile(
    r"\b(?:engineers?|developers?|analysts?|associates?|consultants?|trainees?|interns?|specialists?|scientists?"
    r"|officers?|executives?|designers?|testers?|accountants?|auditors?|advis[eo]rs?|administrators?|assistants?"
    r"|representatives?|technicians?|programmers?|researchers?|writers?|apprentices?|agents?|operators?|clerks?"
    r"|nurses?|teachers?|pharmacists?|chemists?|lawyers?|economists?|actuar(?:y|ies|ial)|underwriters?|bankers?"
    r"|editors?|marketers?|strategists?|buyers?|planners?|controllers?|sde|swe|coordinators?|managers?)\b",
    re.IGNORECASE,
)
# "Campus Recruiter", "Early Careers Talent Acquisition Partner",
# "University Relations Specialist", "Graduate Program Coordinator" — the
# early-career words describe who they hire, not the role itself.
_RECRUITER_TITLE_RE = re.compile(
    r"\b(?:recruit\w*|talent\s+acquisition|sourcer|sourcing|hiring\s+(?:specialist|coordinator|manager|partner)"
    r"|(?:university|campus|student|graduate|school)\s+relations|ambassadors?"
    r"|(?:programs?|programmes?)\s+(?:coordinator|manager|lead|administrator|specialist|officer)"
    r"|(?:early[\s-]+careers?|early\s+talent|campus|university|graduate|emerging\s+talent)\s+"
    r"(?:partner|specialist|coordinator|manager|lead|advisor|consultant|recruiter|relations))\b",
    re.IGNORECASE,
)
# who a recruiter role hires for ("Talent Acquisition Trainee" is itself an
# entry-level job, so trainee/intern are not audience words here)
_AUDIENCE_WORD_RE = re.compile(
    r"\b(?:graduates?|grad|campus|university|universities|early[\s-]+careers?|early\s+talent|emerging\s+talent"
    r"|students?|freshers?|entry[\s-]*level)\b",
    re.IGNORECASE,
)


def not_a_job_reason(title: str, href: str = "", *, posting_evidence: bool = False) -> str:
    """Why ``title``/``href`` is not a job posting ("" when it may be one)."""
    title = _normalize(title)
    if not title:
        return "empty title"
    if _NOT_A_JOB_RE.search(title) or _INFO_PAGE_RE.search(title):
        return "career story / event / informational page"
    if _LANDING_PAGE_RE.search(title):
        return "career landing page"
    if _LANGUAGE_PICKER_RE.search(title) or is_location_only(title):
        return "country / location picker"
    if href and _NOT_A_JOB_URL_RE.search(href):
        return "blog / story URL"
    if _RECRUITER_TITLE_RE.search(title) and _AUDIENCE_WORD_RE.search(title):
        return "recruiting / programme-staff role, not an entry-level posting"
    if _PROGRAMME_RE.search(title) and not _ROLE_NOUN_RE.search(title) and not posting_evidence:
        return "programme page without job-posting evidence"
    return ""


def is_real_job(title: str, href: str = "", *, posting_evidence: bool = False) -> bool:
    return not not_a_job_reason(title, href, posting_evidence=posting_evidence)


# ---------------------------------------------------------------------------
# Seniority (checked against the title only — descriptions routinely say
# things like "reporting to the Senior Manager")
# ---------------------------------------------------------------------------

_SENIOR_TITLE_RE = re.compile(
    r"\b(?:"
    r"senior|sr\b\.?|staff|principal|lead(?!\s+generation)|leader|manager|director|head(?!\s+office)"
    r"|vice[\s-]+president|vp|svp|avp|evp|chief|c[etfo]o|president"
    r"|architect|distinguished|partner|supervisor"
    r")\b",
    re.IGNORECASE,
)
# "Software Engineer (Mid-level)", "Mid-Senior Analyst", "Experienced Developer"
_MID_LEVEL_RE = re.compile(r"\bmid[\s-]*(?:level|senior|career|weight)\b|\bexperienced\b", re.IGNORECASE)

# "Software Engineer II", "SDE III", "Analyst IV" — levelled titles above the
# entry band. Case-sensitive so the pronoun/letter "v"/"i" never matches.
_LEVEL_TITLE_RE = re.compile(r"(?<![\w'])(?:II|III|IV|V|VI)(?![\w'])|\b(?:L|Level\s*)[3-9]\b")
# "SDE-2", "Software Engineer 2", "Analyst 3" (but not "Engineer 2026")
_LEVEL_NUM_TITLE_RE = re.compile(
    r"\b(?:sde|swe|engineer|developer|analyst|associate|consultant|scientist)[\s-]*[2-5]\b(?![\d.])",
    re.IGNORECASE,
)
# Level fields in descriptions / page metadata:
#   "Seniority level: Mid-Senior level", "Management Level: 10 – Senior Analyst",
#   "Career level: Senior Analyst"
_LEVEL_FIELD_RE = re.compile(
    r"\b(?:seniority|experience|career|management|job|position|grade)\s+level\s*[:=|-]\s*(?P<val>[^\n|;]{1,60})",
    re.IGNORECASE,
)
_ENTRY_LEVEL_VALUE_RE = re.compile(r"^\s*(?:entry|fresher|graduate|internship|trainee)\b", re.IGNORECASE)


def _level_field_rejection(text: str) -> str:
    for m in _LEVEL_FIELD_RE.finditer(text or ""):
        val = m.group("val").strip()
        if _ENTRY_LEVEL_VALUE_RE.search(val):
            continue
        if _SENIOR_TITLE_RE.search(val) or _MID_LEVEL_RE.search(val):
            return m.group(0).strip()
    return ""


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
# agreement", "2 year graduate programme", "15 years of full-time education".
_DURATION_NOUNS = (
    r"(?:bond|agreement|contract|commitment|service|tenure|training|internship|"
    r"program|programme|course|degree|diploma|warranty|lock[\s-]?in|notice|"
    r"probation|stay|stipend|period|term|fixed[\s-]term|with|"
    r"education|schooling|studies|study|academics?|school|college|graduation)"
)
_DURATION_AFTER_RE = re.compile(
    r"^\s*'?s?'?\s*(?:of\s+)?(?:[\w-]+\s+){0,2}?" + _DURATION_NOUNS + r"\b",
    re.IGNORECASE,
)
# what may sit between the quantity and the word "experience":
#   "2 years experience", "3 years' experience", "2 yrs of relevant work exp",
#   "2 years C++ experience", "3 years .NET experience", "3 yrs of hands-on exposure"
# ("1 year of internship experience" is still experience, so only the
# commitment-type nouns break the link)
_FILLER_STOP = r"(?:bond|agreement|contract|commitment|with|program|programme|course|degree|term|education|schooling)"
_EXP_AFTER_RE = re.compile(
    r"^\s*'?s?'?\s*(?:\(\s*)?(?:of\s+)?"
    r"(?:(?!" + _FILLER_STOP + r"\b)[\w/&+#.'-]+\s+){0,4}?(?:experience|exp|exposure)\b",
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
# "experience required: 2 years", "Experience - 2-4 yrs", "exp of min 2 years",
# "Experienced: 2 years", "Experience in Java - 3 years"
_EXP_BEFORE_RE = re.compile(
    r"\b(?:experienced?|exp)\.?\s*(?:\((?:in\s+)?years?\))?\s*"
    r"(?:(?:in|with|on)\s+[\w/&+#. ]{1,30}?\s*(?=[:=-]))?"
    r"(?:required|requirement|needed|level|range|of)?\s*[:=-]?\s*(?:of\s+)?(?:about\s+|around\s+)?$",
    re.IGNORECASE,
)
# A field whose whole value is the quantity: "Java: 3 years", "SQL - 2 yrs".
# The label must not be a duration ("Duration: 6 months", "Bond: 2 years").
_FIELD_LABEL_RE = re.compile(r"^\s*(?P<label>[A-Za-z][\w/&+#. ()'-]{0,40}?)\s*[:=]\s*$")
_NON_EXPERIENCE_LABEL_RE = re.compile(
    r"\b(?:duration|bond|agreement|contract|age|notice|probation|tenure|term|period|program|programme|"
    r"course|degree|education|qualification|stipend|internship|training|validity|commitment|lock|"
    r"service|salary|ctc|package|founded|established|history)\b",
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
# the company describing itself, not a requirement on the applicant; and
# colleagues: "Mentored by senior engineers with 10+ years of experience",
# "Work alongside engineers who have 8+ years", "Team members have 10 years"
_COLLEAGUES = (
    r"(?:engineers|colleagues|experts|leaders|mentors|professionals|specialists|consultants|developers|"
    r"architects|scientists|veterans|seniors|people|peers|teammates|team\s+members|members|staff|coaches|managers)"
)
_OVER = r"(?:over\s+|more\s+than\s+|nearly\s+|almost\s+|a\s+combined\s+|an\s+average\s+of\s+)?"
_COMPANY_CLAIM_BEFORE_RE = re.compile(
    r"\b(?:we|our\s+\w+|the\s+company|company|firm|team)\s+(?:have|has|bring|brings|boasts?)\s+" + _OVER + r"$"
    r"|\bwith\s+(?:over|more\s+than|nearly|almost)\s+$"
    r"|\b(?:mentored|coached|guided|supported|trained|led|surrounded|backed|taught)\s+by\s+(?:[\w-]+\s+){0,4}?"
    + _COLLEAGUES + r"\s+(?:(?:who|that)\s+(?:have|has|bring)\s+|with\s+|having\s+|boasting\s+)" + _OVER + r"$"
    r"|\b(?:alongside|with|from|among)\s+(?:our\s+|the\s+)?(?:[\w-]+\s+){0,3}?" + _COLLEAGUES
    + r"\s+(?:who|that)\s+(?:have|has|bring)\s+" + _OVER + r"$"
    r"|\b(?:team\s+members|teammates|colleagues|our\s+(?:[\w-]+\s+){0,2}?" + _COLLEAGUES + r")\s+"
    r"(?:have|has|bring|brings|with|boast|boasts)\s+" + _OVER + r"$",
    re.IGNORECASE,
)
# requirements flagged as optional don't make a role experienced
_PREFERRED_RE = re.compile(
    r"\b(?:preferred|preferably|is\s+a\s+plus|a\s+plus|nice\s+to\s+have|desirable|"
    r"advantageous|an\s+advantage|bonus|good\s+to\s+have)\b",
    re.IGNORECASE,
)
_MANDATORY_RE = re.compile(r"\b(?:required|must|mandatory|essential|minimum|at\s+least)\b", re.IGNORECASE)
# A *section heading* that starts optional content: "Preferred qualifications:",
# "Nice to have", "Bonus points". An inline field with a value ("Good to have
# skills : NA", "Nice to have: Kubernetes") is not a heading — it qualifies
# only itself.
_PREFERRED_HEADING_RE = re.compile(
    r"^\s*(?:preferred|desired|desirable|nice[\s-]+to[\s-]+have|good[\s-]+to[\s-]+have|bonus|additional|optional|plus)\b",
    re.IGNORECASE,
)
# Section headings that switch preferred-mode off for the lines below them
_REQUIRED_HEADING_RE = re.compile(
    r"\b(?:minimum|basic|required|requirements?|must[\s-]have|qualifications|eligibility|"
    r"responsibilities|what\s+you(?:'ll)?\s+(?:need|bring|do)|who\s+you\s+are|about)\b",
    re.IGNORECASE,
)
_HEADING_MAX_LEN = 60
_CLAUSE_SPLIT_RE = re.compile(r",|\s+\band\b\s+|\s+\bbut\b\s+")
# "Freshers or candidates with 1-2 years", "1-2 years or freshers" — freshers
# offered as an explicit alternative to a small, unqualified range. Anything
# stronger ("Freshers can apply / 2+ years", "freshers or minimum 2 years")
# is a requirement: explicit experience wins.
_FRESHER_OR_RE = re.compile(r"\bfreshers?\s+or\b|\bor\s+freshers?\b", re.IGNORECASE)
_MAX_ALTERNATIVE_YEARS = 2
# split on sentence ends ("Min. 2 years" stays together: the next char isn't
# a capital), semicolons, bullets and line breaks
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z])|;|\s*[•·▪●]\s*|\n")

# 15+ years is never an entry requirement — it's company boilerplate like
# "with over 150 years of experience in healthcare"
_MAX_PLAUSIBLE_YEARS = 15

# "Prior experience required in sales", "Relevant industry experience
# required", "Must have prior professional experience" — a requirement with
# no number. "Bachelor's degree or equivalent practical experience" is not.
_PRIOR_EXPERIENCE_RE = re.compile(
    r"\b(?:prior|previous|relevant|industry|professional|work)\s+"
    r"(?:(?:work|professional|industry|relevant|hands[\s-]on|domain)\s+)?experience\b",
    re.IGNORECASE,
)
_NEEDS_RE = re.compile(r"\b(?:required|must|mandatory|essential|necessary|needed)\b", re.IGNORECASE)
# requirements that need no "required": "Proven experience as an SAP SD
# Consultant", "a proven track record", and unfilled templates such as
# "at least [X] years of experience" (a real number was meant to be there)
_PROVEN_RE = re.compile(
    r"\bproven\s+(?:track\s+record|(?:[\w-]+\s+){0,2}?experience)\b"
    r"|\b(?:at\s+least|minimum(?:\s+of)?)\s+\[\s*(?:x|n|xx|#)\s*\]\+?\s*(?:years?|yrs?)\b"
    r"|\[\s*(?:x|n|xx|#)\s*\]\+?\s*(?:years?|yrs?)\s+(?:of\s+)?(?:[\w-]+\s+){0,3}?experience\b",
    re.IGNORECASE,
)
_NEGATION_RE = re.compile(
    r"\b(?:no|not|without|nil|zero|none|don't|doesn't|do\s+not|does\s+not|isn't|is\s+not|never)\b|\bn/a\b",
    re.IGNORECASE,
)
_EQUIVALENT_RE = re.compile(r"\b(?:or|and/or)\s+(?:an?\s+)?equivalent\b|\bequivalent\s+(?:\w+\s+)?experience\b", re.IGNORECASE)


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
    if len(sentence) > _HEADING_MAX_LEN or _QUANTITY_RE.search(sentence):
        return False
    # "Label : value" is a field, not a heading
    _, sep, value = sentence.partition(":")
    return not (sep and value.strip())


def parse_experience_requirements(text: str, *, title: bool = False) -> list[ExperienceRequirement]:
    """Return every experience requirement stated in ``text``.

    Quantities are only treated as experience when the wording makes that
    clear: a range/"N+" of years (in job text that is always experience), a
    single number tied to "experience" or "minimum"/"at least", a field whose
    whole value is the quantity ("Java: 3 years"), or the unit-less
    "Experience: 2-4" form. Durations such as "6-month internship", "2 year
    contract", "minimum 2 years service agreement" or "15 years of full-time
    education" are ignored.

    With ``title=True`` any number of years counts ("Engineer — 2 years").

    Requirements stated as preferred / nice-to-have — inline or under a
    "Preferred qualifications" heading — are skipped unless the line itself
    is mandatory ("required", "must", "minimum", "at least"). A small range
    offered as an explicit alternative to freshers ("Freshers or 1-2 years")
    is skipped too.
    """
    reqs: list[ExperienceRequirement] = []
    in_preferred_section = False
    for sentence in _sentences(text):
        if _is_heading(sentence):
            if _PREFERRED_HEADING_RE.search(sentence) or (_PREFERRED_RE.search(sentence) and not _MANDATORY_RE.search(sentence)):
                in_preferred_section = True
                continue
            if _REQUIRED_HEADING_RE.search(sentence):
                in_preferred_section = False
        mandatory_line = bool(_MANDATORY_RE.search(sentence))

        found: list[tuple[re.Match, float, float | None, bool]] = []
        for m in _QUANTITY_RE.finditer(sentence):
            lo = _to_number(m.group("lo"))
            hi = _to_number(m.group("hi")) if m.group("hi") else None
            is_months = m.group("unit").lower().startswith("mo")
            plus = bool(m.group("plus") or m.group("plus2"))
            qualified = bool(m.group("qual") or m.group("qual2"))

            before = sentence[max(0, m.start() - 40):m.start()]
            wide_before = sentence[max(0, m.start() - 90):m.start()]
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
            # "Java: 3 years" — a labelled field whose value is the quantity
            label = _FIELD_LABEL_RE.match(sentence[:m.start()])
            field_value = bool(
                label and not sentence[m.end():].strip(" .;)")
                and not _NON_EXPERIENCE_LABEL_RE.search(label.group("label"))
            )
            if _COMPANY_CLAIM_BEFORE_RE.search(wide_before):
                continue
            if is_months:
                # "3-6 months internship" is a duration, not a requirement;
                # "18+ months" / "6 months minimum" are requirements
                is_requirement = linked_to_experience or plus or bool(m.group("qual2")) or field_value
            elif _DURATION_AFTER_RE.search(after) and not linked_to_experience:
                is_requirement = False
            else:
                is_requirement = (
                    linked_to_experience
                    or title
                    or standalone
                    or field_value
                    or qualified
                    or hi is not None
                    or plus
                )
            if not is_requirement:
                continue
            if is_months:
                lo, hi = lo / 12, (hi / 12 if hi is not None else None)
            alternative = bool(
                _FRESHER_OR_RE.search(sentence) and hi is not None and not plus and not qualified
                and lo <= _MAX_ALTERNATIVE_YEARS and not mandatory_line
            )
            found.append((m, lo, hi, alternative))

        covered = [(m.start(), m.end()) for m, *_ in found]
        for m in _UNITLESS_RE.finditer(sentence):
            lo_start = m.start("lo")
            if any(a <= lo_start < b for a, b in covered):
                continue
            lo = _to_number(m.group("lo"))
            hi = _to_number(m.group("hi")) if m.group("hi") else None
            found.append((m, lo, hi, False))

        if in_preferred_section and not mandatory_line:
            continue
        for m, lo, hi, alternative in found:
            if alternative:
                continue
            if not mandatory_line and _clause_is_optional(sentence, m.start(), m.end()):
                continue
            if lo > _MAX_PLAUSIBLE_YEARS or (hi is not None and hi < lo):
                continue
            reqs.append(ExperienceRequirement(lo, hi, m.group(0).strip()))
    return reqs


def prior_experience_requirement(text: str) -> str:
    """A mandatory experience requirement without a number ("Prior
    experience required in sales"), or ""."""
    in_preferred_section = False
    for sentence in _sentences(text):
        if _is_heading(sentence):
            if _PREFERRED_HEADING_RE.search(sentence):
                in_preferred_section = True
                continue
            if _REQUIRED_HEADING_RE.search(sentence):
                in_preferred_section = False
        proven = _PROVEN_RE.search(sentence)
        if proven and not _NEGATION_RE.search(sentence[:proven.start()][-30:]) and not _PREFERRED_RE.search(sentence) \
                and not (in_preferred_section and not _MANDATORY_RE.search(sentence)):
            return proven.group(0).strip()
        for clause in _CLAUSE_SPLIT_RE.split(sentence):
            m = _PRIOR_EXPERIENCE_RE.search(clause)
            if not m or not _NEEDS_RE.search(clause):
                continue
            if _NEGATION_RE.search(clause) or _EQUIVALENT_RE.search(clause) or _QUANTITY_RE.search(clause):
                continue
            if _PREFERRED_RE.search(clause) or (in_preferred_section and not _MANDATORY_RE.search(clause)):
                continue
            return clause.strip()
    return ""


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
    r"|(?:experience|exp)\s*[:-]?\s*(?:0|zero|nil|none|freshers?)\b(?!\s*(?:-|to)\s*[1-9])"
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
    # job-board metadata: "Seniority level: Entry level", or a line that is just "Entry level"
    r"|(?:seniority|experience|career|job|position)\s+level\s*[:=|-]\s*entry(?:[\s-]*level)?"
    r"|^\s*entry[\s-]*level\s*$"
    r")\b",
    re.IGNORECASE | re.MULTILINE,
)

# A description signal about people the role works *with* or hires, rather
# than the applicant: "mentor recent graduates", "onboard campus hires",
# "responsible for hiring freshers", "build our campus hiring pipeline",
# "the team has many fresh graduates".
_STAFF_CONTEXT_RE = re.compile(
    r"\b(?:mentor\w*|coach\w*|onboard\w*|guid(?:e|es|ing)|supervis\w*|manag\w*|support(?:s|ing)?|"
    r"administ\w*|coordinat\w*|oversee\w*|overseeing|lead(?:s|ing)?|run(?:s|ning)?|teach\w*|"
    r"train(?:s|ing)?|hir(?:e|es|ing)|recruit\w*|interview\w*|sourc(?:e|es|ing)|screen\w*|assess\w*|"
    r"build(?:s|ing)?|driv(?:e|es|ing)|own(?:s|ing)?|responsible\s+for|"
    r"work(?:s|ing)?\s+(?:with|alongside)|alongside|alumni|"
    r"(?:team|teams|colleagues|department|group|function)\s+(?:has|have|includes?|comprises?|consists?\s+of|of))\b"
    r"[^.,;:\n]{0,40}\Z",
    re.IGNORECASE,
)
# ...but the company hiring the applicant is fine: "We are hiring freshers",
# "We are hiring through campus hiring for this role", "Start your career as a fresh graduate"
_APPLICANT_CONTEXT_RE = re.compile(
    r"\bwe(?:'re|\s+are)?\s+(?:actively\s+|currently\s+|now\s+)?(?:hiring|recruiting|inviting|looking\s+for)\b[^.,;:\n]{0,30}\Z"
    r"|\bas\s+(?:an?\s+)?(?:\w+\s+)?\Z",
    re.IGNORECASE,
)
# after the phrase: "recent graduates will report to you", "graduates on your team"
_STAFF_AFTER_RE = re.compile(
    r"^[^.\n]{0,20}?\b(?:(?:will|would|who\s+will)\s+)?(?:report(?:s|ing)?\s+(?:in)?to\s+(?:you|this\s+role|this\s+position)"
    r"|be\s+(?:reporting\s+to|managed\s+by|mentored\s+by|supervised\s+by|trained\s+by)\s+you"
    r"|on\s+your\s+team|in\s+your\s+team|under\s+you(?:r)?\b)"
    r"|^\s*(?:pipeline|programs?|programmes?|process(?:es)?|strategy|events?|initiatives?|efforts?|calendar|operations|targets?|budget)\b",
    re.IGNORECASE,
)


def _applicant_signal(pattern: re.Pattern, text: str) -> re.Match | None:
    """First match of ``pattern`` in ``text`` that is about the applicant."""
    for m in pattern.finditer(text):
        before = text[max(0, m.start() - 60):m.start()]
        after = text[m.end():m.end() + 60]
        if _STAFF_AFTER_RE.search(after):
            continue
        if _STAFF_CONTEXT_RE.search(before) and not _APPLICANT_CONTEXT_RE.search(before):
            continue
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


def has_explicit_zero_experience(text: str) -> bool:
    """True when ``text`` states experience starting at 0 ("0-1 years",
    "Experience: 0-2") and no requirement above 0 — the only card-level
    evidence strong enough to alert without a readable detail page."""
    reqs = parse_experience_requirements(text)
    return bool(reqs) and all(r.min_years == 0 for r in reqs)


def classify_job(title: str, description: str = "", url: str = "", *,
                 strict_text: str = "", posting_evidence: bool = False) -> Classification:
    """Classify a posting from its title and any extra text available.

    ``description`` can be card text, a detail-page description, or both
    concatenated. ``strict_text`` is evidence that may only *reject* (page
    fields outside the description, hidden text, level metadata): a
    requirement found there counts, but a fresher phrase there doesn't.
    ``posting_evidence`` says the scraper read a real JobPosting (ATS API or
    schema.org data), which lets a programme-titled posting be judged as a job.

    Seniority is judged from the title; experience requirements and
    entry-level evidence from title + description. Positive signals never
    override an explicit experience requirement.
    """
    title_n = _normalize(title)
    desc_n = _normalize(description)
    # line breaks kept so a signal's context never spans two bullet points
    desc_lines = "\n".join(n for n in (_normalize(x) for x in (description or "").splitlines()) if n)
    strict_lines = "\n".join(n for n in (_normalize(x) for x in (strict_text or "").splitlines()) if n)
    full = f"{title_n}\n{desc_n}" if desc_n else title_n

    reason = not_a_job_reason(title_n, url, posting_evidence=posting_evidence)
    if reason:
        return Classification(Category.NOT_A_JOB, f"not a job posting: {reason}")

    m = _SENIOR_TITLE_RE.search(title_n)
    if m:
        return Classification(Category.SENIOR, f"seniority in title: '{m.group(0)}'")
    m = _MID_LEVEL_RE.search(title_n)
    if m:
        return Classification(Category.EXPERIENCED, f"experience level in title: '{m.group(0)}'")

    if _NO_FRESHERS_RE.search(full) or _NO_FRESHERS_RE.search(strict_lines):
        return Classification(Category.EXPERIENCED, "posting says freshers are not eligible")

    reqs = parse_experience_requirements(title or "", title=True)
    reqs += parse_experience_requirements(description or "")
    strict_reqs = parse_experience_requirements(strict_text or "")
    experienced = [r for r in reqs + strict_reqs if r.min_years > 0]
    if experienced:
        worst = max(experienced, key=lambda r: r.min_years)
        return Classification(
            Category.EXPERIENCED,
            f"explicit requirement: {_describe(worst)} "
            f"(minimum {_fmt_years(worst.min_years)} yr)",
        )

    level = _level_field_rejection(desc_lines) or _level_field_rejection(strict_lines)
    if level:
        return Classification(Category.EXPERIENCED, f"experience level: '{level}'")
    prior = prior_experience_requirement(description or "") or prior_experience_requirement(strict_text or "")
    if prior:
        return Classification(Category.EXPERIENCED, f"explicit requirement: '{prior}' (prior experience)")

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
        return Classification(Category.ENTRY_LEVEL, f"entry-level signal: '{m.group(0).strip()}'")

    m = _AMBIGUOUS_RE.search(title_n)
    if m:
        return Classification(
            Category.UNKNOWN,
            f"'{m.group(0)}' alone is not evidence of an entry-level role",
        )
    return Classification(Category.UNKNOWN, "no fresher/entry-level evidence")
