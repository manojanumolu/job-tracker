"""Rollback safety for the email-reliability release (Nov 2026).

The next release records shared alerts as "sending", "uncertain" and
"failed" (and personal ones the same way, with a schema marker "v": 2). If
that release were ever rolled back to this code, none of those may be
emailed again: only a record that was never claimed, or was explicitly
released ("pending"), is still pending here. The records below are exactly
what the new release writes."""
import json

import pytest

import alerts
import config_store
import repo_sync
from config_store import alert_pending
from user_store import MemoryBackend, UserStore

GATE = {"real_job_posting": True, "india_location": True, "detail_read": True, "detail_is_this_job": True,
        "no_conflicting_experience": True, "not_programme_story_talent_recruiter": True,
        "evidence_not_staff_context": True, "fresher_or_entry_evidence": True}


def _rec(n, **extra):
    return {"title": f"Graduate Engineer {n}", "url": f"https://jobs.example/sanofi/{n}", "company": "Sanofi",
            "company_id": "sanofi", "location": "Hyderabad", "category": "FRESHER", "reason": "r",
            "notified": False, "first_seen": "2099-01-01T00:00:00+00:00", "evidence": {"checks": dict(GATE)}, **extra}


# what the email-reliability release writes into seen_jobs.json
NEWER_STATES = {
    "sending": {"notify_state": "sending", "claim_id": "c1", "claimed_at": "2099-01-01T00:00:00+00:00",
                "sending_at": "2099-01-01T00:00:01+00:00"},
    "uncertain": {"notify_state": "uncertain", "claim_id": "c2", "claimed_at": "2099-01-01T00:00:00+00:00",
                  "sending_at": "2099-01-01T00:00:01+00:00"},
    "failed": {"notify_state": "failed", "send_attempts": 5, "claim_id": "c3"},
    "claimed-with-id": {"notify_state": "claimed", "claim_id": "c4", "claimed_at": "2099-01-01T00:00:00+00:00"},
    "a-future-state": {"notify_state": "queued-elsewhere"},
}
STILL_PENDING = {
    "never claimed": {},
    "released for a retry": {"notify_state": "pending", "send_attempts": 2, "released_claim": "c5",
                             "released_from": "sending", "next_attempt_at": "2099-01-01T00:15:00+00:00"},
}


@pytest.mark.parametrize("name", sorted(NEWER_STATES))
def test_newer_states_are_never_pending(name):
    assert alert_pending(_rec(1, **NEWER_STATES[name])) is False


@pytest.mark.parametrize("name", sorted(STILL_PENDING))
def test_genuinely_pending_records_still_are(name):
    assert alert_pending(_rec(1, **STILL_PENDING[name])) is True


def test_known_states_behave_exactly_as_before():
    old = lambda j: (alert_pending({**j, "notify_state": None}) and j.get("notify_state") not in ("claimed", "sent", "skipped"))
    for state in (None, "", "pending", "claimed", "sent", "skipped"):
        j = _rec(1, notify_state=state)
        assert alert_pending(j) is old(j), state


@pytest.mark.parametrize("name", sorted(NEWER_STATES))
@pytest.mark.parametrize("local", [None, "pending", "claimed"])
def test_a_merge_never_turns_a_newer_state_back_into_pending(name, local):
    remote = _rec(1, **NEWER_STATES[name])
    mine = _rec(1, **({"notify_state": local} if local else {}))
    assert repo_sync.merge_record(remote, mine)["notify_state"] == remote["notify_state"]
    assert not alert_pending(repo_sync.merge_record(remote, mine))


def test_old_alert_run_sends_only_genuinely_pending_jobs(tmp_path, monkeypatch):
    """This version's alerts.run over a ledger the newer release wrote:
    only the never-claimed and released records are emailed."""
    monkeypatch.setenv("ALERT_RECIPIENT", "me@example.com")
    monkeypatch.setattr(config_store, "SEEN_FILE", tmp_path / "seen_jobs.json")
    monkeypatch.setattr(config_store, "COMPANIES_FILE", tmp_path / "companies.json")
    monkeypatch.setattr(config_store, "SETTINGS_FILE", tmp_path / "settings.json")
    (tmp_path / "companies.json").write_text("[]")
    (tmp_path / "settings.json").write_text("{}")
    seen = ([_rec(i, **extra) for i, extra in enumerate(NEWER_STATES.values())]
            + [_rec(100 + i, **extra) for i, extra in enumerate(STILL_PENDING.values())]
            + [_rec(200, notify_state="sent", notified=True)])
    (tmp_path / "seen_jobs.json").write_text(json.dumps(seen))
    remote = {"seen": list(seen)}

    def publisher(message, local):
        remote["seen"] = repo_sync.merge_seen(remote["seen"], local["data/seen_jobs.json"])
        config_store.SEEN_FILE.write_text(json.dumps(remote["seen"]))
        return True
    sent = []
    for _ in range(3):                                      # three successive scans
        alerts.run(send=lambda jobs, to: sent.append(sorted(j["title"] for j in jobs)), publisher=publisher)
    assert sent == [["Graduate Engineer 100", "Graduate Engineer 101"]]
    by_title = {j["title"]: j for j in remote["seen"]}
    for i, name in enumerate(NEWER_STATES):
        assert by_title[f"Graduate Engineer {i}"]["notify_state"] == NEWER_STATES[name]["notify_state"], name


def test_old_personal_ledger_never_reclaims_newer_records():
    """This version's claim_delivery only claims a missing or "pending"
    record: what the newer release recorded as sending, uncertain or failed
    (or a live claim) is never claimed, so never sent again."""
    store = UserStore(MemoryBackend(), None)
    user = store.for_uid("UserAaaaaaaaaaaaaaaaaaaaaaaa1")
    docs = {"sending": {"state": "sending", "v": 2, "claim_id": "r1", "sending_at": "2099-01-01T00:00:00Z"},
            "uncertain": {"state": "uncertain", "v": 2}, "failed": {"state": "failed", "v": 2, "attempts": 5},
            "claimed": {"state": "claimed", "v": 2, "claim_id": "r2", "claimed_at": "2099-01-01T00:00:00Z"},
            "sent": {"state": "sent", "v": 2}}
    for key, doc in docs.items():
        store.backend.set(user._delivery_path(key), {"job_key": key, **doc})
        assert user.claim_delivery(key, "old-run") is False, key
    store.backend.set(user._delivery_path("retry"), {"job_key": "retry", "state": "pending", "v": 2, "attempts": 1,
                                                       "next_attempt_at": "2099-01-01T00:15:00Z"})
    assert user.claim_delivery("retry", "old-run") is True                  # a released retry is still retried
