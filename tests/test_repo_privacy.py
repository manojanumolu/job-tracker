"""Public-repository privacy (2026-10-13).

The repository is public, so:
* no tracked file holds a real credential (only documented placeholders and
  obviously fake test values are allowed)
* no secrets / credential file is tracked, and .gitignore keeps them out
* the alert recipient comes ONLY from the ALERT_RECIPIENT secret — never
  from settings.json, so a personal address never has to be committed
* the public data files hold no email address
* removing the recipient changed nothing else: the committed scan history,
  deduplication identities, dismissals and scheduler state still load
"""
import json
import re
import subprocess
from pathlib import Path

import pytest

import access
import alerts
import config_store
import notifier
import repo_sync
import scan_schedule

REPO = Path(__file__).resolve().parent.parent
README = """# Fresher Job Tracker
An automated platform for discovering fresher and entry-level job opportunities in India.
Monitors company career pages for relevant openings.
Filters opportunities using job eligibility and experience criteria.
Sends email alerts when qualifying new jobs are found.
Features a dashboard for monitoring jobs, companies, and scan status.
Built with Python, Streamlit, and GitHub Actions.
"""

# Credential shapes. A match is a failure unless the value is a documented
# placeholder or an obviously fake test value (SAFE below).
SECRET_PATTERNS = {
    "Google API key": r"AIza[0-9A-Za-z_-]{35}",
    "GitHub token": r"\b(?:gh[pousr]_[0-9A-Za-z]{36,}|github_pat_[0-9A-Za-z_]{60,})",
    "PEM private key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?:\\n|\s)*[A-Za-z0-9+/=]{100,}",
    "Gmail app password": r"(?i)(?:app_?password|GMAIL_APP_PASSWORD)\s*[=:]\s*[\"']?([a-z]{4}\s?[a-z]{4}\s?[a-z]{4}\s?[a-z]{4})\b",
    "assigned secret": r"(?i)\b(?:client_secret|cookie_secret|JT_OWNER_PASSWORD|api_key|FIREBASE_WEB_API_KEY|GITHUB_TOKEN)"
                       r"\b\s*[=:]\s*[\"']?([^\"'\s,}]{12,})",
    "Slack / OpenAI key": r"(?:xox[baprs]-[0-9A-Za-z-]{10,}|sk-[A-Za-z0-9]{32,})",
    "signed JWT": r"eyJ[A-Za-z0-9_-]{20,}\.eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}",
}
SAFE = re.compile(r"(?i)x{6,}|placeholder|fake|never-real|your-|example|dummy|not-real|test|\$\{\{|secrets\.|"
                  r"os\.environ|getenv|st\.secrets|<[a-z_ -]+>|\.\.\.|…|redacted")
SECRET_FILES = re.compile(r"(?i)(^|/)(\.env(\..+)?|secrets\.toml|.*service[-_]?account.*\.json|.*firebase-adminsdk.*\.json"
                          r"|.*\.(pem|key|p12|pfx)|id_rsa.*|credentials?\.json)$")
DATA_FILES = ("data/settings.json", "data/companies.json", "data/seen_jobs.json", "data/logos.json")


def tracked_files() -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True).stdout
        files = [f for f in out.splitlines() if f]
    except (OSError, subprocess.CalledProcessError):        # e.g. an exported copy without .git
        files = [p.relative_to(REPO).as_posix() for p in REPO.rglob("*")
                 if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts
                 and ".pytest_cache" not in p.parts]
    return [f for f in files if (REPO / f).is_file()]


def _text(path: str) -> str | None:
    data = (REPO / path).read_bytes()
    if b"\0" in data[:4096]:
        return None                                          # binary (the logo)
    return data.decode("utf-8", errors="replace")


def find_secrets(text: str) -> list[tuple[str, str]]:
    """(kind, redacted value) for every credential-shaped value that isn't
    a documented placeholder."""
    found = []
    for kind, pattern in SECRET_PATTERNS.items():
        for m in re.finditer(pattern, text):
            value = m.group(m.lastindex or 0)
            line = text[text.rfind("\n", 0, m.start()) + 1:text.find("\n", m.end())]
            if SAFE.search(value) or SAFE.search(line):
                continue
            found.append((kind, value[:3] + "…"))                # never the value itself
    return found


