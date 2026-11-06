"""ADP's job feed is checked against its own last good size (Nov 2026).

On 6 Nov 2026 ADP's feed was well-formed XML each time but listed 1,482,
then 0, then 1,493, then 788 postings, and every reading was recorded as a
healthy scan, so ADP's vacancies silently went unseen. These tests run the
real scan (scraper.run_all) on canned feeds. ServiceNow, the other XML-feed
company, has no size guard and is checked to behave exactly as before. No
network; nothing is emailed."""
import json

import httpx
import pytest

import config_store
import scraper
import sources

REAL_CLIENT = httpx.Client
GUARD = scraper.COMPANY_SOURCES["adp"][0]["size_guard"]
DESC = ("Graduate role. Freshers and new graduates welcome. 0 years of experience required. "
        + "Build and run reliable payroll services for customers across the world. " * 3)
OLD = "2020-01-01T00:00:00+00:00"


def _feed(total: int, india: int = 2) -> str:
    """A well-formed feed of ``total`` postings, ``india`` of them in India."""
    rows = []
    for i in range(total):
        city, country = ("Hyderabad", "India") if i < india else ("Berlin", "Germany")
        rows.append(f"<job><title><![CDATA[Graduate Software Engineer {i}]]></title>"
                    f"<referencenumber><![CDATA[R{i}]]></referencenumber>"
                    f"<url><![CDATA[https://jobs.adp.example/job/{i}]]></url>"
                    f"<city><![CDATA[{city}]]></city><state><![CDATA[]]></state><country><![CDATA[{country}]]></country>"
                    f"<description><![CDATA[<p>{DESC}</p>]]></description></job>")
    return '<?xml version="1.0"?><source><publisher>X</publisher>' + "".join(rows) + "</source>"


@pytest.fixture
def scan(tmp_path, monkeypatch):
    """run_all over ADP and ServiceNow, each serving what ``answers`` says."""
    monkeypatch.setattr(config_store, "SEEN_FILE", tmp_path / "seen_jobs.json")
    monkeypatch.setattr(config_store, "COMPANIES_FILE", tmp_path / "companies.json")
    monkeypatch.setattr(scraper.time, "sleep", lambda s: None)
    kept = {"title": "Payroll Analyst", "url": "https://jobs.adp.example/job/old", "company": "ADP", "company_id": "adp",
            "uid": "adp|url:jobs.adp.example/job/old", "first_seen": OLD, "dismissed": True, "notified": False}
    (tmp_path / "seen_jobs.json").write_text(json.dumps([kept]))
    (tmp_path / "companies.json").write_text(json.dumps([
        {"id": "adp", "name": "ADP", "url": "https://jobs.adp.com/en/jobs/", "last_success": OLD},
        {"id": "servicenow", "name": "ServiceNow", "url": "https://careers.servicenow.com/jobs/", "last_success": OLD},
    ]))
    answers = {"adp": lambda: httpx.Response(200, text=_feed(1482)),
               "servicenow": lambda: httpx.Response(200, text=_feed(0))}

    def handler(request):
        return answers["adp" if "adp" in request.url.host else "servicenow"]()
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: REAL_CLIENT(*a, **{**kw, "transport": httpx.MockTransport(handler)}))

    def run(adp=None, servicenow=None):
        if adp is not None:
            answers["adp"] = adp if callable(adp) else (lambda t=adp: httpx.Response(200, text=t))
        if servicenow is not None:
            answers["servicenow"] = servicenow if callable(servicenow) else (lambda t=servicenow: httpx.Response(200, text=t))
        scraper.run_all()
        cos = {c["id"]: c for c in json.loads((tmp_path / "companies.json").read_text())}
        return cos, json.loads((tmp_path / "seen_jobs.json").read_text())
    return run


# ── the decision ──────────────────────────────────────────────────────

def _base(n):
    return {"baseline": {"postings": n, "at": OLD}}


@pytest.mark.parametrize("postings, previous, accepted", [
    (1482, _base(1476), True),            # a normal feed
    (1463, _base(1493), True),            # the largest natural move seen (-2 %)
    (1200, _base(1482), True),            # a real but plausible change (-19 %): not called a failure
    (0, _base(1482), False),              # 6 Nov 09:02: empty but well-formed
    (788, _base(1482), False),            # 6 Nov 15:02: 53 % of the last good size
    (1480, None, True),                   # first scan, no baseline: judged on its own size
    (1480, {}, True),
    (0, None, False),                     # first scan, empty
    (500, None, False),                   # first scan, far below ADP's normal size
])
def test_the_size_check(postings, previous, accepted):
    ok, why, record = scraper._feed_size_check(postings, GUARD, previous)
    assert ok is accepted
    if ok:
        assert record == {"baseline": {"postings": postings, "at": record["baseline"]["at"]}} and not why
    else:
        assert why.startswith("feed size anomaly") and "not read as no jobs" in why
        assert record.get("baseline") == (previous or {}).get("baseline")             # the last good size kept


def test_the_reason_tells_an_empty_feed_from_a_short_one():
    _, empty, _ = scraper._feed_size_check(0, GUARD, _base(1482))
    _, short, _ = scraper._feed_size_check(788, GUARD, _base(1482))
    assert "listed no postings" in empty and "1,482 at the last good scan" in empty
    assert "listed 788 postings" in short and "1,482 at the last good scan" in short


