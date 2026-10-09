"""Merge-safe publishing of the scraper's JSON data to the repository.

The Actions run and the Streamlit app both write seen_jobs.json /
companies.json. A text-level ``git rebase -X theirs`` let a run silently
overwrite a dismissal made in the app meanwhile. Instead, each publish
re-reads the latest remote copy and merges *records*:

  seen_jobs.json  — union by job identity; flags only move forward
                    (dismissed, notified, notify_state never go back)
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

BASE = Path(__file__).parent
DATA_FILES = ("seen_jobs.json", "companies.json")
SCAN_FIELDS = ("status", "status_reason", "scan_note", "last_checked", "last_job", "scan", "source")
_STATE_RANK = {None: 0, "": 0, "pending": 0, "claimed": 1, "sent": 2, "skipped": 2}


def record_key(job: dict) -> str:
    return job.get("uid") or job_uid(job.get("company", ""), job.get("url", ""))


def merge_record(remote: dict, local: dict) -> dict:
    merged = {**remote, **local}
    merged["dismissed"] = bool(remote.get("dismissed") or local.get("dismissed"))
    merged["notified"] = bool(remote.get("notified") or local.get("notified"))
    rs, ls = remote.get("notify_state"), local.get("notify_state")
    state = rs if _STATE_RANK.get(rs, 0) >= _STATE_RANK.get(ls, 0) else ls
    # a run that claimed a job and then failed to send releases *its own*
    # claim so the next run retries; any other claim is never undone
    released = local.get("released_claim")
    if ls == "pending" and rs == "claimed" and released and released == remote.get("claimed_at"):
        state = "pending"
        merged.pop("claimed_at", None)
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
                if name == "seen_jobs.json":
                    merged[name] = merge_seen(rem or [], data)
                elif name == "companies.json":
                    merged[name] = merge_companies(rem, data) if rem is not None else data
                elif name == "logos.json":
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