# ---------------------------------------------------------------------------
# the scanner itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "key = AIzaSyA" + "b" * 32,
    "token: ghp_" + "a1B2" * 9,
    "GMAIL_APP_PASSWORD=" + "abcd efgh " + "ijkl mnop",
    'client_secret = "GOCSPX-' + "Zq9" * 6 + '"',
    '"private_key": "-----BEGIN PRIVATE KEY-----\\n' + "MIIEv" * 40 + '"',
    ".".join(["eyJ" + "hbGciOiJSUzI1NiIsImtpZCI6Ijk", "eyJ" + "zdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6", "SflKxwRJSMeKKF2QT4fwpMeJf36POk6"]),
])
def test_the_scanner_catches_real_looking_credentials(text):
    assert find_secrets(text)


@pytest.mark.parametrize("text", [
    "GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxx",
    "GMAIL_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx",
    "FIREBASE_WEB_API_KEY=your-firebase-web-api-key",
    'API_KEY = "AIzaFAKE-test-key-never-real-0123456789"',
    "GMAIL_APP_PASSWORD: ${{ secrets.GMAIL_APP_PASSWORD }}",
    '"private_key": "-----BEGIN PRIVATE KEY-----\\nnot-real\\n"',
])
def test_documented_placeholders_are_allowed(text):
    assert find_secrets(text) == []


# ---------------------------------------------------------------------------
# the repository as committed
# ---------------------------------------------------------------------------

def test_no_tracked_file_contains_a_credential():
    problems = {}
    for f in tracked_files():
        text = _text(f)
        if text is not None and (hits := find_secrets(text)):
            problems[f] = hits
    assert problems == {}


def test_no_secrets_file_is_tracked():
    assert [f for f in tracked_files() if SECRET_FILES.search(f) and f != ".env.example"] == []


def test_env_example_holds_placeholders_only():
    for line in (REPO / ".env.example").read_text("utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            value = line.split("=", 1)[1].strip()
            assert SAFE.search(value) or re.fullmatch(r"x[x-]*", value), line.split("=", 1)[0]


@pytest.mark.parametrize("path, ignored", [
    (".env", True), (".env.local", True), (".env.production", True), (".streamlit/secrets.toml", True),
    ("job-tracker-3-firebase-adminsdk-a1b2c.json", True), ("service-account.json", True), ("deploy.pem", True),
    (".env.example", False), ("data/settings.json", False), ("data/companies.json", False),
])
def test_gitignore_keeps_secrets_out(path, ignored):
    try:
        r = subprocess.run(["git", "check-ignore", "-q", "--no-index", path], cwd=REPO, capture_output=True)
    except OSError:
        pytest.skip("git isn't available")
    if r.returncode not in (0, 1):
        pytest.skip("not a git checkout")
    assert (r.returncode == 0) is ignored


@pytest.mark.parametrize("name", DATA_FILES)
def test_public_data_files_hold_no_email_address(name):
    text = (REPO / name).read_text("utf-8")
    assert not re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}", text)


def test_settings_json_has_no_recipient():
    settings = json.loads((REPO / "data" / "settings.json").read_text("utf-8"))
    assert isinstance(settings, dict) and "recipient_email" not in settings


def test_readme_is_the_short_public_description():
    assert (REPO / "README.md").read_text("utf-8").replace("\r\n", "\n") == README


# ---------------------------------------------------------------------------
# the recipient: the secret, and nothing else
# ---------------------------------------------------------------------------

def test_recipient_comes_only_from_the_secret(monkeypatch):
    monkeypatch.setenv("ALERT_RECIPIENT", "  alerts@example.org ")
    assert access.alert_recipient({"recipient_email": "public@example.com"}) == ("alerts@example.org", "secret")
    monkeypatch.delenv("ALERT_RECIPIENT")
    assert access.alert_recipient({"recipient_email": "public@example.com"}) == ("", "")      # never the public file
    assert access.alert_recipient({}) == access.alert_recipient(None) == ("", "")


