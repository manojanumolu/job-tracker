"""What a job asks for, read from its text (M6, Nov 2026).

Turns a posting's text into requirements the matcher can compare with a
résumé — each one with the line that states it:

  skills          {skill: {importance, quote, certain}}
                  importance: "mandatory" (a requirements section or a
                  "must / required" line), "preferred" ("nice to have",
                  "is a plus", a preferred section) or "unspecified" (named,
                  but the posting doesn't say which)
  experience      {min_years, max_years, fresher, quote} or None
  education       {levels, specializations, years, quote} or None
  employment_type full-time / internship / contract / part-time / ""
  seniority       from the title: senior / lead / manager / ""
  conditions      other stated eligibility conditions (minimum marks, no
                  backlogs, location restrictions, ...), each a quote
  contradictions  e.g. "fresher" and "3+ years" in the same posting

Where the text comes from (``source``), best first:
  * "pasted"   the full description an admin pasted for this job (kept in
               their own account)
  * "stored"   the requirement lines the scanner keeps since M6
               (evidence.requirements, scraper._posting_requirements)
  * "tracker"  only the title and the experience lines kept since M1 —
               the result says how little that is ("quality")

Descriptive only: nothing here changes whether a job is listed or emailed
(the scanner's classifier and job_filters decide that, unchanged).
"""

from __future__ import annotations

import hashlib
import json
import re

import skill_taxonomy as tax

MAX_LINES = 400
MAX_LINE = 240

_MANDATORY_HEAD = re.compile(
    r"^(?:requirements?|required(?: skills| qualifications)?|minimum qualifications?|basic qualifications?|"
    r"must[- ]haves?|mandatory(?: skills)?|essential(?: skills| criteria)?|what you(?:'ll)? need|what we(?:'re| are) "
    r"looking for|who you are|qualifications?|eligibility(?: criteria)?|skills(?: required)?|key skills|"
    r"technical skills|you (?:have|bring|will need)|key requirements|requirements and qualifications|"
    r"skills (?:and|&) qualifications|what you bring|your profile|candidate profile|job requirements?)\s*:?$", re.I)
_PREFERRED_HEAD = re.compile(
    r"^(?:preferred(?: qualifications?| skills| experience)?|nice[- ]to[- ]haves?|good[- ]to[- ]haves?|bonus(?: points)?|"
    r"desirable(?: skills)?|desired(?: skills| profile| qualifications)?|additional (?:qualifications|skills)|plus(?:es)?|"
    r"it(?:'s| is| would be) (?:a plus|great) if you|added advantages?|optional skills)\s*:?$", re.I)
_CONTEXT_HEAD = re.compile(
    r"^(?:responsibilities|key responsibilities|what you(?:'ll| will) do|the role|role overview|about (?:us|the role|the "
    r"team|the company)|job description|overview|benefits|perks|what we offer|why join us|your role|day to day|"
    r"duties)\s*:?$", re.I)
_PREFERRED_CUE = re.compile(r"\b(?:preferred|preferably|nice[- ]to[- ]have|good[- ]to[- ]have|(?:is|are|would be) "
                            r"(?:a |an )?(?:plus|bonus|advantage)|plus point|added advantage|advantageous|desirable|"
                            r"ideally|optional|bonus)\b", re.I)
_MANDATORY_CUE = re.compile(r"\b(?:must|required|requires?|mandatory|minimum|essential|need(?:s)? to have|should have|"
                            r"strong (?:knowledge|understanding|proficiency|command)|proficien\w+ (?:in|with)|"
                            r"hands[- ]on (?:experience|knowledge)|solid (?:knowledge|understanding))\b", re.I)

_FRESHER = re.compile(r"\b(?:fresher?s?|entry[- ]level|new grad(?:uate)?s?|recent graduates?|campus (?:hire|hiring|"
                      r"recruitment|drive)|graduate (?:trainee|engineer|program(?:me)?)|no (?:prior )?experience "
                      r"(?:is )?required|0\s*(?:-|–|to)\s*[12]\s*(?:years?|yrs?))\b", re.I)
