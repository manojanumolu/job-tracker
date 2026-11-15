"""The M6 matching engine: one résumé profile against one job's requirements
— explainable, evidence-based, deterministic (Nov 2026).

What it returns is an *estimate*, never a prediction: a 90 % match does not
mean a 90 % chance of being hired, and nothing here says "eligible",
"shortlisted" or "interview-ready". Each part is computed and shown
separately:

  1. mandatory skills   the skills the posting requires (or, when it doesn't
                        say which, every skill it names — and it says so)
  2. preferred skills   "nice to have" skills
  3. experience         professional employment only; internships and
                        academic projects never count as employment
  4. education          degree level, degree, specialisation, batch year
  5. evidence           how much of the matched skills is shown in projects,
                        internships or jobs (not just listed), and relevant
                        certifications
  6. overall estimate   a weighted mean of the parts that could be computed
                        (DEFAULT_WEIGHTS, configurable per admin), or
                        "Insufficient evidence" when too little is known

Eligibility is separate from the score. A stated condition the résumé fails
(experience below the minimum, a batch year outside the allowed ones, a
lower degree than required) is a **critical** issue: it is listed first and
the verdict reads "Eligibility issue" however high the skills score is.

Skill statuses (STATUSES): demonstrated / mentioned / related /
needs_verification / not_found — each row with the requirement line, the
résumé evidence, its section and its classification.
"""

from __future__ import annotations

import hashlib
import json

import skill_taxonomy as tax
from resume_profile import DEMONSTRATING, LEVELS

ENGINE_VERSION = 1
DEFAULT_WEIGHTS = {"mandatory": 40, "preferred": 15, "experience": 20, "education": 15, "evidence": 10}
WEIGHT_LABELS = {"mandatory": "Mandatory skill alignment", "preferred": "Preferred skill alignment",
                 "experience": "Experience alignment", "education": "Education alignment",
                 "evidence": "Project & certification evidence"}
STATUSES = {
    "demonstrated": ("Explicitly demonstrated", 1.0),
    "mentioned": ("Mentioned but not demonstrated", 0.5),
    "related": ("Related or transferable skill", 0.35),
    "needs_verification": ("Needs manual verification", 0.25),
    "not_found": ("Not found", 0.0),
}
KIND_LABELS = {"employment": "Employment", "internship": "Internship", "academic_project": "Academic project",
               "personal_project": "Personal project", "project": "Project", "certification": "Certification",
               "coursework": "Coursework", "listed": "Skills list", "education": "Education", "mentioned": "Mentioned",
               "user": "Added by you", "needs_verification": "Ambiguous wording"}
MIN_SKILLS = 2                      # fewer named skills than this: no skills score
BANDS = ((75, "Strong"), (55, "Good"), (35, "Partial"), (0, "Low"))


def clean_weights(weights: object) -> dict[str, int]:
    """Whole-number weights for the five parts, each 0-100, summing to 100.
    Anything else falls back to DEFAULT_WEIGHTS."""
    if not isinstance(weights, dict):
        return dict(DEFAULT_WEIGHTS)
    try:
        w = {k: int(weights.get(k, -1)) for k in DEFAULT_WEIGHTS}
    except (TypeError, ValueError):
        return dict(DEFAULT_WEIGHTS)
    if any(not 0 <= v <= 100 for v in w.values()) or sum(w.values()) != 100:
        return dict(DEFAULT_WEIGHTS)
    return w


def band(score: int | None) -> str:
    if score is None:
        return "Insufficient evidence"
    return next(label for floor, label in BANDS if score >= floor)


# ── skills ───────────────────────────────────────────────────────────────────

def _best_evidence(info: dict) -> dict:
    ev = info.get("evidence") or [{}]
    demo = [e for e in ev if e.get("kind") in DEMONSTRATING and e.get("certain")]
    return (demo or ev)[0]


