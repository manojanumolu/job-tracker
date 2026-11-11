"""Time-bound repost suppression (Nov 2026).

A posting with the same title and location as an earlier record but a new
ATS ID is a "repost" only while that key was sighted within
scraper.REPOST_WINDOW_DAYS. The sightings are the record's first_seen and
its repost_last_seen stamp, written when a scan finds the record's own
posting (same uid) still listed — never when a candidate is suppressed, or
the suppression would renew itself and the window could never expire.

Investigated on the 2026-11-11 production data: every scan since 6 Nov
records 36-39 suppressions — the same open duplicate requisitions (Cisco's
several "Software Engineer" openings in Bengaluru, Deloitte's second
"Consultant | Anaplan | Bengaluru") judged again each scan. Those stay
suppressed while their recorded twin is still listed (tested below); what
the window changes is a key that went quiet: it no longer blocks for ever.
"""
from collections import Counter
from datetime import datetime, timedelta, timezone

import scraper

NOW = datetime(2026, 11, 11, 6, 0, tzinfo=timezone.utc)
DAY = timedelta(days=1)
WINDOW = timedelta(days=scraper.REPOST_WINDOW_DAYS)


def _record(req: str, first_seen: datetime | None = NOW - 10 * DAY, **extra) -> dict:
    """A stored seen_jobs record for one Cisco Software Engineer requisition."""
    rec = {"title": "Software Engineer", "company": "Cisco", "location": "Bengaluru, India",
           "url": f"https://jobs.cisco.example/careers-home/jobs/{req}",
           "category": "FRESHER", "notified": True}
    rec["uid"] = scraper.job_uid(rec["company"], rec["url"])
    if first_seen is not None:
        rec["first_seen"] = first_seen.isoformat()
    rec.update(extra)
    return rec


def _found(req: str, title: str = "Software Engineer", location: str = "Bengaluru, India") -> dict:
    """A job the scan accepted this run (a new requisition ID)."""
    return {"title": title, "company": "Cisco", "location": location,
            "url": f"https://jobs.cisco.example/careers-home/jobs/{req}",
            "category": "FRESHER", "reason": "experience starts at 0: '0-1 year'"}


def test_repost_within_window_is_suppressed_and_counted():
    seen, stats = [_record("1001", NOW - 10 * DAY)], Counter()
    assert scraper.merge_new_jobs(seen, [_found("2002")], now=NOW, stats=stats) == []
    assert stats["reposts_suppressed"] == 1 and stats["reposts_window_realerted"] == 0


def test_key_quiet_past_the_window_is_alerted_as_new():
    seen, stats = [_record("1001", NOW - WINDOW - 2 * DAY)], Counter()
    new = scraper.merge_new_jobs(seen, [_found("2002")], now=NOW, stats=stats)
    assert [j["url"] for j in new] == [_found("2002")["url"]]
    assert new[0]["notified"] is False and new[0]["first_seen"] == NOW.isoformat()
    assert stats["reposts_window_realerted"] == 1 and stats["reposts_suppressed"] == 0


def test_key_quiet_just_inside_the_window_is_still_a_repost():
    seen = [_record("1001", NOW - WINDOW + DAY)]
    assert scraper.merge_new_jobs(seen, [_found("2002")], now=NOW) == []


def test_still_listed_posting_keeps_its_key_suppressing_concurrent_duplicates():
    # the Cisco case: the recorded posting (old first_seen) is STILL listed —
    # it appears in this scan's found jobs — so a same-key new requisition in
    # the same scan is judged against a sighting from today, not from months ago
    old = _record("1001", NOW - WINDOW - 30 * DAY)
    found_again = {k: old[k] for k in ("title", "company", "location", "url", "category")}
    stats = Counter()
    assert scraper.merge_new_jobs([old], [found_again, _found("2002")], now=NOW, stats=stats) == []
    assert old["repost_last_seen"] == "2026-11-11"
    assert stats["reposts_suppressed"] == 1 and stats["repost_stamps_updated"] == 1


def test_fresh_stamp_keeps_the_window_sliding():
    seen = [_record("1001", NOW - WINDOW - 30 * DAY, repost_last_seen=f"{NOW - 2 * DAY:%Y-%m-%d}")]
    assert scraper.merge_new_jobs(seen, [_found("2002")], now=NOW) == []


def test_stale_stamp_and_stale_first_seen_realert():
    seen = [_record("1001", NOW - WINDOW - 30 * DAY, repost_last_seen=f"{NOW - WINDOW - 2 * DAY:%Y-%m-%d}")]
    assert len(scraper.merge_new_jobs(seen, [_found("2002")], now=NOW)) == 1


def test_suppression_itself_never_stamps_so_the_window_can_expire():
    # a suppressed candidate must not refresh the key, or a rotation would be
    # suppressed for ever again (the very bug this change removes)
    seen, stats = [_record("1001", NOW - 10 * DAY)], Counter()
    scraper.merge_new_jobs(seen, [_found("2002")], now=NOW, stats=stats)
    assert "repost_last_seen" not in seen[0] and stats["repost_stamps_updated"] == 0


