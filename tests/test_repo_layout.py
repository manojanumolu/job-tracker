"""The repository layout (Oct 15, 2026): a short root, related files grouped.

  streamlit_app.py   the app — stays at the root, where Streamlit Community
                     Cloud runs it; it puts src/ on the import path
  requirements.txt   the deployed lock — at the root, where Cloud reads it
  src/               the app's modules, the scanner and the notifiers
  data/              the shared data files the scans and the app publish
  config/            Firestore rules and the disposable-address list
  requirements/      the test and logo-finder locks

Every path a workflow, the app or a module uses must point at a real file."""
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ROOT_DIRS = {".devcontainer", ".github", ".streamlit", "assets", "config", "data", "docs", "requirements", "src", "tests"}
ROOT_FILES = {".env.example", ".gitignore", "README.md", "pytest.ini", "requirements.in", "requirements.txt", "runtime.txt",
              "streamlit_app.py"}


def _tracked() -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return [line for line in out.splitlines() if line]


def test_the_root_holds_only_folders_and_the_files_tools_need_there():
    top = {p.split("/", 1)[0] for p in _tracked()}
    assert top == ROOT_DIRS | ROOT_FILES, sorted(top ^ (ROOT_DIRS | ROOT_FILES))
    assert [p for p in _tracked() if "/" not in p and p.endswith(".py")] == ["streamlit_app.py"]


def test_modules_data_and_config_are_grouped():
    for name in ("access", "alerts", "config_store", "notifier", "scraper", "sources", "user_alerts", "user_store",
                 "email_policy", "firebase_auth", "scan_schedule", "scan_handoff", "repo_sync", "company_logos"):
        assert (REPO / "src" / f"{name}.py").is_file(), name
    for name in ("companies.json", "seen_jobs.json", "settings.json", "logos.json"):
        assert (REPO / "data" / name).is_file(), name
    assert (REPO / "config" / "firestore.rules").is_file() and (REPO / "config" / "disposable_domains.txt").is_file()


def test_the_app_puts_src_on_the_path_and_reads_data_from_data():
    src = (REPO / "streamlit_app.py").read_text("utf-8")
    assert src.index('_SRC = os.path.join(') < src.index("from config_store import")      # before any module import
    files = re.search(r"_DATA_FILES = \[([^\]]*)\]", src).group(1)
    for path in re.findall(r'"([^"]+)"', files):
        assert path.startswith("data/") and (REPO / path).is_file(), path


def test_every_script_a_workflow_runs_exists():
    seen = []
    for wf in (REPO / ".github" / "workflows").glob("*.yml"):
        text = wf.read_text("utf-8")
        for script in re.findall(r"\bpython3? (?:-m \S+ )?([\w./-]+\.py)\b", text):
            seen.append(script)
            assert (REPO / script).is_file(), (wf.name, script)
        for req in re.findall(r"-r (\S+\.txt)", text):
            assert (REPO / req).is_file(), (wf.name, req)
        for path in re.findall(r"^\s+((?:src|data)/[\w./-]+)\s*$", text, re.M):      # sparse checkouts, artifacts
            assert (REPO / path).is_file(), (wf.name, path)
    assert {"src/scraper.py", "src/alerts.py", "src/user_alerts.py", "src/scan_schedule.py", "src/scan_handoff.py",
            "src/company_logos.py"} <= set(seen)


def _fresh(name: str):
    """The module as loaded from this checkout's src/ (other tests import
    copies from temporary folders, which stay in sys.modules)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(f"_layout_{name}", REPO / "src" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_modules_resolve_the_repository_root():
    names = ("config_store", "company_logos", "email_policy", "repo_sync", "scan_handoff", "scan_schedule", "user_alerts")
    config_store, company_logos, email_policy, repo_sync, scan_handoff, scan_schedule, user_alerts = map(_fresh, names)
    for mod in (config_store, company_logos, email_policy, repo_sync, scan_handoff, scan_schedule, user_alerts):
        assert Path(mod.BASE).resolve() == REPO, mod.__name__
    assert config_store.SEEN_FILE == REPO / "data" / "seen_jobs.json"
    assert email_policy.BLOCKLIST_FILE == REPO / "config" / "disposable_domains.txt"
    assert scan_handoff.DATA == REPO / "data" and company_logos.LOGOS_FILE == "data/logos.json"
    assert repo_sync.DATA_FILES == ("data/seen_jobs.json", "data/companies.json")