def skill_row(skill: str, req: dict, profile: dict) -> dict:
    """One requirement compared with the résumé (see STATUSES)."""
    skills = profile.get("skills") or {}
    info = skills.get(skill)
    row = {"skill": skill, "category": tax.category(skill), "importance": req["importance"],
           "requirement": req["quote"], "requirement_certain": bool(req.get("certain", True)),
           "status": "not_found", "evidence": "", "section": "", "classification": "", "related_to": "", "note": ""}
    if info:
        status = info.get("status") or "mentioned"
        if status not in STATUSES:
            status = "needs_verification"
        e = _best_evidence(info)
        row.update(status=status, evidence=e.get("text", ""), section=e.get("section", ""),
                   classification=KIND_LABELS.get(e.get("kind", ""), e.get("kind", "")))
        if info.get("source") == "user":
            row["note"] = "You added this skill; the résumé text doesn't show it."
    else:
        for other, note in tax.related(skill).items():
            oi = skills.get(other)
            if oi and oi.get("status") in ("demonstrated", "mentioned"):
                e = _best_evidence(oi)
                row.update(status="related", related_to=other, evidence=e.get("text", ""), section=e.get("section", ""),
                           classification=KIND_LABELS.get(e.get("kind", ""), ""), note=f"{other} — {note}")
                break
    if not row["requirement_certain"]:
        row["note"] = (row["note"] + " " if row["note"] else "") + \
            "The posting's wording is ambiguous for this skill; check what it means."
    return row


def _alternatives(rows: list[dict], reqs: dict) -> None:
    """"Python or Java": only the best-shown alternative is scored; the
    others are marked as alternatives (shown, not counted against)."""
    rank = {s: i for i, s in enumerate(STATUSES)}
    by = {r["skill"]: r for r in rows}
    done: set[str] = set()
    for r in rows:
        alts = (reqs.get("skills") or {}).get(r["skill"], {}).get("alternatives") or []
        group = [r] + [by[a] for a in alts if a in by]
        if len(group) < 2 or r["skill"] in done:
            continue
        best = min(group, key=lambda x: rank[x["status"]])
        for x in group:
            done.add(x["skill"])
            if x is not best:
                x["alternative_of"] = best["skill"]
                x["note"] = (x["note"] + " " if x["note"] else "") + \
                    f"The posting accepts {x['skill']} or {best['skill']}; {best['skill']} is the one scored."


def _coverage(rows: list[dict]) -> dict:
    scored = [r for r in rows if r["requirement_certain"] and not r.get("alternative_of")]
    if len(scored) < 1:
        return {"score": None, "count": 0, "counts": {}}
    credit = sum(STATUSES[r["status"]][1] for r in scored)
    counts = {s: sum(1 for r in scored if r["status"] == s) for s in STATUSES}
    return {"score": round(100 * credit / len(scored)), "count": len(scored), "counts": counts}


# ── experience and education ────────────────────────────────────────────────

def _int(v) -> int:
    """A stored count, or 0 for anything that isn't one (a damaged record)."""
    try:
        return max(0, int(v or 0))
    except (TypeError, ValueError):
        return 0


def _experience(profile: dict, reqs: dict, issues: list[dict]) -> dict:
    req = reqs.get("experience")
    totals = profile.get("totals") if isinstance(profile.get("totals"), dict) else {}
    months = _int(totals.get("employment_months"))
    years = round(months / 12, 1)
    intern = _int(totals.get("internship_months"))
    undated = _int(totals.get("undated_employment"))
    basis = (f"{years:g} year(s) of dated professional employment"
             + (f"; {intern} month(s) of internships (not counted as employment)" if intern else "")
             + (f"; {undated} employment entr{'y' if undated == 1 else 'ies'} without dates" if undated else ""))
    out = {"score": None, "candidate_years": years, "internship_months": intern, "basis": basis, "requirement": ""}
    if reqs.get("seniority") and years < 3:
        level = reqs["seniority"]
        issues.append({"severity": "warning", "area": "Seniority",
                       "text": f"The title reads as a {level}-level role; the résumé shows {years:g} year(s) of employment.",
                       "quote": reqs.get("title", "")})
    if not req:
        out["requirement"] = "The posting doesn't state an experience requirement."
        return out
    lo, hi = req.get("min_years") or 0, req.get("max_years")
    out["requirement"] = req.get("quote", "")
    if req.get("fresher") or lo == 0:
        cap = hi if hi is not None else 1
        if years <= max(cap, 1):
            score = 100
        elif hi is not None and years > hi + 1:
            score = 20
            issues.append({"severity": "critical", "area": "Experience",
                           "text": f"The posting asks for {lo}–{hi} years; the résumé shows {years:g} years of employment.",
                           "quote": req.get("quote", "")})
        else:
            score = 60 if years <= 2 else 35
            issues.append({"severity": "warning", "area": "Experience",
                           "text": f"This is a fresher / entry-level role and the résumé shows {years:g} years of "
                                   "employment. Some fresher roles exclude experienced applicants — check the posting.",
                           "quote": req.get("quote", "")})
    elif years >= lo:
        score = 100 if hi is None or years <= hi + 1 else 70
    else:
        score = round(100 * years / lo) if lo else 0
        if undated:
            issues.append({"severity": "verify", "area": "Experience",
                           "text": f"The posting asks for at least {lo} year(s); some employment entries have no "
                                   "dates, so the total couldn't be checked.", "quote": req.get("quote", "")})
        else:
            sev = "critical" if years < lo * 0.75 else "warning"
            issues.append({"severity": sev, "area": "Experience",
                           "text": f"The posting asks for at least {lo} year(s) of experience; the résumé shows "
                                   f"{years:g} year(s) of employment" + (" (internships and projects don't count)."
                                                                         if intern or profile.get("projects") else "."),
                           "quote": req.get("quote", "")})
    out["score"] = score
    return out


