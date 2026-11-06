"""The catalogue is a watchlist (Nov 2026): a company stays monitored whatever it
is hiring for today, a job published later is found and alerted once, a failed
or malformed answer is never read as "no jobs", and a company without a usable
source stays listed without being scanned. No network; nothing is ever emailed."""
import json
import smtplib
from pathlib import Path

import httpx
import pytest

import alerts
import config_store
import job_filters
import scraper
import sources
from config_store import in_shared_alerts, monitoring_state
from test_account1_monitoring import FakeRemote, Mailer

REPO = Path(__file__).resolve().parent.parent
ACCOUNT_2 = {"metlife", "avalara", "pokerstars", "sanofi", "pwc"}
ACTIVE_ADDED = {"amazon", "microsoft", "google", "infosys", "deloitte", "qualcomm", "cisco", "jiohotstar", "amd", "netflix",
                "oracle", "ibm", "adp", "servicenow", "swiggy"}
TAILORING = {"locations": ["Hyderabad"], "job_families": ["Software engineering", "Data & analytics", "Cloud & DevOps"]}
LONG = "Build and run reliable services for customers across the world. " * 2
REAL_CLIENT = httpx.Client


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _catalogue():
    return {c["id"]: c for c in json.loads((REPO / "data" / "companies.json").read_text("utf-8"))}


# ── the catalogue ─────────────────────────────────────────────────

def test_statuses_of_the_added_companies():
    cat = _catalogue()
    for cid in ACTIVE_ADDED:
        assert monitoring_state(cat[cid]) == "" and scraper.sources_for(cat[cid]), cid
        assert in_shared_alerts(cat[cid]) and "Playwright" not in scraper.source_label(cat[cid])


def test_manual_only_companies_left_the_catalogue():
    """Nov 2026 cleanup: Uber (no readable job list or official feed), TCS
    (Access Denied to the scanner) and Zepto (bot challenge) could only be
    checked by hand, so they are no longer listed. They never had a scan or
    a saved job, so no history went with them."""
    cat = _catalogue()
    assert not ({"uber", "tcs", "zepto"} & set(cat))
    seen = json.loads((REPO / "data" / "seen_jobs.json").read_text("utf-8"))
    assert not [j for j in seen if j.get("company_id") in {"uber", "tcs", "zepto"}]


def test_every_listed_company_is_monitored_automatically():
    """The catalogue lists only companies the scheduled scan reads by itself:
    a configured official source, no monitoring hold, never the generic
    browser fallback."""
    for cid, c in _catalogue().items():
        assert monitoring_state(c) == "", cid
        assert scraper.sources_for(c), cid
        assert "Playwright" not in scraper.source_label(c) and "Not scanned" not in scraper.source_label(c), cid


def test_account_2s_companies_are_untouched():
    cat = _catalogue()
    assert all(not in_shared_alerts(cat[cid]) and monitoring_state(cat[cid]) == "" for cid in ACCOUNT_2)
    assert not (ACCOUNT_2 & ACTIVE_ADDED)
    assert len(cat) == 44 and len([c for c in cat.values() if in_shared_alerts(c)]) == 39


def test_discovery_is_not_narrowed_at_the_source():
    """The filters decide after discovery: no level filter, and India, not one city."""
    assert scraper.COMPANY_SOURCES["google"] == [{"type": "google", "location": "Hyderabad, India"}]
    for cid in ("microsoft", "qualcomm", "deloitte"):
        assert scraper.COMPANY_SOURCES[cid][0]["location"] == "India"


# ── a scan over time, as run_all runs it ──────────────────────────

