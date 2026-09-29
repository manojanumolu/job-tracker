"""Adversarial regression suite from the Sep 2026 pipeline audit.

Every case here was either accepted when it must not be (FP), rejected when
it is a genuine fresher role (FN), or is a boundary worth pinning. Categories
are pinned exactly so a regression can't hide behind "still not accepted".
"""
import pytest

from job_classifier import Category, classify_job, parse_experience_requirements

C = Category
ACC_HDR = "Project Role : Custom Software Engineer\nMust have skills : Java\nGood to have skills : NA\n"

CASES = [
    # ---- experienced roles that must never be emailed
    ("FP01", "Associate Software Engineer", "2+ years required", C.EXPERIENCED),
    ("FP02", "Graduate Software Engineer", "Minimum 3 years professional experience", C.EXPERIENCED),
    ("FP03", "Software Engineer", "Mentor fresh graduates. 5 years experience.", C.EXPERIENCED),
    ("FP04", "Analyst", "Freshers welcome. 4 years required.", C.EXPERIENCED),
    ("FP05", "Engineer I", "2 years relevant experience", C.EXPERIENCED),
    ("FP06", "Associate Consultant", "3 years experience", C.EXPERIENCED),
    ("FP07", "Junior Developer", "2 years required", C.EXPERIENCED),
    # the live Accenture layout: "Good to have skills : NA" hid the next line
    ("FP08", "Custom Software Engineer", ACC_HDR + "Minimum 3 year(s) of experience is required\n"
     "Educational Qualification : 15 years full time education", C.EXPERIENCED),
    ("FP09", "Graduate Engineer", "Nice to have: Kubernetes\nMinimum 2 years of experience in Java", C.EXPERIENCED),
    ("FP10", "Graduate Engineer", "Preferred skills: Python\n3+ years of experience in backend", C.EXPERIENCED),
    ("FP11", "Graduate Engineer", "Bonus points\nMust have 3 years of experience", C.EXPERIENCED),
    ("FP12", "Trainee Analyst", "Good to have: SQL\nExperience: 2-4 years", C.EXPERIENCED),
    ("FP13", "Associate", "Freshers or experienced candidates. Minimum 2 years experience.", C.EXPERIENCED),
    ("FP14", "Graduate Analyst", "Freshers can apply / 2+ years of experience in audit", C.EXPERIENCED),
    ("FP15", "Associate Engineer", "Open to freshers\nRequired: two or more years of experience", C.EXPERIENCED),
    ("FP16", "Graduate Trainee", "Experience: 2", C.EXPERIENCED),
    ("FP17", "Graduate Trainee", "Min Experience: 2", C.EXPERIENCED),
    ("FP18", "Graduate Trainee", "Experience in years: 3", C.EXPERIENCED),
    ("FP19", "Graduate Trainee", "Prior experience required in sales", C.EXPERIENCED),
    ("FP20", "Graduate Trainee", "Relevant industry experience required", C.EXPERIENCED),
    ("FP21", "Graduate Trainee", "Must have prior professional experience in audit", C.EXPERIENCED),
    ("FP22", "Graduate Trainee", "Years of experience required: 2", C.EXPERIENCED),
    ("FP23", "Graduate Trainee", "2Y+ exp", C.EXPERIENCED),
    ("FP24", "Graduate Trainee", "Experience: 2 Years 6 Months", C.EXPERIENCED),
    ("FP25", "Graduate Trainee", "Work experience of 24 months", C.EXPERIENCED),
    ("FP26", "Graduate Trainee", "A minimum of 2 years in an audit role is required", C.EXPERIENCED),
    ("FP27", "Graduate Trainee", "You have 3+ yrs. in data engineering", C.EXPERIENCED),
    ("FP28", "Graduate Trainee", "Experience\n2-4 years", C.EXPERIENCED),
    ("FP29", "Graduate Trainee", "Experience:\n3+ years", C.EXPERIENCED),
    ("FP30", "Graduate Trainee", "We welcome graduates with 2 years of experience", C.EXPERIENCED),
    ("FP31", "Entry Level Analyst", "Candidates should possess 3 yrs of hands-on exposure", C.EXPERIENCED),
    ("FP32", "Graduate Analyst", "Experience: Two to Four years", C.EXPERIENCED),
    ("FP33", "Graduate Analyst", "Experience Level: Mid-Senior level", C.EXPERIENCED),
    ("FP34", "Graduate Analyst", "Seniority level: Mid-Senior", C.EXPERIENCED),
    ("FP36", "Associate Manager", "", C.SENIOR),
    ("FP37", "Software Engineer III", "", C.EXPERIENCED),
    ("FP38", "SDE II", "", C.EXPERIENCED),
    ("FP39", "Software Engineer (Mid-level)", "freshers welcome", C.EXPERIENCED),
    ("FP40", "Graduate Engineer", "Experience: 2+", C.EXPERIENCED),
    ("FP41", "Graduate Engineer", "3 plus years experience", C.EXPERIENCED),
    ("FP42", "Graduate Engineer", "Experience in Java - 3 years", C.EXPERIENCED),
    ("FP43", "Graduate Engineer", "Java: 3 years", C.EXPERIENCED),
    ("FP44", "Graduate Engineer", "Total exp 4 yrs, relevant exp 2 yrs", C.EXPERIENCED),
    ("FP56", "Software Engineer", "Our team includes junior engineers and freshers. 6+ years required", C.EXPERIENCED),
    ("FP66", "Intern", "1 year internship experience required", C.EXPERIENCED),
    ("FP67", "Graduate Engineer", "Experience: 2 - 3 Yrs\nFreshers can also apply", C.EXPERIENCED),
    ("FP68", "Graduate Engineer", "Experienced: 2 years", C.EXPERIENCED),
    ("FP69", "Graduate Analyst", "Minimum 2 yrs' exp.", C.EXPERIENCED),
    ("FP70", "Graduate Analyst", "Experience Required - 2 Years", C.EXPERIENCED),
    # ---- pages / roles that are not entry-level job postings
    ("FP45", "Recent Graduate Program Coordinator", "", C.NOT_A_JOB),
    ("FP46", "Graduate Recruitment Coordinator", "", C.NOT_A_JOB),
    ("FP47", "University Relations Specialist", "hiring university graduates", C.NOT_A_JOB),
    ("FP48", "Inside Sanofi's Graduate Program", "Hyderabad, India", C.NOT_A_JOB),
    ("FP49", "Early Careers at Accenture", "", C.NOT_A_JOB),
    ("FP50", "Graduate Program", "", C.NOT_A_JOB),
    ("FP51", "India (English)", "Graduate programmes for freshers", C.NOT_A_JOB),
    ("FP52", "Join our Talent Network", "freshers welcome", C.NOT_A_JOB),
    ("FP53", "Talent Community - Early Careers", "", C.NOT_A_JOB),
    ("FP54", "Campus Ambassador Program Manager", "", C.NOT_A_JOB),
    ("FP55", "Graduate Careers Fair 2026", "", C.NOT_A_JOB),
    # ---- staff / hiring context: the phrase is about other people
    ("FP57", "Software Engineer", "Work with recent graduates", C.UNKNOWN),
    ("FP58", "Software Engineer", "You will train freshers", C.UNKNOWN),
    ("FP59", "Software Engineer", "You will be responsible for hiring freshers", C.UNKNOWN),
    ("FP60", "Software Engineer", "Build our campus hiring pipeline", C.UNKNOWN),
    ("FP61", "Software Engineer", "Drive entry-level hiring programs", C.UNKNOWN),
    ("FP62", "Software Engineer", "Interview new graduates for the team", C.UNKNOWN),
    ("FP63", "Consultant", "Recent graduates will report to you", C.UNKNOWN),
    ("FP64", "Software Engineer", "The team has many fresh graduates", C.UNKNOWN),
    ("FP65", "Intern", "Internship experience required", C.UNKNOWN),
    # ---- genuine fresher / entry-level roles that must still be accepted
    ("FN01", "Software Engineer", "No prior experience required", C.FRESHER),
    ("FN02", "Associate Software Engineer", "Minimum 0 year(s) of experience is required\n"
     "Educational Qualification : Minimum 15 years of full-time education", C.FRESHER),
    ("FN03", "Associate Software Engineer", ACC_HDR + "Minimum 0 year(s) of experience is required\n"
     "Educational Qualification : 15 years full time education", C.FRESHER),
    ("FN04", "Graduate Engineer Trainee", "Minimum 15 years of full time education required", C.ENTRY_LEVEL),
    ("FN05", "Graduate Trainee", "Bachelor's degree (4 years)", C.ENTRY_LEVEL),
    ("FN06", "Graduate Trainee", "Minimum 60% marks. 2 years of study abroad is a plus", C.ENTRY_LEVEL),
    ("FN07", "Graduate Trainee", "Eligibility: B.E/B.Tech with minimum 3 years of degree", C.ENTRY_LEVEL),
    ("FN08", "Graduate Trainee", "You will receive 2 years of on-the-job mentoring", C.ENTRY_LEVEL),
    ("FN09", "Graduate Trainee", "Minimum 2 years commitment to the role", C.ENTRY_LEVEL),
    ("FN10", "Graduate Trainee", "2 years of experience preferred but freshers are welcome", C.FRESHER),
    ("FN11", "Trainee", "Experience: 0 years", C.FRESHER),
    ("FN12", "Trainee", "Experience: 0 - 1 Years", C.FRESHER),
    ("FN13", "Associate", "Experience: Freshers", C.FRESHER),
    ("FN14", "Associate", "Exp: 0 yrs", C.FRESHER),
    ("FN15", "Software Engineer", "Freshers are eligible to apply", C.FRESHER),
    ("FN16", "Software Engineer", "This role is open to freshers", C.FRESHER),
    ("FN17", "Software Engineer", "Entry level", C.ENTRY_LEVEL),
    ("FN18", "Software Engineer - Fresher", "Must have excellent communication", C.FRESHER),
    ("FN19", "Software Engineer", "Batch: 2025/2026 graduates", C.ENTRY_LEVEL),
    ("FN20", "Graduate Trainee", "Age limit: 18 to 25 years", C.ENTRY_LEVEL),
    ("FN21", "Graduate Trainee", "Stipend 25k for first 6 months, 2 year service bond", C.ENTRY_LEVEL),
    ("FN22", "Graduate Software Engineer", "Our company has 25 years of experience", C.ENTRY_LEVEL),
    ("FN23", "Graduate Software Engineer", "Leading provider with 30+ years of experience", C.ENTRY_LEVEL),
    ("FN24", "Graduate Software Engineer", "We have been in business for 10 years", C.ENTRY_LEVEL),
    ("FN25", "Graduate Software Engineer", "Salary: 3-5 LPA", C.ENTRY_LEVEL),
    ("FN26", "Graduate Software Engineer", "Our clients include senior leaders", C.ENTRY_LEVEL),
    ("FN27", "Graduate Software Engineer", "Qualification: B.Tech 2024 (4 years)", C.ENTRY_LEVEL),
    ("FN28", "Graduate Software Engineer", "Required: 0-1 year(s) of experience", C.FRESHER),
    ("FN29", "Engineering Graduate Trainee", "Minimum 15 years of full time education is required.", C.ENTRY_LEVEL),
    ("FN30", "Graduate Software Engineer", "Leading team of 5 years of consistent growth", C.ENTRY_LEVEL),
    ("FN31", "Management Trainee", "A 2-year leadership rotation", C.ENTRY_LEVEL),
    # colleagues' experience is not the applicant's requirement; with no
    # entry evidence in the title "Associate" stays UNKNOWN (not EXPERIENCED)
    ("FN32", "Associate Software Engineer", "Mentored by senior engineers with 10+ years of experience", C.UNKNOWN),
    ("FN33", "Graduate Software Engineer", "Work alongside engineers who have 8+ years of experience", C.ENTRY_LEVEL),
    ("FN34", "Graduate Software Engineer", "Our leaders bring 20 years of experience", C.ENTRY_LEVEL),
    ("FN35", "Graduate Software Engineer", "Pursuing or completed a 3-year diploma", C.ENTRY_LEVEL),
]


