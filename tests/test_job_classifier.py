import pytest

from job_classifier import (
    Category,
    classify_job,
    parse_experience_requirements,
)

ACCEPTED = {Category.FRESHER, Category.ENTRY_LEVEL}


# ---------------------------------------------------------------------------
# Titles / postings that must be alerted on
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "title, description",
    [
        ("Graduate Software Engineer", ""),
        ("New Grad Software Engineer", ""),
        ("Entry Level Data Analyst", ""),
        ("Entry-Level Data Analyst", ""),
        ("Fresher Software Engineer", ""),
        ("Software Engineer — 0-1 years", ""),
        ("Software Engineer — 0–1 years", ""),
        ("Software Engineer (0 to 1 year)", ""),
        ("Software Engineer — 0-2 years", ""),
        ("Software Engineer - 0-2 Yrs", ""),
        ("Trainee Engineer", ""),
        ("Graduate Engineer Trainee", ""),
        ("Campus Hire - Software Developer", ""),
        ("Early Career Program - Analyst", ""),
        ("Recent Graduate - Business Analyst", ""),
        ("University Graduate Software Engineer 2026", ""),
        ("Management Trainee", ""),
        # evidence in the description rather than the title
        ("Software Engineer", "We are hiring freshers from the 2025 batch."),
        ("Associate Software Engineer", "Experience: 0-1 years. B.Tech in CS."),
        ("Associate Software Engineer", "No prior experience required."),
        ("Associate Analyst", "This is an entry-level role for recent graduates."),
        ("Data Analyst", "Freshers or candidates with 1-2 years of experience can apply."),
        # company boilerplate isn't a requirement
        ("Graduate Analyst", "Sanofi has over 150 years of experience in healthcare."),
        # a 6-month internship duration isn't an experience requirement
        ("Graduate Trainee", "This is a 6 month training programme followed by a 2 year contract."),
        # optional experience doesn't make the role experienced
        ("Graduate Software Engineer", "Internship experience of 6 months is a plus."),
    ],
)
def test_accepts_fresher_and_entry_level(title, description):
    result = classify_job(title, description)
    assert result.category in ACCEPTED, result
    assert result.accepted
    assert str(result).startswith("ACCEPTED")


# ---------------------------------------------------------------------------
# Postings that must NOT be alerted on
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "title, description, expected",
    [
        ("Associate Software Engineer — 2-4 years", "", Category.EXPERIENCED),
        ("Software Engineer — 2+ years", "", Category.EXPERIENCED),
        ("Software Engineer — 3 years experience", "", Category.EXPERIENCED),
        ("Software Engineer", "3 years experience required", Category.EXPERIENCED),
        ("Senior Software Engineer", "", Category.SENIOR),
        ("Sr. Software Engineer", "", Category.SENIOR),
        ("Sr Software Engineer", "", Category.SENIOR),
        ("Staff Software Engineer", "", Category.SENIOR),
        ("Principal Engineer", "", Category.SENIOR),
        ("Lead Engineer", "", Category.SENIOR),
        ("Engineering Manager", "", Category.SENIOR),
        ("Associate Director, Clinical Operations", "", Category.SENIOR),
        ("Director of Engineering", "", Category.SENIOR),
        ("Head of Data", "", Category.SENIOR),
        ("VP, Engineering", "", Category.SENIOR),
        ("Vice President - Risk", "", Category.SENIOR),
        ("Chief of Staff", "", Category.SENIOR),
        ("Solution Architect", "", Category.SENIOR),
        ("Software Engineer II", "", Category.EXPERIENCED),
        ("Associate Project Specialist – Medical Communications", "", Category.UNKNOWN),
        ("Associate Container Platform Engineer", "", Category.UNKNOWN),
        ("Junior Associate - Evidence Synthesis", "", Category.UNKNOWN),
        ("Summer Internship - Marketing", "", Category.UNKNOWN),
        ("Software Engineer", "", Category.UNKNOWN),
        # positive signals never override an explicit requirement
        ("Graduate Software Engineer", "Minimum 2 years of experience in Java.", Category.EXPERIENCED),
        ("Entry Level Analyst", "Requires 1-3 years of relevant experience.", Category.EXPERIENCED),
        ("Trainee Engineer", "Freshers need not apply.", Category.EXPERIENCED),
        # an entry-level-sounding ambiguous title with experience in the description
        (
            "Associate Project Specialist – Medical Communications",
            "About the job\nBachelor's degree in life sciences.\n"
            "At least 3 years of experience in medical communications.",
            Category.EXPERIENCED,
        ),
        # spotlight / blog content
        ("Meet Nils Libert, Associate Scientist in R&D", "", Category.NOT_A_JOB),
        ("Graduate Engineer", "", Category.NOT_A_JOB),  # url checked below
    ],
)
def test_rejects_non_fresher(title, description, expected):
    url = "https://example.com/blog/graduate-story" if title == "Graduate Engineer" else ""
    result = classify_job(title, description, url=url)
    assert result.category == expected, result
    assert not result.accepted
    assert str(result).startswith("REJECTED")


