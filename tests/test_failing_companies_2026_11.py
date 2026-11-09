"""The three failing companies of 9 Nov 2026 (Deloitte, JioHotstar, ADP).

* Deloitte: one slow SuccessFactors results page (ReadTimeout after the
  first page had loaded) failed the whole scan; a timed-out page is now asked
  for again, and only a timeout is retried.
* ADP: the partial feed (~700-810 postings, no India jobs) came back two
  scans in a row, so "3 in a row = the new normal" would soon have accepted
  it; it now takes 8, and an anomalous feed is read once more in the scan.
* JioHotstar: Workday answers 403; that is never retried and never healthy.
* Every failure now records since when, and how many scans in a row, it has
  failed; a failed scan keeps the company's last_job.

No network; nothing is emailed."""
import json

import httpx
import pytest

import config_store
import scraper
import sources

REAL_CLIENT = httpx.Client
DESC = ("Graduate role. Freshers and new graduates welcome. 0 years of experience required. "
        + "Build and run reliable services for customers across the world. " * 3)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    waits = []
    monkeypatch.setattr(sources.time, "sleep", lambda s: waits.append(s))
    monkeypatch.setattr(scraper.time, "sleep", lambda s: waits.append(s))
    return waits


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


# ── Deloitte (SuccessFactors) ─────────────────────────────────────────

def _sf_row(i):
    return (f'<tr class="data-row"><td><a href="/job/Hyderabad-Analyst-{i}/{i}/" class="jobTitle-link">Analyst {i}</a>'
            f'</td><td class="colLocation"><span class="jobLocation"> Hyderabad, IN </span></td></tr>')


def _sf_handler(timeouts_on_page2=0, status_on_page2=200):
    calls = {"page2": 0}

    def handler(request):
        start = int(request.url.params.get("startrow", "0"))
        if start == 25:
            calls["page2"] += 1
            if calls["page2"] <= timeouts_on_page2:
                raise httpx.ReadTimeout("slow page", request=request)
            if status_on_page2 != 200:
                return httpx.Response(status_on_page2)
        rows = range(start, min(start + 25, 30))
        return httpx.Response(200, text="Results of <b>30</b> " + "".join(_sf_row(i) for i in rows))
    return handler, calls


CFG = {"host": "https://careers.example.com", "location": "India"}


def test_a_page_that_times_out_once_is_asked_for_again(no_sleep):
    handler, calls = _sf_handler(timeouts_on_page2=1)
    res = sources.successfactors_listings(_client(handler), CFG)
    assert len(res.listings) == 30 and res.seen == 30                    # every page read: nothing lost
    assert calls["page2"] == 2 and no_sleep == [5]


def test_a_page_that_keeps_timing_out_fails_the_scan_with_the_page_named(no_sleep):
    handler, calls = _sf_handler(timeouts_on_page2=99)
    with pytest.raises(sources.SourceError) as e:
        sources.successfactors_listings(_client(handler), CFG)
    assert e.value.status == "failing" and "ReadTimeout" in str(e.value) and "results page 2" in str(e.value)
    assert calls["page2"] == 3 and no_sleep == [5, 15]                    # bounded: 3 tries, then give up


@pytest.mark.parametrize("status", [403, 503])
def test_an_http_error_is_never_retried(no_sleep, status):
    handler, calls = _sf_handler(status_on_page2=status)
    with pytest.raises(sources.SourceError):
        sources.successfactors_listings(_client(handler), CFG)
    assert calls["page2"] == 1 and no_sleep == []


# ── the scan over time (run_all) ─────────────────────────────────────

@pytest.fixture
def scan(tmp_path, monkeypatch):
    """run_all over Deloitte, JioHotstar and ADP; ``answers`` say what each serves."""
    monkeypatch.setattr(config_store, "SEEN_FILE", tmp_path / "seen_jobs.json")
    monkeypatch.setattr(config_store, "COMPANIES_FILE", tmp_path / "companies.json")
    saved = [{"title": "Kept Job", "url": "https://jiostar.example/job/1", "company": "JioHotstar (JioStar)",
              "company_id": "jiohotstar", "uid": "x|1", "first_seen": "2026-11-01T00:00:00+00:00"}]
    (tmp_path / "seen_jobs.json").write_text(json.dumps(saved))
    (tmp_path / "companies.json").write_text(json.dumps([
        {"id": "jiohotstar", "name": "JioHotstar (JioStar)", "url": "https://jiostar.wd102.myworkdayjobs.com/JioStar",
         "last_success": "2026-11-06T15:25:35+00:00", "status": "active", "last_job": "Software Engineer"},
        {"id": "adp", "name": "ADP", "url": "https://jobs.adp.com/en/jobs/",
         "scan": {"feed": {"baseline": {"postings": 1511, "at": "2026-11-09T12:23:27+00:00"}}}},
    ]))
    answers = {"jiostar": lambda: httpx.Response(403), "adp": [lambda: httpx.Response(200, text=_feed(1511))]}
    asked = {"jiostar": 0, "adp": 0}

    def handler(request):
        if "jiostar" in request.url.host:
            asked["jiostar"] += 1
            return answers["jiostar"]()
        asked["adp"] += 1
        queue = answers["adp"]
        return (queue.pop(0) if len(queue) > 1 else queue[0])()
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: REAL_CLIENT(*a, **{**kw, "transport": httpx.MockTransport(handler)}))

    def run(jiostar=None, adp=None):
        if jiostar is not None:
            answers["jiostar"] = jiostar
        if adp is not None:
            answers["adp"] = [(lambda t=t: httpx.Response(200, text=_feed(t))) for t in adp]
        asked.update(jiostar=0, adp=0)
        scraper.run_all()
        cos = {c["id"]: c for c in json.loads((tmp_path / "companies.json").read_text())}
        return cos, dict(asked), json.loads((tmp_path / "seen_jobs.json").read_text())
    return run


