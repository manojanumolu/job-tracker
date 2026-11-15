"""M6 Part 12 E (interview preparation) and the résumé improvement rules:
nothing invented, nothing claimed about employers."""
import re
from datetime import date
from pathlib import Path

import pytest

import interview_prep as ip
import job_requirements as jr
import resume_improve as ri
import resume_profile as rp
import skill_taxonomy as tax

FIX = Path(__file__).parent / "fixtures" / "m6"
TODAY = date(2026, 11, 15)
SWE_TEXT = (FIX / "resume_fresher_swe.txt").read_text()
SWE = rp.build_profile(SWE_TEXT, today=TODAY)
DA = rp.build_profile((FIX / "resume_data_analyst.txt").read_text(), today=TODAY)
BACKEND = jr.requirements({"title": "Associate Software Engineer", "company": "ExampleCo"},
                          (FIX / "job_backend_fresher.txt").read_text())


def _all(qs):
    return [q for t in qs["tracks"].values() for q in t]


@pytest.mark.parametrize("level", ip.LEVELS)
def test_every_level_has_questions_of_that_level_only(level):
    qs = ip.question_set(SWE, BACKEND, level)
    qs_all = _all(qs)
    assert qs["count"] == len(qs_all) >= 8 and all(q["level"] == level for q in qs_all)
    assert {"behavioural", "resume", "project"} <= set(qs["tracks"])
    for q in qs_all:
        assert q["question"] and q["why"] and q["concepts"] and q["structure"] and q["example"]


def test_job_families_choose_their_topics():
    analyst = ip.question_set(DA, jr.requirements({"title": "Data Analyst"}, "Requirements\n• SQL and Excel"), "basic")
    assert analyst["family"] == "Data & analytics"
    assert {"SQL", "Excel", "Statistics"} <= {q["skill"] for q in _all(analyst)}
    qa = ip.question_set({}, jr.requirements({"title": "QA Engineer"}, "Requirements\n• Selenium"), "intermediate")
    assert qa["family"] == "Testing & QA" and "Selenium" in {q["skill"] for q in _all(qa)}
    ops = ip.question_set({}, jr.requirements({"title": "Cloud DevOps Engineer"}, ""), "basic")
    assert {"Linux", "Docker", "AWS", "Git"} & {q["skill"] for q in _all(ops)}


def test_resume_questions_quote_the_resume():
    qs = _all(ip.question_set(SWE, BACKEND, "basic"))
    proj = [q for q in qs if q["track"] == "project"]
    assert proj and "Campus Placement Portal" in proj[0]["question"] and "placement portal" in proj[0]["evidence"]
    res = [q for q in qs if q["track"] == "resume"]
    assert any("Example Fintech" in q["question"] for q in res)
    assert all(q["source"] == "resume" and q["evidence"] for q in proj + res)
    skill_qs = [q for q in res if q["skill"]]
    assert skill_qs and all(SWE["skills"][q["skill"]]["status"] in ("demonstrated", "mentioned") for q in skill_qs)


def test_an_empty_profile_gets_curated_questions_only():
    qs = ip.question_set({}, BACKEND, "basic")
    assert qs["count"] > 5 and "resume" not in qs["tracks"] and "project" not in qs["tracks"]


def test_a_missing_job_description_still_gets_a_sensible_set():
    qs = ip.question_set(SWE, None, "basic")
    assert qs["family"] == "General" and qs["count"] > 5
    qs2 = ip.question_set(SWE, jr.requirements({"title": ""}), "intermediate")
    assert qs2["count"] > 5


def test_no_question_is_repeated():
    for level in ip.LEVELS:
        ids = [q["id"] for q in _all(ip.question_set(SWE, BACKEND, level))]
        texts = [q["question"] for q in _all(ip.question_set(SWE, BACKEND, level))]
        assert len(ids) == len(set(ids)) and len(texts) == len(set(texts))


def test_no_question_claims_to_be_from_an_employer():
    qs = ip.question_set(SWE, BACKEND, "intermediate")
    assert "not questions this employer is known to ask" in qs["disclaimer"]
    for q in _all(qs):
        text = (q["question"] + q["why"] + q["example"]).lower()
        assert "exampleco" not in text and not re.search(r"asked (?:at|by)|real interview question|this company asks", text)
        assert q["source"] in ("bank", "resume")


