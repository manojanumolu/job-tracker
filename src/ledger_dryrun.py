"""Read-only dry run: how this release reads the existing personal alert
ledgers (users/{uid}/deliveries/*), before it is deployed. Counts only.

Safety:
  * Read-only. It only lists and reads documents, and every Firestore write
    method (set, update, create, delete, batch and transaction commits) is
    replaced with one that raises before any client exists. A write attempt
    stops the run.
  * No personal data in the output. It never prints a UID, an email
    address, a job or a document id. Accounts are numbered #1, #2, ... in the
    order of a hash of their UID.
  * Each record is judged by this release's own UserStore.claim_delivery,
    run on an in-memory copy (exactly what the next alert run would decide).
    The copy is thrown away.

Credentials, on your own machine (never paste a key into a chat):
  * Recommended, no key file: sign in as yourself.
        gcloud auth application-default login
        python src/ledger_dryrun.py --project <firebase-project-id>
  * Or a service-account key file you already have:
        python src/ledger_dryrun.py --key-file /path/to/key.json
    A key with only the "Cloud Datastore Viewer" role is enough.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections import Counter

import user_store

ABANDONED_AFTER_S = user_store.DELIVERY_CLAIM_TTL_S


class WriteAttempted(RuntimeError):
    """The dry run tried to write: it never should."""


def install_read_only_guard() -> None:
    """Make every Firestore write raise, whatever calls it."""
    from google.cloud.firestore_v1 import batch, document, transaction

    def refuse(*_a, **_k):
        raise WriteAttempted("the ledger dry run is read-only")
    for cls, names in ((document.DocumentReference, ("set", "update", "create", "delete")),
                       (batch.WriteBatch, ("commit", "set", "update", "create", "delete")),
                       (transaction.Transaction, ("commit", "set", "update", "create", "delete"))):
        for name in names:
            setattr(cls, name, refuse)


def client(project: str | None, key_file: str | None):
    import firebase_admin
    from firebase_admin import credentials, firestore
    cred = credentials.Certificate(key_file) if key_file else credentials.ApplicationDefault()
    opts = {"projectId": project} if project else None
    return firestore.client(firebase_admin.initialize_app(cred, opts, name="ledger-dryrun-readonly"))


def _age_s(stamp, now: float) -> float | None:
    seconds = user_store._iso_seconds(stamp)
    return None if seconds is None else now - seconds


def judge(uid: str, doc: dict, now: float) -> str:
    """What this release would do with one record (on an in-memory copy)."""
    key = doc.get("job_key")
    if not isinstance(key, str) or not key:
        return "unreadable (no job_key): never sent"
    store = user_store.UserStore(user_store.MemoryBackend(), None)
    user = store.for_uid(uid)
    store.backend.set(user._delivery_path(key), json.loads(json.dumps(doc, default=str)))
    stats: dict = {}
    won = user.claim_delivery(key, "dry-run", now=now, stats=stats)
    state = doc.get("state")
    if won:
        return ("abandoned claim: recovered and sent once" if state == "claimed"
                else "pending: retried on the next eligible run")
    if stats.get("legacy_claims_uncertain"):
        return "old-format claim: becomes uncertain, never sent"
    if stats.get("abandoned_sending_uncertain"):
        return "abandoned sending: becomes uncertain, never resent"
    if state == "pending":
        return "pending: waiting out its retry backoff"
    if state in ("claimed", "sending"):
        return f"live {state}: left to the run that holds it"
    if state in ("sent", "failed", "uncertain"):
        return f"{state}: final, never sent again"
    return "unrecognised state: never sent"


def summarise(db, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    accounts = []
    for ref in db.collection("users").list_documents():
        uid = ref.id
        if not user_store.valid_uid(uid):
            continue
        prof = ref.get().to_dict() or {}
        notif = prof.get("notifications") if isinstance(prof.get("notifications"), dict) else {}
        c: Counter = Counter()
        for snap in ref.collection("deliveries").stream():
            doc = snap.to_dict() or {}
            state = doc.get("state")
            c["total"] += 1
            c["old_format"] += doc.get("v") != user_store.DELIVERY_SCHEMA
            c[f"state:{state}"] += 1
            if state in ("claimed", "sending"):
                age = _age_s(doc.get("sending_at" if state == "sending" else "claimed_at"), now)
                c["abandoned (claimed/sending older than 2 h)" if age is None or age > ABANDONED_AFTER_S
                  else "in-flight (claimed/sending under 2 h)"] += 1
            c[f"M2: {judge(uid, doc, now)}"] += 1
        accounts.append({"order": hashlib.sha256(uid.encode()).hexdigest(),
                         "alerts_on": notif.get("enabled") is True, "counts": dict(c)})
    accounts.sort(key=lambda a: a["order"])
    for i, a in enumerate(accounts, 1):
        a["account"] = f"#{i}"
        del a["order"]
    total: Counter = Counter()
    for a in accounts:
        total.update(a["counts"])
    return {"accounts": accounts, "total": dict(total)}


def _print(report: dict) -> None:
    print(f"Accounts: {len(report['accounts'])}")
    for a in report["accounts"]:
        print(f"\nAccount {a['account']} (alerts {'on' if a['alerts_on'] else 'off'})")
        for k, v in sorted(a["counts"].items()):
            print(f"  {k}: {v}")
    t = report["total"]
    uncertain = sum(v for k, v in t.items() if k.startswith("M2:") and "becomes uncertain" in k)
    retried = sum(v for k, v in t.items() if k.startswith("M2:") and ("retried" in k or "recovered" in k))
    print("\nAll accounts")
    print(f"  delivery records: {t.get('total', 0)}")
    print(f"  old-format (written before this release): {t.get('old_format', 0)}")
    for s in ("sent", "failed", "pending", "claimed", "sending", "uncertain"):
        print(f"  state {s}: {t.get(f'state:{s}', 0)}")
    print(f"  abandoned claims: {t.get('abandoned (claimed/sending older than 2 h)', 0)}")
    print(f"  would become uncertain (never sent): {uncertain}")
    print(f"  would be retried or recovered (one send each): {retried}")
    print("\nRead-only: nothing was written. No UID, address or job was printed.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Read-only dry run of the personal alert ledgers (counts only).")
    ap.add_argument("--project", help="Firebase project id (with gcloud application-default credentials)")
    ap.add_argument("--key-file", help="path to a service-account key file (read locally, never printed)")
    ap.add_argument("--json", action="store_true", help="print the counts as JSON")
    args = ap.parse_args(argv)
    install_read_only_guard()
    try:
        report = summarise(client(args.project, args.key_file))
    except WriteAttempted:
        print("Stopped: the dry run attempted a write (nothing was written).", file=sys.stderr)
        return 2
    except Exception as e:                                  # never a traceback: it could carry data
        print(f"Could not read the ledgers: {type(e).__name__}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=1, sort_keys=True)) if args.json else _print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