def _feed(total, india=0):
    rows = "".join(f"<job><title><![CDATA[Payroll Analyst {i}]]></title><referencenumber><![CDATA[R{i}]]></referencenumber>"
                   f"<url><![CDATA[https://jobs.adp.example/job/{i}]]></url><city><![CDATA[{'Hyderabad' if i < india else 'Berlin'}]]></city>"
                   f"<state><![CDATA[]]></state><country><![CDATA[{'India' if i < india else 'Germany'}]]></country>"
                   f"<description><![CDATA[<p>{DESC}</p>]]></description></job>" for i in range(total))
    return '<?xml version="1.0"?><source><publisher>X</publisher>' + rows + "</source>"


def test_jiohotstar_403_is_never_retried_never_healthy_and_its_streak_is_recorded(scan):
    cos, asked, seen = scan()
    jio = cos["jiohotstar"]
    assert jio["status"] == "broken" and jio["status_reason"] == "HTTP 403 from jiostar.wd102.myworkdayjobs.com"
    assert asked["jiostar"] == 1                                          # one request: a refusal is not retried
    assert jio["scan"]["failed_scans"] == 1 and jio["scan"]["failing_since"]
    assert jio["last_success"] == "2026-11-06T15:25:35+00:00" and jio["last_job"] == "Software Engineer"   # kept
    since = jio["scan"]["failing_since"]
    for n in (2, 3):
        cos, asked, seen = scan()
        jio = cos["jiohotstar"]
        assert jio["scan"]["failed_scans"] == n and jio["scan"]["failing_since"] == since
        assert jio["status_reason"].startswith("HTTP 403 from jiostar.wd102.myworkdayjobs.com — failing since ")
        assert f"({n} scans in a row)" in jio["status_reason"] and asked["jiostar"] == 1
    assert [j["title"] for j in seen] == ["Kept Job"]                     # saved jobs untouched


def test_a_recovered_company_clears_its_streak(scan):
    scan()
    scan()
    cos, _, _ = scan(jiostar=lambda: httpx.Response(200, json={"total": 0, "jobPostings": [], "facets": []}))
    jio = cos["jiohotstar"]
    assert jio["status"] == "active" and not jio["status_reason"]
    assert "failed_scans" not in jio["scan"] and "failing_since" not in jio["scan"]


def test_adp_partial_feed_is_read_once_more_and_the_full_one_is_used(scan, no_sleep):
    cos, asked, _ = scan(adp=[805, 1510])                                  # partial, then full on the re-read
    adp = cos["adp"]
    assert adp["status"] == "active" and adp["scan"]["feed"]["baseline"]["postings"] == 1510
    assert asked["adp"] == 2 and 30 in no_sleep


def test_adp_partial_twice_in_the_scan_stays_failing_and_is_read_only_twice(scan):
    cos, asked, _ = scan(adp=[805, 809])
    adp = cos["adp"]
    assert adp["status"] == "failing" and "feed size anomaly" in adp["status_reason"]
    assert adp["scan"]["feed"]["baseline"]["postings"] == 1511 and asked["adp"] == 2


def test_adp_partial_feed_is_not_accepted_as_normal_after_3_scans(scan):
    """9 Nov: 795 then 809. A third ~800 reading would have settled at 3."""
    guard = scraper.COMPANY_SOURCES["adp"][0]["size_guard"]
    assert guard["settle_scans"] == 8 and guard["min_ratio"] == 0.75 and guard["first_scan_min"] == 1000
    for n in range(1, 8):
        cos, _, _ = scan(adp=[800 + n, 800 + n])
        assert cos["adp"]["status"] == "failing", n
        assert cos["adp"]["scan"]["feed"]["baseline"]["postings"] == 1511
    cos, _, _ = scan(adp=[808, 808])                                       # a full day at the same size: real
    assert cos["adp"]["status"] == "active" and cos["adp"]["scan"]["feed"]["baseline"]["postings"] == 808


def test_the_workday_adapter_and_servicenow_are_unchanged():
    assert scraper.COMPANY_SOURCES["servicenow"] == [{"type": "job_feed", "url": "https://careers.servicenow.com/jobs/xml/"}]
    assert "jiohotstar" not in scraper.COMPANY_SOURCES                    # still read through its Workday URL
    assert scraper.sources_for({"id": "jiohotstar", "url": "https://jiostar.wd102.myworkdayjobs.com/JioStar"}) == [
        {"type": "workday", "api": "https://jiostar.wd102.myworkdayjobs.com/wday/cxs/jiostar/JioStar/jobs"}]
