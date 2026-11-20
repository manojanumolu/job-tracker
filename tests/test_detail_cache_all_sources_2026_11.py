"""The 72 h detail-verdict cache for every source, not only Workday (Nov 2026):
an unchanged posting's page is not re-read within 72 h; new, changed and
expired postings are; failures are never cached; rejections are stored
compactly; the cache survives the run (published file) and stays per company
and source. Real _evaluate + Recovery in cache-only mode; no network."""
import json

import pytest

import scraper
from sources import Detail, Listing
from workday_runtime import Recovery, configuration
from workday_state import CACHE_FILE

FRESHER = ("Graduate Engineer Trainee role in Hyderabad, India. Freshers welcome; 0 years of experience "
           "required. Build and run services with the team. " * 3)
SENIOR = "Lead role in Hyderabad, India. Minimum 8 years of experience in software engineering required. " * 3
CO = {"id": "c1793696382", "name": "Mastercard", "url": "https://careers.mastercard.com/us/en"}   # a Phenom company


class Clock:
    def __init__(self):
        self.t = 1800000000.0

    def now(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    clock = Clock()
    cfg = configuration()
    cfg.update(mode="full", company_delay_s=0)

    def controller():
        rec = Recovery(tmp_path, [CO], scraper.sources_for, config=cfg, clock=clock.now, wall=clock.now, sleep=lambda s: None)
        rec.begin_cache(CO)
        return rec
    return clock, controller, tmp_path


def listing(n, desc, title=None, reads=None, ok=True):
    reads = reads if reads is not None else []

    def fetch():
        reads.append(n)
        if not ok:
            return Detail.unreadable("HTTP 503 from job page")
        return Detail(ok=True, description=desc, location="Hyderabad, Telangana, India", posting_evidence=True,
                      matched="JobPosting on the job page", posted_at="2026-11-18")
    lst = Listing(title=title or f"Graduate Engineer Trainee {n}", url=f"https://careers.mastercard.com/us/en/job/R-{n}",
                  location="Hyderabad, Telangana, India", job_id=f"R-{n}", posting_evidence=True)
    lst.fetch_detail = fetch
    return lst, reads


def run(rec, lst):
    scan = scraper.Scan(name="Mastercard")
    token = scraper.DETAIL_CACHE.set(rec)
    try:
        scraper._evaluate(scan, lst, [10])
    finally:
        scraper.DETAIL_CACHE.reset(token)
    return scan


def test_an_unchanged_job_is_not_re_read_and_is_still_found_every_scan(env):
    clock, controller, _ = env
    rec = controller()
    lst, reads = listing(1, FRESHER)
    first = run(rec, lst)
    assert [j["title"] for j in first.jobs] == ["Graduate Engineer Trainee 1"] and reads == [1]
    lst2, _ = listing(1, FRESHER, reads=reads)
    second = run(rec, lst2)
    assert [j["title"] for j in second.jobs] == ["Graduate Engineer Trainee 1"] and reads == [1]   # no second read
    assert rec.stats == {"cache_hits": 1, "cache_misses": 1}


def test_a_rejection_is_replayed_without_a_request_and_stored_compactly(env):
    _, controller, _ = env
    rec = controller()
    lst, reads = listing(2, SENIOR, title="Software Engineer")   # judged on its page, not its title
    assert run(rec, lst).rejected == 1
    entry = next(iter(rec.cache["entries"].values()))
    assert entry["verdict"] == "rejected" and entry["detail"]["description"] == "" and entry["detail"]["reason"]
    again = run(rec, listing(2, SENIOR, title="Software Engineer", reads=reads)[0])
    assert again.rejected == 1 and again.jobs == [] and reads == [2]


def test_a_new_or_changed_posting_is_read(env):
    _, controller, _ = env
    rec = controller()
    reads = []
    run(rec, listing(3, FRESHER, reads=reads)[0])
    run(rec, listing(4, FRESHER, reads=reads)[0])                                   # new posting
    run(rec, listing(3, FRESHER, title="Graduate Engineer Trainee 3 (Data)", reads=reads)[0])   # card changed
    assert reads == [3, 4, 3]


def test_expired_entries_are_read_again(env):
    clock, controller, _ = env
    rec = controller()
    reads = []
    run(rec, listing(5, FRESHER, reads=reads)[0])
    clock.t += 72 * 3600 + 1
    run(rec, listing(5, FRESHER, reads=reads)[0])
    assert reads == [5, 5]


def test_failures_are_never_cached(env):
    _, controller, _ = env
    rec = controller()
    reads = []
    scan = run(rec, listing(6, FRESHER, reads=reads, ok=False)[0])
    assert scan.jobs == [] and rec.cache["entries"] == {}
    run(rec, listing(6, FRESHER, reads=reads)[0])
    assert reads == [6, 6]


def test_the_cache_survives_the_run_through_the_published_file(env):
    clock, controller, tmp_path = env
    rec = controller()
    reads = []
    run(rec, listing(7, FRESHER, reads=reads)[0])
    rec.save("2026-11-20T00:02:00+00:00", "2026-11-20T00:10:00+00:00")
    saved = json.loads((tmp_path / CACHE_FILE).read_text())
    assert len(saved["entries"]) == 1
    rec2 = controller()                                   # the next scan, from the published file
    scan = run(rec2, listing(7, FRESHER, reads=reads)[0])
    assert reads == [7] and [j["title"] for j in scan.jobs] == ["Graduate Engineer Trainee 7"]


def test_entries_are_per_company_and_source(env):
    _, controller, _ = env
    rec = controller()
    reads = []
    run(rec, listing(8, FRESHER, reads=reads)[0])
    rec.begin_cache({"id": "other", "name": "Other", "url": "https://careers.mastercard.com/us/en"})
    run(rec, listing(8, FRESHER, reads=reads)[0])         # same URL, another company: read
    assert reads == [8, 8]


def test_a_cached_job_keeps_its_requisition_id_for_cross_source_matching(env):
    _, controller, _ = env
    rec = controller()
    run(rec, listing(90001, FRESHER)[0])
    job = run(rec, listing(90001, FRESHER)[0]).jobs[0]     # from the cache
    assert job["evidence"]["job_id"] == "id:R-90001" and scraper._requisition(job) == "R-90001"
    seen = [{"title": "x", "company": "Mastercard", "location": "Pune",
             "url": "https://mastercard.wd1.myworkdayjobs.com/CorporateCareers/job/Pune/X_R-90001"}]
    assert scraper.merge_new_jobs(seen, [job]) == []


def test_run_all_gives_every_non_workday_company_the_cache_and_workday_ones_their_controller(tmp_path, monkeypatch):
    import config_store
    from workday_http import CURRENT
    cos = [dict(CO, status="active"), {"id": "c1793696368", "name": "Salesforce", "status": "active",
                                       "url": "https://salesforce.wd12.myworkdayjobs.com/External_Career_Site"}]
    monkeypatch.setattr(config_store, "load_companies", lambda: cos)
    monkeypatch.setattr(config_store, "load_seen_jobs", lambda: [])
    monkeypatch.setattr(config_store, "save_companies", lambda data, **kw: None)
    monkeypatch.setattr(config_store, "save_seen_jobs", lambda data, **kw: None)
    monkeypatch.setattr(scraper.time, "sleep", lambda s: None)
    seen = {}

    def fake_scan(company):
        seen[company["id"]] = (scraper.DETAIL_CACHE.get(), CURRENT.get())
        return scraper.Scan(name=company["name"])
    monkeypatch.setattr(scraper, "scan_company", fake_scan)
    cfg = configuration()
    cfg.update(mode="full", company_delay_s=0)
    rec = Recovery(tmp_path, cos, scraper.sources_for, config=cfg, sleep=lambda s: None)
    scraper.run_all(recovery=rec)
    assert seen["c1793696382"][0] is rec and seen["c1793696382"][1] is None        # Phenom: cache only
    assert seen["c1793696368"] == (None, rec)                                        # Workday: its controller
    assert scraper.DETAIL_CACHE.get() is None                                        # nothing leaks out


def test_a_listing_that_already_carries_its_detail_is_not_cached(env):
    _, controller, _ = env
    rec = controller()
    lst, reads = listing(11, FRESHER)
    lst.detail = Detail(ok=True, description=FRESHER, location="Hyderabad, Telangana, India", posting_evidence=True,
                        matched="feed")
    lst.fetch_detail = None
    scan = run(rec, lst)
    assert len(scan.jobs) == 1 and rec.cache["entries"] == {} and reads == []      # no request to save, nothing stored