@pytest.fixture
def shared_data(tmp_path, monkeypatch):
    monkeypatch.setattr(config_store, "SEEN_FILE", tmp_path / "seen_jobs.json")
    monkeypatch.setattr(config_store, "COMPANIES_FILE", tmp_path / "companies.json")
    monkeypatch.setattr(config_store, "SETTINGS_FILE", tmp_path / "settings.json")
    (tmp_path / "companies.json").write_text("[]")
    # an old checkout that still carries the field: it must be ignored
    (tmp_path / "settings.json").write_text(json.dumps({"recipient_email": "public@example.com"}))
    gate = {"real_job_posting": True, "india_location": True, "detail_read": True, "detail_is_this_job": True,
            "no_conflicting_experience": True, "not_programme_story_talent_recruiter": True,
            "evidence_not_staff_context": True, "fresher_or_entry_evidence": True}
    job = {"title": "Graduate Engineer", "url": "https://sanofi.example/job/_R1234567", "company": "Sanofi",
           "location": "Hyderabad", "category": "FRESHER", "reason": "r", "notified": False,
           "evidence": {"checks": gate}}
    (tmp_path / "seen_jobs.json").write_text(json.dumps([job]))
    published = {}

    def publisher(message, local):
        published.setdefault("seen", local.get("data/seen_jobs.json"))
        config_store.SEEN_FILE.write_text(json.dumps(local["data/seen_jobs.json"]))
        return True
    return publisher, published


def test_alerts_go_to_the_secret(shared_data, monkeypatch):
    publisher, _ = shared_data
    monkeypatch.setenv("ALERT_RECIPIENT", "alerts@example.org")
    sent = []
    assert alerts.run(send=lambda jobs, to: sent.append(to), publisher=publisher) == 0
    assert sent == ["alerts@example.org"]


def test_without_the_secret_nothing_is_sent_and_jobs_stay_pending(shared_data, monkeypatch):
    publisher, published = shared_data
    monkeypatch.delenv("ALERT_RECIPIENT", raising=False)
    sent = []
    assert alerts.run(send=lambda jobs, to: sent.append(to), publisher=publisher) == 0
    assert sent == [] and published["seen"] is not None
    assert config_store.alert_pending(json.loads(config_store.SEEN_FILE.read_text())[0])     # sent once a secret exists


def test_test_email_needs_the_secret(shared_data, monkeypatch):
    monkeypatch.delenv("ALERT_RECIPIENT", raising=False)
    monkeypatch.setattr(notifier, "_send", lambda *a, **k: pytest.fail("no email may be sent"))
    with pytest.raises(RuntimeError):
        notifier.test_mail()                                   # never falls back to settings.json
    session = {access.OWNER_SESSION_KEY: 9e18}
    monkeypatch.setenv("JT_OWNER_PASSWORD", "a-long-enough-owner-password")
    monkeypatch.delenv("JT_ADMIN_EMAILS", raising=False)
    with pytest.raises(LookupError):
        access.send_test_email(session, {"recipient_email": "public@example.com"})
    monkeypatch.setenv("ALERT_RECIPIENT", "alerts@example.org")
    got = []
    monkeypatch.setattr(notifier, "_send", lambda to, *a, **k: got.append(to))
    assert access.send_test_email(session, {}) == "alerts@example.org" and got == ["alerts@example.org"]


# ---------------------------------------------------------------------------
# nothing else moved: history, identities, dismissals, scheduler state
# ---------------------------------------------------------------------------

def test_committed_scan_history_still_loads_and_dedups():
    seen = json.loads((REPO / "data" / "seen_jobs.json").read_text("utf-8"))
    assert isinstance(seen, list) and seen
    keys = [repo_sync.record_key(j) for j in seen]
    assert all(keys) and len(set(keys)) == len(keys)                 # one identity per job
    assert repo_sync.merge_seen(seen, seen) == seen                  # a re-publish changes nothing
    flagged = [j for j in seen if j.get("dismissed") or j.get("notified")]
    merged = repo_sync.merge_seen(seen, [{k: v for k, v in j.items() if k not in ("dismissed", "notified")}
                                         for j in flagged])
    assert all(m.get("dismissed") == j.get("dismissed") and m.get("notified") == j.get("notified")
               for m, j in zip(merged, seen))                         # flags never go back


def test_scheduler_state_still_reads_from_the_committed_files():
    settings = json.loads((REPO / "data" / "settings.json").read_text("utf-8"))
    companies = json.loads((REPO / "data" / "companies.json").read_text("utf-8"))
    assert scan_schedule.interval_from(settings) in scan_schedule.ALLOWED_INTERVALS
    assert scan_schedule.last_scan(companies) is not None
    decision = scan_schedule.plan("schedule", scan_schedule.PLAN_CRON, settings, companies)
    assert set(decision) == {"scan", "deliver"}
