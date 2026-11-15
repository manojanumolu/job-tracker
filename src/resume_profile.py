"""Structured résumé intelligence for M6: résumé text -> a validated profile.

Deterministic rules, no AI and no network. Every claim keeps its evidence:
the section it came from and the line that says it. Nothing is invented — a
field the résumé doesn't state stays empty and the profile says so.

What "demonstrated" means here (the matcher relies on it):
  * a skill is **demonstrated** only when a line of an employment entry, an
    internship or a project names it — and that line isn't about merely
    learning it ("familiar with", "learning", "exposure to", ...);
  * a skill only in the Skills list, coursework, a certification or the
    summary is **mentioned**: claimed, not shown;
  * a skill a person adds by hand that the text never names is
    **needs_verification**;
  * an ambiguous word ("Go", "AI", "REST") found outside the Skills list is
    **needs_verification** too (see skill_taxonomy).
Academic projects are never counted as professional employment.

The profile is plain JSON (stored by resume_store.py) with SCHEMA = 1.
Contact details are kept masked: the profile only needs to know they exist.
"""

from __future__ import annotations

import re
from datetime import date

import skill_taxonomy as tax
from locations import INDIA_CITIES

SCHEMA = 1
MAX_ENTRIES = 15
MAX_BULLETS = 10
MAX_LINE = 300
MAX_EVIDENCE = 5

SECTION_WORDS = {
    "summary": ["summary", "profile", "objective", "career objective", "about me", "professional summary",
                "profile summary", "career summary"],
    "education": ["education", "academics", "academic background", "academic details", "educational qualifications",
                  "academic qualifications", "qualifications", "educational background", "education details"],
    "experience": ["experience", "work experience", "professional experience", "employment", "employment history",
                   "work history", "career history", "relevant experience"],
    "internships": ["internships", "internship", "internship experience", "industrial training", "internships & training",
                    "trainings", "training"],
    "projects": ["projects", "academic projects", "personal projects", "key projects", "project work",
                 "major project", "minor project", "projects undertaken", "side projects", "selected projects"],
    "skills": ["skills", "technical skills", "key skills", "core competencies", "technologies", "tech stack",
               "skill set", "skillset", "tools", "technical proficiency", "tools & technologies",
               "tools and technologies", "areas of expertise", "programming languages", "languages & tools"],
    "certifications": ["certifications", "certification", "certificates", "licenses & certifications",
                       "licenses and certifications", "courses & certifications", "online courses", "courses"],
    "achievements": ["achievements", "awards", "honors", "honours", "accomplishments", "awards & achievements",
                     "hackathons", "competitions"],
    "coursework": ["relevant coursework", "coursework", "subjects", "key courses", "courses taken"],
    "other": ["languages", "languages known", "hobbies", "interests", "extracurricular activities",
              "extra-curricular activities", "activities", "positions of responsibility", "volunteering",
              "volunteer experience", "publications", "declaration", "personal details", "personal information",
              "references", "strengths", "leadership"],
}
_HEADING = {w: sec for sec, words in SECTION_WORDS.items() for w in words}
SECTIONS = tuple(SECTION_WORDS)

KINDS = ("employment", "internship", "academic_project", "personal_project", "project", "certification",
         "coursework", "listed", "education", "mentioned", "user")
DEMONSTRATING = ("employment", "internship", "academic_project", "personal_project", "project")
_RANK = {k: i for i, k in enumerate(KINDS)}

_BULLET = re.compile(r"^\s*(?:[•\-*–—◦▪►✓✔>]|o\s|\d{1,2}[.)]\s)\s*")
_WEAK = re.compile(r"^\s*(?:currently |now )?learning\b|\b(?:familiar(?:ity)? with|(?:currently|now) learning|"
                   r"learning (?:to|about|how)|learn(?:ed|t|ing) the basics|exposure to|basic (?:knowledge|understanding)|"
                   r"introduction to|interested in|aspir(?:ing|e) to|beginner|currently exploring|want to learn|"
                   r"knowledge of)\b", re.I)

