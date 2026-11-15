"""M6 Part 12 C and D — the matching engine and skill gaps, on realistic
résumé and job fixtures. Scores are recomputed independently here; a
critical eligibility failure must stay visible whatever the score."""
from datetime import date
from pathlib import Path

import pytest

import job_requirements as jr
import resume_match as rm
import resume_profile as rp

FIX = Path(__file__).parent / "fixtures" / "m6"
TODAY = date(2026, 11, 15)


def prof(text):
    return rp.build_profile(text, today=TODAY)


def reqs(text, title="Software Engineer", **job):
    return jr.requirements({"title": title, "company": "ExampleCo", "location": job.pop("location", "Hyderabad, India"),
                            **job}, text)


FRESHER_CV = prof((FIX / "resume_fresher_swe.txt").read_text())
ANALYST_CV = prof((FIX / "resume_data_analyst.txt").read_text())
BACKEND_JD = (FIX / "job_backend_fresher.txt").read_text()
EXPERIENCED_CV = prof("""Kiran Rao
Bengaluru · kiran@example.com
EDUCATION
B.Tech in Computer Science, Example University, 2019
EXPERIENCE
Senior Software Engineer, Example Corp | Jan 2021 – Present
• Built Java and Spring Boot microservices with PostgreSQL and Kafka
• Designed REST APIs and CI/CD pipelines on AWS with Docker
Software Engineer, Example Labs | Jul 2019 – Dec 2020
• Python and SQL data services
SKILLS
Java, Python, SQL, Spring Boot, Docker, AWS, Kafka, Data Structures and Algorithms
""")


def A(profile, r, **kw):
    return rm.analyse(profile, r, **kw)


def row(result, skill):
    return next(x for x in result["rows"] if x["skill"] == skill)


# ── C. matching cases ────────────────────────────────────────────────────────

def test_1_excellent_fresher_match():
    r = A(FRESHER_CV, reqs(BACKEND_JD))
    assert r["overall"] >= 80 and r["critical"] == 0 and r["eligibility"] == "no_issue_found"
    assert r["verdict"] == "Strong" and r["confidence"] == "High"


def test_2_partial_fresher_match():
    r = A(ANALYST_CV, reqs(BACKEND_JD))
    assert r["overall"] is not None and 20 <= r["overall"] < 70
    assert row(r, "REST APIs")["status"] == "not_found"
    assert any(i["area"] == "Education" for i in r["issues"])          # B.Sc Statistics vs CS/IT/ECE, 2024 vs 2025/26


def test_3_experienced_candidate_for_a_fresher_role():
    r = A(EXPERIENCED_CV, reqs(BACKEND_JD))
    exp = [i for i in r["issues"] if i["area"] == "Experience"]
    assert exp and exp[0]["severity"] == "critical" and "0–1 years" in exp[0]["text"]
    assert r["verdict"] == "Eligibility issue"


def test_4_fresher_for_an_experienced_role():
    r = A(FRESHER_CV, reqs("Requirements\n• 3+ years of experience with Python and SQL\n• REST APIs"))
    crit = [i for i in r["issues"] if i["severity"] == "critical"]
    assert crit and crit[0]["area"] == "Experience" and "internships and projects don't count" in crit[0]["text"]
    assert r["components"]["experience"] == 0


def test_5_a_missing_mandatory_skill():
    r = A(FRESHER_CV, reqs("Requirements\n• Must have Kafka and Python\n"))
    kafka = row(r, "Kafka")
    assert (kafka["importance"], kafka["status"]) == ("mandatory", "not_found")
    assert [x["skill"] for x in rm.pair_gaps(r)["missing_mandatory"]] == ["Kafka"]


def test_6_a_missing_preferred_skill_only_lowers_the_preferred_part():
    base = A(FRESHER_CV, reqs("Requirements\n• Python and SQL\nNice to have\n• Django"))
    miss = A(FRESHER_CV, reqs("Requirements\n• Python and SQL\nNice to have\n• Scala"))
    assert base["components"]["mandatory"] == miss["components"]["mandatory"] == 100
    assert base["components"]["preferred"] == 100 and miss["components"]["preferred"] == 0
    assert miss["critical"] == 0 and row(miss, "Scala")["importance"] == "preferred"


def test_7_all_mandatory_skills_present():
    r = A(FRESHER_CV, reqs("Requirements\n• Python, SQL, REST APIs and Django"))
    assert r["components"]["mandatory"] == 100
    assert all(x["status"] == "demonstrated" for x in r["rows"])


