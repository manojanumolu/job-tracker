"""M6 Part 12 B — structured résumé extraction: sections, education,
dates, experience vs internships vs projects, skills with evidence, and
the things it must NOT do (invent, over-claim, trust ambiguous words)."""
from datetime import date

import pytest

import resume_profile as rp
import skill_taxonomy as tax

TODAY = date(2026, 11, 15)


def P(text, **kw):
    return rp.build_profile(text, today=TODAY, **kw)


CV = """Meera Iyer
Chennai | meera@example.com | 9876543210

EDUCATION
M.Tech in Data Science, Example Institute of Technology, 2023 – 2025, CGPA 9.1/10
B.E. (ECE), Example Engineering College, 2019 - 2023, 82%

WORK EXPERIENCE
Data Analyst, Example Analytics Pvt Ltd | Jul 2025 – Present
• Built Power BI dashboards for 4 business teams using DAX and SQL Server
• Automated weekly reports with Python and Pandas
Software Engineer Intern, Example Systems | Jan 2025 - Jun 2025
• Wrote unit tests in pytest for a Django service

PROJECTS
Fraud Detection (Final-year project)
• Trained a random forest classifier with scikit-learn; precision 0.91
Portfolio site — personal project
• React and Tailwind CSS, deployed on Firebase

SKILLS
Languages: Python, SQL, C, R
Tools: Git, Docker, Excel

CERTIFICATIONS
Microsoft Certified: Power BI Data Analyst Associate
Coursera — Deep Learning Specialization

RELEVANT COURSEWORK
Probability and Statistics, Machine Learning, Operating Systems

ACHIEVEMENTS
Winner, Example Hackathon 2024
"""


@pytest.fixture(scope="module")
def cv():
    return P(CV)


def test_sections_name_and_masked_contact(cv):
    assert cv["name"] == "Meera Iyer"
    assert set(cv["sections_found"]) >= {"education", "experience", "projects", "skills", "certifications", "coursework",
                                         "achievements"}
    c = cv["contact"]
    assert c["email"] == "m•••@example.com" and c["phone"].endswith("10") and "98765" not in c["phone"]
    assert c["location"] == "Chennai"


def test_education_degree_specialisation_years_and_scores(cv):
    mt, be = cv["education"]
    assert (mt["level"], mt["specialization"], mt["end_year"], mt["score"]) == ("masters", "Data Science", 2025, "9.1/10")
    assert (be["level"], be["specialization"], be["end_year"], be["score"]) == ("bachelors", "Electronics & Communication",
                                                                               2023, "82%")
    assert cv["graduation_year"] == 2025                     # the highest degree


def test_employment_internships_and_their_months(cv):
    job, intern = cv["experience"]
    assert job["kind"] == "employment" and job["title"] == "Data Analyst" and "Example Analytics" in job["organisation"]
    assert job["dates"] == {"start": "2025-07", "end": "present", "months": 17, "conflict": False}
    assert intern["kind"] == "internship" and intern["dates"]["months"] == 6
    assert cv["totals"]["employment_months"] == 17 and cv["totals"]["internship_months"] == 6


def test_projects_are_academic_or_personal_never_employment(cv):
    (fraud, site) = cv["projects"]
    assert fraud["kind"] == "academic_project" and "scikit-learn" in fraud["skills"]
    assert site["kind"] == "personal_project" and {"React", "CSS", "Firebase"} <= set(site["skills"])
    assert cv["totals"]["employment_months"] == 17           # projects add nothing to employment


def test_certifications_achievements_coursework(cv):
    assert cv["certifications"][0].startswith("Microsoft Certified") and len(cv["certifications"]) == 2
    assert cv["achievements"] == ["Winner, Example Hackathon 2024"]
    assert cv["coursework"] == ["Probability and Statistics", "Machine Learning", "Operating Systems"]