_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_MON = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_YEAR = r"(?:19[89]\d|20[0-4]\d)"
_POINT = rf"(?:{_MON}\.?\s*'?\s*(?:{_YEAR}|\d{{2}})|(?:0?[1-9]|1[0-2])\s*[/.-]\s*{_YEAR}|{_YEAR}\s*[/.-]\s*(?:0?[1-9]|1[0-2])(?!\d)|{_YEAR})"
_NOW = r"(?:present|current(?:ly)?|now|ongoing|till date|to date|today)"
_RANGE = re.compile(rf"({_POINT})\s*(?:-|–|—|to|till|until|→)\s*({_POINT}|{_NOW})", re.I)
_SINGLE = re.compile(_POINT, re.I)

_DEGREES = [
    ("doctorate", r"ph\.?\s?d|doctorate"),
    ("masters", r"m\.?\s?tech|m\.e\.?|m\.?\s?sc|mca|mba|m\.?\s?s\.?(?=\s+in\b)|master'?s?|m\.?\s?com|pgdm|post[- ]?graduat\w*"),
    ("bachelors", r"b\.?\s?tech|b\.e\.?|b\.?\s?e\.?(?=\s*\(|\s+in\b)|b\.?\s?sc|bca|bba|b\.?\s?com|b\.?\s?s\.?(?=\s+in\b)|"
                  r"bachelor'?s?|b\.?\s?a\.?(?=\s+in\b)|b\.?\s?arch|undergraduate|graduat(?:e|ion) in"),
    ("diploma", r"diploma|polytechnic"),
    ("school", r"12th|xii|hsc|intermediate|class\s*(?:12|10|x|xii)|senior secondary|higher secondary|10th|ssc|cbse|icse"),
]
_DEGREE_RE = [(lvl, re.compile(rf"(?<![A-Za-z])(?:{p})(?![A-Za-z])", re.I)) for lvl, p in _DEGREES]
LEVELS = {"school": 0, "diploma": 1, "bachelors": 2, "masters": 3, "doctorate": 4}

SPECIALISATIONS = {
    "Computer Science": r"computer science|\bcse\b|\bcs\b|computer engineering|computer science and engineering|\bcsc\b",
    "Information Technology": r"information technology|(?-i:\bIT\b)",
    "Electronics & Communication": r"electronics (?:and|&) communication|\bece\b|electronics (?:and|&) telecommunication|\bentc\b|electronics",
    "Electrical": r"electrical(?: and electronics)?|\beee\b|\bee\b",
    "Mechanical": r"mechanical|\bmech\b",
    "Civil": r"\bcivil\b",
    "Data Science": r"data science",
    "AI & ML": r"artificial intelligence|\bai\s*(?:&|and)\s*ml\b|\baiml\b|\bai/ml\b|machine learning",
    "Mathematics": r"mathematics|\bmaths?\b",
    "Statistics": r"statistics",
    "Physics": r"physics",
    "Commerce": r"commerce|\bb\.?\s?com\b",
    "Business": r"business administration|management|\bbba\b|\bmba\b",
    "Computer Applications": r"computer applications|\bbca\b|\bmca\b",
    "Chemical": r"chemical",
    "Biotechnology": r"biotech\w*",
}
_SPEC_RE = {k: re.compile(v, re.I) for k, v in SPECIALISATIONS.items()}

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_PHONE = re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}(?!\d)|(?<!\d)\+\d{1,3}[\s-]?\d{3,4}[\s-]?\d{3,4}[\s-]?\d{0,4}(?!\d)")
_LINK = re.compile(r"(?:https?://)?(?:www\.)?(linkedin\.com|github\.com|gitlab\.com|leetcode\.com|kaggle\.com|"
                   r"hackerrank\.com|codechef\.com|behance\.net)(/[\w\-./%]*)?", re.I)
_INSTITUTION = re.compile(r"universit|college|institute|\biit\b|\bnit\b|\biiit\b|school|academy|vidyalaya|polytechnic", re.I)
_CITY_RE = re.compile(r"\b(" + "|".join(sorted(map(re.escape, INDIA_CITIES), key=len, reverse=True)) + r")\b", re.I)
_SCORE = re.compile(r"(?:cgpa|gpa|cpi|sgpa)\s*[:\-]?\s*(\d{1,2}(?:\.\d{1,2})?)\s*(?:/\s*(\d{1,2}))?|(\d{2}(?:\.\d{1,2})?)\s*%", re.I)
_ACADEMIC = re.compile(r"\b(?:academic|course project|coursework|capstone|final[- ]year|semester|mini[- ]project|"
                       r"major[- ]project|minor[- ]project|b\.?\s?tech project|thesis|dissertation|college project|"
                       r"university project|lab project|guided by|under the guidance)\b", re.I)