def test_the_bank_is_well_formed():
    assert len(ip.BANK) >= 60 and all(q["skill"] == "" or tax.known(q["skill"]) for q in ip.BANK)
    for level in ip.LEVELS:
        assert sum(q["level"] == level for q in ip.BANK) >= 8
    for track in ("programming", "dsa", "database", "framework", "cloud", "behavioural", "hr", "technical"):
        assert any(q["track"] == track for q in ip.BANK)


# ── résumé improvement ──────────────────────────────────────────────────────

def _skills_named(text):
    return set(tax.find_skills(text))


def test_suggestions_never_add_skills_numbers_or_claims():
    sugg = ri.suggestions(SWE, BACKEND, ri.text_findings(SWE, SWE_TEXT, {"file_type": "txt"}))
    assert sugg
    resume_skills = set(SWE["skills"])
    for s in sugg:
        after = s["after"].replace(ri.PLACEHOLDER, "")
        if s["type"] != "keyword":
            assert _skills_named(after) <= _skills_named(s["before"]) | resume_skills, s
        assert set(re.findall(r"\d+", after)) <= set(re.findall(r"\d+", s["before"])), s      # no invented figures
        for word in ("led a team of", "increased revenue", "certified in", "years of experience"):
            assert word not in after.lower() or word in s["before"].lower()


def test_outcomes_use_a_placeholder_and_contributions_a_verb_choice():
    sugg = ri.suggestions(SWE, None, [])
    outcome = next(s for s in sugg if s["type"] == "no_outcome")
    assert outcome["after"].endswith(ri.PLACEHOLDER)
    vague = next(s for s in sugg if s["type"] == "unclear_contribution")
    assert vague["before"] == "Worked on a team project" and vague["after"].startswith("[your action:")


def test_a_keyword_is_only_suggested_where_a_related_skill_is_shown():
    r = jr.requirements({"title": "Engineer"}, "Requirements\n• PostgreSQL\n• Kubernetes\n• Scala")
    kw = [s for s in ri.suggestions(DA, r, []) if s["type"] == "keyword"]
    assert [s["title"] for s in kw] == ["The posting asks for PostgreSQL; your résumé shows MySQL"]
    assert "if you really used PostgreSQL" in kw[0]["after"] and "Scala" not in str(kw)


def test_unsupported_skills_repetition_formatting_and_ats():
    text = ("Name Person\nSKILLS\nPython, Java, Docker\n• Built a portal with Python for the college fest\n"
            "- Built a portal with Python for the college fest\nJan 2024 - 06/2024\nPROJECTS\nX\n"
            "• Built a REST API in Python for students\n")
    prof = rp.build_profile(text, today=TODAY)
    findings = ri.text_findings(prof, text, {"tables": 2, "header_text": "me@example.com", "file_type": "docx"})
    kinds = {f["type"] for f in findings}
    assert {"repetition", "formatting", "ats"} <= kinds
    titles = " ".join(f["title"] for f in findings)
    assert "Tables in the document" in titles and "Contact details only in the header" in titles and "Dates use two formats" in titles
    unsupported = [s for s in ri.suggestions(prof, None, findings) if s["type"] == "unsupported_skill"]
    assert unsupported and "Java" in unsupported[0]["before"] and "Docker" in unsupported[0]["before"]


def test_a_multi_column_pdf_and_icons_are_ats_risks():
    frag = "\n".join(["ab"] * 30) + "\n☎ +91 99999 99999 ✉ me@example.com"
    findings = ri.text_findings({}, frag, {"file_type": "pdf"})
    titles = [f["title"] for f in findings]
    assert "The PDF may use columns" in titles and "Icons or emoji in the text" in titles


def test_suggestion_ids_are_stable_and_unique():
    a = ri.suggestions(SWE, BACKEND, [])
    b = ri.suggestions(SWE, BACKEND, [])
    assert [s["id"] for s in a] == [s["id"] for s in b] and len({s["id"] for s in a}) == len(a)
    assert all(re.fullmatch(r"[0-9a-f]{12}", s["id"]) for s in a)


def test_the_export_lists_only_what_was_accepted():
    sugg = ri.suggestions(SWE, None, [])
    text = ri.export_text("My Resume", sugg[:2])
    assert text.startswith("Résumé improvement notes — My Resume") and sugg[0]["title"] in text
    assert sugg[2]["title"] not in text and "Replace every placeholder" in text
