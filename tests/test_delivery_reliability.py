"""Email delivery reliability (Milestone 2, Nov 2026): crash recovery, retries,
concurrent workers and account isolation — for both ledgers:

  * the shared email   (alerts.py, notify_state in seen_jobs.json, published
                        through git: every publish is a compare-and-swap)
  * personal emails    (user_alerts.py, users/{uid}/deliveries in Firestore:
                        every transition is a compare-and-set)

Nothing here sends an email or touches a real repository or Firestore: the
publisher, the mailer and the store are in-memory fakes. A "crash" is a
BaseException raised at the step being tested, so no except-Exception
handler in the code can turn it into a clean outcome.
"""
import json
import smtplib
import socket
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest

import alerts
import config_store
import notifier
import repo_sync
import user_alerts
from config_store import CLAIM_TTL, MAX_SEND_ATTEMPTS, delivery_health, job_key, retry_backoff
from notifier import DeliveryNotSent, DeliveryUncertain
from user_store import (DELIVERY_CLAIM_TTL_S, DELIVERY_MAX_ATTEMPTS, DELIVERY_SCHEMA, MemoryBackend, UserStore,
                        UserStoreError)

T0 = datetime(2030, 3, 1, 6, 0, tzinfo=timezone.utc)
RECIPIENT = "shared-recipient@example.com"
GATE = {"real_job_posting": True, "india_location": True, "detail_read": True, "detail_is_this_job": True,
        "no_conflicting_experience": True, "not_programme_story_talent_recruiter": True,
        "evidence_not_staff_context": True, "fresher_or_entry_evidence": True}


class Crash(BaseException):
    """The process dying at this point (SIGKILL, runner lost, OOM)."""


def _rec(n, **extra):
    return {"title": f"Graduate Engineer {n}", "url": f"https://jobs.example/sanofi/{n}", "company": "Sanofi",
            "location": "Hyderabad", "category": "FRESHER", "reason": "r", "notified": False,
            "first_seen": "2030-02-28T00:00:00+00:00", "evidence": {"checks": dict(GATE)}, **extra}


# ===========================================================================
# shared email (alerts.py)
# ===========================================================================

class Remote:
    """repo_sync.publish stand-in: merges into an in-memory remote and writes
    the merged copy locally. ``fail``: message prefixes whose push fails;
    ``crash``: message prefixes at which the process dies before pushing."""

    def __init__(self, seen):
        self.seen = list(seen)
        self.fail, self.crash, self.calls = set(), set(), []
        self.before = {}            # message prefix -> callable run before that push lands (interleaving)

    def __call__(self, message, local):
        self.calls.append(message)
        for prefix, hook in list(self.before.items()):
            if message.startswith(prefix):
                del self.before[prefix]
                hook()
        if any(message.startswith(c) for c in self.crash):
            raise Crash(message)
        if any(message.startswith(f) for f in self.fail):
            return False
        self.seen = repo_sync.merge_seen(self.seen, local["data/seen_jobs.json"])
        config_store.SEEN_FILE.write_text(json.dumps(self.seen))
        return True

    def state(self, n):
        return next(j for j in self.seen if j["url"].endswith(f"/{n}"))


class Mail:
    """send_alerts stand-in. ``plan``: one entry per call — None (accepted),
    an exception instance to raise, or "crash"."""

    def __init__(self, *plan):
        self.plan, self.sent, self.calls = list(plan), [], 0

    def __call__(self, jobs, recipient):
        self.calls += 1
        step = self.plan.pop(0) if self.plan else None
        if step == "crash":
            raise Crash("died inside send")
        if isinstance(step, BaseException):
            raise step
        self.sent.append(sorted(j["title"] for j in jobs))


@pytest.fixture
def shared(tmp_path, monkeypatch):
    monkeypatch.setenv("ALERT_RECIPIENT", RECIPIENT)
    monkeypatch.setattr(config_store, "SEEN_FILE", tmp_path / "seen_jobs.json")
    monkeypatch.setattr(config_store, "COMPANIES_FILE", tmp_path / "companies.json")
    monkeypatch.setattr(config_store, "SETTINGS_FILE", tmp_path / "settings.json")
    (tmp_path / "companies.json").write_text("[]")
    (tmp_path / "settings.json").write_text("{}")

    def make(*records):
        (tmp_path / "seen_jobs.json").write_text(json.dumps(list(records)))
        return Remote(records)
    return make