_DEGREE_TOKENS = {
    "btech": r"b\.?\s?tech|\bb\.e\b|\bbe\b(?=\s*/|\s*\(|\s+in\b)|engineering degree|bachelor of engineering|bachelor of technology",
    "bsc": r"b\.?\s?sc|bachelor of science", "bca": r"\bbca\b", "bcom": r"b\.?\s?com", "bba": r"\bbba\b",
    "mtech": r"m\.?\s?tech|\bm\.e\b", "msc": r"m\.?\s?sc", "mca": r"\bmca\b", "mba": r"\bmba\b|pgdm",
}
import re as _re  # noqa: E402

_DEG_RX = {k: _re.compile(v, _re.I) for k, v in _DEGREE_TOKENS.items()}


def _degree_tokens(text: str) -> set[str]:
    return {k for k, rx in _DEG_RX.items() if rx.search(text or "")}


def _education(profile: dict, reqs: dict, issues: list[dict]) -> dict:
    req = reqs.get("education")
    degrees = [e for e in profile.get("education") or [] if e.get("level") and e["level"] != "school"]
    top = max(degrees, key=lambda e: (LEVELS.get(e["level"], 0), e.get("end_year") or 0), default=None)
    grad = profile.get("graduation_year")
    out = {"score": None, "requirement": "", "candidate": "", "checks": []}
    if top:
        out["candidate"] = " ".join(x for x in (top.get("degree"), top.get("specialization"),
                                                 str(top.get("end_year") or "")) if x)
    if not req:
        out["requirement"] = "The posting doesn't state an education requirement."
        return out
    out["requirement"] = req.get("quote", "")
    score = 100
    if req.get("levels"):
        need = min(LEVELS[lv] for lv in req["levels"] if lv in LEVELS)
        if top is None:
            score = min(score, 50)
            issues.append({"severity": "verify", "area": "Education", "text": "The posting states a degree "
                           "requirement and no degree was recognised in the résumé — check it.", "quote": req["quote"]})
        elif LEVELS[top["level"]] < need:
            score = 0
            issues.append({"severity": "critical", "area": "Education",
                           "text": f"The posting asks for {' / '.join(req['levels'])}; the résumé's highest degree is "
                                   f"{top.get('degree') or top['level']}.", "quote": req["quote"]})
        else:
            want = _degree_tokens(req.get("quote", ""))
            have = _degree_tokens(top.get("degree", "") + " " + top.get("evidence", ""))
            if want and have and not (want & have) and LEVELS[top["level"]] == need:
                score = min(score, 70)
                issues.append({"severity": "warning", "area": "Education",
                               "text": f"The posting names {', '.join(sorted(want))}; the résumé shows "
                                       f"{', '.join(sorted(have))}.", "quote": req["quote"]})
    specs = req.get("specializations") or []
    if specs and top is not None:
        if not top.get("specialization"):
            score = min(score, 70)
            issues.append({"severity": "verify", "area": "Education", "text": "The posting lists eligible branches "
                           f"({', '.join(specs)}); the résumé's specialisation wasn't recognised.", "quote": req["quote"]})
        elif top["specialization"] not in specs:
            strict = bool(_re.search(r"\bonly\b|must|eligible|mandatory", req.get("quote", ""), _re.I))
            score = min(score, 30 if strict else 50)
            issues.append({"severity": "critical" if strict else "warning", "area": "Education",
                           "text": f"The posting lists {', '.join(specs)}; the résumé shows {top['specialization']}.",
                           "quote": req["quote"]})
    years = req.get("years") or []
    if years:
        if grad is None:
            score = min(score, 50)
            issues.append({"severity": "verify", "area": "Education",
                           "text": f"The posting is for the {', '.join(map(str, years))} batch; the résumé's "
                                   "graduation year wasn't found.", "quote": req["quote"]})
        elif grad not in years:
            score = 0
            issues.append({"severity": "critical", "area": "Education",
                           "text": f"The posting is for the {', '.join(map(str, years))} batch; the résumé shows "
                                   f"graduation in {grad}.", "quote": req["quote"]})
    out["score"] = score
    return out