def test_8_a_skill_only_in_coursework_is_mentioned_not_demonstrated():
    r = A(FRESHER_CV, reqs("Requirements\n• Operating systems and DBMS concepts"))
    assert row(r, "Operating systems")["status"] == "mentioned" and row(r, "Operating systems")["classification"] == "Coursework"
    assert r["components"]["mandatory"] == 50


def test_9_a_skill_demonstrated_in_a_project():
    x = row(A(FRESHER_CV, reqs("Requirements\n• Django and MySQL")), "Django")
    assert x["status"] == "demonstrated" and x["classification"] == "Academic project" and "placement portal" in x["evidence"]


def test_10_a_skill_demonstrated_through_employment():
    x = row(A(EXPERIENCED_CV, reqs("Requirements\n• Kafka")), "Kafka")
    assert x["status"] == "demonstrated" and x["classification"] == "Employment"


def test_11_related_skills_are_transferable_not_exact():
    r = A(ANALYST_CV, reqs("Requirements\n• PostgreSQL and Tableau\n• Power BI"))
    pg = row(r, "PostgreSQL")
    assert pg["status"] == "related" and pg["related_to"] == "MySQL" and "relational SQL databases" in pg["note"]
    assert row(r, "Power BI")["status"] == "demonstrated"
    assert rm.STATUSES["related"][1] < rm.STATUSES["demonstrated"][1]


def test_12_degree_mismatches():
    hi = A(FRESHER_CV, reqs("Requirements\n• M.Tech in Computer Science\n• Python"))
    assert any(i["severity"] == "critical" and i["area"] == "Education" for i in hi["issues"])
    other = A(ANALYST_CV, reqs("Requirements\n• B.Tech / B.E. required\n• SQL and Python"))
    edu = [i for i in other["issues"] if i["area"] == "Education"]
    assert edu and edu[0]["severity"] == "warning" and "btech" in edu[0]["text"]


def test_13_a_graduation_year_outside_the_batch_is_critical():
    r = A(FRESHER_CV, reqs("Eligibility\n• B.Tech, 2024 or 2025 batch only\n• Python and SQL"))
    crit = [i for i in r["issues"] if i["severity"] == "critical"]
    assert crit and "graduation in 2026" in crit[0]["text"] and r["components"]["education"] == 0


def test_14_location_mismatch():
    strict = A(FRESHER_CV, reqs("Requirements\n• Python and SQL\n• Must be based in Pune", location="Pune, India"))
    assert any(i["area"] == "Location" and i["severity"] == "warning" for i in strict["issues"])
    soft = A(FRESHER_CV, reqs("Requirements\n• Python and SQL", location="Pune, India"))
    loc = [i for i in soft["issues"] if i["area"] == "Location"]
    assert loc and loc[0]["severity"] == "info" and soft["critical"] == 0


def test_15_an_insufficient_job_description():
    r = rm.analyse(FRESHER_CV, jr.requirements({"title": "Graduate Engineer Trainee", "evidence": {}}))
    assert r["overall"] is None and r["band"] == "Insufficient evidence" and r["verdict"] == "Insufficient evidence"
    assert any("Paste the full job description" in n for n in r["notes"]) and r["confidence"] == "Low"


def test_16_insufficient_resume_evidence():
    r = A(prof("Some Name\nHello"), reqs(BACKEND_JD))
    assert r["overall"] is None and any("no recognisable skills" in n for n in r["notes"])


def test_17_duplicate_job_postings_give_identical_results():
    job = {"title": "Associate Engineer", "company": "X", "evidence": {"requirements": [
        {"text": "Python and SQL required", "importance": "mandatory"}]}}
    r1, r2 = jr.requirements(job), jr.requirements(dict(job))
    assert jr.fingerprint(r1) == jr.fingerprint(r2)
    assert rm.analyse(FRESHER_CV, r1) == rm.analyse(FRESHER_CV, r2)


def test_18_multiple_resumes_for_the_same_candidate():
    r = reqs("Requirements\n• SQL, Excel and Power BI\n• Statistics", title="Data Analyst")
    results = {"swe": A(FRESHER_CV, r), "analyst": A(ANALYST_CV, r)}
    assert rm.best_resume(results) == "analyst"


def test_19_one_resume_against_many_jobs_ranks_eligible_first():
    jobs = [reqs(BACKEND_JD), reqs("Requirements\n• 5+ years of Java"), reqs("Requirements\n• Excel and Tableau")]
    ranked = sorted((A(FRESHER_CV, j) for j in jobs), key=rm.sort_key)
    assert ranked[0]["critical"] == 0 and ranked[-1]["critical"] >= 1