def _run(remote, mail, now, run_id):
    try:
        return alerts.run(send=mail, publisher=remote, now=now, run_id=run_id)
    except Crash:
        config_store.SEEN_FILE.write_text(json.dumps(remote.seen))   # the next run checks out the remote
        return "crashed"


def test_happy_path_and_no_duplicates_across_successive_scans(shared):
    remote, mail = shared(_rec(1), _rec(2)), Mail()
    assert _run(remote, mail, T0, "r1") == 0
    for i in range(2, 6):
        assert _run(remote, mail, T0 + timedelta(hours=i), f"r{i}") == 0
    assert mail.sent == [["Graduate Engineer 1", "Graduate Engineer 2"]]
    assert remote.state(1)["notify_state"] == "sent" and remote.state(1)["notified"] is True
    assert remote.calls[:3] == [alerts.DATA_MESSAGE, alerts.SENDING_MESSAGE, alerts.SENT_MESSAGE]


def test_crash_before_sending_is_recovered_and_sent_once(shared):
    """Died after claiming, before anything was marked "sending": the claim
    is abandoned, and a later run recovers and sends it — once."""
    remote, mail = shared(_rec(1)), Mail()
    remote.crash.add(alerts.SENDING_MESSAGE)
    assert _run(remote, mail, T0, "r1") == "crashed"
    assert remote.state(1)["notify_state"] == "claimed" and mail.calls == 0
    remote.crash.clear()
    assert _run(remote, mail, T0 + timedelta(minutes=30), "r2") == 0        # within the TTL: left alone
    assert mail.calls == 0 and remote.state(1)["claim_id"] == "r1"
    assert _run(remote, mail, T0 + CLAIM_TTL + timedelta(minutes=1), "r3") == 0
    assert mail.sent == [["Graduate Engineer 1"]] and remote.state(1)["notify_state"] == "sent"
    for i in range(4, 8):
        _run(remote, mail, T0 + timedelta(hours=i * 3), f"r{i}")
    assert mail.calls == 1


def test_failed_sending_publish_sends_nothing(shared):
    remote, mail = shared(_rec(1)), Mail()
    remote.fail.add(alerts.SENDING_MESSAGE)
    assert _run(remote, mail, T0, "r1") == 1 and mail.calls == 0


def test_crash_during_sending_is_never_resent(shared):
    """Died inside the SMTP conversation: Gmail may have the message. The job
    stays "sending" and is settled as "uncertain" — never sent again."""
    remote, mail = shared(_rec(1)), Mail("crash")
    assert _run(remote, mail, T0, "r1") == "crashed"
    assert remote.state(1)["notify_state"] == "sending"
    for i in range(1, 6):
        _run(remote, mail, T0 + CLAIM_TTL * i + timedelta(minutes=1), f"r{i + 1}")
    assert mail.calls == 1 and remote.state(1)["notify_state"] == "uncertain"
    assert delivery_health(remote.seen, T0 + timedelta(days=1))["uncertain"] == 1


def test_crash_after_sending_before_finalization_is_never_resent(shared):
    """The uncertainty window: Gmail accepted the email, the run died before
    "sent" was recorded. At most once means it is not resent — and it is
    counted as uncertain, not silently as sent."""
    remote, mail = shared(_rec(1)), Mail()
    remote.crash.add(alerts.SENT_MESSAGE)
    assert _run(remote, mail, T0, "r1") == "crashed"
    assert mail.sent == [["Graduate Engineer 1"]] and remote.state(1)["notify_state"] == "sending"
    remote.crash.clear()
    for i in range(1, 4):
        _run(remote, mail, T0 + CLAIM_TTL * i + timedelta(minutes=1), f"r{i + 1}")
    assert mail.calls == 1 and remote.state(1)["notify_state"] == "uncertain"