_PERSONAL = re.compile(r"\b(?:personal project|side project|self[- ]initiated|hobby project|open[- ]source|"
                       r"freelance)\b|github\.com", re.I)
_INTERN = re.compile(r"\bintern(?:ship)?s?\b|\bapprentice(?:ship)?\b|\bsummer trainee\b|\bindustrial training\b", re.I)


# ── small helpers ────────────────────────────────────────────────────────────

def _clip(s: str, n: int = MAX_LINE) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def heading_of(line: str) -> str | None:
    """The section a heading line opens, or None for an ordinary line."""
    s = line.strip().strip(":-–—|•*#=_ ").strip()
    if not s or len(s) > 48 or len(s.split()) > 6:
        return None
    key = re.sub(r"\s+", " ", s.lower().replace("&amp;", "&"))
    if key in _HEADING:
        return _HEADING[key]
    key2 = re.sub(r"[^a-z& ]", "", key).strip()
    return _HEADING.get(key2)


def _point(text: str, end: bool = False) -> tuple[int, int] | None:
    t = text.strip().lower().replace("’", "'")
    m = re.fullmatch(rf"({_MON})\.?\s*'?\s*(\d{{4}}|\d{{2}})", t)
    if m:
        y = int(m.group(2))
        y = y + 2000 if y < 100 else y
        return (y, _MONTHS[m.group(1)[:3]])
    m = re.fullmatch(r"(\d{1,2})\s*[/.-]\s*(\d{4})", t)
    if m:
        return (int(m.group(2)), max(1, min(12, int(m.group(1)))))
    m = re.fullmatch(r"(\d{4})\s*[/.-]\s*(\d{1,2})", t)
    if m:
        return (int(m.group(1)), max(1, min(12, int(m.group(2)))))
    m = re.fullmatch(r"\d{4}", t)
    if m:
        return (int(t), 12 if end else 1)
    return None


def date_range(text: str, today: date) -> dict | None:
    """The first date range in ``text``: {"start": "YYYY-MM", "end": "YYYY-MM"
    or "present", "months": n, "conflict": bool}. None if there isn't one."""
    m = _RANGE.search(text or "")
    if not m:
        return None
    start = _point(m.group(1))
    present = bool(re.fullmatch(_NOW, m.group(2).strip(), re.I))
    end = (today.year, today.month) if present else _point(m.group(2), end=True)
    if start is None or end is None:
        return None
    months = (end[0] - start[0]) * 12 + (end[1] - start[1]) + 1
    conflict = months <= 0 or start > (today.year, today.month)
    return {"start": f"{start[0]:04d}-{start[1]:02d}", "end": "present" if present else f"{end[0]:04d}-{end[1]:02d}",
            "months": max(0, months), "conflict": conflict}


def _strip_dates(s: str) -> str:
    s = _RANGE.sub(" ", s)
    s = re.sub(rf"\(\s*{_POINT}\s*\)", " ", s, flags=re.I)
    s = re.sub(r"\(\s*(?:expected|pursuing|ongoing)\s*\)|\b(?:expected|pursuing)\b", " ", s, flags=re.I)
    s = re.sub(r"\(\s*\)", " ", s)
    return " ".join(s.split()).strip(" |,–—-:")


def mask_email(addr: str) -> str:
    local, _, domain = addr.partition("@")
    return (local[:1] + "•••@" + domain) if local and domain else ""


def mask_phone(num: str) -> str:
    digits = re.sub(r"\D", "", num)
    return ("•" * max(0, len(digits) - 2)) + digits[-2:] if len(digits) >= 8 else ""


# ── sections and entries ─────────────────────────────────────────────────────

