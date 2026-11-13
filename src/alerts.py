"""Publish the scrape results and send the shared alert email — at most once per job.

Run by the Check Jobs workflow after scraper.py. Every state change below is
published (merged with the latest remote copy and pushed) before the next
step, and a push lands only on the copy it was merged with, so publishing is
a compare-and-swap: repo_sync.merge_record decides every race (Nov 2026).

  0. recover  claims abandoned by a run that died (config_store.CLAIM_TTL):
              never-sent ones ("claimed") go back to pending and are retried;
              ones that may have reached Gmail ("sending") become "uncertain"
              and are never resent; claims from before claim ids are treated
              as "uncertain" too (the old code could have sent them)
  1. claim    mark every alertable job "claimed" with this run's claim_id
  2. publish  data + claims. If this push fails, NOTHING is emailed.
  3. mark     "sending", only on claims this run still owns, then publish —
              if that push fails, nothing is sent (the claims are recovered
              by a later run, safely: they were never attempted)
  4. send     one email with the jobs this run marked
  5. record   "sent"; or, if Gmail definitely did not take it, back to pending
              with a backoff (or "failed" after MAX_SEND_ATTEMPTS); or, if its
              fate is unknown (connection lost after the message was handed
              over), "uncertain" — then publish

Guarantee: a job is emailed at most once. Not exactly once: if the process
dies after Gmail accepts the message but before step 5 is published, the job
stays "sending" and the next run records it "uncertain" (delivered, but not
known to be). Monitoring counts every such case (config_store.delivery_health).
"""

from __future__ import annotations

import logging
import os
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone

from access import alert_recipient
from config_store import (
    CLAIM_TTL, MAX_SEND_ATTEMPTS, _iso_time, delivery_health, excluded_from_shared_alerts, load_companies,
    load_seen_jobs, load_settings, retry_backoff, retry_due, send_attempts, shared_alert_cutoff, shared_alert_pending,
    shared_tailoring,
)
from email_policy import email_domain
from repo_sync import publish, record_key

logging.basicConfig(level=logging.INFO, format="[alerts] %(message)s")
log = logging.getLogger("alerts")

DATA_MESSAGE = "chore: update seen_jobs + companies [skip ci]"
SENDING_MESSAGE = "chore: mark alerts as sending [skip ci]"
SENT_MESSAGE = "chore: record alert delivery [skip ci]"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(now: datetime) -> str:
    return now.isoformat()


def recover(seen: list[dict], now: datetime, stats: Counter) -> None:
    """Step 0: settle claims abandoned by a run that died (see the module doc)."""
    for j in seen:
        if not isinstance(j, dict):
            continue
        state = j.get("notify_state")
        if state not in ("claimed", "sending"):
            continue
        if state == "claimed" and not j.get("claim_id"):
            # a claim from before claim ids: that code sent without marking
            # "sending" first, so this job may have been emailed
            j["notify_state"] = "uncertain"
            stats["legacy_claims_uncertain"] += 1
            continue
        since = _iso_time(j.get("sending_at") if state == "sending" else j.get("claimed_at"))
        if since is not None and now - since <= CLAIM_TTL:
            continue                                     # a live run may still own it
        if state == "sending":
            j["notify_state"] = "uncertain"
            stats["abandoned_sending_uncertain"] += 1
        else:
            j["notify_state"] = "pending"
            j["released_claim"] = j.get("claim_id")
            j["released_from"] = "claimed"          # repo_sync.merge_record: never lands on a "sending"
            stats["abandoned_claims_recovered"] += 1


def claim(seen: list[dict], now: str | datetime | None = None, excluded: frozenset[str] = frozenset(),
          tailoring: dict | None = None, cutoff: datetime | None = None, run_id: str = "") -> set[str]:
    """Step 1. ``excluded``: companies outside the shared alerts (another
    admin's monitoring); ``tailoring``: the shared email's tailoring;
    ``cutoff``: the shared email's cutoff (backlog is never claimed). A job
    waiting out its retry backoff is not claimed yet."""
    when = now if isinstance(now, datetime) else (_iso_time(now) if now else _now())
    run_id = run_id or uuid.uuid4().hex
    keys = set()
    for j in seen:
        if shared_alert_pending(j, excluded, tailoring, cutoff) and retry_due(j, when):
            j["notify_state"] = "claimed"
            j["claim_id"] = run_id
            j["claimed_at"] = _iso(when)
            j.pop("sending_at", None)
            keys.add(record_key(j))
    return keys


def _mine(seen: list[dict], keys: set[str], run_id: str, state: str) -> list[dict]:
    return [j for j in seen if isinstance(j, dict) and record_key(j) in keys
            and j.get("notify_state") == state and j.get("claim_id") == run_id]


def to_send(seen: list[dict], keys: set[str], run_id: str = "") -> list[dict]:
    """The jobs this run marked "sending" and that nobody dismissed meanwhile."""
    return [j for j in _mine(seen, keys, run_id, "sending") if not j.get("dismissed") and not j.get("notified")]