_YEARS_EXP = [
    (re.compile(r"(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})\s*\+?\s*(?:years?|yrs?)", re.I), "range"),
    (re.compile(r"(?:minimum|min\.?|at least|over|more than)\s*(?:of\s*)?(\d{1,2})\s*\+?\s*(?:years?|yrs?)", re.I), "min"),
    (re.compile(r"(\d{1,2})\s*(?:\+|plus)\s*(?:years?|yrs?)", re.I), "min"),
    (re.compile(r"(\d{1,2})\s*(?:years?|yrs?)\s*(?:of\s*)?(?:\w+\s){0,3}?(?:experience|exp\b)", re.I), "min"),
]
_EXP_WORD = re.compile(r"experience|exp\b|years?|yrs?", re.I)

_LEVEL_RE = [
    ("doctorate", re.compile(r"\bph\.?\s?d\b|doctorate", re.I)),
    ("masters", re.compile(r"\bm\.?\s?tech\b|\bm\.e\b|\bm\.?\s?sc\b|\bmca\b|\bmba\b|master'?s|post[- ]?graduat", re.I)),
    ("bachelors", re.compile(r"\bb\.?\s?tech\b|\bb\.e\b|\bb\.?\s?sc\b|\bbca\b|\bbba\b|\bb\.?\s?com\b|bachelor'?s?|"
                             r"\bgraduat(?:e|es|ion)\b(?! trainee)|\bbe\s*/\s*b\.?tech|\bug\b|undergraduate|"
                             r"engineering degree|degree in", re.I)),
    ("diploma", re.compile(r"\bdiploma\b", re.I)),
]
_EDU_LINE = re.compile(r"degree|b\.?\s?tech|\bb\.e\b|\bbe\b|bachelor|master|m\.?\s?tech|\bmca\b|\bbca\b|b\.?\s?sc|"
                       r"graduat|diploma|ph\.?d|batch|pass(?:ed)?[- ]?outs?|year of passing|\byop\b|cgpa|percentage|"
                       r"qualification|discipline|branch|stream", re.I)
_GRAD_YEARS = re.compile(r"\b(20[1-4]\d)\b")
_BATCH = re.compile(r"batch|pass(?:ed|ing)?[- ]?(?:outs?|year)|year of passing|\byop\b|graduat\w*|class of", re.I)
_EMP_TYPE = [("internship", re.compile(r"\binternship\b|\bintern\b(?!al|ation)", re.I)),
             ("contract", re.compile(r"\bcontract(?:ual)?\b|\bc2h\b|contract[- ]to[- ]hire|temporary", re.I)),
             ("part-time", re.compile(r"\bpart[- ]time\b", re.I)),
             ("full-time", re.compile(r"\bfull[- ]time\b|\bpermanent\b", re.I))]
_SENIOR = [("manager", re.compile(r"\b(?:manager|head of|director)\b", re.I)),
           ("lead", re.compile(r"\b(?:lead|principal|staff|architect)\b", re.I)),
           ("senior", re.compile(r"\b(?:senior|sr\.?|sde\s*(?:iii|3)|level\s*[3-9])\b", re.I))]