def test_reason_names_the_requirement():
    result = classify_job("Associate Software Engineer — 2-4 years")
    assert "2-4 years" in result.reason
    assert "explicit requirement" in result.reason


def test_reason_names_the_entry_signal():
    result = classify_job("New Grad Software Engineer")
    assert "New Grad" in result.reason


def test_associate_is_not_evidence():
    result = classify_job("Associate Project Specialist – Medical Communications")
    assert result.category == Category.UNKNOWN
    assert "Associate" in result.reason


# ---------------------------------------------------------------------------
# Experience parsing variations
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text, min_years",
    [
        ("1+ years", 1),
        ("2+ years", 2),
        ("3+ years", 3),
        ("2 years+", 2),
        ("1-3 years", 1),
        ("2-4 years", 2),
        ("3-5 years", 3),
        ("3 - 5 years", 3),
        ("1–3 yrs", 1),         # en dash
        ("2—4 yrs", 2),         # em dash
        ("2 to 4 years", 2),
        ("minimum 2 years", 2),
        ("Minimum of 2 years", 2),
        ("Min. 2 yrs", 2),
        ("at least 2 years", 2),
        ("At Least 2 Years", 2),
        ("atleast 2 years", 2),
        ("2 years of experience", 2),
        ("3 years experience", 3),
        ("3 years' experience", 3),
        ("3 years’ experience", 3),
        ("2 yrs experience", 2),
        ("2 yr experience", 2),
        ("1 year experience", 1),
        ("2 yrs exp", 2),
        ("Experience required: 2 years", 2),
        ("EXPERIENCE REQUIRED: 2 YEARS", 2),
        ("Experience: 2-4 yrs", 2),
        ("Experience - 3 years", 3),
        ("experience of 2 years", 2),
        ("2 years relevant experience", 2),
        ("2 years of relevant work experience", 2),
        ("two years of experience", 2),
        ("1.5 years of experience", 1.5),
        ("18 months of experience", 1.5),
        ("0-1 years", 0),
        ("0–1 years", 0),
        ("0 to 1 years", 0),
        ("0-2 years", 0),
    ],
)
def test_parse_experience_variations(text, min_years):
    reqs = parse_experience_requirements(text)
    assert reqs, f"no requirement parsed from {text!r}"
    assert min(r.min_years for r in reqs) == pytest.approx(min_years)


@pytest.mark.parametrize(
    "text",
    [
        "Graduate Software Engineer",
        "6 month internship",
        "3-6 months internship programme",
        "This is a 2 year fixed-term contract.",
        "Founded 25 years ago",
        "With over 150 years of experience in healthcare",
        "3+ years of Python experience is a plus.",
        "2 years experience preferred",
        "Class of 2025",
    ],
)
def test_parse_ignores_non_requirements(text):
    assert parse_experience_requirements(text) == []


@pytest.mark.parametrize(
    "title",
    [
        "software engineer — 2+ YEARS",
        "SOFTWARE ENGINEER (2-4 Yrs)",
        "Software Engineer | 2 Yr Experience",
        "Software Engineer, experience: 3 years.",
        "Software Engineer [3–5 years]",
    ],
)
def test_capitalisation_and_punctuation_variants_rejected(title):
    assert classify_job(title).category == Category.EXPERIENCED


@pytest.mark.parametrize(
    "title",
    ["GRADUATE SOFTWARE ENGINEER", "new grad software engineer", "Entry-level Data Analyst",
     "FRESHER - Software Engineer", "software engineer (0–1 yr)"],
)
def test_capitalisation_variants_accepted(title):
    assert classify_job(title).accepted


def test_multiline_description_bullets():
    description = (
        "Responsibilities\n"
        "• Build dashboards\n"
        "• Work with senior stakeholders\n"
        "Qualifications\n"
        "• 2-4 years in data analytics\n"
    )
    result = classify_job("Data Analyst", description)
    assert result.category == Category.EXPERIENCED


def test_seniority_words_in_description_do_not_reject():
    # "senior"/"manager" in the body describe colleagues, not the role
    result = classify_job(
        "Graduate Data Analyst",
        "You will report to the Senior Manager and support the team lead.",
    )
    assert result.accepted


def test_seniority_word_boundaries():
    # substrings of senior words must not trigger a rejection
    assert classify_job("Leadership Development Programme - Graduate").accepted
    assert classify_job("Management Trainee").accepted


