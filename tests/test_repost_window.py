"""Time-bound repost suppression (Nov 2026), with a deployment-safe start.

A posting with the same company, title and location as an earlier record but
a new ATS ID is a "repost" only while that key was sighted within
scraper.REPOST_WINDOW_DAYS (45 whole UTC days). Sightings: a record's
first_seen, and its repost_last_seen stamp — written when a scan finds the
record's own posting still listed and accepted, when a record is created,
and once, on the first scan after deployment, for every record that has no
reliable stamp (so missing history can never release a duplicate).

Investigated on the 2026-11-11 production data: every scan since 6 Nov
records 36-39 suppressions — the same open duplicate requisitions (Cisco's
several "Software Engineer" openings in Bengaluru, Deloitte's second
"Consultant | Anaplan | Bengaluru") judged again each scan. Those stay
suppressed while their recorded twin is listed; what the window changes is a
key that went quiet for more than 45 days: it no longer blocks for ever.
"""
import copy
import json
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import scraper

REPO = Path(__file__).resolve().parent.parent
WINDOW = scraper.REPOST_WINDOW_DAYS
UTC = timezone.utc
NOW = datetime(2026, 11, 11, 6, 0, tzinfo=UTC)
DAY = timedelta(days=1)


def _record(req: str, first_seen: datetime | str | None = NOW - 10 * DAY, **extra) -> dict:
    """A stored seen_jobs record for one Cisco Software Engineer requisition."""
    rec = {"title": "Software Engineer", "company": "Cisco", "location": "Bengaluru, India",
           "url": f"https://jobs.cisco.example/careers-home/jobs/{req}",
           "category": "FRESHER", "notified": True}
    rec["uid"] = scraper.job_uid(rec["company"], rec["url"])
    if first_seen is not None:
        rec["first_seen"] = first_seen if isinstance(first_seen, str) else first_seen.isoformat()
    rec.update(extra)
    return rec


def _found(req: str, title: str = "Software Engineer", location: str = "Bengaluru, India") -> dict:
    """A job the scan accepted this run (a new requisition ID)."""
    return {"title": title, "company": "Cisco", "location": location,
            "url": f"https://jobs.cisco.example/careers-home/jobs/{req}",
            "category": "FRESHER", "reason": "experience starts at 0: '0-1 year'"}


def _again(rec: dict) -> dict:
    """The recorded posting found again by a scan (same uid)."""
    return {k: rec[k] for k in ("title", "company", "location", "url", "category")}


def _stamp(d: date | datetime) -> str:
    return (d.date() if isinstance(d, datetime) else d).isoformat()


# ---------------------------------------------------------------------------
# the window itself (records that already carry a reliable stamp)
# ---------------------------------------------------------------------------

def test_repost_within_window_is_suppressed_and_counted():
    seen, stats = [_record("1001", repost_last_seen=_stamp(NOW - 10 * DAY))], Counter()
    assert scraper.merge_new_jobs(seen, [_found("2002")], now=NOW, stats=stats) == []
    assert stats["reposts_suppressed"] == 1 and stats["reposts_window_realerted"] == 0


def test_genuinely_reopened_job_is_alerted_after_the_full_window():
    quiet = NOW - (WINDOW + 1) * DAY
    seen, stats = [_record("1001", quiet - 30 * DAY, repost_last_seen=_stamp(quiet))], Counter()
    new = scraper.merge_new_jobs(seen, [_found("2002")], now=NOW, stats=stats)
    assert [j["url"] for j in new] == [_found("2002")["url"]]
    assert new[0]["notified"] is False and new[0]["first_seen"] == NOW.isoformat()
    assert stats["reposts_window_realerted"] == 1 and stats["reposts_suppressed"] == 0


@pytest.mark.parametrize("days_quiet, suppressed", [(0, True), (1, True), (WINDOW - 1, True), (WINDOW, True),
                                                    (WINDOW + 1, False), (WINDOW + 30, False)])
def test_window_edges_in_whole_utc_days(days_quiet, suppressed):
    # last sighted on day D: suppressed through D + 45 inclusive, alerted from D + 46
    seen = [_record("1001", NOW - 400 * DAY, repost_last_seen=_stamp(NOW - days_quiet * DAY))]
    assert (scraper.merge_new_jobs(seen, [_found("2002")], now=NOW) == []) is suppressed