@pytest.mark.parametrize("skill, status, kind", [
    ("Power BI", "demonstrated", "employment"),
    ("SQL Server", "demonstrated", "employment"),
    ("Pandas", "demonstrated", "employment"),
    ("JUnit / pytest", "demonstrated", "internship"),
    ("Django", "demonstrated", "internship"),
    ("scikit-learn", "demonstrated", "academic_project"),
    ("React", "demonstrated", "personal_project"),
    ("Docker", "mentioned", "listed"),
    ("Deep learning", "mentioned", "certification"),
    ("Operating systems", "mentioned", "coursework"),
])
def test_each_skill_keeps_its_strongest_evidence(cv, skill, status, kind):
    info = cv["skills"][skill]
    assert info["status"] == status and info["kinds"][0] == kind
    assert info["evidence"][0]["text"] and info["evidence"][0]["section"]


def test_abbreviations_and_aliases_are_the_same_skill():
    p = P("SKILLS\nJS, TS, Postgres, k8s, GCP, sklearn, OOPS, DSA\n")
    assert {"JavaScript", "TypeScript", "PostgreSQL", "Kubernetes", "GCP", "scikit-learn", "OOP",
            "Data structures & algorithms"} <= set(p["skills"])


def test_similar_skill_names_stay_apart():
    p = P("PROJECTS\nShop app\n• Built with Java and Spring Boot\n\nSKILLS\nJavaScript\n")
    assert p["skills"]["Java"]["status"] == "demonstrated" and p["skills"]["JavaScript"]["status"] == "mentioned"
    q = P("PROJECTS\nApp\n• React Native mobile app\n")
    assert "React Native" in q["skills"] and "React" not in q["skills"]
    r = P("SKILLS\nSQL Server, MySQL, NoSQL\n")
    assert {"SQL Server", "MySQL", "NoSQL"} <= set(r["skills"])


@pytest.mark.parametrize("text, absent", [
    ("ACHIEVEMENTS\nI go to every hackathon I can\n", "Go"),
    ("SUMMARY\nExcellent at teamwork, excel at deadlines? no: excellent communication\n", "Excel"),
    ("PROJECTS\nPortfolio\n• Hosted at github.com/me/portfolio\n", "Git"),
    ("SUMMARY\nI am reactive to feedback and like Java coffee? no.\n", "React"),
    ("PROJECTS\nShop\n• Bootstrapped the business plan\n", "CSS"),
    ("SUMMARY\nA rusty bike and a swift reply\n", "Rust"),
    ("SUMMARY\nI make swift decisions\n", "Swift"),
    ("SUMMARY\nI react quickly to feedback and react well under pressure\n", "React"),
])
def test_false_positive_words_are_not_skills(text, absent):
    assert absent not in P(text)["skills"]


def test_ambiguous_words_need_verification_outside_a_skills_list():
    p = P("PROJECTS\nChatbot\n• Used AI to answer questions, written in Go\n\nSKILLS\nC, Python\n")
    assert p["skills"]["Generative AI / LLMs"]["status"] == "needs_verification"
    assert p["skills"]["C"]["status"] == "mentioned"                     # a skills list is trusted
    assert "Go" not in p["skills"] or p["skills"]["Go"]["status"] == "needs_verification"


def test_learning_a_skill_is_not_demonstrating_it():
    p = P("PROJECTS\nTodo app\n• Learning Docker to containerise it\n• Familiar with Kubernetes basics\n")
    assert p["skills"]["Docker"]["status"] == "mentioned" and p["skills"]["Kubernetes"]["status"] == "mentioned"


def test_multiple_projects_each_keep_their_skills():
    p = P("PROJECTS\nA\n• Flask API\n\nB\n• Angular UI\n\nC\n• Tableau dashboard\n")
    assert [x["skills"] for x in p["projects"]] == [["Flask"], ["Angular"], ["Tableau"]]


def test_conflicting_and_future_dates_are_flagged_not_trusted():
    p = P("EXPERIENCE\nAnalyst, X Corp | Jun 2025 – Jan 2024\n• Did reporting with Excel\n"
          "\nDeveloper, Y Corp | Jan 2030 – Present\n• Python\n")
    assert all(x["dates"]["conflict"] for x in p["experience"])
    assert p["totals"]["employment_months"] == 0
    assert any("Check the dates" in w for w in p["warnings"])


