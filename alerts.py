"""Publish the scrape results and send alert emails — at most once per job.

Run by the Check Jobs workflow after scraper.py:

  1. claim    mark every alertable job (accepted, not dismissed, never sent)
              as "claimed"
  2. publish  merge + push the data. If this push fails, NOTHING is emailed:
              the jobs are not durable yet, and the next run finds them again.
  3. send     email the claimed jobs (skipping any dismissed meanwhile)
  4. publish  record "sent" (or release the claim if sending failed, so the
              next run retries)

Because the claim is pushed *before* the email goes out, a push failure after
sending can never cause the same job to be emailed again: the durable state
already says "claimed", which is never re-sent. (The trade-off is
at-most-once delivery: if the final push fails after a failed send, that
alert is not retried — it is logged loudly instead.)
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone

from config_store import alert_pending, load_companies, load_seen_jobs, load_settings
from repo_sync import publish, record_key

logging.basicConfig(level=logging.INFO, format="[alerts] %(message)s")
log = logging.getLogger("alerts")

DATA_MESSAGE = "chore: update seen_jobs + companies [skip ci]"
SENT_MESSAGE = "chore: record alert delivery [skip ci]"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def claim(seen: list[dict], now: str | None = None) -> set[str]:
    now = now or _now()
    keys = set()
    for j in seen:
        if alert_pending(j):
            j["notify_state"] = "claimed"
            j["claimed_at"] = now
            keys.add(record_key(j))
    return keys


def to_send(seen: list[dict], keys: set[str]) -> list[dict]:
    return [j for j in seen if isinstance(j, dict) and record_key(j) in keys
            and j.get("notify_state") == "claimed" and not j.get("dismissed") and not j.get("notified")]


def finalize(seen: list[dict], keys: set[str], sent: bool, now: str | None = None) -> None:
    now = now or _now()
    for j in seen:
        if not isinstance(j, dict) or record_key(j) not in keys or j.get("notify_state") != "claimed":
            continue
        if j.get("dismissed"):
            j["notify_state"] = "skipped"       # dismissed before the email went out
        elif sent:
            j["notified"] = True
            j["notify_state"] = "sent"
            j["notified_at"] = now
        else:
            j["notify_state"] = "pending"       # release our own claim -> retried next run
            j["released_claim"] = j.pop("claimed_at", None)


def run(send=None, publisher=publish) -> int:
    if send is None:
        from notifier import send_alerts as send
    seen = load_seen_jobs()
    companies = load_companies()
    recipient = (load_settings().get("recipient_email") or "").strip()
    keys = claim(seen) if recipient else set()
    if not recipient:
        log.info("No recipient email configured — publishing data only")

    if not publisher(DATA_MESSAGE, {"seen_jobs.json": seen, "companies.json": companies}):
        log.error("Could not publish the scrape results — no email sent (the jobs will be found again next run)")
        return 1
    if not keys:
        log.info("No new alerts")
        return 0

    seen = load_seen_jobs()          # the merged, published copy
    jobs = to_send(seen, keys)
    ok = True
    if jobs:
        try:
            send(jobs, recipient)
            log.info("Sent %d job alert(s) to %s", len(jobs), recipient)
        except Exception as e:
            ok = False
            log.error("Sending the alert email failed: %s — claims released, will retry next run", e)
    finalize(seen, keys, ok)
    if not publisher(SENT_MESSAGE, {"seen_jobs.json": seen}):
        log.error("Could not record the delivery state. The jobs stay 'claimed' and are NOT re-sent. %s",
                  "" if ok else "Because sending also failed, these alerts were lost: "
                  + ", ".join(j.get("title", "?") for j in jobs))
        return 1
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
