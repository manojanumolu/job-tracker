"""Personal filters (job_filters.py) and the per-user notifier
(user_alerts.py), on the in-memory backend. Nothing is emailed."""
import json
import threading
import time
from pathlib import Path

import pytest

import job_filters
import user_alerts
from config_store import alert_pending, job_key
from user_store import MemoryBackend, UserStore, UserStoreError

REPO = Path(__file__).resolve().parent.parent
A, B = "UserAaaaaaaaaaaaaaaaaaaaaaaa1", "UserBbbbbbbbbbbbbbbbbbbbbbbb2"
PASSED = {"checks": {"india": True, "experience": True}}


def job(n, company="sanofi", title="Software Engineer", location="Pune · India", category="FRESHER",
        first_seen="2030-01-02T00:00:00+00:00", evidence=PASSED, **extra):
    return {"id": f"{company}_{n}", "url": f"https://jobs.example/{company}/{n}", "company": company.title(),
            "company_id": company, "title": title, "location": location, "category": category,
            "first_seen": first_seen, "evidence": evidence, **extra}


# ---------------------------------------------------------------------------
# the shared gate comes first and is never loosened
# ---------------------------------------------------------------------------

def test_global_gate_is_exactly_todays_alert_gate():
    """passes_global_gate == alert_pending without the per-recipient state
    (sent / claimed / dismissed), on every real record and edge cases."""
    real = json.loads((REPO / "seen_jobs.json").read_text("utf-8"))
    edge = [job(1), job(2, category="EXPERIENCED"), job(3, category=None), job(4, evidence={}),
            job(5, evidence={"checks": {"india": True, "experience": False}}), job(6, evidence={"checks": {}}),
            job(7, evidence="nope"), {"title": "legacy keyword match"}, job(8, category="ENTRY_LEVEL")]
    for rec in real + edge:
        fresh = {**rec, "notified": False, "notify_state": None, "dismissed": False}
        assert job_filters.passes_global_gate(rec) is alert_pending(fresh), rec.get("title")


@pytest.mark.parametrize("title, families", [
    ("Graduate Software Engineer", {"Software engineering"}),
    ("IN_Specialist 3_ Frontend Developer_AppTech_Advisory_Pune", {"Software engineering"}),
    ("Infra Tech Support Practitioner", {"IT support & infrastructure"}),
    ("Scientific Sales Executive", {"Sales & business development", "Science & healthcare"}),
    ("Associate - Data Analyst", {"Data & analytics"}),
    ("QA Automation Tester", {"Testing & QA"}),
    ("Trainee", set()),
    ("Maintenance Fitter", set()),                    # "ai" inside a word never matches
])
def test_job_families_from_titles(title, families):
    assert job_filters.job_families({"title": title}) == families


@pytest.mark.parametrize("location, choices", [
    ("Bangalore · India", {"Bengaluru"}), ("Gurugram � India", {"Delhi NCR"}), ("Navi Mumbai, India", {"Mumbai"}),
    ("Remote - India", {"Remote"}), ("Pune; Hyderabad", {"Pune", "Hyderabad"}), ("India", set()),
    ("Punekar Road", set()),
])
def test_location_choices(location, choices):
    assert job_filters.job_location_choices({"location": location}) == choices


def test_general_and_tailored():
    sw_pune = job(1)
    data_blr = job(2, title="Data Analyst", location="Bengaluru · India", category="ENTRY_LEVEL")
    prefs = {"job_families": ["Software engineering"], "locations": ["Pune"], "experience": ["fresher"]}
    general = dict(mode="general", prefs=prefs, watch_all=True, watchlist=set())
    tailored = {**general, "mode": "tailored"}
    assert job_filters.for_person(sw_pune, **general) and job_filters.for_person(data_blr, **general)
    assert job_filters.for_person(sw_pune, **tailored) and not job_filters.for_person(data_blr, **tailored)
    any_prefs = {**tailored, "prefs": {"job_families": [], "locations": [], "experience": []}}
    assert job_filters.for_person(data_blr, **any_prefs)                       # empty = any
    assert not job_filters.for_person(data_blr, **{**tailored, "prefs": {**prefs, "job_families": []}})


def test_company_scope():
    j = job(1, company="sanofi")
    assert job_filters.in_scope(j, True, set())
    assert job_filters.in_scope(j, False, {"sanofi"}) and not job_filters.in_scope(j, False, {"pwc"})
    assert not job_filters.in_scope({**j, "company_id": None}, False, {"sanofi"})


# ---------------------------------------------------------------------------
# the notifier
# ---------------------------------------------------------------------------

class Identity:
    """Firebase Authentication as the notifier sees it, by UID."""
    def __init__(self):
        self.accounts = {}

    def account(self, uid):
        return self.accounts.get(uid)

    def google_account(self, sub):
        return None


