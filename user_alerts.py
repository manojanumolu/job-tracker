"""Per-user alert emails: one email per person, with the jobs that are new
for them, recorded in their own ledger users/{uid}/deliveries/{job}.

Who gets what:
  1. the shared eligibility gate (job_filters.passes_global_gate) — the
     same jobs today's shared alerts may send; this module never loosens it
  2. not dismissed by this person
  3. found after they turned alerts on (never a backlog)
  4. their company scope, and in Tailored mode their preferences
Where it goes: the account's CURRENT verified email, read from Firebase
Authentication by UID at send time — never an address anyone typed. A
deleted, disabled or unverified account gets nothing.

Exactly once, per person: each job is claimed in the person's own ledger
before sending (one concurrent claimant wins), finalized as "sent" after a
successful send, or released for the next run if the send failed. A crash
between claim and finalize leaves the claim in place: that job is then
never sent rather than sent twice (the shared notifier makes the same
choice). One person's state never touches another's.

The shared notifier (alerts.py) is unchanged and keeps running; this one
is not wired into any workflow yet. ``python user_alerts.py`` is a dry run
(counts only, nothing claimed or sent) unless ``--send`` is given.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import job_filters
from access import redact_emails
from config_store import job_key
from user_store import UserStore, UserStoreError, configure

log = logging.getLogger("user_alerts")
BASE = Path(__file__).parent


def _parse_time(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def select_jobs(seen: list[dict], view: dict, dismissed: set[str], since: datetime) -> list[dict]:
    """The jobs this person should be emailed about (before the ledger)."""
    out, keys = [], set()
    for j in seen:
        if not job_filters.passes_global_gate(j):
            continue
        key = job_key(j)
        found = _parse_time(j.get("first_seen"))
        if key in keys or key in dismissed or found is None or found < since:
            continue
        if job_filters.for_person(j, mode=view["mode"], prefs=view["prefs"], watch_all=view["watch_all"],
                                  watchlist=view["watchlist"]):
            out.append(j)
            keys.add(key)
    return out


def run_for_user(store: UserStore, uid: str, seen: list[dict], send, run_id: str,
                 dry_run: bool = False) -> tuple[str, int]:
    """(outcome, number of jobs) for one person."""
    user = store.for_uid(uid)
    view = user.personal_view()
    notif = view["notifications"]
    since = _parse_time(notif.get("enabled_at"))
    if notif.get("enabled") is not True or since is None:
        return "off", 0
    account = store.account(uid)                     # Firebase is the source of truth, by UID
    if account is None:
        return "deleted", 0
    if account["disabled"]:
        return "disabled", 0
    if not account["email_verified"] or not account["email"]:
        return "unverified", 0
    jobs = select_jobs(seen, view, user.dismissed(), since)
    if dry_run:
        return ("would_send" if jobs else "nothing"), len(jobs)
    claimed = [j for j in jobs if user.claim_delivery(job_key(j), run_id)]
    if not claimed:
        return "nothing", 0
    try:
        send(claimed, account["email"])
        ok = True
    except Exception as e:
        ok = False
        log.error("personal alert for %s… failed: %s — released for the next run", uid[:6], redact_emails(e))
    for j in claimed:
        user.finalize_delivery(job_key(j), run_id, ok)
    return ("sent" if ok else "failed"), len(claimed)


def run(store: UserStore, seen: list[dict], send, *, run_id: str | None = None, dry_run: bool = False) -> dict:
    """Every person, independently: one failure never stops the others."""
    run_id = run_id or uuid.uuid4().hex
    stats: Counter = Counter()
    for uid in store.user_ids():
        try:
            outcome, n = run_for_user(store, uid, seen, send, run_id, dry_run)
        except UserStoreError:
            outcome, n = "error", 0
        stats[outcome] += 1
        stats[f"{outcome}_jobs"] += n
    return dict(stats)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Per-user job alerts (dry run unless --send).")
    parser.add_argument("--send", action="store_true", help="really claim and send (default: dry run)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="[user_alerts] %(message)s")
    store, problem = configure({"FIREBASE_SERVICE_ACCOUNT": os.environ.get("FIREBASE_SERVICE_ACCOUNT", "")},
                               os.environ.get("FIREBASE_PROJECT_ID", ""))
    if store is None:
        log.info("Per-user alerts are not set up%s — nothing to do", f" ({problem})" if problem else "")
        return 1 if problem else 0
    seen = json.loads((BASE / "seen_jobs.json").read_text("utf-8"))
    send = None
    if args.send:
        from notifier import send_alerts as send
    stats = run(store, seen if isinstance(seen, list) else [], send, dry_run=not args.send)
    log.info("%s: %s", "sent" if args.send else "dry run", json.dumps(stats, sort_keys=True))
    return 1 if stats.get("failed") or stats.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
