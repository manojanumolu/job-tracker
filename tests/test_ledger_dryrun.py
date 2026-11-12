"""src/ledger_dryrun.py: the read-only, counts-only dry run of the personal
alert ledgers, run by the owner before this release is deployed.

The judgement of each record is checked everywhere; the full run (listing
real Firestore documents, the write guard, no writes, no identifiers in
the output) against the Firestore emulator when it is running."""
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import ledger_dryrun
import user_store

REPO = Path(__file__).resolve().parent.parent
FS_HOST = os.environ.get("FIRESTORE_EMULATOR_HOST", "")
NOW = time.time()


def _ts(hours_ago):
    return user_store._now_iso(NOW - hours_ago * 3600)


# every shape the current code (main) can leave, then this release's own
OLD = [({"state": "sent"}, "sent: final, never sent again"),
       ({"state": "pending"}, "pending: retried on the next eligible run"),
       ({"state": "claimed", "claim_id": "r", "claimed_at": _ts(400)}, "old-format claim: becomes uncertain, never sent"),
       ({"state": "claimed", "claim_id": "r", "claimed_at": _ts(0.2)}, "old-format claim: becomes uncertain, never sent"),
       ({"state": "odd"}, "unrecognised state: never sent")]
NEW = [({"state": "claimed", "v": 2, "claim_id": "r", "claimed_at": _ts(3)}, "abandoned claim: recovered and sent once"),
       ({"state": "claimed", "v": 2, "claim_id": "r", "claimed_at": _ts(0.2)}, "live claimed: left to the run that holds it"),
       ({"state": "sending", "v": 2, "claim_id": "r", "sending_at": _ts(3)}, "abandoned sending: becomes uncertain, never resent"),
       ({"state": "pending", "v": 2, "attempts": 1, "next_attempt_at": _ts(-1)}, "pending: waiting out its retry backoff"),
       ({"state": "failed", "v": 2}, "failed: final, never sent again"),
       ({"state": "uncertain", "v": 2}, "uncertain: final, never sent again")]


@pytest.mark.parametrize("doc, verdict", OLD + NEW)
def test_each_record_is_judged_by_this_releases_claim_logic(doc, verdict):
    assert ledger_dryrun.judge("UserAaaaaaaaaaaaaaaaaaaaaaaa1", {"job_key": "k", **doc}, NOW) == verdict


def test_judging_works_on_a_copy():
    doc = {"job_key": "k", "state": "claimed", "claim_id": "r", "claimed_at": _ts(400)}
    before = json.dumps(doc, sort_keys=True)
    ledger_dryrun.judge("UserAaaaaaaaaaaaaaaaaaaaaaaa1", doc, NOW)
    assert json.dumps(doc, sort_keys=True) == before


# ── against the emulator ───────────────────────────────────────────────────

emulator = pytest.mark.skipif(not FS_HOST.startswith(("127.0.0.1", "localhost")),
                              reason="Firestore emulator not running (FIRESTORE_EMULATOR_HOST unset)")
ACCOUNTS = {"DryRunUserAaaaaaaaaaaaaaaaa1": [d for d, _ in OLD], "DryRunUserBbbbbbbbbbbbbbbbb2": [d for d, _ in NEW],
            "DryRunUserCccccccccccccccccc3": []}


def _db():
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import firestore
    return firestore.Client(project="demo-job-tracker", credentials=AnonymousCredentials())


def _content(db):
    out = {}
    for ref in db.collection("users").list_documents():
        out[ref.path] = ref.get().to_dict()
        for s in ref.collection("deliveries").stream():
            out[s.reference.path] = s.to_dict()
    return hashlib.sha256(json.dumps(out, sort_keys=True, default=str).encode()).hexdigest()


@pytest.fixture
def seeded(tmp_path):
    db = _db()
    for ref in db.collection("users").list_documents():          # a clean emulator project
        for s in ref.collection("deliveries").list_documents():
            s.delete()
        ref.delete()
    for uid, docs in ACCOUNTS.items():
        db.document(f"users/{uid}").set({"email": "private-person@example.org", "notifications": {"enabled": True}})
        for i, doc in enumerate(docs):
            key = f"secretco_{i}|https://jobs.example/{uid}/{i}"
            db.document(f"users/{uid}/deliveries/{hashlib.sha256(key.encode()).hexdigest()}").set({"job_key": key, **doc})
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    key_file = tmp_path / "throwaway-key.json"
    key_file.write_text(json.dumps({"type": "service_account", "project_id": "demo-job-tracker", "private_key": pem,
                                    "private_key_id": "x", "client_email": "e@demo-job-tracker.iam.gserviceaccount.com",
                                    "client_id": "0", "token_uri": "https://oauth2.googleapis.com/token"}))
    return db, key_file


def _run(key_file, *extra):
    return subprocess.run([sys.executable, str(REPO / "src" / "ledger_dryrun.py"), "--key-file", str(key_file),
                           "--project", "demo-job-tracker", *extra], capture_output=True, text=True, timeout=120,
                          env={**os.environ, "GCLOUD_PROJECT": "demo-job-tracker"})


@emulator
def test_the_dry_run_counts_every_account_and_writes_nothing(seeded):
    db, key_file = seeded
    before = _content(db)
    r = _run(key_file, "--json")
    assert r.returncode == 0, r.stderr[-400:]
    report = json.loads(r.stdout)
    assert _content(db) == before                                         # nothing written
    assert len(report["accounts"]) == 3 and all(a["alerts_on"] for a in report["accounts"])
    t = report["total"]
    assert t["total"] == len(OLD) + len(NEW) and t["old_format"] == len(OLD)
    assert t["M2: old-format claim: becomes uncertain, never sent"] == 2
    assert t["M2: abandoned sending: becomes uncertain, never resent"] == 1
    assert t["M2: pending: retried on the next eligible run"] == 1
    assert sorted(a["counts"].get("total", 0) for a in report["accounts"]) == [0, len(OLD), len(NEW)]


@emulator
def test_the_output_has_no_identifiers(seeded):
    db, key_file = seeded
    for args in ((), ("--json",)):
        r = _run(key_file, *args)
        assert r.returncode == 0, r.stderr[-400:]
        out = r.stdout + r.stderr
        for secret in ("private-person", "example.org", "DryRunUser", "secretco", "jobs.example"):
            assert secret not in out
        for uid in ACCOUNTS:
            assert uid not in out and hashlib.sha256(uid.encode()).hexdigest() not in out


@emulator
def test_the_write_guard_stops_any_write(seeded):
    db, _ = seeded
    code = (f"import sys; sys.path.insert(0, {str(REPO / 'src')!r}); import ledger_dryrun; "
            "ledger_dryrun.install_read_only_guard(); "
            "from google.auth.credentials import AnonymousCredentials; from google.cloud import firestore; "
            "db = firestore.Client(project='demo-job-tracker', credentials=AnonymousCredentials()); "
            "ref = db.document('users/DryRunUserAaaaaaaaaaaaaaaaa1')\n"
            "for attempt in (lambda: ref.set({'x': 1}), lambda: ref.update({'x': 1}), lambda: ref.delete(),\n"
            "                lambda: db.batch().commit()):\n"
            "    try:\n        attempt(); print('WROTE')\n"
            "    except ledger_dryrun.WriteAttempted:\n        print('refused')\n")
    before = _content(db)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert r.stdout.split() == ["refused"] * 4, r.stdout + r.stderr[-300:]
    assert _content(db) == before