def finalize(seen: list[dict], keys: set[str], outcome: str, now: datetime | None = None,
             run_id: str = "", stats: Counter | None = None) -> None:
    """Step 5 for this run's "sending" jobs. ``outcome``: "sent",
    "not_sent" (Gmail definitely refused or was unreachable) or "uncertain"."""
    now = now or _now()
    stats = stats if stats is not None else Counter()
    for j in _mine(seen, keys, run_id, "sending"):
        if j.get("dismissed") and outcome != "sent":
            j["notify_state"] = "skipped"             # dismissed before the email went out
            continue
        if outcome == "sent":
            j["notified"] = True
            j["notify_state"] = "sent"
            j["notified_at"] = _iso(now)
            stats["sent"] += 1
        elif outcome == "uncertain":
            j["notify_state"] = "uncertain"
            stats["uncertain"] += 1
        else:
            attempts = send_attempts(j) + 1
            j["send_attempts"] = attempts
            if attempts >= MAX_SEND_ATTEMPTS:
                j["notify_state"] = "failed"
                stats["failed_final"] += 1
            else:
                j["notify_state"] = "pending"          # release our own claim -> retried after the backoff
                j["released_claim"] = run_id
                j["released_from"] = "sending"
                j["next_attempt_at"] = _iso(now + retry_backoff(attempts))
                stats["retries_scheduled"] += 1


def _usable_recipient(addr: str) -> bool:
    return bool(addr) and "@" in addr and bool(email_domain(addr))


def run(send=None, publisher=publish, now: datetime | None = None, run_id: str | None = None) -> int:
    if send is None:
        from notifier import send_alerts as send
    from notifier import DeliveryUncertain

    now = now or _now()
    run_id = run_id or uuid.uuid4().hex
    stats: Counter = Counter()
    seen = load_seen_jobs()
    companies = load_companies()
    try:
        settings = load_settings()
    except (OSError, ValueError):
        settings = {}
    # the address is never logged, not even masked: Actions logs of a public repo are public
    recipient, _ = alert_recipient(settings)
    if recipient and not _usable_recipient(recipient):
        log.error("The ALERT_RECIPIENT secret is not a usable email address — publishing data only")
        recipient = ""
    recover(seen, now, stats)
    keys = (claim(seen, now, excluded=excluded_from_shared_alerts(companies), tailoring=shared_tailoring(settings),
                  cutoff=shared_alert_cutoff(settings, now), run_id=run_id)
            if recipient else set())
    if not recipient:
        log.info("No recipient email configured (the ALERT_RECIPIENT secret) — publishing data only")

    if not publisher(DATA_MESSAGE, {"data/seen_jobs.json": seen, "data/companies.json": companies}):
        log.error("Could not publish the scrape results — no email sent (the jobs will be found again next run)")
        return 1
    if not keys:
        _report(stats, load_seen_jobs(), now)
        log.info("No new alerts")
        return 0

    # step 3: mark "sending" on the claims this run still owns after the merge
    seen = load_seen_jobs()
    owned = _mine(seen, keys, run_id, "claimed")
    if len(owned) < len(keys):
        stats["claims_lost_to_another_run"] += len(keys) - len(owned)
    for j in owned:
        if j.get("dismissed"):
            j["notify_state"] = "skipped"
        else:
            j["notify_state"] = "sending"
            j["sending_at"] = _iso(now)
    if not owned:
        _report(stats, seen, now)
        log.info("No alerts to send: every claim belongs to another run")
        return 0
    if not publisher(SENDING_MESSAGE, {"data/seen_jobs.json": seen}):
        log.error("Could not record the send attempt — nothing sent; the claims are retried by a later run")
        return 1

    # step 4: send exactly what this run marked, from the merged copy
    seen = load_seen_jobs()
    jobs = to_send(seen, keys, run_id)
    outcome = "sent"
    if jobs:
        try:
            send(jobs, recipient)
            log.info("Sent %d job alert(s) to the configured recipient", len(jobs))      # no address, not even masked
        except DeliveryUncertain as e:
            outcome = "uncertain"
            log.error("Sending the alert email stopped after the message was handed to Gmail (%s) — recorded as "
                      "uncertain, never resent", getattr(e, "stage", type(e).__name__))
        except Exception as e:
            outcome = "not_sent"
            # the exception TYPE / stage only: an SMTP error can echo the address
            log.error("Sending the alert email failed (%s) — not sent; retried after a backoff",
                      getattr(e, "stage", "") or type(e).__name__)
    finalize(seen, keys, outcome, now, run_id, stats)
    if not publisher(SENT_MESSAGE, {"data/seen_jobs.json": seen}):
        log.error("Could not record the delivery outcome. The jobs stay 'sending' and a later run records them "
                  "'uncertain' — they are NOT re-sent. %s",
                  "" if outcome == "sent" else "Because sending did not succeed either, these alerts were lost: "
                  + ", ".join(j.get("title", "?") for j in jobs))
        return 1
    _report(stats, load_seen_jobs(), now)
    return 0 if outcome == "sent" else 1


def _report(stats: Counter, seen: list[dict], now: datetime) -> None:
    """One log line of counts for monitoring (no job, title or address)."""
    health = delivery_health(seen, now)
    log.info("delivery: this run %s · ledger %s", dict(sorted(stats.items())) or "{}", health)


if __name__ == "__main__":
    # publish() hard-resets the working tree to the remote branch tip — right
    # for a throwaway Actions checkout, destructive on a developer machine
    if os.environ.get("GITHUB_ACTIONS") != "true" and os.environ.get("ALERTS_ALLOW_LOCAL") != "1":
        sys.exit("alerts.py resets this checkout to the remote branch; it only runs in GitHub Actions "
                 "(set ALERTS_ALLOW_LOCAL=1 to run it in a disposable clone).")
    sys.exit(run())