def test_stamp_is_day_granular_and_counted_once():
    old = _record("1001", NOW - 10 * DAY)
    found_again = {k: old[k] for k in ("title", "company", "location", "url", "category")}
    stats = Counter()
    scraper.merge_new_jobs([old], [found_again], now=NOW, stats=stats)
    scraper.merge_new_jobs([old], [found_again], now=NOW + timedelta(hours=3), stats=stats)
    assert stats["repost_stamps_updated"] == 1                    # same UTC day: written once
    scraper.merge_new_jobs([old], [found_again], now=NOW + DAY, stats=stats)
    assert stats["repost_stamps_updated"] == 2 and old["repost_last_seen"] == "2026-11-12"


def test_legacy_record_without_any_date_keeps_todays_behaviour():
    # no first_seen and no stamp: the sighting is unknown, so the repost is
    # suppressed exactly as before this change — nothing old re-emails
    seen, stats = [_record("1001", first_seen=None)], Counter()
    assert scraper.merge_new_jobs(seen, [_found("2002")], now=NOW, stats=stats) == []
    assert stats["reposts_suppressed"] == 1 and stats["reposts_window_realerted"] == 0


def test_after_a_realert_a_second_same_key_candidate_in_the_scan_is_its_repost():
    seen, stats = [_record("1001", NOW - WINDOW - 2 * DAY)], Counter()
    new = scraper.merge_new_jobs(seen, [_found("2002"), _found("3003")], now=NOW, stats=stats)
    assert len(new) == 1 and stats["reposts_window_realerted"] == 1 and stats["reposts_suppressed"] == 1


def test_different_location_is_never_a_repost_whatever_the_window():
    seen = [_record("1001", NOW - 5 * DAY)]
    new = scraper.merge_new_jobs(seen, [_found("2002", location="Pune, India")], now=NOW)
    assert [j["location"] for j in new] == ["Pune, India"]


def test_run_all_saves_seen_jobs_when_only_stamps_changed(monkeypatch, tmp_path):
    old = _record("1001", NOW - 10 * DAY)
    found_again = {k: old[k] for k in ("title", "company", "location", "url", "category")}
    company = {"id": "cisco", "name": "Cisco", "url": "https://jobs.cisco.example"}
    saved = {}
    fake = scraper.Scan("Cisco")
    fake.jobs, fake.sources_ok = [dict(found_again)], 1
    monkeypatch.setattr(scraper, "scan_company", lambda c: fake)
    monkeypatch.setattr("config_store.load_companies", lambda: [company])
    monkeypatch.setattr("config_store.load_seen_jobs", lambda: [old])
    monkeypatch.setattr("config_store.save_companies", lambda data, commit=True: saved.setdefault("companies", data))
    monkeypatch.setattr("config_store.save_seen_jobs", lambda data, commit=True: saved.setdefault("seen", data))
    monkeypatch.setattr(scraper.time, "sleep", lambda s: None)
    assert scraper.run_all() == []                     # nothing new — but the stamp must still be persisted
    assert "seen" in saved and saved["seen"][0].get("repost_last_seen")
    run = saved["companies"][0]["scan"]["run"]
    assert run["reposts_suppressed"] == 0 and run["reposts_window_realerted"] == 0


def test_run_record_counts_realerts():
    seen, stats = [_record("1001", NOW - WINDOW - 2 * DAY)], Counter()
    scraper.merge_new_jobs(seen, [_found("2002")], now=NOW, stats=stats)
    assert dict(stats) == {"reposts_window_realerted": 1}


def test_publish_merge_never_moves_a_sighting_backwards():
    import repo_sync
    base = _record("1001")
    newer, older = dict(base, repost_last_seen="2026-11-11"), dict(base, repost_last_seen="2026-10-01")
    assert repo_sync.merge_record(newer, older)["repost_last_seen"] == "2026-11-11"     # remote newer
    assert repo_sync.merge_record(older, newer)["repost_last_seen"] == "2026-11-11"     # local newer
    assert repo_sync.merge_record(base, newer)["repost_last_seen"] == "2026-11-11"      # first stamp
    assert "repost_last_seen" not in repo_sync.merge_record(base, dict(base))            # nothing invented


def test_production_data_has_no_key_outside_the_window_on_deploy_day():
    # deploying the window must not release a burst of re-alerts: on
    # 2026-11-11 the oldest dated record (2026-09-30) is inside 45 days
    import json
    from pathlib import Path
    seen = json.loads((Path(__file__).resolve().parent.parent / "data" / "seen_jobs.json").read_text("utf-8"))
    # (deterministic as the file grows: later records and stamps only make a
    # key's last sighting newer, never older than it was on that day)
    deploy_day = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)
    keys = {}
    for j in seen:
        k = scraper._repost_key(j) if isinstance(j, dict) else None
        if k:
            keys.setdefault(k, []).append(j)
    expired = [k for k, v in keys.items()
               if (s := scraper._last_sighting(v)) and deploy_day - s > timedelta(days=scraper.REPOST_WINDOW_DAYS)]
    assert expired == []