def test_20_an_empty_resume():
    r = A(prof(""), reqs(BACKEND_JD))
    assert r["overall"] is None and all(x["status"] == "not_found" for x in r["rows"])


def test_21_unsupported_requirements_are_not_guessed():
    r = A(FRESHER_CV, reqs("Requirements\n• COBOL and Haskell\n• Mainframe JCL"))
    assert r["rows"] == [] and r["overall"] is None


def test_22_a_contradictory_job_description_is_flagged():
    r = A(FRESHER_CV, reqs("Freshers welcome!\nRequirements\n• 3+ years of experience in Python"))
    assert any(i["area"] == "Posting" and "both" in i["text"] for i in r["issues"])


# ── the score itself ──────────────────────────────────────────────────────────

def test_the_overall_estimate_is_the_documented_weighted_mean():
    r = A(FRESHER_CV, reqs(BACKEND_JD))
    c, w = r["components"], rm.DEFAULT_WEIGHTS
    used = {k: v for k, v in c.items() if v is not None}
    expected = round(sum(used[k] * w[k] for k in used) / sum(w[k] for k in used))
    assert r["overall"] == expected
    credit = {"demonstrated": 1.0, "mentioned": 0.5, "related": 0.35, "needs_verification": 0.25, "not_found": 0.0}
    mand = [x for x in r["rows"] if x["importance"] == "mandatory" and not x.get("alternative_of")]
    assert c["mandatory"] == round(100 * sum(credit[x["status"]] for x in mand) / len(mand))


def test_custom_weights_change_the_estimate_predictably_and_bad_ones_fall_back():
    r = reqs(BACKEND_JD)
    only_skills = A(FRESHER_CV, r, weights={"mandatory": 100, "preferred": 0, "experience": 0, "education": 0, "evidence": 0})
    assert only_skills["overall"] == only_skills["components"]["mandatory"]
    for bad in ({"mandatory": 90}, {"mandatory": 50, "preferred": 50, "experience": 10, "education": 0, "evidence": 0},
                {"mandatory": -10, "preferred": 50, "experience": 40, "education": 10, "evidence": 10}, "x", None):
        assert rm.clean_weights(bad) == rm.DEFAULT_WEIGHTS


def test_a_critical_issue_is_never_hidden_by_a_high_skills_score():
    r = A(EXPERIENCED_CV, reqs("Requirements\n• 0-1 years of experience (freshers only)\n• Java, Spring Boot, Kafka, SQL"))
    assert r["components"]["mandatory"] == 100 and r["critical"] >= 1
    assert r["verdict"] == "Eligibility issue" and r["issues"][0]["severity"] == "critical"
    assert rm.sort_key(r) > rm.sort_key(A(FRESHER_CV, reqs("Requirements\n• Java")))


def test_alternatives_count_once():
    r = A(FRESHER_CV, reqs("Requirements\n• Python or Java\n• SQL"))
    assert row(r, "Java").get("alternative_of") == "Python" and r["components"]["mandatory"] == 100


def test_the_result_is_an_estimate_never_a_promise():
    r = A(FRESHER_CV, reqs(BACKEND_JD))
    text = str(r).lower()
    assert r["kind"] == "estimate"
    for word in ("guaranteed", "shortlisted", "interview-ready", "will be hired", "probability"):
        assert word not in text


def test_the_engine_never_changes_eligibility_decisions():
    """M6 only reads jobs: the classifier, the safety gate and job_filters
    don't import it, and the scanner adds requirement lines only after
    every gate has passed."""
    import inspect

    import job_classifier
    import job_filters
    import scraper
    for mod in (job_classifier, job_filters):
        src = inspect.getsource(mod)
        assert "resume_match" not in src and "job_requirements" not in src
    src = inspect.getsource(scraper.decide)
    assert src.index('job["evidence"] = _evidence(') < src.index("_posting_requirements(")


def test_the_scanner_stores_requirement_lines_after_the_gates_and_never_fails_a_job(monkeypatch):
    import scraper
    lines = scraper._posting_requirements(BACKEND_JD)
    assert {"text": "Strong knowledge of Python or Java", "importance": "mandatory"} in lines
    assert {"text": "Experience with Docker and AWS", "importance": "preferred"} in lines
    monkeypatch.setattr(jr, "posting_requirement_lines", lambda d: 1 / 0)
    assert scraper._posting_requirements(BACKEND_JD) == []