def test_continuously_active_duplicate_stays_suppressed_however_old_its_twin():
    # the Cisco case: the recorded posting (first seen long ago) is STILL listed —
    # found again in this scan — so a same-key new requisition is judged against today
    old = _record("1001", NOW - 300 * DAY, repost_last_seen=_stamp(NOW - (WINDOW + 50) * DAY))
    stats = Counter()
    assert scraper.merge_new_jobs([old], [_again(old), _found("2002")], now=NOW, stats=stats) == []
    assert old["repost_last_seen"] == _stamp(NOW)
    assert stats["reposts_suppressed"] == 1 and stats["repost_stamps_updated"] == 1


def test_daily_sightings_keep_a_duplicate_suppressed_for_months():
    twin = _record("1001", NOW, repost_last_seen=_stamp(NOW))
    for d in range(1, 200, 3):                                 # scanned every few days for ~6.5 months
        now = NOW + d * DAY
        assert scraper.merge_new_jobs([twin], [_again(twin), _found("2002")], now=now) == []
    assert twin["repost_last_seen"] == _stamp(NOW + 199 * DAY)


def test_suppression_itself_never_stamps_so_the_window_can_expire():
    seen, stats = [_record("1001", repost_last_seen=_stamp(NOW - 10 * DAY))], Counter()
    scraper.merge_new_jobs(seen, [_found("2002")], now=NOW, stats=stats)
    assert seen[0]["repost_last_seen"] == _stamp(NOW - 10 * DAY) and stats["repost_stamps_updated"] == 0


def test_after_a_realert_a_second_same_key_candidate_in_the_scan_is_its_repost():
    quiet = NOW - (WINDOW + 2) * DAY
    seen, stats = [_record("1001", quiet, repost_last_seen=_stamp(quiet))], Counter()
    new = scraper.merge_new_jobs(seen, [_found("2002"), _found("3003")], now=NOW, stats=stats)
    assert len(new) == 1 and stats["reposts_window_realerted"] == 1 and stats["reposts_suppressed"] == 1


def test_different_location_is_never_a_repost_whatever_the_window():
    seen = [_record("1001", repost_last_seen=_stamp(NOW))]
    new = scraper.merge_new_jobs(seen, [_found("2002", location="Pune, India")], now=NOW)
    assert [j["location"] for j in new] == ["Pune, India"]


def test_new_records_are_stamped_when_created():
    new = scraper.merge_new_jobs([], [_found("2002")], now=NOW)
    assert new[0]["repost_last_seen"] == _stamp(NOW)


# ---------------------------------------------------------------------------
# deployment: missing or unreliable history never releases a duplicate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("deploy", ["2026-11-11", "2026-11-14", "2026-11-15", "2026-12-01", "2027-03-01",
                                    "2028-01-01"])
@pytest.mark.parametrize("first_seen", [None, "2026-09-30T08:00:00+00:00", "2026-11-01T23:30:00-05:00", "garbage"])
def test_record_without_a_stamp_counts_as_sighted_on_deployment_day(deploy, first_seen):
    deploy_at = datetime.fromisoformat(deploy + "T21:02:00+00:00")
    rec = _record("1001", first_seen=first_seen)
    stats = Counter()
    # first scan after deployment: the duplicate is NOT released, however old first_seen is
    assert scraper.merge_new_jobs([rec], [_found("2002")], now=deploy_at, stats=stats) == []
    assert rec["repost_last_seen"] == deploy and stats["repost_dates_backfilled"] == 1
    # still suppressed on the last day of the window, even if never sighted again ...
    assert scraper.merge_new_jobs([rec], [_found("2002")], now=deploy_at + WINDOW * DAY) == []
    # ... and released the day after: a genuinely inactive key does come back
    assert len(scraper.merge_new_jobs([rec], [_found("2002")], now=deploy_at + (WINDOW + 1) * DAY)) == 1


@pytest.mark.parametrize("bad_stamp", ["", "11/11/2026", "2026-13-01", "2026-11-31", "2026-11-11T00:00:00Z",
                                       "2099-01-01", 20261111, None])