def split_sections(text: str) -> list[tuple[str, str, list[str]]]:
    """[(section, heading as written, lines)], in document order. Text before
    the first heading is the "header" (name, contact)."""
    out: list[tuple[str, str, list[str]]] = [("header", "", [])]
    for raw in (text or "").split("\n"):
        line = raw.strip()
        sec = heading_of(line) if line else None
        if sec:
            out.append((sec, line.strip(" :"), []))
            continue
        out[-1][2].append(line)
    return [s for s in out if s[0] != "header" or any(s[2])] or [("header", "", [])]


def split_entries(lines: list[str]) -> list[dict]:
    """Group a section's lines into entries: a few header lines (title,
    organisation, dates) followed by their bullet points."""
    entries: list[dict] = []
    cur: dict | None = None
    for line in lines:
        if not line.strip():
            cur = None if cur is None or cur["bullets"] or len(cur["head"]) >= 2 else cur
            continue
        if _BULLET.match(line):
            text = _BULLET.sub("", line).strip()
            if not text:
                continue
            if cur is None:
                cur = {"head": [], "bullets": []}
                entries.append(cur)
            cur["bullets"].append(text)
            continue
        if cur is not None and not cur["bullets"] and len(cur["head"]) < 3:
            cur["head"].append(line.strip())
        else:
            cur = {"head": [line.strip()], "bullets": []}
            entries.append(cur)
    return entries[:MAX_ENTRIES * 2]


def _education_entries(lines: list[str]) -> list[dict]:
    """Education lines grouped per qualification: a line naming a degree
    starts a new entry when the current one already has a degree."""
    entries: list[dict] = []
    cur: dict | None = None
    for line in lines:
        text = _BULLET.sub("", line).strip()
        if not text:
            cur = None
            continue
        has_degree = any(rx.search(text) for _, rx in _DEGREE_RE)
        if cur is None or (has_degree and cur["degree"]):
            cur = {"head": [], "bullets": [], "degree": False}
            entries.append(cur)
        cur["head"].append(text)
        cur["degree"] = cur["degree"] or has_degree
    return entries[:MAX_ENTRIES]


def _title_org(head: list[str]) -> tuple[str, str]:
    parts: list[str] = []
    for h in head:
        h = _strip_dates(h)
        parts += [p.strip() for p in re.split(r"\s+(?:at|@)\s+|\s*\|\s*|\s+[–—-]\s+|,\s+(?=[A-Z])", h) if p.strip()]
    parts = [p for p in parts if not _CITY_RE.fullmatch(p) and len(p) > 1]
    title = parts[0] if parts else ""
    org = parts[1] if len(parts) > 1 else ""
    return _clip(title, 120), _clip(org, 120)


# ── the profile ──────────────────────────────────────────────────────────────

_URL = re.compile(r"(?:https?://|www\.)\S+|\b[\w.-]+\.(?:com|in|io|org|net|dev|app)/\S*", re.I)


def skills_in(line: str) -> dict[str, bool]:
    """Skills a line names, links removed first: a github.com link is not
    the skill Git, and a URL path can spell anything."""
    return tax.find_skills(_URL.sub(" ", line))


def _evidence_kind(section: str, entry_kind: str, line: str) -> str:
    if section in ("experience", "internships", "projects"):
        return "mentioned" if _WEAK.search(line) else entry_kind
    return {"skills": "listed", "coursework": "coursework", "certifications": "certification",
            "education": "education"}.get(section, "mentioned")