def _person(store, uid, email, *, verified=True, watch_all=True, follow=(), mode="general", prefs=None,
            enabled=True, enabled_at="2030-01-01T00:00:00Z"):
    u = store.for_uid(uid)
    u.ensure_profile(email, "", verified)
    for c in follow:
        u.watch(c, {"sanofi", "pwc", "accenture"})
    u.set_watch_all(watch_all)
    if prefs:
        u.set_preferences(mode, **prefs)
    if enabled:
        u.set_notifications(True, mode)
        store.backend.set(f"users/{uid}", {"notifications": {"enabled": True, "mode": mode, "enabled_at": enabled_at}},
                          merge=True)
    store.identity.accounts[uid] = {"uid": uid, "email": email, "email_verified": verified, "disabled": False}
    return u


@pytest.fixture
def store():
    return UserStore(MemoryBackend(), Identity())


class Mailer:
    def __init__(self, fail_for=()):
        self.sent, self.fail_for, self.lock = [], set(fail_for), threading.Lock()

    def __call__(self, jobs, to):
        if to in self.fail_for:
            raise RuntimeError(f"SMTP refused {to}")
        with self.lock:
            self.sent.append((to, sorted(job_key(j) for j in jobs)))


def test_each_person_gets_their_own_jobs_at_their_verified_address(store):
    _person(store, A, "alice@example.org", watch_all=False, follow=["sanofi"])
    _person(store, B, "bob@example.org")
    seen = [job(1, "sanofi"), job(2, "pwc"), job(3, "pwc", category="EXPERIENCED")]
    mail = Mailer()
    stats = user_alerts.run(store, seen, mail, run_id="r1")
    assert sorted(mail.sent) == [("alice@example.org", [job_key(seen[0])]),
                                 ("bob@example.org", sorted([job_key(seen[0]), job_key(seen[1])]))]
    assert stats["sent"] == 2 and stats["sent_jobs"] == 3


def test_the_address_comes_from_firebase_not_the_profile(store):
    _person(store, A, "alice@example.org")
    store.backend.set(f"users/{A}", {"email": "attacker@evil.example"}, merge=True)   # a tampered profile
    mail = Mailer()
    user_alerts.run(store, [job(1)], mail)
    assert [to for to, _ in mail.sent] == ["alice@example.org"]


@pytest.mark.parametrize("state, outcome", [
    (None, "deleted"),
    ({"uid": A, "email": "alice@example.org", "email_verified": True, "disabled": True}, "disabled"),
    ({"uid": A, "email": "alice@example.org", "email_verified": False, "disabled": False}, "unverified"),
    ({"uid": A, "email": "", "email_verified": True, "disabled": False}, "unverified"),
])
def test_deleted_disabled_or_unverified_accounts_get_nothing(store, state, outcome):
    _person(store, A, "alice@example.org")
    store.identity.accounts[A] = state
    mail = Mailer()
    assert user_alerts.run(store, [job(1)], mail)[outcome] == 1 and mail.sent == []
    assert store.for_uid(A).delivery(job_key(job(1))) is None                  # nothing claimed either


def test_alerts_off_means_no_email(store):
    _person(store, A, "alice@example.org", enabled=False)
    mail = Mailer()
    assert user_alerts.run(store, [job(1)], mail)["off"] == 1 and mail.sent == []


def test_tailored_mode_and_dismissals(store):
    u = _person(store, A, "alice@example.org", mode="tailored",
                prefs={"job_families": ["Data & analytics"], "locations": ["Bengaluru"], "experience": []})
    seen = [job(1, title="Data Analyst", location="Bangalore · India"), job(2, title="Data Analyst"),
            job(3, title="Software Engineer", location="Bengaluru"), job(4, title="Data Scientist", location="Bengaluru")]
    u.dismiss({job_key(seen[3])})
    mail = Mailer()
    user_alerts.run(store, seen, mail)
    assert mail.sent == [("alice@example.org", [job_key(seen[0])])]


def test_only_jobs_found_after_alerts_were_turned_on(store):
    _person(store, A, "alice@example.org", enabled_at="2030-01-01T00:00:00Z")
    seen = [job(1, first_seen="2029-12-31T23:59:59+00:00"), job(2, first_seen="2030-01-01T00:00:01+00:00"),
            job(3, first_seen=None)]
    mail = Mailer()
    user_alerts.run(store, seen, mail)
    assert mail.sent == [("alice@example.org", [job_key(seen[1])])]


def test_the_shared_gate_is_never_bypassed(store):
    _person(store, A, "alice@example.org")
    seen = [job(1, category="EXPERIENCED"), job(2, evidence={"checks": {"india": False}}), job(3, category=None)]
    mail = Mailer()
    assert user_alerts.run(store, seen, mail).get("nothing") == 1 and mail.sent == []


