from __future__ import annotations

import json
import logging
import os
from pathlib import Path

BASE = Path(__file__).parent
COMPANIES_FILE = BASE / "companies.json"
SETTINGS_FILE = BASE / "settings.json"
SEEN_FILE = BASE / "seen_jobs.json"

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
        _commit_file(COMPANIES_FILE, "companies.json", "chore: update companies.json")


def load_settings() -> dict:
    return _load(SETTINGS_FILE)


def save_settings(settings: dict, commit: bool = True) -> None:
    _save(SETTINGS_FILE, settings)
    if commit:
        _commit_file(SETTINGS_FILE, "settings.json", "chore: update settings.json")


def load_seen_jobs() -> list[dict]:
    if not SEEN_FILE.exists():
        return []
    return _load(SEEN_FILE)


def save_seen_jobs(jobs: list[dict], commit: bool = True) -> None:
    _save(SEEN_FILE, jobs)
    if commit:
        _commit_file(SEEN_FILE, "seen_jobs.json", "chore: update seen_jobs.json")


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
    from github import Github
    return Github(token).get_repo(GITHUB_REPO)


def fetch_remote_json(repo_paths: list[str], token: str | None = None, repo=None) -> dict:
    """Latest parsed content of each file on GitHub; a file that can't be
    fetched or parsed is left out (callers fall back to the local copy)."""
    token = token if token is not None else os.environ.get("GITHUB_TOKEN")
    if not token and repo is None:
        return {}
    out = {}
    try:
        repo = repo or _get_repo(token)
    except Exception as e:
        log.warning("GitHub unavailable for refresh: %s", e)
        return out
    for path in repo_paths:
        try:
            out[path] = json.loads(repo.get_contents(path).decoded_content.decode("utf-8"))
        except Exception as e:
            log.warning("Could not fetch %s from GitHub: %s", path, e)
    return out


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
