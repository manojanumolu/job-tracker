import pytest

import scraper
from identity import ats_job_id, canonical_url, job_uid, same_page
from locations import india_segments, is_india, is_location_only

# ---------------------------------------------------------------------------
# India location detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "Hyderabad", "Bengaluru, Karnataka", "Bangalore", "Remote - India", "India", "IN-KA", "IN-TG",
    "Karnataka", "Telangana", "Maharashtra", "Tamil Nadu", "Kerala", "Thane", "Lucknow", "Bhopal",
    "Mangaluru", "Vizag", "Madurai", "Hosur", "Vijayawada", "Bengaluru Millenia", "Kolkata DN 57",
    "Gurugram 10 C", "Hyderabad - Salarpuria", "Telangana, IN", "Pune, IN", "IN", "IND",
    "Junglee Bangalore", "Kolkata (AC) - Bangalore - RMZ Hebbal", "Mumbai, Maharashtra",
])
def test_india_locations(text):
    assert is_india(text)


@pytest.mark.parametrize("text", [
    "Hyderabad, Pakistan", "Delhi, Ontario, Canada", "Kochi, Japan", "Lahore, Punjab", "Salem, MA",
    "Columbus, IN",            # Indiana, not India
    "Indiana, USA", "International", "Remote", "Remote (Global)", "APAC", "Singapore", "London",
    "Dublin - One Spencer Dock", "Lublin - ul. Nałęczowska 14", "",
])
def test_not_india(text):
    assert not is_india(text)


@pytest.mark.parametrize("text, expected", [
    # multi-location: kept when India is explicitly one of the valid locations
    ("India or US", ["India"]),
    ("India / Singapore", ["India"]),
    ("Austin, TX | Pune, India", ["Pune, India"]),
    ("Singapore | Hyderabad", ["Hyderabad"]),
    ("Hyderabad, Pakistan | Pune", ["Pune"]),
    ("United States | Canada", []),
])
def test_multi_location_policy(text, expected):
    assert india_segments(text) == expected
    assert is_india(text) == bool(expected)


@pytest.mark.parametrize("text, only", [
    ("Hyderabad, India", True), ("India", True), ("Bengaluru", True),
    ("Graduate Engineer - Hyderabad", False), ("Careers in India", False), ("Paris", False),
])
def test_location_only_titles(text, only):
    assert is_location_only(text) == only


def test_jsonld_location_variants():
    assert is_india(scraper._jsonld_location({"jobLocation": {"address": {"addressCountry": "IN-KA"}}}))
    assert is_india(scraper._jsonld_location({"jobLocation": {"@type": "Place", "name": "Hyderabad"}}))
    assert is_india(scraper._jsonld_location({"jobLocationType": "TELECOMMUTE",
                                              "applicantLocationRequirements": [{"name": "United States"}, {"name": "IN"}]}))
    assert not is_india(scraper._jsonld_location({"jobLocationType": "TELECOMMUTE"}))
    assert not is_india(scraper._jsonld_location({"jobLocationType": "TELECOMMUTE",
                                                  "applicantLocationRequirements": {"name": "US"}}))
    mixed = scraper._jsonld_location({"jobLocation": [{"address": {"addressLocality": "Austin", "addressCountry": "US"}},
                                                      {"address": {"addressLocality": "Pune", "addressCountry": "IN"}}]})
    assert is_india(mixed)


# ---------------------------------------------------------------------------
# Job identity / deduplication
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("url, expected", [
    ("https://sanofi.wd3.myworkdayjobs.com/SanofiCareers/job/Hyderabad/Associate---HEVA--Evidence-Synthesis-_R2852866",
     "workday:R2852866"),
    ("https://sanofi.wd3.myworkdayjobs.com/SanofiCareers/job/Hyderabad/Associate---Evidence-Synthesis_R2849882-1",
     "workday:R2849882-1"),
    ("https://flutterbe.wd3.myworkdayjobs.com/FlutterInt_External/job/X/Store-Specialist_JR141427", "workday:JR141427"),
    ("https://www.accenture.com/in-en/careers/jobdetails?id=ATCI-5780951-S2070513_en&title=Custom+Software+Engineer",
     "accenture:ATCI-5780951-S2070513_en"),
    ("https://careers.npci.org.in/jobs/Careers/190737000005689329/Associate-Quality-Assurance-NBBL", "zoho:190737000005689329"),
    ("https://www.metlifecareers.com/en_US/ml/JobDetail/GG13-Investment-Finace-Accounting/19919", "avature:19919"),
    ("https://careers.avalara.com/careers-home/jobs/17498?lang=en-us", "jibe:17498"),
    ("https://boards.greenhouse.io/acme/jobs/4012345", "greenhouse:4012345"),
    ("https://example.com/careers/opening", ""),
])
def test_ats_job_ids(url, expected):
    assert ats_job_id(url) == expected


