import json
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
