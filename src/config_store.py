from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent          # the repository root (this file is in src/)
DATA = BASE / "data"
COMPANIES_FILE = DATA / "companies.json"
SETTINGS_FILE = DATA / "settings.json"
SEEN_FILE = DATA / "seen_jobs.json"

# GitHub repo details (owner/repo)
GITHUB_REPO = "manojanumolu/job-tracker"


def _load(path: Path) -> any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(path: Path, data: any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_companies() -> list[dict]:
    return _load(COMPANIES_FILE)


def save_companies(companies: list[dict], commit: bool = True) -> None:
    _save(COMPANIES_FILE, companies)
    if commit:
        _commit_file(COMPANIES_FILE, "data/companies.json", "chore: update companies.json")


def load_settings() -> dict:
    return _load(SETTINGS_FILE)


def save_settings(settings: dict, commit: bool = True) -> None:
    _save(SETTINGS_FILE, settings)
    if commit:
        _commit_file(SETTINGS_FILE, "data/settings.json", "chore: update settings.json")


def load_seen_jobs() -> list[dict]:
    if not SEEN_FILE.exists():
        return []
    return _load(SEEN_FILE)


def save_seen_jobs(jobs: list[dict], commit: bool = True) -> None:
    _save(SEEN_FILE, jobs)
    if commit:
        _commit_file(SEEN_FILE, "data/seen_jobs.json", "chore: update seen_jobs.json")


def _commit_file(local_path: Path, repo_path: str, message: str) -> None:
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        return
    try:
        from github import Github, GithubException
        g = Github(token)
        repo = g.get_repo(GITHUB_REPO)
        content = local_path.read_text(encoding="utf-8")
        try:
            existing = repo.get_contents(repo_path)
            repo.update_file(repo_path, message, content, existing.sha)
        except GithubException:
            repo.create_file(repo_path, message, content)
    except Exception as e:
        print(f"[config_store] GitHub commit failed for {repo_path}: {e}")


# ---------------------------------------------------------------------------
# Merge-safe updates (used by the Streamlit app)
# ---------------------------------------------------------------------------
# The app's local checkout can be hours behind GitHub: the Actions run commits
# new seen_jobs/companies data every 3 hours. Uploading the local copy would
# silently overwrite those commits — dropping newly found jobs from the dedup
# history (so they get emailed again) or undoing a company added elsewhere.
# update_json() instead applies one change to the *latest* GitHub copy.

log = logging.getLogger(__name__)

_MAX_CONFLICT_RETRIES = 3


def friendly_github_error(e: Exception) -> str:
    """A short user-facing message; details go to the log, not the UI."""
    status = getattr(e, "status", None)
    if status == 401:
        return "GitHub rejected the app's token as invalid or expired."
    if status == 403:
        return "The app's GitHub token doesn't have permission to save changes."
    if status == 404:
        return "The app's GitHub token can't access the repository."
    return "GitHub couldn't be reached right now — please try again in a minute."


def _get_repo(token: str):
    """The repository handle, lazily: no request is made for the repository
    itself, so a read or a save starts with the request it actually needs
    (one GitHub round trip fewer each). Errors such as a bad token surface on
    that first real request instead."""
    from github import Github
    try:
        return Github(token, lazy=True).get_repo(GITHUB_REPO)
    except TypeError:                     # PyGithub without Github(lazy=...)
        return Github(token).get_repo(GITHUB_REPO, lazy=True)


def fetch_remote_json(repo_paths: list[str], token: str | None = None, repo=None) -> dict:
    """Latest parsed content of each file on GitHub; a file that can't be
    fetched or parsed is left out (callers fall back to the local copy).
    With a token the files are read side by side (each on its own client),
    so an explicit Refresh waits for the slowest file, not for all of them
    one after another."""
    token = token if token is not None else os.environ.get("GITHUB_TOKEN")
    if not token and repo is None:
        return {}

    def _one(path: str, handle):
        try:
            handle = handle or _get_repo(token)
            return json.loads(handle.get_contents(path).decoded_content.decode("utf-8"))
        except Exception as e:
            log.warning("Could not fetch %s from GitHub: %s", path, e)
            return None

    if repo is not None or len(repo_paths) < 2:
        got = [(p, _one(p, repo)) for p in repo_paths]
    else:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(len(repo_paths), 6)) as pool:
            got = list(zip(repo_paths, pool.map(lambda p: _one(p, None), repo_paths)))
    return {p: v for p, v in got if v is not None}