def test_canonical_url_normalisation():
    a = "HTTP://WWW.Example.com/careers/job/42/?utm_source=x&b=2&a=1#apply"
    b = "https://example.com/careers/job/42?a=1&b=2"
    assert canonical_url(a) == canonical_url(b)
    assert same_page(a, b)
    assert not same_page("https://example.com/job?id=1", "https://example.com/job?id=2")


def test_same_job_different_urls_is_one_identity():
    base = "https://www.accenture.com/in-en/careers/jobdetails?id=AIOC-S01667217_en"
    assert job_uid("Accenture", base + "&title=IT+Customer+Service+Associate") == \
        job_uid("Accenture", base + "&title=Other&utm_campaign=mail") == job_uid("accenture", base)


def test_same_title_different_jobs_are_distinct():
    a = job_uid("Accenture", "https://www.accenture.com/in-en/careers/jobdetails?id=ATCI-1_en&title=Custom+Software+Engineer")
    b = job_uid("Accenture", "https://www.accenture.com/in-en/careers/jobdetails?id=ATCI-2_en&title=Custom+Software+Engineer")
    assert a != b


def _job(title, url, location="Hyderabad", company="Accenture"):
    return {"title": title, "url": url, "company": company, "location": location, "category": "FRESHER", "reason": "r"}


def test_merge_new_jobs_keeps_distinct_same_title_jobs():
    found = [_job("Trust & Safety New Associate", "https://x.accenture.com/jobdetails?id=A1_en", "Hyderabad"),
             _job("Trust & Safety New Associate", "https://x.accenture.com/jobdetails?id=A2_en", "Gurugram")]
    new = scraper.merge_new_jobs([], found)
    assert len(new) == 2 and new[0]["uid"] != new[1]["uid"]
    assert all(r["notified"] is False and r["uid"] and r["first_seen"] for r in new)


def test_merge_new_jobs_ignores_tracking_and_repeat_urls():
    seen = [{**_job("Graduate Trainee", "https://sanofi.wd3.myworkdayjobs.com/S/job/H/Graduate-Trainee_R1"), "notified": True}]
    found = [_job("Graduate Trainee", "https://sanofi.wd3.myworkdayjobs.com/S/job/Hyderabad/Graduate-Trainee_R1?source=li"),
             _job("Graduate Trainee (renamed)", "https://sanofi.wd3.myworkdayjobs.com/S/job/H/Graduate-Trainee_R1")]
    assert scraper.merge_new_jobs(seen, found) == []


def test_merge_new_jobs_treats_same_title_and_location_as_repost():
    seen = [{**_job("Graduate Trainee", "https://sanofi.wd3.myworkdayjobs.com/S/job/H/Graduate-Trainee_R1"), "notified": True}]
    repost = _job("Graduate Trainee", "https://sanofi.wd3.myworkdayjobs.com/S/job/H/Graduate-Trainee_R9")
    other_city = _job("Graduate Trainee", "https://sanofi.wd3.myworkdayjobs.com/S/job/P/Graduate-Trainee_R10", "Pune")
    assert [r["location"] for r in scraper.merge_new_jobs(seen, [repost, other_city])] == ["Pune"]


def test_legacy_records_without_uid_still_deduplicate():
    # production records (pre-classifier) have no uid — identity comes from their URL
    legacy = {"title": "Associate – HEVA (Evidence Synthesis)", "company": "Sanofi", "notified": True, "dismissed": True,
              "url": "https://sanofi.wd3.myworkdayjobs.com/SanofiCareers/job/Hyderabad/Associate---HEVA--Evidence-Synthesis-_R2852866"}
    again = _job("Associate - HEVA (Evidence Synthesis)", legacy["url"], company="Sanofi")
    assert scraper.merge_new_jobs([legacy], [again]) == []


def test_pwc_workday_ids_with_letter_suffix():
    # PwC ids end in letters ("_570925WD-1") and PwC renames postings, which
    # changes the URL slug: identity must follow the id, not the slug
    old = ("https://pwc.wd3.myworkdayjobs.com/Global_Experienced_Careers/job/Bengaluru-Millenia/"
           "IN-Senior-Associate---Internal-Audit--Internal-Audit-Services--Advisory--Bangalore_570925WD-1")
    renamed = "https://pwc.wd3.myworkdayjobs.com/Global_Experienced_Careers/job/Bengaluru-Millenia/IN-Associate-Internal-Audit_570925WD-1"
    assert ats_job_id(old) == "workday:570925WD-1"
    assert job_uid("PwC", old) == job_uid("PwC", renamed)
    assert job_uid("PwC", old) != job_uid("PwC", old.replace("570925WD-1", "570925WD-2"))
    seen = [{**_job("IN_Senior Associate_Internal Audit", old, company="PwC"), "notified": True}]
    assert scraper.merge_new_jobs(seen, [_job("IN_Associate _ Internal Audit", renamed, "Bengaluru Millenia", "PwC")]) == []