@pytest.fixture
def files(tmp_path, monkeypatch):
    monkeypatch.setattr(config_store, "SEEN_FILE", tmp_path / "seen_jobs.json")
    monkeypatch.setattr(config_store, "COMPANIES_FILE", tmp_path / "companies.json")
    monkeypatch.setattr(config_store, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(scraper.time, "sleep", lambda s: None)
    monkeypatch.setenv("ALERT_RECIPIENT", "me@example.com")
    (tmp_path / "seen_jobs.json").write_text("[]")
    (tmp_path / "settings.json").write_text(json.dumps({"shared_alert_tailoring": TAILORING}))
    (tmp_path / "companies.json").write_text(json.dumps([
        {"id": "netflix", "name": "Netflix", "url": "https://explore.jobs.netflix.net/careers", "last_success": "2020-01-01T00:00:00+00:00"},
        {"id": "tcs", "name": "TCS", "url": "https://www.tcs.com/careers", "monitoring": "blocked", "status": "unknown",
         "last_checked": "", "status_reason": "Access Denied"},
    ]))
    return tmp_path


def _companies(files):
    return {c["id"]: c for c in json.loads((files / "companies.json").read_text())}


def _serve(monkeypatch, positions=None, error=None):
    """Netflix's real adapter on canned answers; ``positions`` = what it lists now."""
    def handler(request):
        if error is not None:
            return error()
        if "/api/apply/v2/jobs/" in request.url.path:
            return httpx.Response(200, json={"job_description": "<p>Bachelor's degree. Freshers and new graduates welcome. 0 years of experience required. "
                                                                f"{LONG}</p>"})
        rows = positions or []
        return httpx.Response(200, json={"count": len(rows), "positions": rows if int(request.url.params["start"]) == 0 else []})
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: REAL_CLIENT(*a, **{**kw, "transport": httpx.MockTransport(handler)}))


FRESHER = {"id": 7, "name": "Graduate Software Engineer", "locations": ["Hyderabad,India"], "display_job_id": "JR7",
           "canonicalPositionUrl": "https://explore.jobs.netflix.net/careers/job/7"}


def test_a_company_with_no_jobs_stays_active_and_a_later_job_is_found_and_alerted_once(files, monkeypatch):
    """The whole path, scan to email step. The sender is a fake and SMTP itself
    fails if touched, so no real email can go out."""
    def no_real_email(*a, **k):
        raise AssertionError("a real email was attempted")
    monkeypatch.setattr(smtplib, "SMTP", no_real_email)
    monkeypatch.setattr(smtplib, "SMTP_SSL", no_real_email)
    mail, remote = Mailer(), FakeRemote()
    # months of nothing: never a problem, never removed, nothing emailed
    _serve(monkeypatch, positions=[])
    for _ in range(3):
        assert scraper.run_all() == []
        assert alerts.run(send=mail, publisher=remote) == 0
    cat = _companies(files)
    assert cat["netflix"]["status"] == "active" and cat["netflix"]["last_success"] > "2020"
    assert json.loads((files / "seen_jobs.json").read_text()) == [] and mail.sent == []
    # then it publishes one: found, and emailed
    _serve(monkeypatch, positions=[FRESHER])
    assert [j["title"] for j in scraper.run_all()] == ["Graduate Software Engineer"]
    assert alerts.run(send=mail, publisher=remote) == 0
    assert mail.sent == [["Graduate Software Engineer"]]
    # and only once, however many scans follow
    for _ in range(2):
        assert scraper.run_all() == []
        assert alerts.run(send=mail, publisher=remote) == 0
    assert len(mail.sent) == 1 and len(json.loads((files / "seen_jobs.json").read_text())) == 1


def test_a_job_elsewhere_is_discovered_but_not_emailed(files, monkeypatch):
    _serve(monkeypatch, positions=[{**FRESHER, "id": 8, "locations": ["Mumbai,India"], "canonicalPositionUrl": "https://explore.jobs.netflix.net/careers/job/8"}])
    assert len(scraper.run_all()) == 1
    seen = json.loads((files / "seen_jobs.json").read_text())
    assert job_filters.passes_global_gate(seen[0]) and not config_store.shared_alert_pending(seen[0], frozenset(), TAILORING)