def _conditions(profile: dict, reqs: dict, issues: list[dict]) -> None:
    top_score = next((e.get("score") for e in profile.get("education") or []
                      if e.get("level") not in ("", "school") and e.get("score")), "")
    for c in reqs.get("conditions") or []:
        kind, quote = c["kind"], c["quote"]
        if kind == "marks":
            cand = None
            if top_score.endswith("%") and c.get("min_percent"):
                cand, need = float(top_score[:-1]), c["min_percent"]
            elif "/" in top_score and c.get("min_cgpa"):
                num, den = top_score.split("/")
                cand, need = (float(num) * 10 / float(den or 10)), c["min_cgpa"]
            if cand is None:
                issues.append({"severity": "verify", "area": "Marks", "text": "The posting sets a minimum mark; "
                               "compare it with your own record (the units differ or no score was found).", "quote": quote})
            elif cand < need:
                issues.append({"severity": "critical", "area": "Marks",
                               "text": f"The posting asks for at least {need:g}; the résumé shows {top_score}.",
                               "quote": quote})
        elif kind == "location":
            loc = (profile.get("contact") or {}).get("location", "")
            if loc and loc.lower() not in (reqs.get("location") or "").lower() and loc.lower() not in quote.lower():
                issues.append({"severity": "warning", "area": "Location",
                               "text": f"The posting restricts where candidates are based; the résumé lists {loc}.",
                               "quote": quote})
            else:
                issues.append({"severity": "verify", "area": "Location", "text": "The posting restricts where "
                               "candidates are based — check it applies to you.", "quote": quote})
        else:
            label = {"backlogs": "Backlogs", "relocation": "Relocation", "shifts": "Shifts", "bond": "Service bond",
                     "gap": "Education gap", "citizenship": "Work authorisation"}.get(kind, kind)
            issues.append({"severity": "verify", "area": label,
                           "text": "A stated condition the résumé can't show either way — check it yourself.",
                           "quote": quote})
    loc = (profile.get("contact") or {}).get("location", "")
    if loc and reqs.get("location") and loc.lower() not in reqs["location"].lower() and not any(
            c["kind"] == "location" for c in reqs.get("conditions") or []):
        issues.append({"severity": "info", "area": "Location",
                       "text": f"The job is in {reqs['location']}; the résumé lists {loc}. Not a stated "
                               "requirement — relocation may be needed.", "quote": ""})
    if reqs.get("employment_type") == "internship" and _int((profile.get("totals") or {}).get("employment_months")) >= 12:
        issues.append({"severity": "warning", "area": "Employment type",
                       "text": "This is an internship and the résumé shows a year or more of employment; internships "
                               "are usually meant for students.", "quote": reqs.get("title", "")})
    for note in reqs.get("contradictions") or []:
        issues.append({"severity": "verify", "area": "Posting", "text": note, "quote": ""})


# ── the analysis ────────────────────────────────────────────────────────────

def _evidence_score(rows: list[dict], profile: dict, reqs: dict) -> dict:
    matched = [r for r in rows if r["status"] in ("demonstrated", "mentioned", "related")]
    if not rows:
        return {"score": None, "shown": 0, "matched": 0, "certifications": []}
    shown = sum(1 for r in matched if r["status"] == "demonstrated")
    certs = [c for c in profile.get("certifications") or [] if any(tax.find_skills(c).get(s) for s in reqs["skills"])]
    base = shown / len(rows)
    score = round(min(100, 85 * base + (15 if certs else 0)))
    return {"score": score, "shown": shown, "matched": len(matched), "certifications": certs[:5]}


