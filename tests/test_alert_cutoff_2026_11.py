"""Audit follow-up (Nov 2026): the shared email's backlog cutoff and the "SW" /
technology-programme job-family words — checked on fakes and on the real
saved records (read-only). No network; nothing is ever emailed."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import alerts
import config_store
import job_filters
from config_store import shared_alert_cutoff, shared_alert_pending, within_shared_cutoff

from test_account1_monitoring import ACCOUNT1_TAILORING, FakeRemote, Mailer, rec, shared  # noqa: F401

REPO = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# ── the cutoff ──────────────────────────────────────────────────────────────

def test_the_cutoff_is_the_later_of_the_setting_and_the_age_limit():
    floor = NOW - config_store.SHARED_MAX_AGE
    assert shared_alert_cutoff({}, NOW) == floor
    assert shared_alert_cutoff({"shared_alert_since": "2026-11-05T00:00:00+00:00"}, NOW) == datetime(2026, 11, 5, tzinfo=timezone.utc)
    assert shared_alert_cutoff({"shared_alert_since": "2020-01-01T00:00:00Z"}, NOW) == floor     # an old setting can't reopen the backlog
    for bad in ("", "yesterday", 5, None, ["2026-11-05"]):
        assert shared_alert_cutoff({"shared_alert_since": bad}, NOW) == floor, bad


def test_backlog_is_never_claimed_but_a_retry_always_is():
    cutoff = datetime(2026, 11, 6, tzinfo=timezone.utc)
    old = rec(1, "Software Engineer", first_seen="2026-11-03T09:00:00+00:00")
    new = rec(2, "Software Engineer", first_seen="2026-11-06T10:00:00+00:00")
    retry = rec(3, "Software Engineer", first_seen="2026-11-01T09:00:00+00:00", released_claim="2026-11-01T09:05:00+00:00")
    assert not within_shared_cutoff(old, cutoff) and within_shared_cutoff(new, cutoff) and within_shared_cutoff(retry, cutoff)
    assert not shared_alert_pending(old, frozenset(), ACCOUNT1_TAILORING, cutoff)
    assert shared_alert_pending(new, frozenset(), ACCOUNT1_TAILORING, cutoff)
    assert shared_alert_pending(retry, frozenset(), ACCOUNT1_TAILORING, cutoff)
    assert shared_alert_pending(old, frozenset(), ACCOUNT1_TAILORING, None)          # no cutoff: as before


def test_widening_the_tailoring_never_floods_the_backlog(shared):  # noqa: F811
    """The 60-record case: jobs saved before the change, held back only by
    the tailoring, stay unsent when the tailoring is cleared; a job found
    after the change goes out."""
    now = datetime.now(timezone.utc)
    backlog = [rec(i, "Finance Associate", first_seen=_iso(now - timedelta(days=3))) for i in range(10, 20)]
    fresh = rec(30, "Finance Associate", first_seen=_iso(now))
    since = _iso(now - timedelta(hours=1))
    shared([dict(j) for j in backlog + [fresh]], {"shared_alert_since": since})        # tailoring removed
    remote, mail = FakeRemote(), Mailer()
    assert alerts.run(send=mail, publisher=remote) == 0
    (sent,) = mail.sent
    assert sent == ["Finance Associate"] and len(sent) == 1


def test_without_the_setting_the_age_limit_still_bounds_the_backlog(shared):  # noqa: F811
    now = datetime.now(timezone.utc)
    stale = rec(1, "Software Engineer", first_seen=_iso(now - timedelta(days=30)))
    recent = rec(2, "Python Developer", first_seen=_iso(now - timedelta(hours=2)))
    shared([dict(stale), dict(recent)], {"shared_alert_tailoring": ACCOUNT1_TAILORING})
    remote, mail = FakeRemote(), Mailer()
    assert alerts.run(send=mail, publisher=remote) == 0
    assert mail.sent == [["Python Developer"]]


def test_the_real_data_sends_no_backlog():
    """The committed settings and saved records: nothing old is claimed,
    whatever the tailoring (60 records were held back only by it)."""
    seen = json.loads((REPO / "data" / "seen_jobs.json").read_text("utf-8"))
    companies = json.loads((REPO / "data" / "companies.json").read_text("utf-8"))
    settings = json.loads((REPO / "data" / "settings.json").read_text("utf-8"))
    since = config_store._iso_time(settings.get("shared_alert_since"))
    assert since is not None
    excluded = config_store.excluded_from_shared_alerts(companies)
    cutoff = shared_alert_cutoff(settings)
    old = [j for j in seen if (config_store._iso_time(j.get("first_seen")) or since) < since]
    for tailoring in (config_store.shared_tailoring(settings), None):
        assert not [j for j in old if shared_alert_pending(j, excluded, tailoring, cutoff)]


# ── the "SW" and technology-programme words ─────────────────────────────────

@pytest.mark.parametrize("title", [
    "2027 Campus Hire_Engineer_SW", "Engineer - RF SW - M.Tech - Communication background - 2025/26",
    "Associate Engineer - RF SW", "SW Developer", "Sr. SW Engineer", "Technology Early Career Entry Program",
    "Technology Graduate Programme 2026", "Technology Associate",
])
def test_software_titles_now_in_the_software_family(title):
    assert "Software engineering" in job_filters.job_families({"title": title}), title


@pytest.mark.parametrize("title", [
    "Social Worker (SW)", "Swiss Desk Engineer", "SWAT Analyst", "Technology Sales Manager",
    "Information Technology Recruiter", "Technology Risk Lead", "Sweeper",
    # hardware / VLSI / embedded stay outside the email on purpose (shown under Explore)
    "ASIC Verification Engineer", "2027 Campus Hire_ Engineer_ HW", "Embedded Firmware Engineer",
    "Formal Verification Engineer - New College Graduate 2026",
])
def test_unrelated_and_hardware_titles_stay_out(title):
    assert "Software engineering" not in job_filters.job_families({"title": title}), title


def test_the_real_records_the_new_words_recover():
    """Read-only replay of the saved records: exactly these three software
    roles now match the Account 1 email (none is emailed: they are backlog)."""
    seen = json.loads((REPO / "data" / "seen_jobs.json").read_text("utf-8"))
    companies = json.loads((REPO / "data" / "companies.json").read_text("utf-8"))
    excluded = config_store.excluded_from_shared_alerts(companies)
    matching = sorted(j["title"] for j in seen if shared_alert_pending(j, excluded, ACCOUNT1_TAILORING))
    assert matching == sorted(["2027 Campus Hire_Engineer_SW",
                               "Engineer - RF SW - M.Tech - Communication background - 2025/26",
                               "Technology Early Career Entry Program"])


def test_the_owners_dismissed_qualcomm_role_stays_dismissed():
    seen = json.loads((REPO / "data" / "seen_jobs.json").read_text("utf-8"))
    (q,) = [j for j in seen if j.get("title") == "2027 Campus Hire_Associate Engineer_SW"]
    assert q["dismissed"] and not config_store.alert_pending(q)