def test_failed_final_publish_after_success_is_never_resent(shared):
    remote, mail = shared(_rec(1)), Mail()
    remote.fail.add(alerts.SENT_MESSAGE)
    assert _run(remote, mail, T0, "r1") == 1
    remote.fail.clear()
    for i in range(1, 4):
        _run(remote, mail, T0 + CLAIM_TTL * i + timedelta(minutes=1), f"r{i + 1}")
    assert mail.calls == 1


def test_gmail_rejection_retries_with_backoff_then_gives_up(shared):
    remote = shared(_rec(1))
    mail = Mail(*[DeliveryNotSent("rcpt", permanent=True)] * 10)
    now, calls_at = T0, []
    for i in range(30):
        before = mail.calls
        _run(remote, mail, now, f"r{i}")
        if mail.calls > before:
            calls_at.append(now)
        now += timedelta(minutes=15)
    assert mail.calls == MAX_SEND_ATTEMPTS and mail.sent == []
    rec = remote.state(1)
    assert rec["notify_state"] == "failed" and rec["send_attempts"] == MAX_SEND_ATTEMPTS
    gaps = [b - a for a, b in zip(calls_at, calls_at[1:])]
    assert all(g >= retry_backoff(n + 1) for n, g in enumerate(gaps)), gaps     # never earlier than the backoff
    assert [retry_backoff(n) for n in range(1, 6)] == [timedelta(minutes=15), timedelta(minutes=30),
                                                       timedelta(hours=1), timedelta(hours=2), timedelta(hours=2)]


def test_retry_succeeds_after_a_transient_failure(shared):
    remote = shared(_rec(1))
    mail = Mail(DeliveryNotSent("connect"), None)
    assert _run(remote, mail, T0, "r1") == 1
    assert remote.state(1)["notify_state"] in (None, "pending") and remote.state(1)["send_attempts"] == 1
    assert _run(remote, mail, T0 + timedelta(minutes=5), "r2") == 0 and mail.calls == 1     # still backing off
    assert _run(remote, mail, T0 + timedelta(minutes=16), "r3") == 0
    assert mail.sent == [["Graduate Engineer 1"]] and remote.state(1)["notify_state"] == "sent"


def test_network_timeout_after_handover_is_uncertain(shared):
    remote, mail = shared(_rec(1)), Mail(DeliveryUncertain("data"))
    assert _run(remote, mail, T0, "r1") == 1
    assert remote.state(1)["notify_state"] == "uncertain"
    for i in range(2, 6):
        _run(remote, mail, T0 + timedelta(hours=i * 3), f"r{i}")
    assert mail.calls == 1


def test_legacy_claim_without_claim_id_is_never_sent(shared):
    """A claim left by the code before this milestone: that code sent without
    marking "sending" first, so the job may already have been emailed."""
    remote, mail = shared(_rec(1, notify_state="claimed", claimed_at="2030-02-01T00:00:00+00:00"), _rec(2)), Mail()
    assert _run(remote, mail, T0, "r1") == 0
    assert mail.sent == [["Graduate Engineer 2"]] and remote.state(1)["notify_state"] == "uncertain"


def test_concurrent_worker_that_claims_first_wins(shared):
    """Run B claims and publishes while run A is between claiming and its
    first push: A's push merges onto B's claim, A owns nothing, one email."""
    remote, mail = shared(_rec(1), _rec(2)), Mail()
    remote.before[alerts.DATA_MESSAGE] = lambda: _run(remote, mail, T0, "B")
    assert _run(remote, mail, T0, "A") == 0
    assert mail.sent == [["Graduate Engineer 1", "Graduate Engineer 2"]]
    assert remote.state(1)["claim_id"] == "B" and remote.state(1)["notify_state"] == "sent"


def test_concurrent_worker_after_claims_are_published_sends_nothing(shared):
    """Run B starts after A's claims are published but before A sends."""
    remote, mail = shared(_rec(1)), Mail()
    remote.before[alerts.SENDING_MESSAGE] = lambda: _run(remote, mail, T0 + timedelta(minutes=1), "B")
    assert _run(remote, mail, T0, "A") == 0
    assert mail.sent == [["Graduate Engineer 1"]] and remote.state(1)["claim_id"] == "A"