@pytest.mark.parametrize("bad", [
    lambda: httpx.Response(503), lambda: httpx.Response(429), lambda: httpx.Response(403),
    lambda: httpx.Response(200, text="<html>a sign-in page</html>"), lambda: httpx.Response(200, json={"unexpected": 1}),
])
def test_a_failed_or_malformed_answer_is_blocked_not_zero_jobs_and_recovers_by_itself(files, monkeypatch, bad):
    _serve(monkeypatch, error=bad)
    scraper.run_all()
    company = _companies(files)["netflix"]
    assert company["status"] != "active" and company["status_reason"]
    assert company["last_success"] == "2020-01-01T00:00:00+00:00"       # a failed scan isn't a success
    assert "netflix" in _companies(files)                                # and it is still in the catalogue
    _serve(monkeypatch, positions=[])
    scraper.run_all()
    company = _companies(files)["netflix"]
    assert company["status"] == "active" and not company["status_reason"] and company["last_success"] > "2020"


def test_a_company_without_a_usable_source_stays_listed_and_is_not_scanned(files, monkeypatch):
    called = []
    real = scraper.scan_company
    monkeypatch.setattr(scraper, "scan_company", lambda c: (called.append(c["id"]), real(c))[1])
    _serve(monkeypatch, positions=[])
    scraper.run_all()
    assert called == ["netflix"]
    cat = _companies(files)
    assert cat["tcs"]["monitoring"] == "blocked" and cat["tcs"]["status"] == "unknown" and cat["tcs"]["last_checked"] == ""
    assert cat["tcs"]["status_reason"] == "Access Denied" and "scan" not in cat["tcs"]
    assert cat["netflix"]["scan"]["run"]["portals"] == 1 and cat["netflix"]["scan"]["run"]["portals_failed"] == 0


# ── an empty answer: only the source's own "none" counts ──────────

def test_a_sources_own_count_of_zero_is_healthy():
    assert sources.amazon_listings(_client(lambda r: httpx.Response(200, json={"hits": 0, "jobs": []})), {}).listings == []
    assert sources.jibe_listings(_client(lambda r: httpx.Response(200, json={"jobs": [], "totalCount": 0})), {"host": "https://x.example"}).listings == []
    empty = {"host": "https://x.example", "domain": "x.example"}
    assert sources.eightfold_v2_listings(_client(lambda r: httpx.Response(200, json={"count": 0, "positions": []})), empty).listings == []
    assert sources.oracle_listings(_client(lambda r: httpx.Response(200, json={"items": [{"TotalJobsCount": 0, "requisitionList": []}]})),
                                   {"api": "https://x.example", "site": "S", "location_facet": "1", "job_page": "https://x.example/job"}).listings == []
    assert sources.ibm_listings(_client(lambda r: httpx.Response(200, json={"hits": {"total": {"value": 0}, "hits": []}})), {}).listings == []
    assert sources.swiggy_listings(_client(lambda r: httpx.Response(200, json={"total": 0, "jobs": []})), {}).listings == []
    feed = '<?xml version="1.0"?><source><publisher>X</publisher></source>'
    assert sources.job_feed_listings(_client(lambda r: httpx.Response(200, text=feed)), {"url": "https://x.example/f"}).listings == []
    page = "<html>There are currently no open positions matching this search.</html>"
    assert sources.successfactors_listings(_client(lambda r: httpx.Response(200, text=page)), {"host": "https://x.example"}).listings == []


@pytest.mark.parametrize("read, cfg, answer", [
    (sources.amazon_listings, {}, {"jobs": []}),                                    # no count at all
    (sources.amazon_listings, {}, {"unexpected": 1}),
    (sources.jibe_listings, {"host": "https://x.example"}, {"jobs": []}),
    (sources.eightfold_listings, {"host": "https://x.example", "domain": "x"}, {"data": {}}),
    (sources.eightfold_v2_listings, {"host": "https://x.example", "domain": "x"}, {"error": "no"}),
    (sources.oracle_listings, {"api": "https://x.example", "site": "S", "location_facet": "1", "job_page": "https://x.example/j"}, {"items": []}),
    (sources.ibm_listings, {}, {"errors": ["no"]}),
    (sources.swiggy_listings, {}, {"jobs": []}),                                    # no total
    (sources.infosys_listings, {}, {"unexpected": 1}),
])
def test_a_malformed_json_answer_is_never_zero_jobs(read, cfg, answer):
    with pytest.raises(sources.SourceError):
        read(_client(lambda r, a=answer: httpx.Response(200, json=a)), cfg)