def test_unreliable_stamps_are_treated_as_missing(bad_stamp):
    rec = _record("1001", NOW - 300 * DAY)
    rec["repost_last_seen"] = bad_stamp
    assert scraper.merge_new_jobs([rec], [_found("2002")], now=NOW) == []
    assert rec["repost_last_seen"] == _stamp(NOW)


def test_backfill_is_once_only_and_never_moves_a_reliable_stamp():
    old = _record("1001", NOW - 300 * DAY)
    kept = _record("1002", NOW - 300 * DAY, repost_last_seen=_stamp(NOW - 40 * DAY))
    stats = Counter()
    assert scraper.backfill_repost_dates([old, kept], NOW, stats) == 1
    assert old["repost_last_seen"] == _stamp(NOW) and kept["repost_last_seen"] == _stamp(NOW - 40 * DAY)
    # a later scan doesn't restamp it: the 45 days started at deployment
    assert scraper.backfill_repost_dates([old, kept], NOW + 20 * DAY, stats) == 0
    assert old["repost_last_seen"] == _stamp(NOW) and stats["repost_dates_backfilled"] == 1


# ---------------------------------------------------------------------------
# time zones and the UTC day boundary
# ---------------------------------------------------------------------------

def test_first_seen_with_an_offset_is_read_as_its_utc_day():
    # 23:30 at UTC-05:00 on 27 Sep is 04:30 UTC on 28 Sep
    assert scraper._first_seen_day("2026-09-27T23:30:00-05:00") == date(2026, 9, 28)
    assert scraper._first_seen_day("2026-09-28T03:00:00+05:30") == date(2026, 9, 27)
    assert scraper._first_seen_day("2026-09-28T03:00:00Z") == date(2026, 9, 28)
    assert scraper._first_seen_day("2026-09-28T03:00:00") == date(2026, 9, 28)        # naive = UTC
    assert scraper._first_seen_day("not a time") is None


def test_scan_time_with_an_offset_uses_the_utc_day():
    # 01:00 IST on 12 Nov is 19:30 UTC on 11 Nov: stamped 2026-11-11
    ist = timezone(timedelta(hours=5, minutes=30))
    new = scraper.merge_new_jobs([], [_found("2002")], now=datetime(2026, 11, 12, 1, 0, tzinfo=ist))
    assert new[0]["repost_last_seen"] == "2026-11-11"


@pytest.mark.parametrize("clock, suppressed", [("23:59:59", True), ("00:00:00+1", False)])
def test_release_happens_exactly_at_utc_midnight(clock, suppressed):
    last = date(2026, 9, 1)
    rec = _record("1001", "2026-08-01T00:00:00+00:00", repost_last_seen=last.isoformat())
    day = last + timedelta(days=WINDOW) + (timedelta(days=1) if clock.endswith("+1") else timedelta())
    now = datetime.fromisoformat(f"{day.isoformat()}T{clock.removesuffix('+1')}+00:00")
    assert (scraper.merge_new_jobs([rec], [_found("2002")], now=now) == []) is suppressed


def test_a_scan_crossing_midnight_stamps_each_record_with_its_own_utc_day():
    a, b = _record("1001", repost_last_seen="2026-11-01"), _record("1002", repost_last_seen="2026-11-01")
    scraper.merge_new_jobs([a, b], [_again(a)], now=datetime(2026, 11, 11, 23, 59, tzinfo=UTC))
    scraper.merge_new_jobs([a, b], [_again(b)], now=datetime(2026, 11, 12, 0, 1, tzinfo=UTC))
    assert (a["repost_last_seen"], b["repost_last_seen"]) == ("2026-11-11", "2026-11-12")


# ---------------------------------------------------------------------------
# the production data: no deployment date releases anything on day one
# ---------------------------------------------------------------------------

def _production_seen() -> list[dict]:
    return [j for j in json.loads((REPO / "data" / "seen_jobs.json").read_text("utf-8")) if isinstance(j, dict)]


def _duplicate_of_every_key(seen: list[dict]) -> list[dict]:
    """Worst case: a NEW same-company/title/location requisition for every
    recorded key, and none of the recorded postings found again."""
    out = []
    for i, j in enumerate(seen):
        if scraper._repost_key(j):
            out.append({"title": j.get("title", ""), "company": j.get("company", ""), "location": j.get("location", ""),
                        "url": f"https://replay.example/careers-home/jobs/{900000 + i}", "category": "FRESHER",
                        "reason": "replay"})
    return out