def test_concurrent_worker_cannot_recover_a_live_send(shared):
    """B runs while A is inside SMTP: A's "sending" is fresh, so B neither
    resends it nor settles it."""
    remote = shared(_rec(1))
    inner = Mail()
    outer_calls = []

    def a_sends(jobs, recipient):
        outer_calls.append(1)
        _run(remote, inner, T0 + timedelta(minutes=2), "B")        # B runs mid-send
    assert _run(remote, a_sends, T0, "A") == 0
    assert outer_calls == [1] and inner.calls == 0 and remote.state(1)["notify_state"] == "sent"


def test_merge_rules_for_claims():
    base = {"url": "u", "title": "t"}
    a_claim = {**base, "notify_state": "claimed", "claim_id": "A", "claimed_at": "x"}
    b_claim = {**base, "notify_state": "claimed", "claim_id": "B", "claimed_at": "y"}
    assert repo_sync.merge_record(a_claim, b_claim)["claim_id"] == "A"              # the published claim wins
    a_sending = {**a_claim, "notify_state": "sending"}
    assert repo_sync.merge_record(a_sending, {**base, "notify_state": "pending", "released_claim": "B"}
                                  )["notify_state"] == "sending"                    # a stranger can't release it
    assert repo_sync.merge_record(a_claim, {**base, "notify_state": "pending", "released_claim": "A",
                                            "send_attempts": 1})["notify_state"] in (None, "pending")
    assert repo_sync.merge_record({**base, "send_attempts": 3}, {**base, "send_attempts": 1})["send_attempts"] == 3
    # a recovery saw "claimed", but the owner marked it "sending" meanwhile: the release must not land
    recovered = {**base, "notify_state": "pending", "released_claim": "A", "released_from": "claimed"}
    assert repo_sync.merge_record(a_sending, recovered)["notify_state"] == "sending"
    assert repo_sync.merge_record(a_claim, recovered)["notify_state"] in (None, "pending")
    # ... and the recovering run's own claim replaces the abandoned one only while it is still "claimed"
    takeover = {**base, "notify_state": "claimed", "claim_id": "C", "claimed_at": "z", "released_claim": "A",
                "released_from": "claimed"}
    assert repo_sync.merge_record(a_claim, takeover)["claim_id"] == "C"
    assert repo_sync.merge_record(a_sending, takeover)["claim_id"] == "A"
    assert repo_sync.merge_record(a_sending, takeover)["notify_state"] == "sending"
    # a settled record keeps its own claim id against a stale live copy
    settled = {**a_claim, "notify_state": "sent", "notified": True}
    assert repo_sync.merge_record(settled, b_claim)["claim_id"] == "A"
    sent = {**base, "notify_state": "sent", "notified": True}
    assert repo_sync.merge_record(sent, {**base, "notify_state": "pending"})["notify_state"] == "sent"
    assert repo_sync.merge_record({**base, "notify_state": "pending"}, sent)["notify_state"] == "sent"


def test_an_unusable_recipient_publishes_data_and_claims_nothing(shared, monkeypatch, caplog):
    monkeypatch.setenv("ALERT_RECIPIENT", "not-an-address")
    remote, mail = shared(_rec(1)), Mail()
    assert _run(remote, mail, T0, "r1") == 0
    assert mail.calls == 0 and remote.state(1).get("notify_state") in (None, "pending")
    assert "not-an-address" not in caplog.text


def test_shared_logs_carry_counts_never_the_address(shared, caplog):
    caplog.set_level("INFO")
    remote, mail = shared(_rec(1), _rec(2)), Mail(DeliveryNotSent("rcpt", permanent=True))
    _run(remote, mail, T0, "r1")
    _run(remote, mail, T0 + timedelta(hours=1), "r2")
    assert "retries_scheduled" in caplog.text and "delivery: this run" in caplog.text
    assert RECIPIENT not in caplog.text and "shared-recipient" not in caplog.text


def test_dismissing_a_retrying_job_stops_it(shared):
    remote, mail = shared(_rec(1)), Mail(DeliveryNotSent("connect"))
    _run(remote, mail, T0, "r1")
    remote.seen = [{**j, "dismissed": True} for j in remote.seen]
    config_store.SEEN_FILE.write_text(json.dumps(remote.seen))
    _run(remote, mail, T0 + timedelta(hours=3), "r2")
    assert mail.calls == 1