def test_a_job_is_sent_once(store):
    _person(store, A, "alice@example.org")
    seen = [job(1)]
    mail = Mailer()
    user_alerts.run(store, seen, mail, run_id="r1")
    user_alerts.run(store, seen, mail, run_id="r2")
    user_alerts.run(store, seen + [job(2)], mail, run_id="r3")
    assert mail.sent == [("alice@example.org", [job_key(seen[0])]), ("alice@example.org", [job_key(job(2))])]
    assert store.for_uid(A).delivery(job_key(seen[0]))["state"] == "sent"


def test_a_failed_send_is_retried_and_does_not_touch_anyone_else(store):
    _person(store, A, "alice@example.org")
    _person(store, B, "bob@example.org")
    seen = [job(1)]
    flaky = Mailer(fail_for={"alice@example.org"})
    stats = user_alerts.run(store, seen, flaky, run_id="r1")
    assert stats["failed"] == 1 and stats["sent"] == 1
    assert flaky.sent == [("bob@example.org", [job_key(seen[0])])]
    assert store.for_uid(A).delivery(job_key(seen[0]))["state"] == "pending"     # released
    assert store.for_uid(B).delivery(job_key(seen[0]))["state"] == "sent"
    fixed = Mailer()
    user_alerts.run(store, seen, fixed, run_id="r2")
    assert fixed.sent == [("alice@example.org", [job_key(seen[0])])]             # retried, Bob not again


def test_failed_send_errors_never_log_the_address(store, caplog):
    _person(store, A, "alice@example.org")
    user_alerts.run(store, [job(1)], Mailer(fail_for={"alice@example.org"}))
    assert "a personal alert email failed (RuntimeError) — released for the next run" in caplog.text
    assert "alice@example.org" not in caplog.text and "a•••" not in caplog.text and A not in caplog.text
    assert "SMTP refused" not in caplog.text                              # never the exception message


def test_concurrent_runs_never_send_twice(store):
    for i in range(5):
        _person(store, f"User{i}aaaaaaaaaaaaaaaaaaaaaaa", f"user{i}@example.org")
    seen = [job(n) for n in range(4)]
    mail = Mailer()

    def slow_send(jobs, to):
        time.sleep(0.01)
        mail(jobs, to)
    threads = [threading.Thread(target=user_alerts.run, args=(store, seen, slow_send), kwargs={"run_id": f"r{i}"})
               for i in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    per_person = {}
    for to, keys in mail.sent:
        per_person.setdefault(to, []).extend(keys)
    assert set(per_person) == {f"user{i}@example.org" for i in range(5)}
    assert all(sorted(keys) == sorted(job_key(j) for j in seen) for keys in per_person.values())   # each once


def test_one_persons_storage_error_does_not_stop_the_others(store, monkeypatch):
    _person(store, A, "alice@example.org")
    _person(store, B, "bob@example.org")
    real = store.for_uid

    def flaky_for_uid(uid):
        u = real(uid)
        if uid == A:
            def boom(*a, **k):
                raise UserStoreError("unavailable")
            u.personal_view = boom
        return u
    monkeypatch.setattr(store, "for_uid", flaky_for_uid)
    mail = Mailer()
    stats = user_alerts.run(store, [job(1)], mail)
    assert stats["error"] == 1 and mail.sent == [("bob@example.org", [job_key(job(1))])]


def test_dry_run_claims_and_sends_nothing(store):
    _person(store, A, "alice@example.org")
    stats = user_alerts.run(store, [job(1)], None, dry_run=True)
    assert stats == {"would_send": 1, "would_send_jobs": 1}
    assert store.for_uid(A).delivery(job_key(job(1))) is None


def test_a_malformed_user_document_id_is_skipped(store):
    _person(store, A, "alice@example.org")
    store.backend.docs["users/__evil__"] = {"email": "x@example.org"}
    store.backend.docs["users/bad id"] = {"email": "y@example.org"}
    assert store.user_ids() == [A]


def test_cli_without_a_service_account_does_nothing(monkeypatch, capsys):
    monkeypatch.delenv("FIREBASE_SERVICE_ACCOUNT", raising=False)
    assert user_alerts.main([]) == 0 and user_alerts.main(["--send"]) == 0


def test_cli_with_a_broken_service_account_fails_without_leaking(monkeypatch, caplog):
    monkeypatch.setenv("FIREBASE_SERVICE_ACCOUNT", json.dumps({"type": "service_account", "private_key": "pk-SECRET"}))
    assert user_alerts.main([]) == 1 and "pk-SECRET" not in caplog.text


def test_the_shared_notifier_is_untouched():
    """alerts.py (the shared notifier) keeps its own gate and ledger and
    knows nothing of the personal one."""
    src = (REPO / "alerts.py").read_text("utf-8")
    assert "user_alerts" not in src and "user_store" not in src