def test_requirements_from_stored_lines_and_from_the_tracker_alone():
    stored = jr.requirements({"title": "Engineer", "evidence": {"requirements": [
        {"text": "Python required", "importance": "mandatory"}, {"text": "AWS is a plus", "importance": "preferred"}]}})
    assert stored["source"] == "stored" and stored["skills"]["AWS"]["importance"] == "preferred"
    old = jr.requirements({"title": "Java Developer", "evidence": {"experience_lines": ["0-1 years in Java"],
                                                                    "skills": {"Java": "Java and SQL"}}})
    assert old["source"] == "tracker" and old["experience"]["fresher"] and "SQL" in old["skills"]


@pytest.mark.parametrize("job", [{}, {"title": None}, {"evidence": "oops"}, {"evidence": {"requirements": "x"}},
                                 {"evidence": {"requirements": [{"text": 5}, {"importance": "mandatory"}, None]}},
                                 {"evidence": {"skills": ["Python"], "experience_lines": [None, 3]}}])
def test_malformed_job_records_never_break_the_engine(job):
    r = rm.analyse(FRESHER_CV, jr.requirements(job))
    assert r["overall"] is None


# ── D. skill gaps ─────────────────────────────────────────────────────────────

def test_no_gaps():
    g = rm.pair_gaps(A(FRESHER_CV, reqs("Requirements\n• Python, SQL and Django")))
    assert not (g["missing_mandatory"] or g["missing_preferred"] or g["related"] or g["next_steps"])


def test_all_mandatory_skills_missing_come_first_with_steps():
    g = rm.pair_gaps(A(ANALYST_CV, reqs("Requirements\n• Java, Spring Boot and Kubernetes\nNice to have\n• Kotlin")))
    assert [x["skill"] for x in g["missing_mandatory"]] == ["Java", "Spring Boot", "Kubernetes"]
    assert [s["priority"] for s in g["next_steps"]] == ["High", "High", "High", "Low"]
    assert "Kubernetes" in g["next_steps"][2]["step"] or "cluster" in g["next_steps"][2]["step"]


def test_partially_demonstrated_skills_get_a_faithful_next_step():
    g = rm.pair_gaps(A(FRESHER_CV, reqs("Requirements\n• Docker and Python")))
    step = next(s for s in g["next_steps"] if s["skill"] == "Docker")
    assert step["priority"] == "Medium" and "only if you really used it" in step["step"]


def test_synonyms_are_not_gaps():
    g = rm.pair_gaps(A(FRESHER_CV, reqs("Requirements\n• Postgres and JS")))
    assert not g["missing_mandatory"]


def test_duplicate_requirements_are_one_row():
    r = A(FRESHER_CV, reqs("Requirements\n• Python\n• Strong Python skills\n• python3"))
    assert [x["skill"] for x in r["rows"]] == ["Python"]


def test_global_gaps_prioritise_required_frequent_and_role_relevant_skills():
    jobs = [reqs("Requirements\n• Kafka and Java", title="Backend Developer"),
            reqs("Requirements\n• Kafka", title="Software Engineer"),
            reqs("Requirements\n• Tableau\nNice to have\n• Kafka", title="Data Analyst"),
            reqs("Requirements\n• Tableau", title="Business Analyst"),
            reqs("Requirements\n• Scala", title="Software Engineer")]
    gaps = rm.global_gaps(FRESHER_CV, jobs)
    assert [g["skill"] for g in gaps][:2] == ["Kafka", "Tableau"]
    assert "Scala" not in [g["skill"] for g in gaps]                     # named once: not worth listing
    focus = rm.global_gaps(FRESHER_CV, jobs, family="Data & analytics")
    assert focus[0]["skill"] == "Tableau"


def test_an_empty_catalogue_has_no_gaps():
    assert rm.global_gaps(FRESHER_CV, []) == []


def test_a_related_skill_lowers_the_priority_and_says_so():
    jobs = [reqs("Requirements\n• Spring Boot", title="Java Developer")] * 3 + \
           [reqs("Requirements\n• Scala", title="Java Developer")] * 3
    gaps = {g["skill"]: g for g in rm.global_gaps(FRESHER_CV, jobs)}
    assert gaps["Spring Boot"]["related_to"] == "Java" and gaps["Spring Boot"]["priority"] < gaps["Scala"]["priority"]
    assert gaps["Spring Boot"]["next_step"].startswith("You already have Java")
