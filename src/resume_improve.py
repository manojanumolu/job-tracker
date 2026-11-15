"""Résumé improvement suggestions for one résumé and (optionally) one job
(M6, Nov 2026).

The rule this module never breaks: **a suggestion only rewords or
restructures what the résumé already says**. It never adds a skill, an
employer, a certification, a date, an achievement or a number. Where a
measurable outcome would help, the "after" text holds a visible placeholder
— "[add a real figure you can verify]" — for the person to fill in or
delete. Missing skills are never suggested as keywords to add: a keyword is
suggested only where the résumé already shows a closely related skill, and
then only as "if you used X there, say so".

Each suggestion: id (stable), type, title, before, after, why, section.
The person accepts or rejects each one (stored by resume_store); the
original résumé and its extracted profile are never changed.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter

import skill_taxonomy as tax

TYPES = {
    "keyword": "Missing relevant keyword",
    "weak_description": "Weak project description",
    "unclear_contribution": "Unclear technical contribution",
    "no_outcome": "Missing measurable outcome",
    "repetition": "Repeated or unnecessary content",
    "formatting": "Inconsistent formatting",
    "unsupported_skill": "Claimed skill without evidence",
    "ats": "Possible ATS parsing problem",
}
PLACEHOLDER = "[add a real figure you can verify]"
_ACTION = re.compile(r"^(?:built|developed|designed|implemented|created|led|automated|optimi[sz]ed|reduced|"
                     r"improved|increased|deployed|wrote|tested|migrated|analy[sz]ed|integrated|refactored|"
                     r"engineered|architected|launched|delivered|configured|trained|modelled|modeled|researched|"
                     r"maintained|debugged|fixed|managed|coordinated|presented|published|set up|added|"
                     r"containeri[sz]ed|scaled|secured|monitored)\b", re.I)
_VAGUE = re.compile(r"^(?:worked on|involved in|helped(?: with| in)?|responsible for|participated in|assisted(?: in| with)?|"
                    r"part of|was part of|handled|did|exposure to)\b", re.I)
_NUMBER = re.compile(r"\d")
_TEAM = re.compile(r"\b(?:team|group|we|our)\b", re.I)
_MONTH_WORD = re.compile(r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{4}\b", re.I)
_MONTH_NUM = re.compile(r"\b(?:0?[1-9]|1[0-2])/\d{4}\b")


def _id(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12]


def _s(kind: str, title: str, before: str, after: str, why: str, section: str) -> dict:
    return {"id": _id(kind, before, after), "type": kind, "type_label": TYPES[kind], "title": title,
            "before": before, "after": after, "why": why, "section": section}


def _rewrite(bullet: str, skills: list[str]) -> str:
    """Restructure a bullet as action → what → how → result, keeping its own
    words. Adds nothing but a placeholder for an outcome."""
    del skills                                   # never append a technology the line itself doesn't name
    text = bullet.strip().rstrip(".")
    text = _VAGUE.sub("", text).strip(" ,:-")
    if text and not _ACTION.match(text):
        # the verb is the person's to choose: a placeholder, not a claim ("Built" may not be true)
        text = "[your action: built / designed / tested / …] " + (text[0].lower() + text[1:] if text[:1].isupper()
                                                                  and not text[:2].isupper() else text)
    return f"{text} — result: {PLACEHOLDER}"


def suggestions(profile: dict, reqs: dict | None = None, findings: list[dict] | None = None) -> list[dict]:
    """All suggestions for this résumé (and job, if given), most useful first.
    ``findings``: the text checks text_findings made at upload."""
    out: list[dict] = []
    skills = profile.get("skills") or {}

    # 1. keywords: only where a related skill is already shown (never "add X")
    for skill, req in ((reqs or {}).get("skills") or {}).items():
        if skill in skills or req.get("importance") == "preferred" and not req.get("certain", True):
            continue
        for other in tax.related(skill):
            info = skills.get(other)
            if info and info.get("status") == "demonstrated":
                ev = (info.get("evidence") or [{}])[0].get("text", "")
                out.append(_s("keyword", f"The posting asks for {skill}; your résumé shows {other}",
                              ev, f"{ev}  ← if you really used {skill} here too, name it in this line; if not, leave it.",
                              f"Applicant-tracking systems and recruiters look for the posting's words. "
                              f"{other} is related ({tax.related(skill)[other]}), but only name {skill} if it is true.",
                              (info.get("evidence") or [{}])[0].get("section", "")))
                break

    # 2-4. bullets of projects and experience
    entries = [("projects", p) for p in profile.get("projects") or []] + \
              [("experience", e) for e in profile.get("experience") or []]
    for section, e in entries:
        bullets = e.get("bullets") or []
        label = e.get("title") or e.get("organisation") or section
        if section == "projects" and len(" ".join(bullets).split()) < 12:
            before = " / ".join(bullets) or label
            out.append(_s("weak_description", f"“{label}” needs a fuller description", before,
                          f"{label}: what it does and for whom · the technologies you used · your part · "
                          f"result: {PLACEHOLDER}",
                          "A project with one short line gives an interviewer nothing to ask about.", section))
        for b in bullets:
            names = e.get("skills") or []
            if _VAGUE.match(b) or (_TEAM.search(b) and not re.search(r"\bI\b|\bmy\b|\bled\b", b)):
                out.append(_s("unclear_contribution", f"Say what you did in “{label}”", b, _rewrite(b, names),
                              "Phrases like “worked on” or a team result hide your own contribution. Start with the "
                              "action you took.", section))
            elif not _NUMBER.search(b) and len(b.split()) >= 5:
                out.append(_s("no_outcome", f"Add an outcome to a bullet in “{label}”", b,
                              f"{b.rstrip('.')} — result: {PLACEHOLDER}",
                              "A verifiable result (time saved, users, accuracy, speed) makes the work concrete. Only "
                              "use a number you can back up; otherwise leave the line as it is.", section))
            elif len(b.split()) < 5:
                out.append(_s("weak_description", f"Expand a short bullet in “{label}”", b,
                              f"{_rewrite(b, names)}",
                              "Very short bullets read as filler.", section))

    verbs = Counter(m.group(0).lower() for b in [b for _, e in entries for b in e.get("bullets") or []]
                    for m in [re.match(r"[A-Za-z]+", b)] if m)
    for verb, n in verbs.items():
        if n >= 4 and verb not in ("i",):
            out.append(_s("repetition", f"“{verb.capitalize()}” starts {n} bullets", f"{verb.capitalize()} … (×{n})",
                          "Vary the opening verbs to match what you did: built, designed, tested, automated, analysed…",
                          "Repeating one verb makes different achievements look alike.", ""))

    # 7. claimed skills without evidence
    listed = [s for s, i in skills.items() if i.get("status") == "mentioned" and "listed" in (i.get("kinds") or [])
              and tax.category(s) not in (tax.PRO,)]
    if listed:
        shown = ", ".join(listed[:8])
        out.append(_s("unsupported_skill", "Skills listed but never shown", f"Skills: {shown}",
                      "For each, add a project or work bullet that shows it in use — or remove the ones you can't "
                      "talk about in an interview.",
                      "Interviewers ask about every listed skill; unsupported ones are easy to expose.", "skills"))

    for f in findings or []:                     # text checks made once, at upload (text_findings)
        if isinstance(f, dict) and f.get("type") in TYPES and isinstance(f.get("before"), str):
            out.append(f)

    seen: set[str] = set()
    uniq = [s for s in out if not (s["id"] in seen or seen.add(s["id"]))]
    order = list(TYPES)
    return sorted(uniq, key=lambda s: order.index(s["type"]))[:40]


def text_findings(profile: dict, raw_text: str, doc_meta: dict | None = None) -> list[dict]:
    """The checks that need the résumé's text or file (repetition, date and
    bullet formats, ATS problems). Run once, at upload: their results are
    stored with the profile so the text itself never has to be kept."""
    out: list[dict] = []
    meta = doc_meta or {}
    # repetition
    lines = [re.sub(r"^[\s•\-*–◦▪►✓>]+", "", ln).strip() for ln in (raw_text or "").split("\n")]
    lines = [ln for ln in lines if len(ln) > 25]                 # bullets stripped: "• x" and "- x" are the same line
    for line, n in Counter(ln.lower() for ln in lines).items():
        if n > 1:
            original = next(ln for ln in lines if ln.lower() == line)
            out.append(_s("repetition", "The same line appears more than once", original, original + "  (keep once)",
                          f"It appears {n} times.", ""))
    # 6. formatting
    text = raw_text or ""
    if _MONTH_WORD.search(text) and _MONTH_NUM.search(text):
        out.append(_s("formatting", "Dates use two formats", f"{_MONTH_WORD.search(text).group(0)} and "
                      f"{_MONTH_NUM.search(text).group(0)}", "Use one format throughout, e.g. “Jan 2025 – Jun 2025”.",
                      "Consistent dates read cleanly and parse reliably.", ""))
    marks = {m for m in re.findall(r"(?m)^\s*([•\-*–◦▪►✓>])\s", text)}
    if len(marks) > 1:
        out.append(_s("formatting", "Several bullet styles", " ".join(sorted(marks)), "Use one bullet style (•).",
                      "Mixed bullet characters look unedited and some are dropped by ATS parsers.", ""))

    # ATS
    secs = set(profile.get("sections_found") or [])
    missing = [s for s in ("education", "skills") if s not in secs] + \
              ([] if secs & {"experience", "internships", "projects"} else ["experience or projects"])
    if missing:
        out.append(_s("ats", "Standard section headings not found", "Headings found: " + (", ".join(sorted(secs)) or "none"),
                      "Use plain headings: Education, Experience (or Internships), Projects, Skills, Certifications.",
                      f"Applicant-tracking systems look for standard headings; missing: {', '.join(missing)}.", ""))
    if not (profile.get("contact") or {}).get("email"):
        out.append(_s("ats", "No email address found", "(no email)", "Put your email in the body of the résumé, "
                      "near your name — not only in a header, footer or image.", "Recruiters and ATS need it.", ""))
    if meta.get("tables"):
        out.append(_s("ats", "Tables in the document", f"{meta['tables']} table(s)", "Use plain text lines instead of "
                      "tables for skills and education.", "Some ATS parsers read tables out of order or drop them.", ""))
    if meta.get("header_text") and _EMAIL_RE.search(meta["header_text"]) and not _EMAIL_RE.search(raw_text or ""):
        out.append(_s("ats", "Contact details only in the header", "Header/footer: contact details",
                      "Repeat your contact details at the top of the body.",
                      "Many ATS parsers skip headers and footers.", ""))
    short = [ln for ln in (raw_text or "").split("\n") if 0 < len(ln.strip()) <= 3]
    if len(short) > 25 and meta.get("file_type") == "pdf":
        out.append(_s("ats", "The PDF may use columns", f"{len(short)} very short text fragments",
                      "Use a single-column layout.", "Multi-column PDFs are often read in the wrong order.", ""))
    if re.search(r"[☀-➿\U0001F300-\U0001FAFF]", raw_text or ""):
        out.append(_s("ats", "Icons or emoji in the text", "(icons found)", "Replace icons with words "
                      "(“Email:”, “Phone:”).", "Icons are dropped or garbled by ATS parsers.", ""))

    return out


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


def export_text(resume_name: str, accepted: list[dict]) -> str:
    """The accepted suggestions as plain text (a checklist to apply by hand —
    the original résumé is never edited by this app)."""
    lines = [f"Résumé improvement notes — {resume_name}", "",
             "Apply these by hand in your own résumé file. Replace every placeholder with a real, verifiable "
             "figure, or remove it.", ""]
    for i, s in enumerate(accepted, 1):
        lines += [f"{i}. {s['type_label']}: {s['title']}", f"   Before: {s['before']}", f"   After:  {s['after']}",
                  f"   Why:    {s['why']}", ""]
    return "\n".join(lines)