_CONDITIONS = [
    ("marks", re.compile(r"(?:minimum|min\.?|at least|above|>=?)\s*(\d{2})\s*%|(\d{2})\s*%\s*(?:and above|or above|"
                         r"throughout|aggregate|in all)|cgpa\s*(?:of\s*)?(?:>=?|above|of at least|minimum|min\.?)?\s*"
                         r"(\d(?:\.\d{1,2})?)", re.I)),
    ("backlogs", re.compile(r"\bno (?:active |standing |current )?backlogs?\b|\bbacklogs?\b", re.I)),
    ("location", re.compile(r"\b(?:must be (?:based|located) in|only (?:candidates|applicants) (?:from|based in|"
                            r"located in)|local candidates only|should be (?:based|located) in|candidates from)\b", re.I)),
    ("relocation", re.compile(r"\b(?:willing(?:ness)? to relocate|relocation)\b", re.I)),
    ("shifts", re.compile(r"\b(?:night|rotational|rotating|us) shifts?\b", re.I)),
    ("bond", re.compile(r"\b(?:service (?:agreement|bond)|bond of|\bbond\b)", re.I)),
    ("gap", re.compile(r"\b(?:education gap|gap in education|year gap|no gaps?)\b", re.I)),
    ("citizenship", re.compile(r"\b(?:indian (?:citizens?|nationals?)|citizenship|work (?:permit|authori[sz]ation))\b", re.I)),
]
from resume_profile import SPECIALISATIONS  # noqa: E402  (one list for both sides)
_SPEC_RE = {k: re.compile(v, re.I) for k, v in SPECIALISATIONS.items()}
_ANY_BRANCH = re.compile(r"any (?:branch|stream|discipline|specialization|graduate)|all branches|all streams", re.I)
_BULLET = re.compile(r"^\s*(?:[•\-*–—◦▪►✓✔>]|o\s|\d{1,2}[.)]\s)\s*")
_URL = re.compile(r"(?:https?://|www\.)\S+", re.I)
_OR = re.compile(r"\bor\b|\beither\b|\bany (?:one )?of\b", re.I)


def _clip(s: str, n: int = MAX_LINE) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _line_skills(line: str) -> dict[str, bool]:
    return tax.find_skills(_URL.sub(" ", line))


def classify_lines(text: str) -> list[dict]:
    """[{text, importance}] for every content line of a posting: importance
    from the section the line sits in, overridden by an explicit cue on the
    line itself ("preferred", "must have")."""
    out: list[dict] = []
    current = "unspecified"
    for raw in (text or "").replace("\r", "\n").split("\n")[:MAX_LINES]:
        line = _BULLET.sub("", raw).strip()
        if not line:
            continue
        head = line.rstrip(":").strip()
        if len(head) <= 60 and len(head.split()) <= 8:
            if _PREFERRED_HEAD.match(head):
                current = "preferred"
                continue
            if _MANDATORY_HEAD.match(head):
                current = "mandatory"
                continue
            if _CONTEXT_HEAD.match(head):
                current = "context"
                continue
        importance = current
        if _PREFERRED_CUE.search(line):
            importance = "preferred"
        elif _MANDATORY_CUE.search(line) and current != "preferred":
            importance = "mandatory"
        out.append({"text": _clip(line), "importance": importance})
    return out


def _experience(lines: list[str]) -> dict | None:
    best: dict | None = None
    fresher_quote = ""
    for line in lines:
        if _FRESHER.search(line) and not fresher_quote:
            fresher_quote = line
        if not _EXP_WORD.search(line):
            continue
        for rx, kind in _YEARS_EXP:
            m = rx.search(line)
            if not m:
                continue
            lo = int(m.group(1))
            hi = int(m.group(2)) if kind == "range" else None
            if lo > 30 or (hi is not None and (hi > 40 or hi < lo)):
                continue
            if best is None or lo > best["min_years"]:
                best = {"min_years": lo, "max_years": hi, "fresher": False, "quote": _clip(line)}
            break
    if best is None and fresher_quote:
        return {"min_years": 0, "max_years": 1, "fresher": True, "quote": _clip(fresher_quote)}
    if best is not None and fresher_quote and best["min_years"] <= 1:
        best["fresher"] = True
    if best is not None and fresher_quote and best["min_years"] >= 2:
        best["conflict_quote"] = _clip(fresher_quote)
    return best


