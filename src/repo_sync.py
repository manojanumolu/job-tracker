"""Merge-safe publishing of the scraper's JSON data to the repository.

The Actions run and the Streamlit app both write seen_jobs.json /
companies.json. A text-level ``git rebase -X theirs`` let a run silently
overwrite a dismissal made in the app meanwhile. Instead, each publish
re-reads the latest remote copy and merges *records*:

  seen_jobs.json  — union by job identity; flags only move forward
                    (dismissed, notified, notify_state and the repost
                    window's repost_last_seen never go back)
  companies.json  — the remote list wins for membership and configuration
                    (added/removed/renamed in the app); this run only
                    contributes its scan results (status, last_checked, ...)
  logos.json      — this run's entries (it read the remote copy first); a
                    logo already found remotely is never replaced by a miss
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

from identity import job_uid

log = logging.getLogger("repo_sync")

BASE = Path(__file__).resolve().parent.parent          # the repository root (this file is in src/)
DATA_FILES = ("data/seen_jobs.json", "data/companies.json")
SCAN_FIELDS = ("status", "status_reason", "scan_note", "last_checked", "last_success", "last_job", "scan", "source")
_STATE_RANK = {None: 0, "": 0, "pending": 0, "claimed": 1, "sending": 2,
               "sent": 3, "skipped": 3, "failed": 3, "uncertain": 3}
_LIVE = ("claimed", "sending")
# the fields that say who holds a claim and how far it got
_CLAIM_FIELDS = ("claim_id", "claimed_at", "sending_at")


def record_key(job: dict) -> str:
    return job.get("uid") or job_uid(job.get("company", ""), job.get("url", ""))


def _owner(rec: dict) -> str:
    """Who holds a claim: its claim_id, or for a claim made before claim ids
    (Nov 2026) its claimed_at."""
    return rec.get("claim_id") or rec.get("claimed_at") or ""


def merge_record(remote: dict, local: dict) -> dict:
    """One job, merged from the latest remote copy and this run's copy.
    The publish is a compare-and-swap: a push only lands on the remote it
    was merged with, so the rules here decide every race —

      * flags only move forward (dismissed, notified, the state's rank);
      * a live claim belongs to whoever made it first: a different run's
        claim on the remote is kept, and this run's competing claim or
        "sending" mark is dropped (it then finds nothing of its own to send);
      * "sending" lands only on this run's own claim — never over a claim
        that was released or taken meanwhile;
      * a release (back to "pending", for a retry or a recovered claim)
        lands only when it names the claim the remote still holds."""
    merged = {**remote, **local}
    merged["dismissed"] = bool(remote.get("dismissed") or local.get("dismissed"))
    merged["notified"] = bool(remote.get("notified") or local.get("notified"))
    rs, ls = remote.get("notify_state"), local.get("notify_state")
    state = rs if _STATE_RANK.get(rs, 0) >= _STATE_RANK.get(ls, 0) else ls
    keep_remote_claim = False
    if rs in _LIVE and ls in _LIVE + ("sent", "skipped", "failed", "uncertain") and _owner(local) != _owner(remote):
        # another run holds this claim: nothing of ours changes it
        state, keep_remote_claim = rs, True
    elif ls in _LIVE and rs in ("sent", "skipped", "failed", "uncertain"):
        # settled elsewhere while we held a stale copy: the settled record,
        # and whose claim it was, stand
        state, keep_remote_claim = rs, True
    elif ls == "sending" and rs not in _LIVE:
        # our claim was released or finished elsewhere before we marked it:
        # we must not send it, so the remote state stands
        state, keep_remote_claim = rs, True
    # a run that claimed a job and then failed to send (or a later run that
    # recovered an abandoned claim) releases *that* claim so it is retried;
    # any other claim is never undone
    # — and only from the state it saw: a recovery releases an abandoned
    # "claimed", never a "sending" (which may have reached Gmail)
    released = local.get("released_claim")
    released_ok = (bool(released) and released == _owner(remote) and rs in _LIVE
                   and local.get("released_from", rs) == rs)
    if ls == "pending" and released_ok:
        state = "pending"
        keep_remote_claim = False
        for k in _CLAIM_FIELDS:
            merged.pop(k, None)
    elif ls == "claimed" and rs == "claimed" and released_ok and local.get("released_from") == "claimed":
        # a run recovered the abandoned claim and claimed the job itself in
        # the same publish: its claim replaces the abandoned one
        state, keep_remote_claim = "claimed", False
    if keep_remote_claim:
        for k in _CLAIM_FIELDS + ("send_attempts", "next_attempt_at", "released_claim", "released_from"):
            if k in remote:
                merged[k] = remote[k]
            else:
                merged.pop(k, None)
    # (legacy records without a notify_state are left exactly as they are)
    if merged["notified"] and (rs or ls):
        state = "sent"
    if state:
        merged["notify_state"] = state
    else:
        merged.pop("notify_state", None)
    for k in ("claimed_at", "notified_at"):
        if remote.get(k) and not local.get(k) and not (k == "claimed_at" and state == "pending"):
            merged[k] = remote[k]
    attempts = [a for a in (remote.get("send_attempts"), local.get("send_attempts")) if isinstance(a, int)]
    if attempts and not keep_remote_claim:
        merged["send_attempts"] = max(attempts)
    # the repost-window sighting (scraper.REPOST_LAST_SEEN, "YYYY-MM-DD")
    # only moves forward, like every other flag here
    stamps = [v for v in (remote.get("repost_last_seen"), local.get("repost_last_seen")) if isinstance(v, str) and v]
    if stamps:
        merged["repost_last_seen"] = max(stamps)
    if not merged["dismissed"]:
        merged.pop("dismissed", None)
    return merged


def merge_seen(remote: list, local: list) -> list:
    """Union of both lists by job identity; remote order first, new local records appended."""
    remote = [j for j in remote or [] if isinstance(j, dict)]
    local = [j for j in local or [] if isinstance(j, dict)]
    local_by_key = {}
    for j in local:
        local_by_key.setdefault(record_key(j), j)
    out, used = [], set()
    for r in remote:
        k = record_key(r)
        out.append(merge_record(r, local_by_key[k]) if k in local_by_key else r)
        used.add(k)
    for j in local:
        k = record_key(j)
        if k not in used:
            out.append(j)
            used.add(k)
    return out


def merge_companies(remote: list, local: list) -> list:
    local_by_id = {c.get("id"): c for c in local or [] if isinstance(c, dict)}
    out = []
    for r in remote or []:
        if not isinstance(r, dict):
            continue
        c = dict(r)
        mine = local_by_id.get(c.get("id"))
        if mine:
            for f in SCAN_FIELDS:
                if f in mine:
                    c[f] = mine[f]
        out.append(c)
    return out


def merge_logos(remote: dict | None, local: dict) -> dict:
    rem = (remote or {}).get("logos") if isinstance(remote, dict) else None
    rem = rem if isinstance(rem, dict) else {}
    mine = local.get("logos") if isinstance(local, dict) and isinstance(local.get("logos"), dict) else {}
    out = {}
    for k, e in mine.items():
        r = rem.get(k)
        keep_remote = (isinstance(r, dict) and r.get("status") == "ok"
                       and not (isinstance(e, dict) and e.get("status") == "ok"))
        out[k] = r if keep_remote else e
    return {**local, "logos": out}


# ---------------------------------------------------------------------------
# git plumbing
# ---------------------------------------------------------------------------

def _git(*args: str, cwd: Path = BASE, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, check=check, capture_output=True, text=True)


def _remote_json(ref: str, name: str, cwd: Path):
    r = _git("show", f"{ref}:{_repo_path(name, cwd)}", cwd=cwd, check=False)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except ValueError:
        return None


def _repo_path(name: str, cwd: Path) -> str:
    prefix = _git("rev-parse", "--show-prefix", cwd=cwd).stdout.strip()
    return f"{prefix}{name}"


def publish(message: str, local: dict[str, object], *, cwd: Path = BASE, branch: str | None = None,
            remote: str = "origin", attempts: int = 3) -> bool:
    """Merge ``local`` data ({"seen_jobs.json": [...], "companies.json": [...]})
    onto the latest remote branch and push. Returns True once pushed (or when
    nothing changed); False if it could not push — the caller must then not
    act as if the data were durable."""
    branch = branch or os.environ.get("PUBLISH_BRANCH", "main")
    for attempt in range(1, attempts + 1):
        try:
            _git("fetch", remote, branch, cwd=cwd)
            ref = f"{remote}/{branch}"
            merged = {}
            for name, data in local.items():
                rem = _remote_json(ref, name, cwd)
                kind = Path(name).name            # names are paths from the repository root ("data/...")
                if kind == "seen_jobs.json":
                    merged[name] = merge_seen(rem or [], data)
                elif kind == "companies.json":
                    merged[name] = merge_companies(rem, data) if rem is not None else data
                elif kind == "logos.json":
                    merged[name] = merge_logos(rem, data)
                else:
                    merged[name] = data
            # start from the remote tip, then write the merged data
            _git("reset", "--hard", ref, cwd=cwd)
            for name, data in merged.items():
                (cwd / name).write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                _git("add", name, cwd=cwd)
            if _git("diff", "--cached", "--quiet", cwd=cwd, check=False).returncode == 0:
                log.info("publish: no data changes")
                return True
            _git("commit", "-m", message, cwd=cwd)
            push = _git("push", remote, f"HEAD:{branch}", cwd=cwd, check=False)
            if push.returncode == 0:
                log.info("publish: pushed (%s)", message)
                return True
            log.warning("publish attempt %d rejected: %s", attempt, push.stderr.strip()[:200])
        except subprocess.CalledProcessError as e:
            log.warning("publish attempt %d failed: %s %s", attempt, e, (e.stderr or "").strip()[:200])
    return False