@pytest.mark.parametrize("text", [
    "<html>a sign-in page</html>",                                                  # not a feed
    '<?xml version="1.0"?><source><job><title><![CDATA[Cut off',                    # cut off part-way
    "",
])
def test_a_broken_feed_is_never_zero_jobs(text):
    with pytest.raises(sources.SourceError):
        sources.job_feed_listings(_client(lambda r: httpx.Response(200, text=text)), {"url": "https://x.example/f"})


def test_a_page_without_rows_or_the_no_results_wording_is_never_zero_jobs():
    with pytest.raises(sources.SourceError):
        sources.successfactors_listings(_client(lambda r: httpx.Response(200, text="<html>nothing</html>")), {"host": "https://x.example"})
    with pytest.raises(sources.SourceError):
        sources.google_listings(_client(lambda r: httpx.Response(200, text="<html>a sign-in page</html>")), {})


# ── the new sources ───────────────────────────────────────────────

def _feed(*jobs):
    rows = "".join(f"<job><title><![CDATA[{t}]]></title><referencenumber><![CDATA[{i}]]></referencenumber><url><![CDATA[https://jobs.example/{i}]]></url>"
                   f"<city><![CDATA[{c}]]></city><state><![CDATA[]]></state><country><![CDATA[{n}]]></country><description><![CDATA[<p>{LONG}</p>]]></description></job>"
                   for i, (t, c, n) in enumerate(jobs))
    return f'<?xml version="1.0"?><source><publisher>X</publisher>{rows}</source>'


def test_the_official_feed_keeps_india_and_reads_the_description():
    feed = _feed(("Engineer", "Hyderabad", "India"), ("Engineer", "Berlin", "Germany"))
    res = sources.job_feed_listings(_client(lambda r: httpx.Response(200, text=feed)), {"url": "https://x.example/f"})
    assert res.seen == 2 and [x.location for x in res.listings] == ["Hyderabad, India"]
    assert res.listings[0].url == "https://jobs.example/0" and res.listings[0].detail.ok and "reliable services" in res.listings[0].detail.description


def test_swiggy_lists_from_its_own_site_and_reads_only_the_public_job_page():
    asked = []

    def handler(request):
        asked.append(request.url.host)
        if request.url.path.endswith("jobs.json"):
            return httpx.Response(200, json={"total": 2, "jobs": [{"t": "Engineer", "l": "Hyderabad, India", "u": "https://jobs.smartrecruiters.com/SWIGGY/1-engineer"},
                                                                  {"t": "Elsewhere", "l": "Dubai, UAE", "u": "https://jobs.smartrecruiters.com/SWIGGY/2-x"}]})
        return httpx.Response(200, json={"content": f"<p>{LONG}</p>"})
    res = sources.swiggy_listings(_client(handler), {})
    assert [x.title for x in res.listings] == ["Engineer"] and res.listings[0].fetch_detail().ok
    assert "api.smartrecruiters.com" not in asked              # robots.txt there disallows it


def test_no_company_uses_the_restricted_smartrecruiters_api():
    text = (REPO / "src" / "scraper.py").read_text("utf-8") + (REPO / "src" / "sources.py").read_text("utf-8")
    assert "api.smartrecruiters.com" not in text


def test_netflix_detail_failure_is_unreadable_not_accepted():
    cfg = {"host": "https://x.example", "domain": "x"}
    assert not sources.eightfold_v2_detail(_client(lambda r: httpx.Response(503)), cfg, "1", "Hyderabad, India").ok