# ---------------------------------------------------------------------------
# Regressions found in the final audit
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "title, description",
    [
        # bare years in a title are always experience
        ("Graduate Software Engineer — 2 years", ""),
        ("Associate Project Specialist — 2 years", ""),
        ("Junior Software Engineer — 2 years", ""),
        ("Graduate Software Engineer (2 Yrs)", ""),
        # "freshers" mentioned about the team, not the applicant
        ("Software Engineer", "You will mentor freshers and must have 5+ years experience"),
        # "preferred" in a neighbouring clause doesn't make the requirement optional
        ("Graduate Software Engineer", "2+ years of experience required, Master's preferred."),
        # unit-less and hyphenated forms
        ("Graduate Analyst", "Years of Experience: 2-4"),
        ("Graduate Analyst", "Experience: 3+"),
        ("Graduate Analyst", "Total Experience (in years): 2"),
        ("Graduate Analyst", "3-year experience in Java"),
        ("Graduate Analyst", "2 years C++ experience"),
        ("Graduate Analyst", "3 years .NET experience"),
        ("Trainee", "1 year of internship experience is required"),
        # numbered levels
        ("Software Engineer 2", ""),
        ("SDE-2", ""),
        ("SDE 3", ""),
    ],
)
def test_audit_experienced_rejected(title, description):
    result = classify_job(title, description)
    assert result.category in {Category.EXPERIENCED, Category.SENIOR}, result


@pytest.mark.parametrize(
    "title, description",
    [
        ("Software Engineer", "You will mentor freshers and new team members."),
        ("Software Engineer", "You will guide trainees on the team."),
        ("Software Engineer", "Support entry-level staff with onboarding."),
        ("Post Graduate Teacher (PGT) - Maths", ""),
        ("Campus Recruiter", ""),
        ("Early Careers Talent Acquisition Partner", ""),
        ("Early Careers", ""),
        ("Graduate Programs", ""),
        ("Explore Early Careers", ""),
        ("Students & Graduates", ""),
        ("Campus Hiring India", ""),
        ("Software Engineer I", ""),
        ("Sales Executive", ""),
    ],
)
def test_audit_no_false_fresher_signal(title, description):
    assert not classify_job(title, description).accepted


@pytest.mark.parametrize(
    "title, description",
    [
        ("Apprentice Software Engineer", ""),
        ("New Graduate Software Engineer", ""),
        ("Software Engineer — no experience required", ""),
        ("Software Engineer I", "Experience: 0-2 years"),
        ("Associate", "Experience: Fresher"),
        ("Associate Consultant", "Experience: 0 - 2 Years"),
        ("Software Engineer", "Years of Experience: 0-1"),
        ("Executive - Operations", "Freshers welcome"),
        ("Software Engineering Intern", "Open to 2026 graduates"),
        ("Graduate Programme 2026 – Technology", ""),
        ("Lead Generation Executive", "Freshers can apply"),
        ("Graduate Software Engineer",
         "Minimum qualifications:\nBachelor's degree.\nPreferred qualifications:\n2 years of experience with Java."),
        # bonds / service agreements / company boilerplate aren't requirements
        ("Software Engineer", "Candidates must sign a minimum 2 years service agreement. Open to freshers."),
        ("Graduate Trainee", "Stipend for 6 months, followed by a 2-year bond."),
        ("Graduate Analyst", "We have 10+ years of experience serving clients."),
        ("Graduate Analyst", "Sanofi, with over 12 years of experience in India"),
        # age limits, notice periods, salary ranges
        ("Graduate Trainee", "Age: 21-25 years. Eligibility: 2024/2025 pass-outs with 60% throughout."),
        ("Graduate Trainee", "Notice period: 0-30 days. CTC 3-5 LPA."),
    ],
)
def test_audit_eligible_accepted(title, description):
    result = classify_job(title, description)
    assert result.accepted, result


@pytest.mark.parametrize(
    "text, min_years",
    [
        ("2+ yrs", 2), ("2 year's experience", 2), ("2 years' experience", 2),
        ("2–4 yrs", 2), ("2 - 4 years", 2), ("2 to 4 years", 2), ("minimum of 2 years", 2),
        ("at least two years", 2), ("two years of experience", 2), ("two to four years", 2),
        ("0–1 year", 0), ("0 to 1 year", 0), ("3-year experience", 3),
        ("Years of Experience: 2-4", 2), ("Experience: 3+", 3),
    ],
)
def test_audit_parse_formats(text, min_years):
    reqs = parse_experience_requirements(text)
    assert reqs and min(r.min_years for r in reqs) == pytest.approx(min_years)


@pytest.mark.parametrize(
    "text",
    ["2-year contract", "2026 graduate", "6-month internship", "2025 batch",
     "15 years of company history", "a 2 year contract with experience in SAP",
     "minimum 2 years bond", "2-year graduate programme", "Age: 21-25 years",
     "Notice period: 0-30 days"],
)
def test_audit_parse_ignores_unrelated_numbers(text):
    assert parse_experience_requirements(text) == []


@pytest.mark.parametrize(
    "title, expected",
    [("2 yrs", 2), ("2 yr", 2), ("2 years", 2), ("Engineer - 1 year", 1)],
)
def test_bare_years_count_in_titles(title, expected):
    reqs = parse_experience_requirements(title, title=True)
    assert [r.min_years for r in reqs] == [expected]
    # ...but not in descriptions, where they're often durations
    assert parse_experience_requirements(title) == []
