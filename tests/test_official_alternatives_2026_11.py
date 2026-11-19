"""Micron and NVIDIA moved from blocked Workday CXS to their official Eightfold
career sites (Nov 2026); a job already recorded from Workday is never re-alerted
when the same requisition is read from the new site. No network."""
import scraper
from workday_runtime import is_workday


def test_micron_and_nvidia_use_their_official_eightfold_sites_not_workday():
    for cid, host in (("c1793696369", "https://careers.micron.com"), ("c1793696370", "https://jobs.nvidia.com")):
        company = {"id": cid, "url": f"https://x.wd1.myworkdayjobs.com/External"}
        src = scraper.sources_for(company)
        assert src[0]["type"] == "eightfold" and src[0]["host"] == host and src[0]["location"] == "India"
        assert not is_workday(src)                       # no longer gated by the Workday recovery controller
        assert scraper.source_label(company) == "Eightfold API"


WD = {"title": "Associate Engineer, IT Eng System Mgr", "company": "Micron", "company_id": "c1793696369",
      "location": "Hyderabad - Phoenix Aquila, India",
      "url": "https://micron.wd1.myworkdayjobs.com/External/job/Hyderabad---Phoenix-Aquila-India/Associate-Engineer--IT-Eng-System-Mgr_JR112672",
      "evidence": {"job_id": "workday:JR112672"}}


def _ef(req, title="Associate Engineer, IT Eng System Mgr", n="44495506"):
    return {"title": title, "company": "Micron", "location": "Hyderabad, Telangana, India",
            "url": f"https://careers.micron.com/careers/job/{n}", "evidence": {"job_id": f"id:{req}"}}


def test_the_same_requisition_from_the_official_site_is_not_a_new_job():
    seen = [dict(WD)]
    assert scraper.merge_new_jobs(seen, [_ef("JR112672")], company_id="c1793696369") == []


def test_a_different_requisition_is_new_and_ids_stay_per_company():
    seen = [dict(WD)]
    new = scraper.merge_new_jobs(seen, [_ef("JR999001", title="Graduate Engineer", n="1")], company_id="c1793696369")
    assert [j["title"] for j in new] == ["Graduate Engineer"]
    other = dict(_ef("JR112672"), company="NVIDIA", url="https://jobs.nvidia.com/careers/job/7")
    assert len(scraper.merge_new_jobs(seen, [other], company_id="c1793696370")) == 1


def test_requisition_parsing():
    assert scraper._requisition(WD) == "JR112672"
    assert scraper._requisition(_ef("JR112672")) == "JR112672"
    assert scraper._requisition({"url": "https://careers.micron.com/careers/job/44495506"}) == ""