def test_delivery_health_counts():
    now = T0
    seen = [_rec(1, notify_state="claimed", claim_id="a", claimed_at=(now - timedelta(minutes=5)).isoformat()),
            _rec(2, notify_state="claimed", claim_id="b", claimed_at=(now - CLAIM_TTL * 2).isoformat()),
            _rec(3, notify_state="sending", claim_id="c", sending_at=(now - CLAIM_TTL * 2).isoformat()),
            _rec(4, notify_state="pending", send_attempts=2), _rec(5, notify_state="failed"),
            _rec(6, notify_state="uncertain"), _rec(7, notify_state="sent"), _rec(8, send_attempts="junk"), "junk"]
    h = delivery_health(seen, now)
    assert h == {"claimed": 2, "sending": 1, "stale_claims": 2, "retrying": 1, "failed": 1, "uncertain": 1,
                 "sent": 1}


# ===========================================================================
# the SMTP conversation (notifier._send): which failures are definitely unsent
# ===========================================================================

class FakeSMTP:
    fail_at = None             # "connect" | "login" | "mail" | "rcpt" | "data" | "data_timeout" | "data_reject"

    def __init__(self, *a, **k):
        if self.fail_at == "connect":
            raise socket.timeout("connect timed out")

    def login(self, *a):
        if self.fail_at == "login":
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    def mail(self, *a):
        return (451, b"try later") if self.fail_at == "mail" else (250, b"ok")

    def rcpt(self, *a):
        return (550, b"no such user") if self.fail_at == "rcpt" else (250, b"ok")

    def data(self, *a):
        if self.fail_at == "data_timeout":
            raise socket.timeout("timed out waiting for the reply")
        if self.fail_at == "data_reject":
            raise smtplib.SMTPDataError(554, b"rejected")
        return (250, b"queued")

    def quit(self):
        pass

    def close(self):
        pass