def build_profile(text: str, *, header_text: str = "", today: date | None = None) -> dict:
    """The structured profile of a résumé's text (see the module doc)."""
    today = today or date.today()
    warnings: list[str] = []
    sections = split_sections(text)
    found = [s for s, _, _ in sections if s != "header"]
    profile: dict = {"schema": SCHEMA, "name": "", "contact": {}, "sections_found": sorted(set(found), key=SECTIONS.index),
                     "education": [], "experience": [], "projects": [], "skills": {}, "certifications": [],
                     "achievements": [], "coursework": [], "summary": "", "totals": {}, "graduation_year": None,
                     "warnings": warnings}
    all_text = (header_text + "\n" + text) if header_text else text

    # name: the first line, when it reads like a name (never guessed from elsewhere)
    first = next((ln.strip() for ln in (header_text + "\n" + text).split("\n") if ln.strip()), "")
    if (2 <= len(first.split()) <= 4 and len(first) <= 50 and not re.search(r"[\d@/|:]", first)
            and heading_of(first) is None and not re.search(r"(?i)\b(?:resume|résumé|curriculum|vitae|cv|biodata|profile)\b", first)
            and all(w[:1].isupper() for w in first.split() if w[:1].isalpha())
            and not tax.find_skills(first)):
        profile["name"] = first.title() if first.isupper() else first

    emails, phones = _EMAIL.findall(all_text), _PHONE.findall(all_text)
    links = sorted({m.group(1).lower() for m in _LINK.finditer(all_text)})
    head_lines = [ln for s, _, lines in sections[:1] for ln in lines][:12] + header_text.split("\n")[:6]
    city = next((m.group(1).title() for ln in head_lines for m in [_CITY_RE.search(ln)] if m), "")
    profile["contact"] = {"email": mask_email(emails[0]) if emails else "", "phone": mask_phone(phones[0]) if phones else "",
                          "links": links, "location": city}
    if not emails:
        warnings.append("No email address was found.")

    evidence: dict[str, list[dict]] = {}

    def note(skill: str, certain: bool, section: str, kind: str, line: str, entry: int | None) -> None:
        if not certain and section != "skills":
            kind = "needs_verification"
        evidence.setdefault(skill, []).append({"section": section, "entry": entry, "kind": kind, "text": _clip(line, 220),
                                               "certain": certain or section == "skills"})

    for section, heading, lines in sections:
        if section in ("experience", "internships", "projects"):
            heading_academic = bool(re.search(r"academic", heading, re.I))
            heading_personal = bool(re.search(r"personal|side", heading, re.I))
            for e in split_entries(lines):
                head, bullets = e["head"], e["bullets"]
                if not head and not bullets:
                    continue
                body = " ".join(head + bullets)
                rng = next((r for r in (date_range(h, today) for h in head + bullets[:1]) if r), None)
                title, org = _title_org(head or bullets[:1])
                if section == "projects":
                    kind = ("academic_project" if heading_academic or _ACADEMIC.search(body) else
                            "personal_project" if heading_personal or _PERSONAL.search(body) else "project")
                    bucket = profile["projects"]
                    item = {"kind": kind, "title": title or _clip(bullets[0] if bullets else "", 80),
                            "bullets": [_clip(b) for b in bullets[:MAX_BULLETS]], "dates": rng}
                else:
                    kind = "internship" if section == "internships" or _INTERN.search(" ".join(head)) else "employment"
                    bucket = profile["experience"]
                    item = {"kind": kind, "title": title, "organisation": org, "dates": rng,
                            "bullets": [_clip(b) for b in bullets[:MAX_BULLETS]]}
                    if rng and rng["conflict"]:
                        warnings.append(f"Check the dates of “{title or org}”: the end is before the start, or the "
                                        "start is in the future.")
                if len(bucket) >= MAX_ENTRIES:
                    continue
                idx = len(bucket)
                skills_here: set[str] = set()
                for ln in head + bullets:
                    for skill, certain in skills_in(ln).items():
                        note(skill, certain, section, _evidence_kind(section, kind, ln), ln, idx)
                        if certain:
                            skills_here.add(skill)
                item["skills"] = tax.sort_skills(skills_here)
                bucket.append(item)
        elif section == "education":
            for e in _education_entries(lines):
                block = e["head"] + e["bullets"]
                body = " ".join(block)
                level = next((lvl for lvl, rx in _DEGREE_RE if rx.search(body)), "")
                if not level and not _INSTITUTION.search(body):
                    for ln in block:
                        for skill, certain in skills_in(ln).items():
                            note(skill, certain, section, "coursework", ln, None)
                    continue
                deg_m = next((rx.search(body) for lvl, rx in _DEGREE_RE if lvl == level), None) if level else None
                spec = next((k for k, rx in _SPEC_RE.items() if rx.search(body)), "")
                years = [int(y) for y in re.findall(_YEAR, body)]
                rng = next((r for r in (date_range(b, today) for b in block) if r), None)
                end_year = int(rng["end"][:4]) if rng and rng["end"] != "present" else (max(years) if years else None)
                if rng and rng["end"] == "present":
                    end_year = max(years) if years and max(years) >= today.year else None
                expected = bool(re.search(r"expected|pursuing|ongoing|present|current", body, re.I)) or bool(
                    end_year and end_year > today.year)
                sm = _SCORE.search(body)
                score = ""
                if sm:
                    score = (f"{sm.group(1)}/{sm.group(2) or '10'}" if sm.group(1) else f"{sm.group(3)}%")
                inst = next((_clip(_strip_dates(ln), 120) for ln in block if _INSTITUTION.search(ln)), "")
                profile["education"].append({
                    "level": level, "degree": _clip(deg_m.group(0), 40) if deg_m else "", "specialization": spec,
                    "institution": inst, "end_year": end_year, "expected": expected, "score": score,
                    "evidence": _clip(body, 220)})
                for ln in block:
                    for skill, certain in skills_in(ln).items():
                        note(skill, certain, section, "education", ln, len(profile["education"]) - 1)
        else:
            for ln in lines:
                if not ln.strip():
                    continue
                clean = _BULLET.sub("", ln).strip()
                if section == "certifications":
                    profile["certifications"].append(_clip(clean, 160))
                elif section == "achievements":
                    profile["achievements"].append(_clip(clean, 200))
                elif section == "coursework":
                    profile["coursework"] += [_clip(c.strip(), 80) for c in re.split(r"[,;|•]", clean) if c.strip()]
                elif section == "summary":
                    profile["summary"] = _clip((profile["summary"] + " " + clean).strip(), 600)
                if section in ("header", "other") and not skills_in(clean):
                    continue
                for skill, certain in skills_in(clean).items():
                    kind = _evidence_kind(section, "", clean)
                    note(skill, certain, section, kind, clean, None)
    for key in ("certifications", "achievements", "coursework"):
        profile[key] = list(dict.fromkeys(profile[key]))[:30]

    # skills: the strongest evidence decides the status
    for skill in tax.sort_skills(evidence):
        ev = sorted(evidence[skill], key=lambda e: (not e["certain"], _RANK.get(e["kind"], 99)))
        kinds = sorted({e["kind"] for e in ev}, key=lambda k: _RANK.get(k, 99))
        certain = [e for e in ev if e["certain"]]
        if not certain:
            status = "needs_verification"
        elif any(e["kind"] in DEMONSTRATING for e in certain):
            status = "demonstrated"
        else:
            status = "mentioned"
        profile["skills"][skill] = {"category": tax.category(skill), "status": status, "kinds": kinds,
                                    "evidence": ev[:MAX_EVIDENCE], "source": "resume"}

    # totals: months of employment and of internships (overlaps counted once)
    def months(kind: str) -> int:
        spans = []
        for x in profile["experience"]:
            r = x.get("dates")
            if x["kind"] == kind and r and not r["conflict"]:
                s = int(r["start"][:4]) * 12 + int(r["start"][5:7])
                e = (today.year * 12 + today.month) if r["end"] == "present" else int(r["end"][:4]) * 12 + int(r["end"][5:7])
                spans.append((s, e))
        total, last = 0, None
        for s, e in sorted(spans):
            if last is not None and s <= last:
                s = last + 1
            if e >= s:
                total += e - s + 1
                last = e if last is None else max(last, e)
        return total

    profile["totals"] = {"employment_months": months("employment"), "internship_months": months("internship"),
                         "projects": len(profile["projects"]),
                         "undated_employment": sum(1 for x in profile["experience"]
                                                   if x["kind"] == "employment" and not x.get("dates"))}
    degrees = [e for e in profile["education"] if e["level"] and e["level"] != "school"]
    top = max(degrees, key=lambda e: (LEVELS[e["level"]], e["end_year"] or 0), default=None)
    profile["graduation_year"] = top["end_year"] if top else None
    if not profile["education"]:
        warnings.append("No education section was recognised.")
    if not profile["skills"]:
        warnings.append("No skills this app knows were found.")
    if not found:
        warnings.append("No standard section headings (Education, Experience, Projects, Skills) were found.")
    return profile