def analyse(profile: dict, reqs: dict, weights: dict | None = None) -> dict:
    """Compare one résumé profile with one job's requirements (module doc)."""
    w = clean_weights(weights)
    issues: list[dict] = []
    rows = [skill_row(s, r, profile) for s, r in (reqs.get("skills") or {}).items()]
    _alternatives(rows, reqs)
    mand = [r for r in rows if r["importance"] == "mandatory"]
    pref = [r for r in rows if r["importance"] == "preferred"]
    unspec = [r for r in rows if r["importance"] == "unspecified"]
    notes: list[str] = []
    if mand:
        mand_basis = mand
    else:
        mand_basis = unspec
        if unspec:
            notes.append("The posting doesn't say which skills are required, so every skill it names is counted "
                         "as a requirement.")
    mc, pc = _coverage(mand_basis), _coverage(pref)
    if mc["count"] < MIN_SKILLS and not mand:
        mc["score"] = None
    edu = _education(profile, reqs, issues)
    exp = _experience(profile, reqs, issues)
    _conditions(profile, reqs, issues)
    evid = _evidence_score([r for r in rows if r["requirement_certain"] and not r.get("alternative_of")], profile, reqs)
    components = {"mandatory": mc["score"], "preferred": pc["score"], "experience": exp["score"],
                  "education": edu["score"], "evidence": evid["score"]}
    available = {k: v for k, v in components.items() if v is not None}
    weight_used = sum(w[k] for k in available)
    resume_empty = not (profile.get("skills") or profile.get("education") or profile.get("experience"))
    if resume_empty or components["mandatory"] is None or weight_used < 50:
        overall = None
        if resume_empty:
            notes.append("The résumé has no recognisable skills, education or experience.")
        elif components["mandatory"] is None:
            notes.append("The job text available names too few skills to compare. Paste the full job description "
                         "to analyse it properly.")
    else:
        overall = round(sum(available[k] * w[k] for k in available) / weight_used)
    critical = [i for i in issues if i["severity"] == "critical"]
    warnings = [i for i in issues if i["severity"] == "warning"]
    order = {"critical": 0, "warning": 1, "verify": 2, "info": 3}
    issues.sort(key=lambda i: order[i["severity"]])
    if critical:
        eligibility = "critical"
    elif warnings:
        eligibility = "warnings"
    elif reqs.get("experience") or reqs.get("education") or reqs.get("conditions"):
        eligibility = "no_issue_found"
    else:
        eligibility = "unknown"
    # how complete the evidence is: the parts computed, the posting text, the résumé's sections
    parts = len(available) / len(components)
    job_q = {"full": 1.0, "partial": 0.6, "minimal": 0.25}[reqs.get("quality", "minimal")]
    secs = set(profile.get("sections_found") or [])
    res_q = (("education" in secs) + bool(secs & {"experience", "internships", "projects"}) + ("skills" in secs)) / 3
    completeness = round(100 * (0.4 * parts + 0.35 * job_q + 0.25 * res_q))
    confidence = "High" if completeness >= 75 else "Medium" if completeness >= 50 else "Low"
    explain = []
    for k, label in WEIGHT_LABELS.items():
        v = components[k]
        explain.append({"part": k, "label": label, "score": v, "weight": w[k],
                        "contribution": None if v is None or overall is None else round(v * w[k] / weight_used, 1)})
    verdict = "Eligibility issue" if critical else band(overall)
    return {
        "engine": ENGINE_VERSION, "kind": "estimate", "overall": overall, "band": band(overall), "verdict": verdict,
        "components": components, "weights": w, "explain": explain, "weight_used": weight_used,
        "rows": rows, "mandatory_basis": "mandatory" if mand else "named",
        "mandatory": {"count": mc["count"], "counts": mc.get("counts", {})},
        "preferred": {"count": pc["count"], "counts": pc.get("counts", {})},
        "experience": exp, "education": edu, "evidence": evid,
        "issues": issues, "eligibility": eligibility, "critical": len(critical),
        "completeness": completeness, "confidence": confidence, "notes": notes,
        "job_source": reqs.get("source"), "job_quality": reqs.get("quality"),
    }


def summary(result: dict) -> dict:
    """The small record kept in analysis history / the match cache."""
    return {"overall": result["overall"], "verdict": result["verdict"], "eligibility": result["eligibility"],
            "critical": result["critical"], "confidence": result["confidence"],
            "components": result["components"], "engine": result["engine"],
            "missing_mandatory": [r["skill"] for r in result["rows"]
                                  if r["status"] == "not_found" and r["importance"] == "mandatory"][:12]}


