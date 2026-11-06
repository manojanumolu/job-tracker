"""Audit follow-up (Nov 2026): posting dates are stored only when a source
states one, and an alert says "new" only for a recently posted vacancy. No
network; nothing is ever emailed."""
from datetime import datetime, timezone

import httpx
import pytest

import notifier
import scraper
import sources

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)


# ── posting dates ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("value, expected", [
    ("2026-11-03", "2026-11-03"), ("2026-11-03T10:15:00Z", "2026-11-03"), ("2026-11-03T10:15:00.000+05:30", "2026-11-03"),
    ("November 4, 2026", "2026-11-04"), ("Nov 4, 2026", "2026-11-04"), ("4 November 2026", "2026-11-04"),
    (1793664000, "2026-11-03"), (1793664000000, "2026-11-03"),
    ("Posted 30+ Days Ago", ""), ("Posted Today", ""), ("", ""), (None, ""), (True, ""), ("2026-13-45", ""),
    ("2099-01-01", ""), ("1999-12-31", ""), (-5, ""), ({"date": "2026-11-03"}, ""),
])
def test_a_posting_date_is_read_only_when_certain(value, expected):
    assert sources.posting_date(value, now=NOW) == expected


def _workday(info):
    def handler(request):
        return httpx.Response(200, json={"jobPostingInfo": info})
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_workday_stores_its_posting_date_and_never_invents_one():
    info = {"title": "Software Engineer", "jobDescription": "<p>" + "Build services. " * 10 + "</p>",
            "location": "Hyderabad", "country": {"descriptor": "India"}}
    d = sources.workday_detail(_workday({**info, "startDate": "2026-11-02"}), "https://x/job/1", "Software Engineer")
    assert d.ok and d.posted_at == "2026-11-02"
    d = sources.workday_detail(_workday(info), "https://x/job/1", "Software Engineer")
    assert d.ok and d.posted_at == ""


def test_the_record_carries_the_posting_date_only_when_stated():
    desc = "Freshers welcome. 0-1 years of experience. Build and run reliable services. " * 3
    detail = sources.Detail(ok=True, description=desc, location="Hyderabad, India", posting_evidence=True,
                            matched="test", posted_at="2026-11-02")
    listing = sources.Listing(title="Software Engineer", url="https://x.example/j/1", location="Hyderabad, India",
                              posting_evidence=True, detail=detail)
    result, job = scraper.decide("X", listing, detail)
    assert job and job["posted_at"] == "2026-11-02"
    detail.posted_at = ""
    result, job = scraper.decide("X", listing, detail)
    assert job and "posted_at" not in job


def test_an_alert_says_new_only_for_a_recent_posting():
    found = "2026-11-06T09:00:00+00:00"
    assert notifier.freshness({"posted_at": "2026-11-05", "first_seen": found}) == "new"
    assert notifier.freshness({"posted_at": "2026-09-01", "first_seen": found}) == "older"
    assert notifier.freshness({"first_seen": found}) == "unknown"
