"""Per-user data against the Firebase EMULATORS (Firestore + Authentication),
through the real Firebase Admin SDK and the app's own user_store.configure().

Run them with the emulators started locally, e.g.
    firebase emulators:start --only firestore,auth --project demo-job-tracker
    FIRESTORE_EMULATOR_HOST=127.0.0.1:8085 FIREBASE_AUTH_EMULATOR_HOST=127.0.0.1:9099 pytest tests/test_firestore_emulator.py

Safety: they refuse to run unless both hosts are on this machine and the
project is a "demo-" project (Firebase never connects demo projects to real
resources). Without the emulators they are skipped.
"""
import hashlib
import json
import os
import threading
import time
from pathlib import Path

import pytest

firebase_admin = pytest.importorskip("firebase_admin")
from firebase_admin import auth  # noqa: E402

import httpx  # noqa: E402

import user_alerts  # noqa: E402
import user_store  # noqa: E402
from config_store import job_key  # noqa: E402
from user_store import UserData, UserStoreError  # noqa: E402

FS_HOST = os.environ.get("FIRESTORE_EMULATOR_HOST", "")
AUTH_HOST = os.environ.get("FIREBASE_AUTH_EMULATOR_HOST", "")
PROJECT = "demo-job-tracker"
REPO = Path(__file__).resolve().parent.parent
A, B = "EmuUserAaaaaaaaaaaaaaaaaaaaa1", "EmuUserBbbbbbbbbbbbbbbbbbbbb2"

pytestmark = pytest.mark.skipif(not (FS_HOST and AUTH_HOST),
                                reason="Firebase emulators not running (FIRESTORE_EMULATOR_HOST / "
                                       "FIREBASE_AUTH_EMULATOR_HOST unset)")


def _local(host: str) -> bool:
    return host.rsplit(":", 1)[0].strip("[]") in ("127.0.0.1", "localhost", "::1")