@pytest.mark.parametrize("text, start, end", [
    ("Intern, X | 06/2024 - 08/2024", "2024-06", "2024-08"),
    ("Intern, X | 2024-06 to 2024-08", "2024-06", "2024-08"),
    ("Intern, X | June 2024 – August 2024", "2024-06", "2024-08"),
    ("Intern, X | Jun'24 - Aug'24", "2024-06", "2024-08"),
    ("Intern, X (2023 - 2024)", "2023-01", "2024-12"),
])
def test_date_formats(text, start, end):
    r = rp.date_range(text, TODAY)
    assert (r["start"], r["end"]) == (start, end)


def test_missing_evidence_stays_missing_nothing_is_invented():
    p = P("Some Person\nSKILLS\nPython\n")
    assert p["education"] == [] and p["experience"] == [] and p["projects"] == [] and p["graduation_year"] is None
    assert p["contact"]["email"] == "" and "No email address was found." in p["warnings"]
    assert "No education section was recognised." in p["warnings"]


def test_ambiguous_qualifications_are_left_for_the_person():
    p = P("EDUCATION\nGraduate, Example College\n")
    (e,) = p["education"]
    assert e["level"] == "" and e["end_year"] is None and e["specialization"] == ""


def test_an_expected_degree_is_marked_expected():
    p = P("EDUCATION\nB.Tech in Information Technology, Example University (2023 – 2027)\n")
    (e,) = p["education"]
    assert e["expected"] and e["end_year"] == 2027 and e["specialization"] == "Information Technology"


def test_an_empty_resume_is_an_empty_profile():
    p = P("")
    assert p["skills"] == {} and p["name"] == "" and p["sections_found"] == []


def test_a_name_is_only_taken_when_it_reads_as_one():
    assert P("Python Developer Resume\nSKILLS\nPython")["name"] == ""
    assert P("CURRICULUM VITAE\nPython")["name"] == ""
    assert P("ANIL KUMAR\nSKILLS\nPython")["name"] == "Anil Kumar"


# ── corrections ──────────────────────────────────────────────────────────────

def test_corrections_apply_without_changing_the_extraction(cv):
    import copy
    before = copy.deepcopy(cv)
    out = rp.apply_corrections(cv, {"remove_skills": ["Docker"], "add_skills": ["Kafka"],
                                    "experience_kind": {"1": "employment"}, "project_kind": {"1": "academic_project"},
                                    "education": {"0": {"specialization": "AI & ML", "end_year": 2026}}, "name": "Meera I."})
    assert cv == before
    assert "Docker" not in out["skills"] and out["skills"]["Kafka"]["status"] == "needs_verification"
    assert out["skills"]["Kafka"]["evidence"][0]["kind"] == "user"
    assert out["experience"][1]["kind"] == "employment" and out["projects"][1]["kind"] == "academic_project"
    assert out["skills"]["Django"]["evidence"][0]["kind"] == "employment"
    assert out["education"][0]["specialization"] == "AI & ML" and out["graduation_year"] == 2026
    assert out["name"] == "Meera I."


def test_confirming_an_ambiguous_skill():
    p = P("PROJECTS\nBot\n• Used AI, with a rule engine, to answer questions\n")
    assert p["skills"]["Generative AI / LLMs"]["status"] == "needs_verification"
    out = rp.apply_corrections(p, {"confirm_skills": ["Generative AI / LLMs"]})
    assert out["skills"]["Generative AI / LLMs"]["status"] == "demonstrated"


def test_the_taxonomy_is_consistent():
    assert all(tax.category(s) in tax.CATEGORIES for s in tax.SKILLS)
    assert all(a in tax.RELATED[b] for a in tax.RELATED for b in tax.RELATED[a])     # symmetric
    assert all(name not in tax.RELATED[name] for name in tax.SKILLS)
    assert set(tax.to_m5(["Flask", "Django", "MySQL"])) == {"Django / Flask", "Databases"}
    import job_intel
    assert all(spec["m5"] in job_intel.SKILLS for spec in tax.SKILLS.values() if spec["m5"])