def update_json(local_path: Path, repo_path: str, mutate, default, message: str,
                token: str | None = None, repo=None) -> tuple[object, bool, str]:
    """Apply ``mutate(data) -> data`` to the freshest copy of a JSON file and
    save it locally and (when a token is configured) to GitHub.

    Returns (data, saved_permanently, user_message_if_not)."""
    token = token if token is not None else os.environ.get("GITHUB_TOKEN")

    def _local_only(reason: str):
        data = mutate(_load(local_path) if local_path.exists() else default)
        _save(local_path, data)
        return data, False, reason

    if not token and repo is None:
        return _local_only("No GitHub token is configured, so this change only lasts until the app restarts.")
    try:
        repo = repo or _get_repo(token)
    except Exception as e:
        log.warning("GitHub unavailable for %s: %s", repo_path, e)
        return _local_only(friendly_github_error(e))

    from github import GithubException

    last_error: Exception | None = None
    for _attempt in range(_MAX_CONFLICT_RETRIES):
        try:
            try:
                existing = repo.get_contents(repo_path)
                current = json.loads(existing.decoded_content.decode("utf-8"))
            except GithubException as ge:
                if ge.status != 404:
                    raise
                existing, current = None, default
            data = mutate(current)
            content = json.dumps(data, indent=2, ensure_ascii=False)
            if existing is None:
                repo.create_file(repo_path, message, content)
            else:
                repo.update_file(repo_path, message, content, existing.sha)
            _save(local_path, data)
            return data, True, ""
        except GithubException as ge:
            last_error = ge
            if ge.status == 409:  # someone else committed in between — re-read and retry
                continue
            break
        except Exception as e:
            last_error = e
            break
    log.warning("Saving %s to GitHub failed: %s", repo_path, last_error)
    return _local_only(friendly_github_error(last_error))


# ---------------------------------------------------------------------------
# Alert records
# ---------------------------------------------------------------------------
# seen_jobs.json doubles as the scraper's dedup history: a record removed from
# it is treated as new on the next run and emailed again. The UI therefore
# *dismisses* alerts (hides them) instead of deleting records.

def job_key(job: dict) -> str:
    """Stable identity of an alert record. ``id`` alone isn't unique — it's
    built from the first 40 characters of the title — so the URL is included."""
    base = job.get("id") or f"{job.get('company', '')}_{job.get('title', '')}"
    return f"{base}|{job.get('url', '')}"


ALERT_CATEGORIES = ("FRESHER", "ENTRY_LEVEL")

# The shared email's delivery states (seen_jobs.json "notify_state", Nov 2026):
#   pending / absent  may be claimed (after next_attempt_at, if a retry)
#   claimed           a run owns it (claim_id) but has NOT started sending —
#                     abandoned for CLAIM_TTL, it is released and retried
#   sending           written durably just before the message goes to Gmail —
#                     abandoned for CLAIM_TTL, it becomes "uncertain", never resent
#   sent              Gmail accepted it (final)
#   skipped           dismissed before it was sent (final)
#   failed            MAX_SEND_ATTEMPTS definite failures (final)
#   uncertain         it may have been delivered: a crash or lost connection
#                     after the message was handed over (final, never resent)
NOTIFY_LIVE_CLAIMS = ("claimed", "sending")
NOTIFY_FINAL = ("sent", "skipped", "failed", "uncertain")
CLAIM_TTL = timedelta(hours=2)          # far longer than a publish job (30 min cap)
MAX_SEND_ATTEMPTS = 5
RETRY_BACKOFF = (timedelta(minutes=15), timedelta(minutes=30), timedelta(hours=1), timedelta(hours=2))