@pytest.mark.parametrize("cid, title, description, expected", CASES, ids=[c[0] for c in CASES])
def test_adversarial_case(cid, title, description, expected):
    result = classify_job(title, description)
    assert result.category == expected, f"{cid}: {result}"
    assert result.accepted == (expected in {C.FRESHER, C.ENTRY_LEVEL})


def test_management_level_title_is_never_accepted():
    # FP35: rejected either as a level field or as a "Label: value" non-title
    result = classify_job("Management Level: 10 – Senior Analyst")
    assert result.category in {C.SENIOR, C.NOT_A_JOB}


def test_case_count():
    # 104 pinned cases + FP35 above = the 105 audit cases
    assert len(CASES) == 104


# ---------------------------------------------------------------------------
# Explicit experience always wins over positive wording
# ---------------------------------------------------------------------------

POSITIVE = ["Freshers welcome.", "Freshers can apply.", "Graduates welcome.", "Open to graduates.",
            "Recent graduates are encouraged to apply.", "This is an entry-level role."]
REQUIREMENTS = ["2+ years of experience.", "3+ years of experience.", "Minimum 2 years of experience.",
                "Minimum 3 years of experience.", "1-3 years of experience.",
                "Prior experience required.", "Relevant experience required.",
                "Professional experience required.", "Experience level: Mid-level.",
                "Seniority level: Mid-Senior level."]