def test_oracle_lists_and_reads_the_requisition():
    def handler(request):
        if request.url.path.endswith("recruitingCEJobRequisitionDetails"):
            return httpx.Response(200, json={"items": [{"ExternalDescriptionStr": f"<p>{LONG}</p>"}]})
        return httpx.Response(200, json={"items": [{"TotalJobsCount": 2, "requisitionList": [
            {"Id": "1", "Title": "Engineer", "PrimaryLocation": "HYDERABAD, TELANGANA, India"},
            {"Id": "2", "Title": "Elsewhere", "PrimaryLocation": "Austin, TX, United States"}]}]})
    res = sources.oracle_listings(_client(handler), {"api": "https://x.example", "site": "S", "location_facet": "1", "job_page": "https://careers.example/job"})
    assert [x.url for x in res.listings] == ["https://careers.example/job/1"] and res.listings[0].fetch_detail().ok


def test_ibm_keeps_its_record_text():
    answer = {"hits": {"total": {"value": 1}, "hits": [{"_source": {"title": "Engineer", "url": "https://careers.ibm.com/job/1", "field_keyword_19": "Hyderabad, IN", "body": LONG}}]}}
    res = sources.ibm_listings(_client(lambda r: httpx.Response(200, json=answer)), {})
    assert res.listings[0].location == "Hyderabad, IN | India" and res.listings[0].detail.ok


def test_deloitte_links_are_not_html_encoded():
    row = ('<tr class="data-row"><td><a href="/job/Hyderabad-T&amp;T-Consultant/1/" class="jobTitle-link">T&amp;T | Consultant</a>'
           '</td><td class="colLocation"><span class="jobLocation"> Hyderabad, IN </span></td></tr>')
    res = sources.successfactors_listings(_client(lambda r: httpx.Response(200, text=f"of <b>1</b> {row}")), {"host": "https://x.example", "location": "India"})
    assert res.listings[0].url == "https://x.example/job/Hyderabad-T&T-Consultant/1/" and res.listings[0].title == "T&T | Consultant"


# ── "try again later" ─────────────────────────────────────────────

def test_a_rate_limited_search_waits_and_asks_again(monkeypatch):
    waits, answers = [], [httpx.Response(429), httpx.Response(200, json={"data": {"count": 0, "positions": []}})]
    monkeypatch.setattr(sources.time, "sleep", waits.append)
    res = sources.eightfold_listings(_client(lambda r: answers.pop(0)), {"host": "https://x.example", "domain": "x"})
    assert res.listings == [] and waits == [20]


def test_a_lasting_rate_limit_is_a_failure_never_zero_jobs(monkeypatch):
    calls = []
    monkeypatch.setattr(sources.time, "sleep", lambda s: None)

    def handler(request):
        calls.append(1)
        return httpx.Response(429)
    with pytest.raises(sources.SourceError):
        sources.eightfold_listings(_client(handler), {"host": "https://x.example", "domain": "x"})
    assert len(calls) == 3                                     # a few polite tries, not a hammering


# ── one vacancy excluded by the owner, by data alone ──────────────

GOOGLE_RESULTS = "https://www.google.com/about/careers/applications/jobs/results/"
GATE = {"real_job_posting": True, "india_location": True, "detail_read": True, "detail_is_this_job": True, "no_conflicting_experience": True,
        "not_programme_story_talent_recruiter": True, "evidence_not_staff_context": True, "fresher_or_entry_evidence": True}


def _job(company, url, title, location="Hyderabad, Telangana, India"):
    return {"title": title, "url": url, "company": company, "location": location, "category": "ENTRY_LEVEL", "reason": "r", "evidence": {"checks": dict(GATE)}}


def _saved():
    return json.loads((REPO / "data" / "seen_jobs.json").read_text("utf-8"))


def test_the_excluded_vacancy_is_known_and_can_never_be_emailed():
    seen = _saved()
    record = next(j for j in seen if "107018346561446598" in j["url"])
    assert record["dismissed"] and not config_store.alert_pending(record) and not job_filters.passes_global_gate(record)
    assert record not in config_store.visible_jobs(seen)
    title = "Software Engineer, PhD, Early Career, 2026"
    for posting in ("107018346561446598", "108281679147082438"):      # and the same vacancy under its second ID
        assert scraper.merge_new_jobs(seen, [_job("Google", f"{GOOGLE_RESULTS}{posting}-software-engineer-phd-early-career-2026", title)], company_id="google") == []