def retry_backoff(attempts: int) -> timedelta:
    """How long after its n-th definite failure a job waits (15 min, 30 min,
    1 h, then 2 h; runs come every few hours, so this mostly means "next run")."""
    return RETRY_BACKOFF[min(max(attempts, 1), len(RETRY_BACKOFF)) - 1]


def retry_due(job: dict, now: datetime | None = None) -> bool:
    """A job whose last send failed may be claimed again only after its backoff."""
    at = _iso_time(job.get("next_attempt_at")) if isinstance(job, dict) else None
    return at is None or (now or datetime.now(timezone.utc)) >= at


def send_attempts(job: dict) -> int:
    """The failed-send count on a record; anything malformed counts as 0."""
    n = job.get("send_attempts") if isinstance(job, dict) else None
    return n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else 0


def delivery_health(seen: list[dict], now: datetime | None = None) -> dict[str, int]:
    """Counts for monitoring the shared email's ledger (no job or address in
    them): live claims, claims older than CLAIM_TTL, retries waiting, and the
    final outcomes."""
    now = now or datetime.now(timezone.utc)
    out = {k: 0 for k in ("claimed", "sending", "stale_claims", "retrying", "failed", "uncertain", "sent")}
    for j in seen or []:
        if not isinstance(j, dict):
            continue
        state = j.get("notify_state")
        if state in out:
            out[state] += 1
        if state in NOTIFY_LIVE_CLAIMS:
            at = _iso_time(j.get("sending_at") if state == "sending" else j.get("claimed_at"))
            if at is None or now - at > CLAIM_TTL:
                out["stale_claims"] += 1
        if state in (None, "", "pending") and send_attempts(j) > 0:
            out["retrying"] += 1
    return out


def alert_pending(job: dict) -> bool:
    """Should this record still be emailed? Only accepted jobs that passed
    every safety-gate check (stored in ``evidence.checks`` by the scraper),
    were never sent, never claimed by a run and are not dismissed. Records
    without a category or gate evidence (pre-classifier) are never emailed."""
    if not isinstance(job, dict):
        return False
    checks = (job.get("evidence") or {}).get("checks") if isinstance(job.get("evidence"), dict) else None
    return (
        job.get("category") in ALERT_CATEGORIES
        and isinstance(checks, dict) and bool(checks) and all(checks.values())
        and not job.get("dismissed")
        and not job.get("notified")
        and job.get("notify_state") not in NOTIFY_LIVE_CLAIMS + NOTIFY_FINAL
    )


# The shared alerts (alerts.py -> ALERT_RECIPIENT) are the PRIMARY admin's
# monitoring scope (access.py). A company another admin adds for their own
# monitoring is marked "shared_alerts": false: still scanned (one shared
# scanner), shown and followable, but its jobs never reach the shared
# email. Absent means included — every company from before Oct 2026 is.

# A company kept in the catalogue that has no usable job source yet: it stays
# listed and isn't scanned. Whether a company has suitable jobs today is never
# a monitoring state. (A configured source that stops working isn't one either:
# the scan keeps trying it and its status recovers by itself.)
MONITORING_BLOCKED, MONITORING_PENDING = "blocked", "pending"


def monitoring_state(company: dict) -> str:
    value = company.get("monitoring") if isinstance(company, dict) else ""
    return value if value in (MONITORING_BLOCKED, MONITORING_PENDING) else ""


def in_shared_alerts(company: dict) -> bool:
    return not (isinstance(company, dict) and company.get("shared_alerts") is False)