@pytest.mark.parametrize("positive", POSITIVE)
@pytest.mark.parametrize("requirement", REQUIREMENTS)
def test_explicit_experience_wins(positive, requirement):
    for text in (f"{positive}\n{requirement}", f"{requirement}\n{positive}", f"{positive} {requirement}"):
        result = classify_job("Graduate Analyst", text)
        assert result.category == C.EXPERIENCED, (text, result)


@pytest.mark.parametrize("text", [
    "Proven experience as an SAP SD Consultant.",
    "A proven track record in B2B sales.",
    "At least [X] years of hands-on experience in SAP SD.",
    "[X]+ years of relevant experience.",
    "Minimum of [N] years in audit.",
])
def test_number_less_requirements(text):
    # found live on PwC postings that also said "0-1 Yrs": conflicting
    # evidence is never emailed
    assert classify_job("Graduate Analyst", f"{text}\nExperience: 0-1 Yrs").category == C.EXPERIENCED


@pytest.mark.parametrize("text", ["Proven ability to learn quickly.", "No proven experience needed.",
                                  "Proven experience is a plus.", "Proven academic track record"])
def test_number_less_requirement_false_alarms(text):
    assert classify_job("Graduate Analyst", text).accepted


@pytest.mark.parametrize("title", ["Software Engineer (Mid-level)", "Mid-Senior Analyst", "Experienced Java Developer"])
def test_mid_level_titles_rejected(title):
    assert classify_job(title, "Freshers welcome").category in {C.EXPERIENCED, C.SENIOR}