def _education(lines: list[str]) -> dict | None:
    levels: list[str] = []
    specs: list[str] = []
    years: set[int] = set()
    quotes: list[str] = []
    any_branch = False
    for line in lines:
        if not _EDU_LINE.search(line):
            continue
        hit = False
        for lvl, rx in _LEVEL_RE:
            if rx.search(line) and lvl not in levels:
                levels.append(lvl)
                hit = True
        for spec, rx in _SPEC_RE.items():
            if rx.search(line) and spec not in specs:
                specs.append(spec)
                hit = True
        if _ANY_BRANCH.search(line):
            any_branch = hit = True
        if _BATCH.search(line):
            for y in _GRAD_YEARS.findall(line):
                years.add(int(y))
                hit = True
        if hit and len(quotes) < 3:
            quotes.append(_clip(line))
    if not (levels or specs or years):
        return None
    return {"levels": levels, "specializations": [] if any_branch else specs, "any_branch": any_branch,
            "years": sorted(years), "quote": " · ".join(quotes)}


def _conditions(lines: list[str]) -> list[dict]:
    out: list[dict] = []
    for line in lines:
        for kind, rx in _CONDITIONS:
            m = rx.search(line)
            if not m:
                continue
            cond = {"kind": kind, "quote": _clip(line)}
            if kind == "marks":
                pct = m.group(1) or m.group(2)
                cond["min_percent"] = int(pct) if pct else None
                cond["min_cgpa"] = float(m.group(3)) if m.group(3) else None
            if not any(c["kind"] == kind for c in out):
                out.append(cond)
    return out


def job_text_lines(job: dict) -> tuple[str, list[dict]]:
    """(source, classified lines) for a stored tracker job (no pasted text)."""
    ev = job.get("evidence") if isinstance(job.get("evidence"), dict) else {}
    stored = ev.get("requirements")
    if isinstance(stored, list) and stored:
        lines = [{"text": _clip(x.get("text", "")), "importance": x.get("importance", "unspecified")}
                 for x in stored if isinstance(x, dict) and isinstance(x.get("text"), str) and x["text"].strip()]
        if lines:
            return "stored", lines
    lines = []
    for raw in (ev.get("experience_lines") or []) + [ev.get("fresher_evidence") or ""]:
        if isinstance(raw, str) and raw.strip():
            lines.append({"text": _clip(raw), "importance": "unspecified"})
    for quote in (ev.get("skills") or {}).values() if isinstance(ev.get("skills"), dict) else []:
        if isinstance(quote, str) and quote.strip():
            lines.append({"text": _clip(quote), "importance": "unspecified"})
    for e in ev.get("experience") or []:
        if isinstance(e, str) and e.strip() and e != "no experience requirement stated":
            lines.append({"text": _clip(e), "importance": "mandatory"})
    seen: set[str] = set()
    return "tracker", [x for x in lines if not (x["text"] in seen or seen.add(x["text"]))]


def requirements(job: dict, pasted: str = "") -> dict:
    """The requirements of ``job`` (a tracker job record), from the pasted
    description when there is one (see the module doc)."""
    title = str(job.get("title") or "")
    if isinstance(pasted, str) and len(pasted.strip()) >= 10:          # anything the admin pasted
        source, lines = "pasted", classify_lines(pasted)
    else:
        source, lines = job_text_lines(job)
    texts = [x["text"] for x in lines]
    skills: dict[str, dict] = {}
    rank = {"mandatory": 0, "preferred": 1, "unspecified": 2, "context": 3}
    for x in [{"text": title, "importance": "unspecified"}] + lines:
        for skill, certain in _line_skills(x["text"]).items():
            imp = x["importance"] if x["importance"] != "context" else "unspecified"
            cur = skills.get(skill)
            if cur is None or rank[imp] < rank[cur["importance"]] or (certain and not cur["certain"]):
                skills[skill] = {"importance": imp if cur is None or rank[imp] <= rank[cur["importance"]]
                                 else cur["importance"], "quote": x["text"], "certain": certain}
    skills = {s: skills[s] for s in tax.sort_skills(skills)}
    # "Python or Java": either one satisfies the requirement (alternatives)
    for skill, r in skills.items():
        if _OR.search(r["quote"]):
            others = [o for o, ro in skills.items() if o != skill and ro["quote"] == r["quote"]]
            if others:
                r["alternatives"] = others
    exp = _experience([title] + texts)
    edu = _education(texts)
    emp = next((kind for kind, rx in _EMP_TYPE if rx.search(title)), "") or next(
        (kind for kind, rx in _EMP_TYPE for t in texts[:60] if rx.search(t)), "")
    seniority = next((kind for kind, rx in _SENIOR if rx.search(title)), "")
    contradictions = []
    if exp and exp.get("conflict_quote"):
        contradictions.append(f"The posting says both “{exp['conflict_quote']}” and “{exp['quote']}”.")
    if emp == "internship" and exp and exp["min_years"] >= 2:
        contradictions.append("It is described as an internship but asks for years of experience.")
    named = len(skills)
    quality = ("full" if source == "pasted" and len(lines) >= 8 else
               "partial" if named >= 3 or (source != "tracker" and lines) else "minimal")
    return {"source": source, "title": title, "company": str(job.get("company") or ""),
            "location": str(job.get("location") or ""), "skills": skills, "experience": exp, "education": edu,
            "employment_type": emp, "seniority": seniority, "conditions": _conditions(texts),
            "contradictions": contradictions, "lines": len(lines), "quality": quality}


