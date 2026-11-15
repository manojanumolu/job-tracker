"""M6 accuracy evaluation (Part 13). Run it to print the report:

    python tests/m6_eval.py

Each metric is computed separately, with the cases that fail listed — the
numbers are reported as they come out, not tuned to a target.

Definitions
  skill extraction       predicted = skills the profile asserts (any status
                         except needs_verification, which asks a person)
                         vs the labelled skills; micro-averaged
  demonstrated           the same for skills marked demonstrated
  false-positive rate    FP / (FP + TN) over the whole skill dictionary
  mandatory extraction   the posting's mandatory skills found vs labelled
  mandatory matching     per pair, the mandatory skills the engine says the
                         résumé shows (demonstrated or mentioned) vs labelled
  eligibility accuracy   per pair, "has a critical issue" vs labelled
  evidence attribution   the strongest evidence kind for labelled skills
  score consistency      share of perturbation checks that hold (repeat,
                         reorder sections, add a demonstrated required skill)
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT / "src", ROOT / "tests"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import job_requirements as jr  # noqa: E402
import resume_match as rm  # noqa: E402
import resume_profile as rp  # noqa: E402
import skill_taxonomy as tax  # noqa: E402
from m6_eval_data import JOBS, PAIRS, RESUMES  # noqa: E402

TODAY = date(2026, 11, 15)
KIND = {"academic_project": "project", "personal_project": "project", "project": "project"}


def _pr(tp: int, fp: int, fn: int) -> tuple[float, float]:
    return (tp / (tp + fp) if tp + fp else 1.0), (tp / (tp + fn) if tp + fn else 1.0)


def evaluate() -> dict:
    profiles = {rid: rp.build_profile(r["text"], today=TODAY) for rid, r in RESUMES.items()}
    reqs = {jid: jr.requirements({"title": j["title"], "location": "India"}, j["text"]) for jid, j in JOBS.items()}
    out: dict = {"failures": {}}

    # skill extraction
    tp = fp = fn = tn = 0
    dtp = dfp = dfn = 0
    fails = []
    verify = 0
    for rid, lab in RESUMES.items():
        skills = profiles[rid]["skills"]
        pred = {s for s, i in skills.items() if i["status"] != "needs_verification"}
        verify += sum(1 for i in skills.values() if i["status"] == "needs_verification")
        gold = set(lab["skills"])
        tp += len(pred & gold)
        fp += len(pred - gold)
        fn += len(gold - pred)
        tn += len(set(tax.SKILLS) - pred - gold)
        if pred != gold:
            fails.append(f"{rid}: extra {sorted(pred - gold)} missed {sorted(gold - pred)}")
        dpred = {s for s, i in skills.items() if i["status"] == "demonstrated"}
        dgold = set(lab["demonstrated"])
        dtp += len(dpred & dgold)
        dfp += len(dpred - dgold)
        dfn += len(dgold - dpred)
        if dpred != dgold:
            fails.append(f"{rid} (demonstrated): extra {sorted(dpred - dgold)} missed {sorted(dgold - dpred)}")
    out["skill_precision"], out["skill_recall"] = _pr(tp, fp, fn)
    out["demonstrated_precision"], out["demonstrated_recall"] = _pr(dtp, dfp, dfn)
    out["false_positive_rate"] = fp / (fp + tn)
    out["needs_verification_flags"] = verify
    out["failures"]["skills"] = fails

    # mandatory skills read from the postings
    tp = fp = fn = 0
    fails = []
    for jid, lab in JOBS.items():
        pred = {s for s, r in reqs[jid]["skills"].items() if r["importance"] == "mandatory"}
        gold = set(lab["mandatory"])
        tp += len(pred & gold)
        fp += len(pred - gold)
        fn += len(gold - pred)
        if pred != gold:
            fails.append(f"{jid}: extra {sorted(pred - gold)} missed {sorted(gold - pred)}")
    out["mandatory_extraction_precision"], out["mandatory_extraction_recall"] = _pr(tp, fp, fn)
    out["failures"]["mandatory_extraction"] = fails

    # matching, eligibility
    tp = fp = fn = 0
    right = 0
    fails_m, fails_e = [], []
    results = {}
    for rid, jid, lab in PAIRS:
        r = rm.analyse(profiles[rid], reqs[jid])
        results[(rid, jid)] = r
        gold_mand = set(JOBS[jid]["mandatory"])
        pred = {x["skill"] for x in r["rows"] if x["skill"] in gold_mand and x["status"] in ("demonstrated", "mentioned")}
        gold = set(lab["satisfied"])
        tp += len(pred & gold)
        fp += len(pred - gold)
        fn += len(gold - pred)
        if pred != gold:
            fails_m.append(f"{rid} × {jid}: extra {sorted(pred - gold)} missed {sorted(gold - pred)}")
        crit = r["critical"] > 0
        if crit == lab["critical"]:
            right += 1
        else:
            fails_e.append(f"{rid} × {jid}: predicted critical={crit}, labelled {lab['critical']} "
                           f"({'; '.join(i['text'] for i in r['issues'] if i['severity'] == 'critical') or 'no critical issue'})")
    out["matching_precision"], out["matching_recall"] = _pr(tp, fp, fn)
    out["eligibility_accuracy"] = right / len(PAIRS)
    out["failures"]["matching"] = fails_m
    out["failures"]["eligibility"] = fails_e

    # evidence attribution
    total = good = 0
    fails = []
    for rid, lab in RESUMES.items():
        for skill, kind in lab["evidence"].items():
            total += 1
            info = profiles[rid]["skills"].get(skill)
            got = KIND.get(info["kinds"][0], info["kinds"][0]) if info else "missing"
            if got == kind:
                good += 1
            else:
                fails.append(f"{rid}/{skill}: expected {kind}, got {got}")
    out["evidence_accuracy"] = good / total if total else 1.0
    out["failures"]["evidence"] = fails

    # graduation year and employment months
    yrs = sum(profiles[rid]["graduation_year"] == lab["grad_year"] for rid, lab in RESUMES.items())
    mon = sum(profiles[rid]["totals"]["employment_months"] == lab["emp_months"] for rid, lab in RESUMES.items())
    out["graduation_year_accuracy"] = yrs / len(RESUMES)
    out["employment_months_accuracy"] = mon / len(RESUMES)
    out["failures"]["dates"] = [f"{rid}: grad {profiles[rid]['graduation_year']} vs {lab['grad_year']}, months "
                                f"{profiles[rid]['totals']['employment_months']} vs {lab['emp_months']}"
                                for rid, lab in RESUMES.items()
                                if profiles[rid]["graduation_year"] != lab["grad_year"]
                                or profiles[rid]["totals"]["employment_months"] != lab["emp_months"]]

    # score consistency
    checks = []
    for (rid, jid), r in results.items():
        checks.append(rm.analyse(profiles[rid], reqs[jid]) == r)                         # deterministic
        text = RESUMES[rid]["text"]
        parts = text.split("\n\n")
        if len(parts) > 2:                                                               # reorder blocks
            alt = rm.analyse(rp.build_profile("\n\n".join([parts[0]] + parts[1:][::-1]), today=TODAY), reqs[jid])
            checks.append(alt["overall"] == r["overall"])
        missing = [x["skill"] for x in r["rows"] if x["status"] == "not_found" and x["importance"] == "mandatory"]
        if missing and r["overall"] is not None:                                         # monotonic
            better = rp.apply_corrections(profiles[rid], {})
            better["skills"][missing[0]] = {"category": tax.category(missing[0]), "status": "demonstrated",
                                            "kinds": ["project"], "source": "resume",
                                            "evidence": [{"section": "projects", "entry": 0, "kind": "project",
                                                          "text": missing[0], "certain": True}]}
            checks.append((rm.analyse(better, reqs[jid])["overall"] or 0) >= r["overall"])
    out["score_consistency"] = sum(checks) / len(checks)
    out["consistency_checks"] = len(checks)
    out["size"] = {"resumes": len(RESUMES), "jobs": len(JOBS), "pairs": len(PAIRS)}
    return out


def report(m: dict) -> str:
    pct = lambda v: f"{100 * v:.1f}%"  # noqa: E731
    lines = [f"M6 evaluation — {m['size']['resumes']} résumés, {m['size']['jobs']} jobs, {m['size']['pairs']} pairs", "",
             f"Skill extraction precision      {pct(m['skill_precision'])}",
             f"Skill extraction recall         {pct(m['skill_recall'])}",
             f"Demonstrated-skill precision    {pct(m['demonstrated_precision'])}",
             f"Demonstrated-skill recall       {pct(m['demonstrated_recall'])}",
             f"False-positive rate             {pct(m['false_positive_rate'])}  (over the {len(tax.SKILLS)}-skill dictionary)",
             f"Needs-verification flags        {m['needs_verification_flags']}  (not counted as extracted)",
             f"Mandatory extraction precision  {pct(m['mandatory_extraction_precision'])}",
             f"Mandatory extraction recall     {pct(m['mandatory_extraction_recall'])}",
             f"Mandatory matching precision    {pct(m['matching_precision'])}",
             f"Mandatory matching recall       {pct(m['matching_recall'])}",
             f"Eligibility accuracy            {pct(m['eligibility_accuracy'])}",
             f"Evidence attribution accuracy   {pct(m['evidence_accuracy'])}",
             f"Graduation-year accuracy        {pct(m['graduation_year_accuracy'])}",
             f"Employment-months accuracy      {pct(m['employment_months_accuracy'])}",
             f"Score consistency               {pct(m['score_consistency'])}  ({m['consistency_checks']} checks)", ""]
    for k, v in m["failures"].items():
        if v:
            lines.append(f"Failing cases — {k}:")
            lines += [f"  - {x}" for x in v]
    return "\n".join(lines)


if __name__ == "__main__":
    print(report(evaluate()))