# ---------------------------------------------------------------------------
# Preferred sections never hide a mandatory requirement
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("title, description", [
    ("Graduate Software Engineer", "Good to have skills: NA\nMinimum 3 years of experience required"),
    ("Graduate Analyst", "Nice to have: Kubernetes\nMinimum 2 years required"),
    ("Trainee", "Preferred skills: Python\n3+ years experience"),
    ("Graduate Engineer", "Bonus points\nMust have 3 years experience"),
    ("Graduate Engineer", "Good to have: SQL\nExperience: 2-4 years"),
    ("Graduate Engineer", "Preferred qualifications:\nKubernetes\nAt least 2 years of experience in Go"),
    ("Graduate Engineer", "Nice to have\nDocker\n2 years of experience is required"),
])
def test_preferred_section_never_hides_mandatory(title, description):
    assert classify_job(title, description).category == C.EXPERIENCED


def test_real_preferred_section_still_optional():
    # a genuine "Preferred qualifications" block without mandatory wording stays optional
    result = classify_job("Graduate Software Engineer",
                          "Minimum qualifications:\nBachelor's degree.\nPreferred qualifications:\n2 years of experience with Java.")
    assert result.accepted


# ---------------------------------------------------------------------------
# Education durations and colleague experience
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Minimum 15 years of full-time education", "Minimum 15 years of full time education required",
    "A 15 years full time education is required.", "Educational Qualification : 15 years full time education",
    "Minimum 16 years of schooling", "3 years of academic study",
])
def test_education_years_are_not_experience(text):
    assert [r for r in parse_experience_requirements(text) if r.min_years > 0] == []


def test_accenture_fresher_with_education_line_is_fresher():
    result = classify_job("Associate Software Engineer",
                          "Minimum 0 year(s) experience\nMinimum 15 years of full-time education")
    assert result.category == C.FRESHER


@pytest.mark.parametrize("text", [
    "Mentored by senior engineers with 10+ years of experience",
    "Work alongside engineers who have 8+ years of experience",
    "Team members have 10 years experience",
    "Learn from colleagues who bring 15 years of experience",
    "Our engineers have 12+ years of experience",
])
def test_colleague_experience_is_not_a_requirement(text):
    assert parse_experience_requirements(text) == []
    assert classify_job("Graduate Software Engineer", text).accepted