ROLE_FAMILIES = [
    ("Data & ML", re.compile(r"\b(?:machine learning|\bml\b|data scien\w+|\bai\b|deep learning|nlp|computer vision|"
                             r"genai|llm)", re.I)),
    ("Data & analytics", re.compile(r"\b(?:data analy\w+|business analy\w+|analytics|\bbi\b|reporting|data engineer\w*|"
                                    r"\betl\b|mis)\b", re.I)),
    ("Testing & QA", re.compile(r"\b(?:qa|quality|test(?:er|ing)?|sdet|automation engineer)\b", re.I)),
    ("Cloud & DevOps", re.compile(r"\b(?:devops|cloud|site reliability|\bsre\b|infrastructure|platform engineer|"
                                  r"systems? administrator|network engineer)\b", re.I)),
    ("Front-end & mobile", re.compile(r"\b(?:front[- ]?end|ui developer|web developer|react|angular|android|ios|mobile|"
                                      r"flutter)\b", re.I)),
    ("Embedded & hardware", re.compile(r"\b(?:embedded|firmware|vlsi|hardware|asic|fpga|rtl|design verification|"
                                       r"electronics)\b", re.I)),
    ("Business & finance", re.compile(r"\b(?:finance|accounts?|accounting|audit|tax|analyst - finance|operations|"
                                      r"sales|marketing|hr)\b", re.I)),
    ("Software engineering", re.compile(r"\b(?:software|developer|engineer|sde|programmer|full[- ]?stack|back[- ]?end|"
                                        r"java|python|\.net|graduate engineer trainee|get|associate)\b", re.I)),
]


def role_family(title: str) -> str:
    """A coarse job family from the title (for gap priorities and interview
    question sets). "General" when the title says nothing recognisable."""
    t = str(title or "")
    return next((fam for fam, rx in ROLE_FAMILIES if rx.search(t)), "General")


def fingerprint(reqs: dict) -> str:
    """A short hash of the requirements (cache key for match results)."""
    return hashlib.sha256(json.dumps(reqs, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def posting_requirement_lines(description: str, limit: int = 30) -> list[dict]:
    """For the scanner (scraper._posting_requirements): the posting lines
    worth keeping for matching — the ones naming a skill, experience,
    education or another eligibility condition — with their importance.
    Descriptive only: called after a job has passed every gate."""
    keep: list[dict] = []
    for x in classify_lines(description):
        t = x["text"]
        if (_line_skills(t) or (_EXP_WORD.search(t) and re.search(r"\d|fresher|entry", t, re.I)) or _FRESHER.search(t)
                or _EDU_LINE.search(t) or any(rx.search(t) for _, rx in _CONDITIONS)):
            item = {"text": _clip(t, 200), "importance": x["importance"] if x["importance"] != "context" else "unspecified"}
            if item not in keep:
                keep.append(item)
        if len(keep) >= limit:
            break
    return keep