def cache_key(profile_hash: str, reqs_fp: str, weights: dict) -> str:
    raw = json.dumps([ENGINE_VERSION, profile_hash, reqs_fp, clean_weights(weights)], sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def profile_hash(profile: dict) -> str:
    return hashlib.sha256(json.dumps(profile, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def sort_key(result: dict) -> tuple:
    """Ranking: no critical issue first, then the estimate (insufficient
    evidence last), then completeness."""
    return (result["critical"] > 0, result["overall"] is None, -(result["overall"] or 0), -result["completeness"])


def best_resume(results: dict[str, dict]) -> str | None:
    """Which résumé (by id) fits one job best, by sort_key; None if none."""
    if not results:
        return None
    return min(results, key=lambda rid: sort_key(results[rid]))


# ── skill gaps ──────────────────────────────────────────────────────────────

_NEXT = {
    "SQL": "Learn SQL joins, GROUP BY and window functions; then optimise two slow queries using indexes and EXPLAIN.",
    "Python": "Build a small Python automation project (file processing or an API client) with tests.",
    "Java": "Build a Java CRUD service with collections, exceptions and unit tests (JUnit).",
    "JavaScript": "Build an interactive page in plain JavaScript: DOM events, fetch() and async/await.",
    "React": "Build a React app with routing, forms and API calls; deploy it.",
    "REST APIs": "Demonstrate REST API development: build an API with proper status codes, validation and docs.",
    "Docker": "Containerise one of your projects with a Dockerfile and docker compose.",
    "Kubernetes": "Deploy a containerised app to a local Kubernetes cluster (kind/minikube): Deployment + Service.",
    "AWS": "Deploy a project on the AWS free tier using EC2 or Lambda, S3 and IAM roles.",
    "Azure": "Deploy a project on Azure App Service; learn resource groups and identities.",
    "GCP": "Deploy a project on Cloud Run; learn IAM basics.",
    "Git": "Use Git branches, pull requests and rebasing on a real project; keep a clean history.",
    "Linux": "Practise the shell daily: files, permissions, processes, grep/sed/awk.",
    "Data structures & algorithms": "Revise arrays, hashing, trees and graphs; solve 40 graded problems.",
    "OOP": "Model a small domain (library, parking lot) with classes, interfaces and tests.",
    "Machine learning": "Train, validate and explain a model on a public dataset end to end; report precision/recall.",
    "Data analysis": "Analyse a public dataset end to end (cleaning, EDA, conclusions) and publish the notebook.",
    "Excel": "Practise lookups, pivot tables and charts on a real dataset.",
    "Power BI": "Build a Power BI dashboard from a public dataset with a data model and DAX measures.",
    "Software testing": "Write test cases and bug reports for an open-source app; learn the STLC.",
    "Test automation": "Automate tests for one of your projects (pytest/JUnit, or Selenium for a web UI).",
    "Selenium": "Automate a web app's main flows with Selenium and the page-object pattern.",
    "Spring Boot": "Build a Spring Boot REST service with JPA and tests.",
    "Django": "Build a Django app with models, auth and the admin; deploy it.",
    "Node.js": "Build an Express API with validation and a database.",
    "CI/CD": "Add a CI pipeline (GitHub Actions) that runs tests on every push to one of your projects.",
    "System design": "Study caching, load balancing and databases; design 3 classic systems on paper.",
    "Communication": "Practise explaining your projects in 2 minutes; write clear project summaries.",
}
_NEXT_CAT = {
    tax.LANG: "Build two small projects in it end to end and solve practice problems.",
    tax.FRAME: "Build and deploy one small project with it, starting from the official tutorial.",
    tax.DB: "Design a schema and build one project that stores and queries data with it.",
    tax.CLOUD: "Use the free tier to deploy one of your projects with it.",
    tax.TOOLS: "Use it on a real project until you can work with it without a guide.",
    tax.DATA: "Work through a public dataset end to end and publish the notebook.",
    tax.CS: "Revise the topic, then solve graded problems on it.",
    tax.TEST: "Add tests to one of your projects using it.",
    tax.PRO: "Practise it in mock interviews and team projects.",
    tax.DOMAIN: "Pair a short course with a hands-on project.",
}


def next_step(skill: str, related_to: str = "") -> str:
    step = _NEXT.get(skill) or _NEXT_CAT.get(tax.category(skill), "Pair a short course with a hands-on project.")
    if related_to:
        step = f"You already have {related_to}: build on it. " + step
    return step


def pair_gaps(result: dict) -> dict:
    """One résumé × one job: the skills grouped by what the résumé shows,
    with next steps for the gaps (mandatory first)."""
    rows = result["rows"]
    pick = lambda st, imp=None: [r for r in rows if r["status"] == st and (imp is None or r["importance"] in imp)]  # noqa: E731
    rows = [r for r in rows if not r.get("alternative_of")]
    missing_m = pick("not_found", ("mandatory",)) + (pick("not_found", ("unspecified",))
                                                     if result["mandatory_basis"] == "named" else [])
    missing_p = pick("not_found", ("preferred",))
    out = {"demonstrated": pick("demonstrated"), "mentioned": pick("mentioned"), "missing_mandatory": missing_m,
           "missing_preferred": missing_p, "related": pick("related"), "verify": pick("needs_verification"),
           "missing_other": [r for r in pick("not_found") if r not in missing_m and r not in missing_p]}
    steps = []
    for r in missing_m:
        steps.append({"skill": r["skill"], "priority": "High", "why": "Required by this posting", "step": next_step(r["skill"])})
    for r in out["related"]:
        steps.append({"skill": r["skill"], "priority": "High" if r["importance"] == "mandatory" else "Medium",
                      "why": f"Related to {r['related_to']}, which the résumé shows",
                      "step": next_step(r["skill"], r["related_to"])})
    for r in out["mentioned"]:
        if r["importance"] in ("mandatory", "unspecified"):
            steps.append({"skill": r["skill"], "priority": "Medium", "why": "Listed but not shown in a project or job",
                          "step": f"Add a project or work bullet that shows {r['skill']} in use — only if you really "
                                  "used it — or build a small project with it."})
    for r in missing_p:
        steps.append({"skill": r["skill"], "priority": "Low", "why": "Preferred, not required", "step": next_step(r["skill"])})
    out["next_steps"] = steps[:10]
    return out


def global_gaps(profile: dict, all_reqs: list[dict], family: str = "", top: int = 10) -> list[dict]:
    """Across the job catalogue: the skills the résumé lacks, prioritised by
    (1) how often jobs *require* them, (2) how often they're named at all,
    (3) relevance to the chosen role family, (4) whether a related skill is
    already there (cheaper to learn). Only skills named by at least two jobs
    — or required by one in the chosen family — are listed: not every
    missing skill is worth learning."""
    from job_requirements import role_family
    have = profile.get("skills") or {}
    stats: dict[str, dict] = {}
    for reqs in all_reqs:
        fam = role_family(reqs.get("title", ""))
        for skill, r in (reqs.get("skills") or {}).items():
            if not r.get("certain", True):
                continue
            info = have.get(skill)
            if info and info.get("status") == "demonstrated":
                continue
            s = stats.setdefault(skill, {"skill": skill, "mandatory": 0, "preferred": 0, "named": 0, "in_family": 0,
                                         "status": (info or {}).get("status", "not_found"), "jobs": []})
            s["named"] += 1
            s["mandatory"] += r["importance"] == "mandatory"
            s["preferred"] += r["importance"] == "preferred"
            s["in_family"] += bool(family) and fam == family
            if len(s["jobs"]) < 5:
                s["jobs"].append(f"{reqs.get('company', '')} · {reqs.get('title', '')}".strip(" ·"))
    out = []
    for skill, s in stats.items():
        rel = next((o for o in tax.related(skill) if (have.get(o) or {}).get("status") in ("demonstrated", "mentioned")), "")
        if s["named"] < 2 and not (s["mandatory"] and s["in_family"]):
            continue
        priority = 3 * s["mandatory"] + s["preferred"] + 1.5 * (s["named"] - s["mandatory"] - s["preferred"])
        if family:                                # relevance: the share of its jobs in the chosen family
            priority *= 0.6 + 0.9 * s["in_family"] / s["named"]
        if rel or s["status"] == "mentioned":
            priority *= 0.8                       # already partly there: still listed, a little lower
        out.append({**s, "related_to": rel, "priority": round(priority, 1), "next_step": next_step(skill, rel)})
    out.sort(key=lambda s: (-s["priority"], tax.ORDER[s["skill"]]))
    return out[:top]