@pytest.mark.parametrize("deploy", ["2026-11-11", "2026-11-14", "2026-11-25", "2026-12-01", "2027-02-01",
                                   "2027-11-11"])
def test_production_data_releases_nothing_on_any_deployment_day(deploy):
    seen = copy.deepcopy(_production_seen())
    found = _duplicate_of_every_key(seen)
    assert len(found) > 50                                          # the real file, not an empty fixture
    deploy_at = datetime.fromisoformat(deploy + "T21:02:00+00:00")
    stats = Counter()
    assert scraper.merge_new_jobs(seen, found, now=deploy_at, stats=stats) == []
    assert stats["reposts_window_realerted"] == 0
    # and nothing for the whole window afterwards, even if no twin is ever seen again
    assert scraper.merge_new_jobs(seen, found, now=deploy_at + WINDOW * DAY) == []


def test_production_data_keys_come_back_only_after_the_full_window():
    seen = copy.deepcopy(_production_seen())
    found = _duplicate_of_every_key(seen)
    deploy_at = datetime(2026, 11, 14, 21, 2, tzinfo=UTC)
    scraper.merge_new_jobs(seen, [], now=deploy_at)                  # the deployment scan: backfill only
    keys = {scraper._repost_key(j) for j in found}
    later = scraper.merge_new_jobs(seen, found, now=deploy_at + (WINDOW + 1) * DAY)
    assert {scraper._repost_key(j) for j in later} == keys          # every quiet key is free again, once each


# ---------------------------------------------------------------------------
# persistence: run_all saves stamps; the publish merge never moves one back
# ---------------------------------------------------------------------------

def _run_all_with(monkeypatch, seen, jobs):
    company = {"id": "cisco", "name": "Cisco", "url": "https://jobs.cisco.example"}
    saved = {}
    fake = scraper.Scan("Cisco")
    fake.jobs, fake.sources_ok = jobs, 1
    monkeypatch.setattr(scraper, "scan_company", lambda c: fake)
    monkeypatch.setattr("config_store.load_companies", lambda: [company])
    monkeypatch.setattr("config_store.load_seen_jobs", lambda: seen)
    monkeypatch.setattr("config_store.save_companies", lambda data, commit=True: saved.setdefault("companies", data))
    monkeypatch.setattr("config_store.save_seen_jobs", lambda data, commit=True: saved.setdefault("seen", data))
    monkeypatch.setattr(scraper.time, "sleep", lambda s: None)
    assert scraper.run_all() == []
    return saved


def test_run_all_saves_seen_jobs_when_only_stamps_changed(monkeypatch):
    old = _record("1001", repost_last_seen="2026-11-01")
    saved = _run_all_with(monkeypatch, [old], [_again(old)])
    assert "seen" in saved and saved["seen"][0]["repost_last_seen"] != "2026-11-01"
    run = saved["companies"][0]["scan"]["run"]
    assert run["reposts_suppressed"] == 0 and run["reposts_window_realerted"] == 0 and run["repost_dates_backfilled"] == 0


def test_run_all_saves_the_deployment_backfill(monkeypatch):
    legacy = _record("1001", NOW - 300 * DAY)                       # saved before deployment: no stamp
    saved = _run_all_with(monkeypatch, [legacy], [])
    assert "seen" in saved and scraper._stamp_day(saved["seen"][0]["repost_last_seen"],
                                                  datetime.now(UTC).date()) is not None
    assert saved["companies"][0]["scan"]["run"]["repost_dates_backfilled"] == 1


def test_publish_merge_never_moves_a_sighting_backwards():
    import repo_sync
    base = _record("1001")
    newer, older = dict(base, repost_last_seen="2026-11-11"), dict(base, repost_last_seen="2026-10-01")
    assert repo_sync.merge_record(newer, older)["repost_last_seen"] == "2026-11-11"     # remote newer
    assert repo_sync.merge_record(older, newer)["repost_last_seen"] == "2026-11-11"     # local newer
    assert repo_sync.merge_record(base, newer)["repost_last_seen"] == "2026-11-11"      # first stamp
    assert "repost_last_seen" not in repo_sync.merge_record(base, dict(base))            # nothing invented