def skill_rows(profile: dict) -> list[dict]:
    """Skills grouped for display: [{skill, category, status, kinds, quote}]."""
    rows = []
    for skill, info in (profile.get("skills") or {}).items():
        ev = info.get("evidence") or [{}]
        rows.append({"skill": skill, "category": info.get("category", ""), "status": info.get("status", ""),
                     "kinds": info.get("kinds", []), "quote": ev[0].get("text", ""), "section": ev[0].get("section", "")})
    return rows


def apply_corrections(profile: dict, corrections: dict) -> dict:
    """The profile with a person's corrections applied (the stored profile
    stays as extracted, so a correction can be undone):

      remove_skills   [skill]  not theirs — dropped
      add_skills      [skill]  theirs, but the text doesn't show it — kept
                               as needs_verification with source "user"
      confirm_skills  [skill]  they confirm an ambiguous/needs-verification
                               hit found in the text — becomes mentioned
                               (or demonstrated if a project/job names it)
      experience_kind {index: "employment"|"internship"}
      project_kind    {index: "academic_project"|"personal_project"|"project"}
      education       {index: {field: value}} for level, specialization, end_year
      name            str
    """
    import copy
    p = copy.deepcopy(profile)
    c = corrections or {}
    if isinstance(c.get("name"), str):
        p["name"] = _clip(c["name"], 60)
    for idx, kind in (c.get("experience_kind") or {}).items():
        i = int(idx)
        if 0 <= i < len(p["experience"]) and kind in ("employment", "internship"):
            p["experience"][i]["kind"] = kind
            for info in p["skills"].values():
                for e in info["evidence"]:
                    if e["section"] in ("experience", "internships") and e["entry"] == i and e["kind"] in DEMONSTRATING:
                        e["kind"] = kind
    for idx, kind in (c.get("project_kind") or {}).items():
        i = int(idx)
        if 0 <= i < len(p["projects"]) and kind in ("academic_project", "personal_project", "project"):
            p["projects"][i]["kind"] = kind
            for info in p["skills"].values():
                for e in info["evidence"]:
                    if e["section"] == "projects" and e["entry"] == i and e["kind"] in DEMONSTRATING:
                        e["kind"] = kind
    for idx, fields in (c.get("education") or {}).items():
        i = int(idx)
        if 0 <= i < len(p["education"]) and isinstance(fields, dict):
            if fields.get("level") in LEVELS:
                p["education"][i]["level"] = fields["level"]
            if fields.get("specialization") in SPECIALISATIONS or fields.get("specialization") == "":
                p["education"][i]["specialization"] = fields["specialization"]
            if isinstance(fields.get("end_year"), int) and 1980 <= fields["end_year"] <= 2045:
                p["education"][i]["end_year"] = fields["end_year"]
    for skill in c.get("remove_skills") or []:
        p["skills"].pop(skill, None)
    for skill in c.get("confirm_skills") or []:
        info = p["skills"].get(skill)
        if info and info["status"] == "needs_verification" and info.get("source") == "resume":
            for e in info["evidence"]:
                e["certain"] = True
                if e["kind"] == "needs_verification":
                    e["kind"] = {"experience": "employment", "internships": "internship",
                                 "projects": "project"}.get(e["section"], "mentioned")
            info["status"] = "demonstrated" if any(e["kind"] in DEMONSTRATING for e in info["evidence"]) else "mentioned"
            info["confirmed"] = True
    for skill in c.get("add_skills") or []:
        if tax.known(skill) and skill not in p["skills"]:
            p["skills"][skill] = {"category": tax.category(skill), "status": "needs_verification", "kinds": ["user"],
                                  "evidence": [{"section": "user", "entry": None, "kind": "user", "certain": False,
                                                "text": "Added by you; the résumé text doesn't show it"}],
                                  "source": "user"}
    p["skills"] = {s: p["skills"][s] for s in tax.sort_skills(p["skills"])}
    degrees = [e for e in p["education"] if e["level"] and e["level"] != "school"]
    top = max(degrees, key=lambda e: (LEVELS[e["level"]], e["end_year"] or 0), default=None)
    p["graduation_year"] = top["end_year"] if top else None
    return p