def _throwaway_service_account() -> dict:
    """A freshly generated key: the emulators accept any credential, and
    nothing real is ever loaded."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    return {"type": "service_account", "project_id": PROJECT, "private_key": pem, "private_key_id": "emulator",
            "client_email": f"emulator@{PROJECT}.iam.gserviceaccount.com", "client_id": "0",
            "token_uri": "https://oauth2.googleapis.com/token"}


@pytest.fixture(scope="module")
def emu():
    if not (_local(FS_HOST) and _local(AUTH_HOST)):
        pytest.fail("emulator hosts must be local — refusing to touch anything else")
    rules = (REPO / "firestore.rules").read_text("utf-8")
    r = httpx.put(f"http://{FS_HOST}/emulator/v1/projects/{PROJECT}:securityRules",
                  json={"rules": {"files": [{"name": "firestore.rules", "content": rules}]}}, timeout=10)
    assert r.status_code == 200, r.text                       # the repository's rules, as deployed
    sa = _throwaway_service_account()
    store, problem = user_store.configure({"firebase_service_account": sa}, PROJECT)
    assert store is not None and problem == ""
    store.test_service_account_json = json.dumps(sa)        # the same credential, as the workflow passes it
    yield store
    firebase_admin.delete_app(store.identity.app)


@pytest.fixture(autouse=True)
def clean(emu):
    for url in (f"http://{FS_HOST}/emulator/v1/projects/{PROJECT}/databases/(default)/documents",
                f"http://{AUTH_HOST}/emulator/v1/projects/{PROJECT}/accounts"):
        assert httpx.delete(url, timeout=10).status_code == 200
    yield


def _account(emu, uid, email, verified=True, google_sub=None):
    record = auth.ImportUserRecord(uid=uid, email=email, email_verified=verified,
                                   provider_data=[auth.UserProvider(uid=google_sub, provider_id="google.com",
                                                                    email=email)] if google_sub else None)
    result = auth.import_users([record], app=emu.identity.app)
    assert result.failure_count == 0


def _docs(path: str) -> dict:
    """Read the emulator directly as an ADMIN (bypassing rules), to inspect."""
    r = httpx.get(f"http://{FS_HOST}/v1/projects/{PROJECT}/databases/(default)/documents/{path}",
                  headers={"Authorization": "Bearer owner"}, timeout=10)
    return r.json() if r.status_code == 200 else {}


# --- profiles -------------------------------------------------------------------

def test_profile_creation_and_repeat_sign_in(emu):
    u = emu.for_uid(A)
    profile, created = u.ensure_profile("Alice@Example.org", "Alice", True)
    assert created and profile["email"] == "alice@example.org"
    again, created2 = emu.for_uid(A).ensure_profile("alice@example.org", "Alice A.", True)
    assert not created2 and again["created_at"] == profile["created_at"] and again["display_name"] == "Alice A."
    assert [d["name"].rsplit("/", 1)[-1] for d in _docs("users").get("documents", [])] == [A]


def test_concurrent_first_sign_ins_make_one_profile(emu):
    results = []
    threads = [threading.Thread(target=lambda: results.append(emu.for_uid(A).ensure_profile("a@example.org")[1]))
               for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count(True) == 1


def test_two_users_are_isolated(emu):
    a, b = emu.for_uid(A), emu.for_uid(B)
    a.ensure_profile("a@example.org", "", True)
    b.ensure_profile("b@example.org", "", True)
    a.dismiss({"job-1|https://x"})
    a.watch("sanofi", {"sanofi", "pwc"})
    a.set_watch_all(False)
    a.set_notifications(True, "tailored")
    a.set_preferences("tailored", ["Data & analytics"], ["Pune"], ["fresher"])
    assert b.dismissed() == set() and b.watchlist() == set()
    view_b = b.personal_view()
    assert view_b["watch_all"] is True and view_b["mode"] == "general" and view_b["prefs"]["job_families"] == []
    assert a.dismissed() == {"job-1|https://x"} and a.watchlist() == {"sanofi"}
    assert a.personal_view()["prefs"]["locations"] == ["Pune"]
    assert not _docs(f"users/{B}/dismissed").get("documents") and not _docs(f"users/{B}/companies").get("documents")
    assert len(_docs(f"users/{A}/dismissed").get("documents", [])) == 1


def test_watchlist_and_dismissals_persist_and_undo(emu):
    a = emu.for_uid(A)
    a.ensure_profile("a@example.org")
    a.watch("pwc", {"pwc"})
    a.dismiss({"k1", "k2"})
    a.restore({"k1"})
    a.unwatch("pwc")
    fresh = emu.for_uid(A)                                     # a new object reads from Firestore
    assert fresh.dismissed() == {"k2"} and fresh.watchlist() == set()


def test_notification_preferences(emu):
    a = emu.for_uid(A)
    a.ensure_profile("a@example.org", "", True)
    assert a.set_notifications(True, "general") == {"enabled": True, "mode": "general", "email": "a@example.org"}
    unverified = emu.for_uid(B)
    unverified.ensure_profile("b@example.org", "", False)
    with pytest.raises(ValueError):
        unverified.set_notifications(True)


# --- delivery ledger (real Firestore transactions) ------------------------------

def test_delivery_claim_transaction(emu):
    a = emu.for_uid(A)
    a.ensure_profile("a@example.org")
    assert a.claim_delivery("job-1", "run-1") and not a.claim_delivery("job-1", "run-2")
    assert not a.finalize_delivery("job-1", "run-2", sent=True)            # not the claimant
    assert a.finalize_delivery("job-1", "run-1", sent=True)
    assert a.delivery("job-1")["state"] == "sent"
    assert not a.claim_delivery("job-1", "run-3")                          # sent can never be claimed again


def test_concurrent_delivery_claims(emu):
    a = emu.for_uid(A)
    a.ensure_profile("a@example.org")
    wins = []
    threads = [threading.Thread(target=lambda i=i: wins.append(a.claim_delivery("job-1", f"run-{i}")))
               for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert wins.count(True) == 1


def test_concurrent_release_and_reclaim(emu):
    """After a failed send, exactly one of several later runs reclaims it —
    every time (a regression: locking transactions used to abort them all)."""
    a = emu.for_uid(A)
    a.ensure_profile("a@example.org")
    for round_ in range(5):
        key = f"job-{round_}"
        assert a.claim_delivery(key, "run-0") and a.finalize_delivery(key, "run-0", sent=False)
        wins, errors = [], []

        def attempt(i):
            try:
                wins.append(a.claim_delivery(key, f"run-{i}"))
            except Exception as e:                       # nothing may escape a claim
                errors.append(type(e).__name__)
        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(1, 11)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert errors == [] and wins.count(True) == 1, (round_, wins, errors)
        assert a.delivery(key)["state"] == "claimed"


# --- the notifier end to end (Auth emulator is the source of truth) -------------

PASSED = {"checks": {"india": True, "experience": True}}


def _job(n, company="pwc", title="Software Engineer"):
    return {"id": f"{company}_{n}", "url": f"https://jobs.example/{n}", "company_id": company, "company": company,
            "title": title, "location": "Pune · India", "category": "FRESHER", "evidence": PASSED,
            "first_seen": "2030-01-02T00:00:00+00:00"}


def _subscriber(emu, uid, email):
    _account(emu, uid, email)
    u = emu.for_uid(uid)
    u.ensure_profile(email, "", True)
    u.set_notifications(True, "general")
    emu.backend.set(f"users/{uid}", {"notifications": {"enabled": True, "mode": "general",
                                                       "enabled_at": "2030-01-01T00:00:00Z"}}, merge=True)
    return u


class Mailer:
    def __init__(self, fail_for=()):
        self.sent, self.fail_for, self.lock = [], set(fail_for), threading.Lock()

    def __call__(self, jobs, to):
        if to in self.fail_for:
            raise RuntimeError("smtp down")
        with self.lock:
            self.sent.append((to, sorted(job_key(j) for j in jobs)))


def test_send_failure_is_retried_and_sent_jobs_never_again(emu):
    _subscriber(emu, A, "a@example.org")
    _subscriber(emu, B, "b@example.org")
    seen = [_job(1)]
    flaky = Mailer(fail_for={"a@example.org"})
    stats = user_alerts.run(emu, seen, flaky, run_id="r1")
    assert stats["sent"] == 1 and stats["failed"] == 1 and flaky.sent == [("b@example.org", [job_key(seen[0])])]
    ok = Mailer()
    user_alerts.run(emu, seen, ok, run_id="r2")
    user_alerts.run(emu, seen, ok, run_id="r3")
    assert ok.sent == [("a@example.org", [job_key(seen[0])])]


def test_concurrent_notifier_runs_send_each_job_once(emu):
    for i in range(3):
        _subscriber(emu, f"EmuConc{i}aaaaaaaaaaaaaaaaaaaa", f"c{i}@example.org")
    seen = [_job(n) for n in range(3)]
    mail = Mailer()
    threads = [threading.Thread(target=user_alerts.run, args=(emu, seen, mail), kwargs={"run_id": f"r{i}"})
               for i in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    per = {}
    for to, keys in mail.sent:
        per.setdefault(to, []).extend(keys)
    assert {to: sorted(k) for to, k in per.items()} == {f"c{i}@example.org": sorted(job_key(j) for j in seen)
                                                        for i in range(3)}


def test_disabled_user(emu):
    _subscriber(emu, A, "a@example.org")
    _account(emu, B, "b@example.org", google_sub="google-sub-b")
    auth.update_user(A, disabled=True, app=emu.identity.app)
    auth.update_user(B, disabled=True, app=emu.identity.app)
    assert emu.account(A)["disabled"] is True
    assert emu.google_account("google-sub-b") == {"uid": B, "disabled": True}
    mail = Mailer()
    assert user_alerts.run(emu, [_job(1)], mail)["disabled"] == 1 and mail.sent == []


def test_missing_or_deleted_user(emu):
    _subscriber(emu, A, "a@example.org")
    auth.delete_user(A, app=emu.identity.app)
    assert emu.account(A) is None and emu.google_account("never-seen-sub") is None
    mail = Mailer()
    assert user_alerts.run(emu, [_job(1)], mail)["deleted"] == 1 and mail.sent == []
    assert emu.for_uid(A).delivery(job_key(_job(1))) is None              # nothing claimed for a deleted UID


def test_google_account_is_found_by_subject_not_email(emu):
    _account(emu, A, "shared@example.org", google_sub="sub-a")
    assert emu.google_account("sub-a") == {"uid": A, "disabled": False}
    assert emu.google_account("shared@example.org") is None                # an email is not an identifier


# --- isolation and hostile input ---------------------------------------------------

@pytest.mark.parametrize("uid", ["", "a/b", "../x", f"{A}/dismissed", "__x__", "x" * 129, " ", None])
def test_malformed_uids_never_reach_firestore(emu, uid):
    with pytest.raises(ValueError):
        UserData(emu.backend, uid)
    with pytest.raises(UserStoreError):
        emu.account(uid)
    assert not _docs("users").get("documents")


def test_hostile_job_keys_and_company_ids_stay_in_the_users_tree(emu):
    a, b = emu.for_uid(A), emu.for_uid(B)
    a.ensure_profile("a@example.org")
    b.ensure_profile("b@example.org")
    for key in (f"../{B}/dismissed/x", f"users/{B}/dismissed/x", "/", "a/b", "__name__", ".."):
        a.dismiss({key})
        a.claim_delivery(key, "r")
    for bad in (f"../{B}", "not-in-catalogue", ""):
        with pytest.raises(ValueError):
            a.watch(bad, {"pwc"})
    assert b.dismissed() == set() and not _docs(f"users/{B}/deliveries").get("documents")
    assert len(a.dismissed()) == 6


def test_clients_cannot_read_or_write_firestore_directly(emu):
    """firestore.rules: no browser or other client gets in — only the
    server's Admin SDK (which the rules don't apply to)."""
    emu.for_uid(A).ensure_profile("a@example.org")
    base = f"http://{FS_HOST}/v1/projects/{PROJECT}/databases/(default)/documents"
    assert httpx.get(f"{base}/users/{A}", timeout=10).status_code == 403
    assert httpx.patch(f"{base}/users/{B}", json={"fields": {"x": {"stringValue": "y"}}}, timeout=10).status_code == 403
    assert httpx.delete(f"{base}/users/{A}", timeout=10).status_code == 403
    assert emu.for_uid(A).profile()["email"] == "a@example.org"           # still there


# --- the app on the emulator -----------------------------------------------------------

from test_firebase_auth import GATED, _login, _login_page, google_env, make_id_token, no_env, ok_password_answer  # noqa: E402,F401
from test_streamlit_app import NEW_RECORD, _html, _key, _nav, app  # noqa: E402,F401


def test_the_app_creates_and_reuses_the_profile_on_the_emulator(app, google_env, emu, monkeypatch):
    monkeypatch.setattr(user_store, "configure", lambda secrets, project_id="": (emu, ""))
    _account(emu, A, "alice@example.org")
    for attempt in range(2):                                                # first and repeat sign-in
        google_env["firebase"](ok_password_answer(localId=A, email="alice@example.org", emailVerified=True,
                                                  idToken=make_id_token(uid=A, email="alice@example.org")))
        at = app([NEW_RECORD], secrets=GATED, owner=False)
        _login(at, email="alice@example.org")
        assert not _login_page(at)
    _nav(at, "jobs")
    at.button(key=_key("dismiss", NEW_RECORD)).click().run()
    assert [d["name"].rsplit("/", 1)[-1] for d in _docs("users").get("documents", [])] == [A]
    assert emu.for_uid(A).dismissed() == {job_key(NEW_RECORD)}
    auth.update_user(A, disabled=True, app=emu.identity.app)                # the owner disables Alice
    at.session_state["_acct_state"] = {**at.session_state["_acct_state"], "at": 0}
    at.run()
    assert _login_page(at) and "This account has been disabled." in _html(at)


# --- the workflow's personal step, end to end on the emulators ---------------------

def _workflow_env(emu, **extra):
    env = {k: v for k, v in os.environ.items() if k not in ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "FIREBASE_PROJECT_ID")}
    env.update({"FIREBASE_SERVICE_ACCOUNT": emu.test_service_account_json, "PYTHONWARNINGS": "ignore",
                "FIRESTORE_EMULATOR_HOST": FS_HOST, "FIREBASE_AUTH_EMULATOR_HOST": AUTH_HOST}, **extra)
    return env


def test_the_workflow_step_with_failing_email_releases_and_touches_nothing_shared(emu):
    """`python user_alerts.py --send` exactly as the step runs it, with no
    Gmail credentials: every send fails, the claims are released for the
    next run, the shared data file is untouched, and the log is counts only."""
    import subprocess
    import sys
    seen_file = REPO / "seen_jobs.json"
    before = hashlib.sha256(seen_file.read_bytes()).hexdigest()
    _subscriber(emu, A, "alice@example.org")
    emu.backend.set(f"users/{A}", {"notifications": {"enabled": True, "mode": "general",
                                                     "enabled_at": "2000-01-01T00:00:00Z"}}, merge=True)
    r = subprocess.run([sys.executable, "user_alerts.py", "--send"], cwd=REPO, env=_workflow_env(emu),
                       capture_output=True, text=True, timeout=180)
    out = r.stdout + r.stderr
    assert r.returncode == 1 and '"failed": 1' in out and "Traceback" not in out
    assert "alice@example.org" not in out and A not in out and "PRIVATE KEY" not in out
    states = {d["fields"]["state"]["stringValue"] for d in _docs(f"users/{A}/deliveries").get("documents", [])}
    assert states == {"pending"}                                          # released, retried next run
    assert hashlib.sha256(seen_file.read_bytes()).hexdigest() == before


def _cli_world(emu, tmp_path, monkeypatch, jobs):
    import notifier
    monkeypatch.setenv("FIREBASE_SERVICE_ACCOUNT", emu.test_service_account_json)
    monkeypatch.delenv("FIREBASE_PROJECT_ID", raising=False)
    monkeypatch.setattr(user_alerts, "BASE", tmp_path)
    (tmp_path / "seen_jobs.json").write_text(json.dumps(jobs), "utf-8")
    mail = Mailer()
    monkeypatch.setattr(notifier, "send_alerts", mail)
    return mail


def test_the_cli_end_to_end_people_filters_and_account_states(emu, tmp_path, monkeypatch):
    _subscriber(emu, A, "a@example.org")
    emu.for_uid(A).watch("sanofi", {"sanofi", "pwc"})
    emu.for_uid(A).set_watch_all(False)                                   # company scope: sanofi
    _subscriber(emu, B, "b@example.org")
    emu.for_uid(B).set_preferences("tailored", ["Data & analytics"], [], [])
    emu.backend.set(f"users/{B}", {"notifications": {"enabled": True, "mode": "tailored",
                                                     "enabled_at": "2030-01-01T00:00:00Z"}}, merge=True)
    gone, off = "EmuGoneaaaaaaaaaaaaaaaaaaaaa3", "EmuOffaaaaaaaaaaaaaaaaaaaaaa4"
    _subscriber(emu, gone, "gone@example.org")
    auth.delete_user(gone, app=emu.identity.app)
    _subscriber(emu, off, "off@example.org")
    auth.update_user(off, disabled=True, app=emu.identity.app)
    jobs = [_job(1, "pwc"), _job(2, "sanofi"), _job(3, "pwc", title="Data Analyst"),
            {**_job(4, "sanofi"), "first_seen": "2029-12-01T00:00:00+00:00"}]          # before the cutoff
    mail = _cli_world(emu, tmp_path, monkeypatch, jobs)
    assert user_alerts.main(["--send"]) == 0
    assert sorted(mail.sent) == [("a@example.org", [job_key(jobs[1])]), ("b@example.org", [job_key(jobs[2])])]
    assert user_alerts.main(["--send"]) == 0 and len(mail.sent) == 2      # nothing twice


def test_concurrent_cli_runs_on_the_emulator_send_once(emu, tmp_path, monkeypatch):
    for i in range(3):
        _subscriber(emu, f"EmuCli{i}aaaaaaaaaaaaaaaaaaaaa", f"cli{i}@example.org")
    jobs = [_job(n) for n in range(3)]
    mail = _cli_world(emu, tmp_path, monkeypatch, jobs)
    threads = [threading.Thread(target=user_alerts.main, args=(["--send"],)) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    per = {}
    for to, keys in mail.sent:
        per.setdefault(to, []).extend(keys)
    assert {to: sorted(k) for to, k in per.items()} == {f"cli{i}@example.org": sorted(job_key(j) for j in jobs)
                                                        for i in range(3)}