@pytest.mark.parametrize("text", [
    "We are hiring engineers with 3+ years of experience",
    "Candidates with 3+ years of experience",
    "Professionals with 5+ years of experience in audit",
])
def test_applicant_experience_with_plural_nouns_still_counts(text):
    assert classify_job("Graduate Analyst", text).category == C.EXPERIENCED


# ---------------------------------------------------------------------------
# Programme / non-job pages
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("title", [
    "Graduate Program", "Graduate Programme", "Graduate Programme 2026 – Technology",
    "Inside our Graduate Program", "Early Careers at Accenture", "Graduate Careers Fair",
    "Join our Talent Network", "Talent Community", "India (English)", "Career Stories",
    "Life at Sanofi", "University Relations", "Graduate Program Coordinator", "Campus Recruiter",
    "Campus Ambassador", "Career event", "Recruitment event", "Hyderabad, India", "India",
    "DO YOU OFFER ENTRY-LEVEL POSITIONS?", "Early Talent", "Students & Graduates",
    "How Motherhood Accelerated My Career at Sanofi", "Leadership Development Programme - Graduate",
])
def test_non_job_pages(title):
    result = classify_job(title, "Freshers welcome. Open to graduates. Entry-level.")
    assert result.category == C.NOT_A_JOB, result


@pytest.mark.parametrize("title", ["Graduate Programme 2026 – Technology", "Leadership Development Programme - Graduate"])
def test_programme_posting_with_real_jobposting_is_judged_as_job(title):
    # an actual ATS/JSON-LD JobPosting for a programme is a job; the landing page isn't
    assert classify_job(title, "", posting_evidence=True).accepted
    assert classify_job(title).category == C.NOT_A_JOB


@pytest.mark.parametrize("title", [
    "Inside Sales Representative", "Benefits Analyst", "Privacy Analyst", "Accessibility Tester",
    "Talent Acquisition Trainee", "Early Career Program - Analyst", "Head Office Administrator",
])
def test_job_titles_that_resemble_non_job_words(title):
    assert classify_job(title).category != C.NOT_A_JOB


def test_head_office_is_not_seniority():
    assert classify_job("Head Office Administrator").category != C.SENIOR


# ---------------------------------------------------------------------------
# Title normalisation (PwC "IN_Senior Associate_GenAI")
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("title, expected", [
    ("IN_Senior Associate_GenAI and Agentic AI", C.SENIOR),
    ("IN-Manager_ SAP HCM_SAP_Advisory_ Mumbai", C.SENIOR),
    ("IN_Associate_Tax_Graduate", C.ENTRY_LEVEL),
])
def test_underscore_titles(title, expected):
    assert classify_job(title).category == expected


# ---------------------------------------------------------------------------
# Strict (reject-only) evidence
# ---------------------------------------------------------------------------

def test_strict_text_can_only_reject():
    # a requirement in page fields outside the description rejects...
    assert classify_job("Graduate Developer", "Build APIs.",
                        strict_text="Experience: 3-5 years").category == C.EXPERIENCED
    assert classify_job("Custom Software Engineer", "Build software.",
                        strict_text="Career level: Custom Software Engineering Senior Analyst").category == C.EXPERIENCED
    # ...but a fresher phrase there (e.g. a similar-jobs widget) is not evidence
    assert classify_job("Custom Software Engineer", "Build software.",
                        strict_text="Similar job: Experience: 0-2 years. Freshers welcome").category == C.UNKNOWN


# ---------------------------------------------------------------------------
# Internships: behaviour documented, not broadened
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("title, description, expected", [
    ("Intern", "", C.UNKNOWN),
    ("Internship", "", C.UNKNOWN),
    ("Software Intern", "", C.UNKNOWN),
    ("Engineering Intern", "", C.UNKNOWN),
    ("Summer Intern", "", C.UNKNOWN),
    ("6-month internship", "", C.UNKNOWN),
    ("12-month internship", "", C.UNKNOWN),
    ("Intern", "Internship experience required", C.UNKNOWN),
    ("Intern", "1 year internship experience required", C.EXPERIENCED),
    # pre-existing definition: an internship explicitly aimed at graduates
    # (a "Graduate" title or "open to 2026 graduates") is an entry-level role
    ("Graduate Intern", "", C.ENTRY_LEVEL),
    ("Software Engineering Intern", "Open to 2026 graduates", C.ENTRY_LEVEL),
])
def test_internship_definition(title, description, expected):
    assert classify_job(title, description).category == expected