def test_the_exclusion_is_that_one_vacancy_not_a_phd_filter():
    from job_classifier import classify_job
    seen, tailoring = _saved(), {"locations": ["Hyderabad"], "job_families": ["Software engineering"]}
    other_phd = _job("Google", f"{GOOGLE_RESULTS}222222222222222222-research-engineer-phd", "Software Engineer, PhD, Systems")
    other = _job("Google", f"{GOOGLE_RESULTS}111111111111111111-software-engineer-early-career", "Software Engineer, Early Career, 2026")
    qualcomm = _job("Qualcomm", "https://careers.qualcomm.com/careers/job/446719784236", "2027 Campus Hire Software Engineer")
    for job, cid in ((other_phd, "google"), (other, "google"), (qualcomm, "qualcomm")):
        new = scraper.merge_new_jobs(seen, [job], company_id=cid)
        assert len(new) == 1 and config_store.shared_alert_pending(new[0], frozenset(), tailoring)
    assert classify_job("Software Engineer, PhD, Early Career, 2026", "Build distributed systems.", url=GOOGLE_RESULTS + "1", posting_evidence=True).accepted
    for name in ("job_classifier.py", "job_filters.py", "alerts.py"):
        assert "107018346561446598" not in (REPO / "src" / name).read_text("utf-8")


def test_the_excluded_qualcomm_vacancy_is_known_and_can_never_be_emailed():
    seen = _saved()
    url = "https://careers.qualcomm.com/careers/job/446719784235"
    record = next(j for j in seen if j["url"] == url)
    assert record["company_id"] == "qualcomm" and record["dismissed"]
    assert not config_store.alert_pending(record) and not job_filters.passes_global_gate(record) and record not in config_store.visible_jobs(seen)
    assert scraper.merge_new_jobs(seen, [_job("Qualcomm", url, "2027 Campus Hire_Associate Engineer_SW", record["location"])], company_id="qualcomm") == []


def test_only_that_qualcomm_vacancy_is_excluded():
    """Other Qualcomm jobs, other 2027 campus hires among them, are judged as before."""
    seen, tailoring = _saved(), {"locations": ["Hyderabad"], "job_families": ["Software engineering"]}
    qualcomm = [c for c in json.loads((REPO / "data" / "companies.json").read_text("utf-8")) if c["id"] == "qualcomm"]
    assert in_shared_alerts(qualcomm[0]) and monitoring_state(qualcomm[0]) == "" and scraper.sources_for(qualcomm[0])
    for n, title in ((1, "2027 Campus Hire Software Engineer"), (2, "2027 Campus Hire_Associate Engineer_Android"), (3, "Graduate Software Engineer")):
        new = scraper.merge_new_jobs(seen, [_job("Qualcomm", f"https://careers.qualcomm.com/careers/job/90000{n}", title)], company_id="qualcomm")
        assert len(new) == 1 and config_store.shared_alert_pending(new[0], frozenset(), tailoring), title
    # one dismissed record, not a company-wide rule (the scan keeps saving other Qualcomm jobs)
    assert sum(j.get("company") == "Qualcomm" and bool(j.get("dismissed")) for j in seen) == 1


# ── a known limitation, to be addressed separately ────────────────

def test_known_limitation_a_job_found_for_the_first_time_can_be_an_old_vacancy():
    """"New" means new to the tracker, not newly published: there is no
    publication-date rule (some sources, Google's among them, publish no date),
    so an old vacancy on a newly added company is still emailed once, and the
    email calls it new. Known and accepted for now; this pins today's behaviour
    so that changing it is a decision."""
    seen = _saved()
    old = {**_job("Qualcomm", "https://careers.qualcomm.com/careers/job/900009", "Graduate Software Engineer"), "posted_on": "2019-01-01"}
    new = scraper.merge_new_jobs(seen, [old], company_id="qualcomm")
    assert len(new) == 1 and config_store.shared_alert_pending(new[0], frozenset(), {"locations": ["Hyderabad"]})