def test_a_lasting_smaller_feed_becomes_the_new_normal_but_an_empty_one_never_does():
    previous = _base(1482)
    for i in range(1, GUARD["settle_scans"]):
        ok, _, previous = scraper._feed_size_check(900 + i, GUARD, previous)       # within 10 % each time
        assert not ok and previous["suspect"]["streak"] == i and previous["baseline"]["postings"] == 1482
    ok, why, previous = scraper._feed_size_check(905, GUARD, previous)
    assert ok and "new normal" in why and previous == {"baseline": {"postings": 905, "at": previous["baseline"]["at"]}}
    previous = _base(1482)
    for _ in range(GUARD["settle_scans"] + 2):
        ok, _, previous = scraper._feed_size_check(0, GUARD, previous)
        assert not ok and previous["baseline"]["postings"] == 1482
    previous = _base(1482)                                                         # sizes that keep jumping never settle
    for n in (900, 300, 900, 300, 900):
        ok, _, previous = scraper._feed_size_check(n, GUARD, previous)
        assert not ok


# ── the real scan, over a production-like day ──────────────────────────

def test_6_november_replayed(scan):
    cos, seen = scan(adp=_feed(1482, india=35))                                  # 06:02, normal
    adp = cos["adp"]
    assert adp["status"] == "active" and not adp["status_reason"] and adp["last_success"] > "2020"
    assert adp["scan"]["feed"]["baseline"]["postings"] == 1482 and adp["scan"]["candidates"] == 35
    good_at, saved = adp["last_success"], len(seen)
    assert saved > 1                                                             # the feed's fresher jobs were saved

    for text, said in ((_feed(0), "listed no postings"), (_feed(788, india=0), "listed 788 postings")):   # 09:02, 15:02
        cos, seen = scan(adp=text)
        adp = cos["adp"]
        assert adp["status"] == "failing" and said in adp["status_reason"]       # never a healthy scan
        assert adp["last_success"] == good_at                                    # a failed scan isn't a success
        assert adp["scan"]["feed"]["baseline"]["postings"] == 1482                # the last good size kept
        assert adp["scan"]["candidates"] == 0 and adp["scan"]["accepted"] == 0    # nothing read from it
        assert len(seen) == saved and seen[0]["title"] == "Payroll Analyst" and seen[0]["dismissed"]   # nothing lost

    cos, seen = scan(adp=_feed(1493, india=36))                                  # 12:02, normal again
    adp = cos["adp"]
    assert adp["status"] == "active" and not adp["status_reason"] and adp["last_success"] > good_at
    assert adp["scan"]["feed"] == {"baseline": {"postings": 1493, "at": adp["scan"]["feed"]["baseline"]["at"]}}
    assert len(seen) >= saved and seen[0]["dismissed"]


@pytest.mark.parametrize("bad", [
    lambda: httpx.Response(200, text='<?xml version="1.0"?><source><job><title><![CDATA[Cut off'),   # truncated
    lambda: httpx.Response(200, text="<html>a sign-in page</html>"),                                 # not a feed
    lambda: httpx.Response(503),
])
def test_a_broken_feed_still_fails_and_keeps_the_last_good_size(scan, bad):
    scan(adp=_feed(1482))
    cos, _ = scan(adp=bad)
    assert cos["adp"]["status"] != "active" and cos["adp"]["status_reason"]
    assert cos["adp"]["scan"]["feed"]["baseline"]["postings"] == 1482
    cos, _ = scan(adp=_feed(1470))
    assert cos["adp"]["status"] == "active" and cos["adp"]["scan"]["feed"]["baseline"]["postings"] == 1470


def test_the_first_scan_after_this_change_is_judged_on_its_own(scan):
    """Production's ADP record has no "feed" yet: a normal feed is simply accepted."""
    cos, _ = scan(adp=_feed(1490))
    assert cos["adp"]["status"] == "active" and cos["adp"]["scan"]["feed"]["baseline"]["postings"] == 1490


# ── ServiceNow: exactly as before ───────────────────────────────────────

def test_servicenow_has_no_size_guard_and_reads_as_before(scan):
    assert scraper.COMPANY_SOURCES["servicenow"] == [{"type": "job_feed", "url": "https://careers.servicenow.com/jobs/xml/"}]
    cos, _ = scan(servicenow=_feed(0))                                            # its own empty feed: healthy, as before
    assert cos["servicenow"]["status"] == "active" and "feed" not in cos["servicenow"]["scan"]
    cos, _ = scan(servicenow=_feed(3, india=1))
    assert cos["servicenow"]["status"] == "active" and cos["servicenow"]["scan"]["postings_seen"] == 3
    cos, _ = scan(servicenow=_feed(0))
    assert cos["servicenow"]["status"] == "active" and "feed" not in cos["servicenow"]["scan"]
    cos, _ = scan(servicenow=lambda: httpx.Response(200, text="<html>nope</html>"))
    assert cos["servicenow"]["status"] != "active"                                # malformed still fails, as before


def test_the_shared_feed_parser_is_unchanged_for_any_feed():
    """job_feed_listings itself has no size rule: the guard lives in ADP's config."""
    empty = sources.job_feed_listings(httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=_feed(0)))),
                                      {"url": "https://x.example/f"})
    assert empty.listings == [] and empty.seen == 0