def excluded_from_shared_alerts(companies: list[dict] | None) -> frozenset[str]:
    """IDs of the catalogue companies outside the shared alerts."""
    return frozenset(str(c["id"]) for c in companies or []
                     if isinstance(c, dict) and c.get("id") and not in_shared_alerts(c))


SHARED_TAILORING_KEY = "shared_alert_tailoring"


def shared_tailoring(settings: dict | None) -> dict | None:
    """The primary admin's email tailoring for the shared email, from
    settings.json: {"locations": [...], "job_families": [...]} in the same
    vocabulary as a person's own tailoring (job_filters). None = every
    qualifying job, exactly as before this setting existed."""
    t = settings.get(SHARED_TAILORING_KEY) if isinstance(settings, dict) else None
    return t if isinstance(t, dict) else None


# The shared email's cutoff (Nov 2026). Widening what the shared email
# matches — its tailoring in settings.json, or the job families in
# job_filters — must never send the backlog of jobs saved earlier that the
# old rules held back (60 records at the time) as if they were new. So a job
# is claimed only if it was first found at or after SHARED_SINCE_KEY (an ISO
# time in settings.json, moved forward whenever those rules change) AND
# within SHARED_MAX_AGE of now — the second bound needs no one to remember
# the first. A job whose own earlier send failed (its claim was released,
# "released_claim") is a retry, not backlog: it is exempt from both, so a
# mail outage still never loses an alert. Personal alerts have their own
# cutoff (the person's enabled_at, user_alerts.py) and don't use this.
SHARED_SINCE_KEY = "shared_alert_since"
SHARED_MAX_AGE = timedelta(days=7)


def _iso_time(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def shared_alert_cutoff(settings: dict | None, now: datetime | None = None) -> datetime:
    """The earliest first_seen the shared email still claims: the later of
    settings.json's shared_alert_since (ignored if missing or malformed) and
    now - SHARED_MAX_AGE."""
    now = now or datetime.now(timezone.utc)
    floor = now - SHARED_MAX_AGE
    since = _iso_time(settings.get(SHARED_SINCE_KEY)) if isinstance(settings, dict) else None
    return max(floor, since) if since else floor


def within_shared_cutoff(job: dict, cutoff: datetime | None) -> bool:
    """Not backlog: first found at/after the cutoff, or a retry. A record
    with no readable first_seen can't be dated, so the cutoff can't judge
    it (every record the scraper writes has one: a dateless record is a
    pre-classifier one, which alert_pending already never emails)."""
    if cutoff is None or not isinstance(job, dict) or job.get("released_claim"):
        return True
    found = _iso_time(job.get("first_seen"))
    return found is None or found >= cutoff


def shared_alert_pending(job: dict, excluded: frozenset[str] | set[str] = frozenset(),
                         tailoring: dict | None = None, cutoff: datetime | None = None) -> bool:
    """alert_pending, within the shared alerts' companies, the shared
    email's tailoring and its cutoff (shared_alert_cutoff; None = no
    cutoff). A job outside them stays untouched (pending) in the shared
    ledger: it is simply never claimed for the shared email."""
    if not (alert_pending(job) and job.get("company_id") not in excluded and within_shared_cutoff(job, cutoff)):
        return False
    if not tailoring:
        return True
    from job_filters import matches_tailoring          # job_filters imports this module
    return matches_tailoring(job, tailoring)


def visible_jobs(seen: list[dict]) -> list[dict]:
    """Alerts to show, newest first (records are appended oldest-first)."""
    return [j for j in reversed(seen) if isinstance(j, dict) and not j.get("dismissed")]


def dismiss_jobs(keys: set[str] | None = None):
    """A mutate() for update_json that hides the given alerts (all when
    ``keys`` is None) while keeping them in the dedup history."""
    def _mutate(seen: list[dict]) -> list[dict]:
        for j in seen:
            if isinstance(j, dict) and (keys is None or job_key(j) in keys):
                j["dismissed"] = True
        return seen
    return _mutate