# ---------------------------------------------------------------------------
# Final review (Sep 30): no "freshers or 1-2 years" exception, broad ranges
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "Freshers or candidates with 1-2 years of experience can apply.",
    "Freshers or 1-2 years experience",
    "Freshers / 1-2 years",
    "Freshers welcome, candidates with 1-2 years may apply",
    "1-2 years or freshers",
    "Fresher / 1 to 2 years",
    "Freshers & 1-3 yrs",
])
@pytest.mark.parametrize("title", ["Data Analyst", "Graduate Analyst", "Trainee", "Associate"])
def test_freshers_never_override_experience(title, text):
    result = classify_job(title, text)
    assert not result.accepted and result.category == C.EXPERIENCED, result


@pytest.mark.parametrize("text", ["Freshers can apply / 2+ years", "Freshers welcome. 2+ years required",
                                  "Freshers or minimum 2 years"])
def test_freshers_with_two_plus_years_rejected(text):
    assert classify_job("Graduate Analyst", text).category == C.EXPERIENCED


@pytest.mark.parametrize("title, text", [
    ("Graduate Analyst", "Experience: 0-3 years"),
    ("Associate", "0-3 years of relevant experience"),
    ("Trainee", "Experience required: 0 to 3 yrs"),
    ("Data Analyst", "Freshers or candidates with 0-2 years of experience can apply."),
    ("Graduate Trainee", "Experience: 0-4 years"),
    ("Graduate Trainee", "Up to 2 years of experience"),
    ("Graduate Trainee", "Maximum 2 years of experience"),
])
def test_zero_start_ranges_below_five_years_stay_eligible(title, text):
    assert classify_job(title, text).category == C.FRESHER


def test_zero_to_three_with_an_explicit_contradiction_is_rejected():
    assert classify_job("Graduate Analyst", "Experience: 0-3 years\nMinimum 2 years in audit required").category == C.EXPERIENCED


BROAD = ["Experience: 0-5 years", "Experience: 0-7 years", "0 - 10 yrs", "Up to 5 years", "Up to 7 years",
         "Up to 10 years of experience", "Experience:\nUp to 7 years", "Experience up to 5 years",
         "Maximum 8 years of experience", "upto 6 yrs exp"]


@pytest.mark.parametrize("text", BROAD)
@pytest.mark.parametrize("title", ["Trainee", "Graduate Trainee", "Graduate Engineer", "Intern", "Intern/ Trainee",
                                   "Associate", "Campus Hire - Analyst", "Apprentice Developer"])
def test_broad_range_title_word_is_not_enough(title, text):
    result = classify_job(title, text)
    assert not result.accepted, result
    assert result.category in {C.UNKNOWN, C.EXPERIENCED}


@pytest.mark.parametrize("evidence, expected", [
    ("Experience: 0-1 years", C.FRESHER),
    ("Fresh graduates are welcome to apply", C.FRESHER),
    ("No prior experience required", C.FRESHER),
    ("Freshers are eligible", C.FRESHER),
    ("This is an entry-level role", C.ENTRY_LEVEL),
    ("Open to recent graduates", C.ENTRY_LEVEL),
    ("Eligible: 2026 batch", C.ENTRY_LEVEL),
])
def test_broad_range_with_independent_evidence(evidence, expected):
    for broad in ("Up to 7 years of experience", "Experience: 0-7 years"):
        assert classify_job("Trainee", f"{broad}\n{evidence}").category == expected


@pytest.mark.parametrize("text", ["Contract for up to 7 years", "Up to 5 years of free training",
                                  "Stipend for up to 6 months", "Rotation of up to 5 years in the programme"])
def test_up_to_durations_are_not_experience(text):
    assert classify_job("Graduate Trainee", text).accepted


def test_live_pwc_intern_trainee_up_to_seven_years():
    # live PwC posting (Sep 2026): the only evidence besides the title word
    result = classify_job("Intern/ Trainee", "Intern/Trainee\nITGC Reviews, IT Internal Audits, Controls Testing\n"
                                             "Experience:\nUp to 7 years\nMinimum Qualification: BE/ BTech/CA")
    assert result.category == C.UNKNOWN and "broad experience range" in result.reason