@pytest.mark.parametrize("fail_at, error, stage, permanent", [
    ("connect", DeliveryNotSent, "connect", False),
    ("login", DeliveryNotSent, "login", True),
    ("mail", DeliveryNotSent, "mail", False),
    ("rcpt", DeliveryNotSent, "rcpt", True),
    ("data_reject", DeliveryNotSent, "data", True),
    ("data_timeout", DeliveryUncertain, "data", None),
])
def test_smtp_failure_phases(monkeypatch, fail_at, error, stage, permanent):
    monkeypatch.setenv("GMAIL_ADDRESS", "sender@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "x")
    monkeypatch.setattr(FakeSMTP, "fail_at", fail_at)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    with pytest.raises(error) as e:
        notifier._send("to@example.com", "s", "<p>h</p>", "t")
    assert e.value.stage == stage
    if permanent is not None:
        assert e.value.permanent is permanent
    assert "to@example.com" not in str(e.value) and "sender@example.com" not in str(e.value)


def test_smtp_success(monkeypatch):
    monkeypatch.setenv("GMAIL_ADDRESS", "sender@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "x")
    monkeypatch.setattr(FakeSMTP, "fail_at", None)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    notifier._send("to@example.com", "s", "<p>h</p>", "t")


# ===========================================================================
# personal emails (user_store ledger + user_alerts)
# ===========================================================================

A, B = "UserAaaaaaaaaaaaaaaaaaaaaaaa1", "UserBbbbbbbbbbbbbbbbbbbbbbbb2"
TS = T0.timestamp()
PASSED = {"checks": {"india": True, "experience": True}}


def pjob(n):
    return {"id": f"sanofi_{n}", "url": f"https://jobs.example/sanofi/{n}", "company": "Sanofi",
            "company_id": "sanofi", "title": "Software Engineer", "location": "Pune · India", "category": "FRESHER",
            "first_seen": "2030-02-01T00:00:00+00:00", "evidence": PASSED}


class Identity:
    def __init__(self):
        self.accounts = {}

    def account(self, uid):
        return self.accounts.get(uid)

    def google_account(self, sub):
        return None


@pytest.fixture
def store():
    s = UserStore(MemoryBackend(), Identity())
    for uid, email in ((A, "alice@example.org"), (B, "bob@example.org")):
        u = s.for_uid(uid)
        u.ensure_profile(email, "", True)
        u.set_watch_all(True)
        u.set_notifications(True, "general")
        s.backend.set(f"users/{uid}", {"notifications": {"enabled": True, "mode": "general",
                                                         "enabled_at": "2030-01-01T00:00:00Z"}}, merge=True)
        s.identity.accounts[uid] = {"uid": uid, "email": email, "email_verified": True, "disabled": False}
    return s


class PMail:
    def __init__(self, plan=None):
        self.plan, self.sent, self.calls, self.lock = dict(plan or {}), [], 0, threading.Lock()

    def __call__(self, jobs, to):
        with self.lock:
            self.calls += 1
        step = self.plan.get(to)
        if isinstance(step, list):
            step = step.pop(0) if step else None
        if step == "crash":
            raise Crash("died inside send")
        if isinstance(step, BaseException):
            raise step
        with self.lock:
            self.sent.append((to, sorted(job_key(j) for j in jobs)))


def _prun(store, mail, at, run_id, seen=None):
    try:
        return user_alerts.run(store, seen or [pjob(1)], mail, run_id=run_id, now=at)
    except Crash:
        return "crashed"


def _state(store, uid, n=1):
    return (store.for_uid(uid).delivery(job_key(pjob(n))) or {}).get("state")


def test_personal_lifecycle_claim_sending_sent(store):
    a = store.for_uid(A)
    key = job_key(pjob(1))
    assert a.claim_delivery(key, "r1", now=TS)
    assert a.delivery(key)["v"] == DELIVERY_SCHEMA and a.delivery(key)["state"] == "claimed"
    assert not a.mark_sending(key, "r2", now=TS)                    # only the claimant
    assert a.mark_sending(key, "r1", now=TS) and a.delivery(key)["state"] == "sending"
    assert not a.finalize_delivery(key, "r2", "sent", now=TS)
    assert a.finalize_delivery(key, "r1", "sent", now=TS) and a.delivery(key)["state"] == "sent"
    assert not a.claim_delivery(key, "r3", now=TS + 10 ** 7)        # sent is final, however much later


def test_personal_crash_before_sending_is_recovered_once(store):
    a = store.for_uid(A)
    key = job_key(pjob(1))
    assert a.claim_delivery(key, "dead", now=TS)                     # ... and the run died
    stats = {}
    assert not a.claim_delivery(key, "r2", now=TS + 600, stats=stats)        # within the TTL
    claimants = [a.claim_delivery(key, f"r{i}", now=TS + DELIVERY_CLAIM_TTL_S + 60, stats=stats) for i in range(3, 6)]
    assert claimants == [True, False, False] and stats == {"abandoned_claims_recovered": 1}
    assert a.delivery(key)["recovered_from"] == "dead"


def test_personal_crash_during_or_after_sending_is_never_resent(store):
    mail = PMail({"alice@example.org": ["crash"]})
    assert _prun(store, mail, T0, "r1") == "crashed"
    assert _state(store, A) == "sending"
    for i in range(1, 6):
        _prun(store, mail, T0 + timedelta(seconds=DELIVERY_CLAIM_TTL_S * i + 60, hours=7 * i), f"r{i + 1}")
    alice = [s for s in mail.sent if s[0] == "alice@example.org"]
    assert alice == [] and _state(store, A) == "uncertain"            # never resent
    assert _state(store, B) == "sent"                                 # Bob unaffected


def test_personal_finalize_failure_after_send_is_never_resent(store, monkeypatch):
    mail = PMail()
    real = store.for_uid(A).finalize_delivery.__func__

    def broken(self, *a, **k):
        if self.uid == A:
            raise UserStoreError("Firestore unavailable")
        return real(self, *a, **k)
    monkeypatch.setattr(type(store.for_uid(A)), "finalize_delivery", broken)
    stats = _prun(store, mail, T0, "r1")
    assert stats["ledger_outcome_unrecorded"] == 1 and _state(store, A) == "sending"
    monkeypatch.undo()
    for i in range(1, 4):
        _prun(store, mail, T0 + timedelta(hours=7 * i), f"r{i + 1}")
    assert [s for s in mail.sent if s[0] == "alice@example.org"] == [("alice@example.org", [job_key(pjob(1))])]
    assert _state(store, A) == "uncertain"


def test_personal_gmail_rejection_backoff_then_failed(store):
    mail = PMail({"alice@example.org": [DeliveryNotSent("rcpt", permanent=True)] * 10})
    at = T0
    for i in range(40):
        _prun(store, mail, at, f"r{i}")
        at += timedelta(minutes=15)
    doc = store.for_uid(A).delivery(job_key(pjob(1)))
    assert doc["state"] == "failed" and doc["attempts"] == DELIVERY_MAX_ATTEMPTS
    assert mail.calls == DELIVERY_MAX_ATTEMPTS + 1                    # five for Alice, one for Bob


def test_personal_timeout_is_uncertain_and_counts_toward_cadence(store):
    mail = PMail({"alice@example.org": [DeliveryUncertain("data")]})
    stats = _prun(store, mail, T0, "r1")
    assert stats["uncertain"] == 1 and stats["sent"] == 1 and _state(store, A) == "uncertain"
    assert store.for_uid(A).personal_view().get("alerts_last_sent")       # the 6-hour cadence still applies
    _prun(store, mail, T0 + timedelta(hours=7), "r2")
    assert [s for s in mail.sent if s[0] == "alice@example.org"] == []


def test_personal_legacy_claim_is_never_sent(store):
    """A personal claim from before this milestone (no schema marker): the
    old code could have sent it, so it becomes "uncertain"."""
    key = job_key(pjob(1))
    path = store.for_uid(A)._delivery_path(key)
    store.backend.set(path, {"job_key": key, "state": "claimed", "claim_id": "old", "claimed_at": "2030-01-01T00:00:00Z"})
    mail = PMail()
    stats = _prun(store, mail, T0, "r1")
    assert stats["ledger_legacy_claims_uncertain"] == 1 and _state(store, A) == "uncertain"
    assert [s for s in mail.sent if s[0] == "alice@example.org"] == []


def test_personal_concurrent_workers_send_each_job_once(store):
    seen = [pjob(n) for n in range(5)]
    mail = PMail()
    threads = [threading.Thread(target=_prun, args=(store, mail, T0, f"w{i}", seen)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    per = Counter()
    for to, keys in mail.sent:
        per.update((to, k) for k in keys)
    assert set(per.values()) == {1} and len(per) == 10


def test_personal_concurrent_recovery_has_one_winner(store):
    a = store.for_uid(A)
    key = job_key(pjob(1))
    a.claim_delivery(key, "dead", now=TS)
    wins = []
    threads = [threading.Thread(target=lambda i=i: wins.append(
        a.claim_delivery(key, f"r{i}", now=TS + DELIVERY_CLAIM_TTL_S + 1))) for i in range(10)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert wins.count(True) == 1


def test_account_isolation_in_every_failure_mode(store):
    """Alice's crash, rejection and uncertainty never change Bob's ledger,
    and Bob's success never marks Alice's job."""
    seen = [pjob(1), pjob(2)]
    mail = PMail({"alice@example.org": [DeliveryNotSent("rcpt", permanent=True), DeliveryUncertain("data")]})
    _prun(store, mail, T0, "r1", seen)
    assert {_state(store, A, 1), _state(store, A, 2)} == {"pending"}
    assert {_state(store, B, 1), _state(store, B, 2)} == {"sent"}
    _prun(store, mail, T0 + timedelta(hours=7), "r2", seen)
    assert {_state(store, A, 1), _state(store, A, 2)} == {"uncertain"}
    assert store.backend.list(f"users/{B}/deliveries") and all(
        d["state"] == "sent" for d in store.backend.list(f"users/{B}/deliveries").values())
    assert [s[0] for s in mail.sent] == ["bob@example.org"]


def test_personal_logs_are_free_of_addresses_and_uids(store, caplog):
    caplog.set_level("INFO")
    mail = PMail({"alice@example.org": [DeliveryNotSent("rcpt", permanent=True)],
                  "bob@example.org": [DeliveryUncertain("data")]})
    _prun(store, mail, T0, "r1")
    for secret in ("alice@example.org", "bob@example.org", A, B, "alice", "bob"):
        assert secret not in caplog.text
    assert "rcpt" in caplog.text and "data" in caplog.text
