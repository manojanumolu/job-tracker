"""Milestone 3 (Nov 2026): the boundaries between data, code and secrets —
see docs/SECURITY_GITHUB.md.

  * data (data/*.json) can live on its own branch (JT_DATA_BRANCH) and in its
    own checkout (JT_DATA_ROOT); nothing in that checkout is ever imported
    or executed, and a malformed setting never falls back to the code branch
  * every workflow job that reads a secret runs in the "production"
    environment, checks out code without stored credentials, and pushes only
    from the data checkout
  * no workflow expression that a writer of data or a dispatcher controls
    reaches a shell

Workflows are read as text (no YAML library in CI), like the other workflow
tests. Nothing here talks to GitHub.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import config_store
import data_location
import repo_sync
from data_location import DataLocationError, data_branch, data_root, valid_branch

REPO = Path(__file__).resolve().parent.parent
WORKFLOWS = sorted((REPO / ".github" / "workflows").glob("*.yml"))
CHECKOUT = "actions/checkout@"


# ---------------------------------------------------------------------------
# data_location: hostile and malformed settings
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["data", "main", "jt-data", "data/v2", "release_1.0", "a" * 100])
def test_valid_branch_names(name):
    assert valid_branch(name)


@pytest.mark.parametrize("name", [
    "", " ", "-x", "--upload-pack=evil", "../main", "data/../main", "a..b", "refs/heads/main", "/data", "data/",
    "data.lock", "x;rm -rf /", "x$(id)", "x`id`", "x y", "x\nmain", "x\tmain", "data~1", "data^", "data:main",
    "x?", "x*", "x[", "x\\y", "dätä", "a" * 101, "data//x", "data/.hidden", "data.", None, 7, ["data"],
])
def test_hostile_branch_names_are_refused(name):
    assert not valid_branch(name)


def test_data_branch_unset_means_the_default_branch():
    assert data_branch({}) is None and data_branch({"JT_DATA_BRANCH": "  "}) is None
    assert data_branch({"JT_DATA_BRANCH": " data "}) == "data"


def test_a_malformed_data_branch_is_an_error_not_a_fallback():
    with pytest.raises(DataLocationError):
        data_branch({"JT_DATA_BRANCH": "../main"})


def test_data_root(tmp_path):
    assert data_root({}) == data_location.BASE
    with pytest.raises(DataLocationError):
        data_root({"JT_DATA_ROOT": str(tmp_path)})                  # no data/ in it
    (tmp_path / "data").mkdir()
    assert data_root({"JT_DATA_ROOT": str(tmp_path)}) == tmp_path.resolve()


# ---------------------------------------------------------------------------
# the app's GitHub reads and writes follow JT_DATA_BRANCH
# ---------------------------------------------------------------------------

class FakeContent:
    def __init__(self, data):
        self.decoded_content = json.dumps(data).encode()
        self.sha = "sha1"


class FakeRepo:
    def __init__(self, files):
        self.files, self.calls = dict(files), []

    def get_contents(self, path, **kw):
        self.calls.append(("get", path, kw))
        return FakeContent(self.files[path])

    def update_file(self, path, message, content, sha, **kw):
        self.calls.append(("update", path, kw))
        self.files[path] = json.loads(content)

    def create_file(self, path, message, content, **kw):
        self.calls.append(("create", path, kw))


def test_app_writes_go_to_the_default_branch_when_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("JT_DATA_BRANCH", raising=False)
    repo = FakeRepo({"data/settings.json": {"a": 1}})
    data, saved, err = config_store.update_json(tmp_path / "settings.json", "data/settings.json",
                                                lambda d: {**d, "b": 2}, {}, "m", token="t", repo=repo)
    assert saved and data == {"a": 1, "b": 2}
    assert [kw for _, _, kw in repo.calls] == [{}, {}]                   # exactly the calls made before M3


def test_app_reads_and_writes_follow_the_data_branch(tmp_path, monkeypatch):
    monkeypatch.setenv("JT_DATA_BRANCH", "data")
    repo = FakeRepo({"data/settings.json": {"a": 1}})
    config_store.update_json(tmp_path / "settings.json", "data/settings.json", lambda d: d, {}, "m",
                             token="t", repo=repo)
    assert repo.calls == [("get", "data/settings.json", {"ref": "data"}),
                          ("update", "data/settings.json", {"branch": "data"})]
    got = config_store.fetch_remote_json(["data/settings.json"], token="t", repo=repo)
    assert got == {"data/settings.json": {"a": 1}} and repo.calls[-1][2] == {"ref": "data"}


def test_a_malformed_data_branch_never_writes_to_github(tmp_path, monkeypatch):
    monkeypatch.setenv("JT_DATA_BRANCH", "../main")
    repo = FakeRepo({"data/settings.json": {"a": 1}})
    data, saved, err = config_store.update_json(tmp_path / "settings.json", "data/settings.json",
                                                lambda d: {**d, "b": 2}, {}, "m", token="t", repo=repo)
    assert not saved and err and not [c for c in repo.calls if c[0] != "get"]
    assert config_store.fetch_remote_json(["data/settings.json"], token="t", repo=repo) == {}


def test_the_app_writes_github_only_through_config_store():
    """Every GitHub contents write in the app goes through update_json (which
    follows JT_DATA_BRANCH); the only other GitHub action is starting Check
    Jobs on main."""
    app = (REPO / "streamlit_app.py").read_text("utf-8")
    assert not re.search(r"\.(create_file|update_file|delete_file)\(", app)
    assert re.findall(r"create_dispatch\(([^)]*)\)", app) == ['ref="main"']


def test_publish_refuses_a_malformed_branch(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(repo_sync, "_git", lambda *a, **k: calls.append(a))
    for bad in ("--force", "../main", "x y", "refs/heads/main"):
        assert repo_sync.publish("m", {"data/seen_jobs.json": []}, cwd=tmp_path, branch=bad) is False
    assert calls == []


def test_publish_works_in_the_data_checkout_by_default(monkeypatch, tmp_path):
    (tmp_path / "data").mkdir()
    code = (f"import sys; sys.path.insert(0, {str(REPO / 'src')!r}); import repo_sync; "
            "print(repo_sync.DATA_ROOT)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                         env={**os.environ, "JT_DATA_ROOT": str(tmp_path)}).stdout.strip()
    assert out == str(tmp_path.resolve())


def test_nothing_in_the_data_checkout_is_ever_imported(tmp_path):
    """A hostile data branch carrying code: with JT_DATA_ROOT pointing at
    it, every module still comes from the code checkout, and the data files
    are read from the data checkout."""
    (tmp_path / "data").mkdir()
    (tmp_path / "src").mkdir()
    for name in ("config_store", "repo_sync", "alerts", "notifier", "user_alerts", "scan_schedule", "json"):
        (tmp_path / "src" / f"{name}.py").write_text("raise SystemExit('hostile code ran')\n")
        (tmp_path / f"{name}.py").write_text("raise SystemExit('hostile code ran')\n")
    (tmp_path / "data" / "seen_jobs.json").write_text('[{"title": "from the data branch"}]')
    code = (f"import sys; sys.path.insert(0, {str(REPO / 'src')!r}); "
            "import config_store, repo_sync, alerts, notifier, user_alerts, scan_schedule; "
            "print(config_store.load_seen_jobs()[0]['title']); "
            "print(all(not m.__file__.startswith(sys.argv[1]) for m in "
            "(config_store, repo_sync, alerts, notifier, user_alerts, scan_schedule)))")
    r = subprocess.run([sys.executable, "-I", "-c", code, str(tmp_path)], capture_output=True, text=True,
                       cwd=REPO, env={**os.environ, "JT_DATA_ROOT": str(tmp_path)})
    assert r.returncode == 0, r.stderr[-500:]
    assert r.stdout.split() == ["from", "the", "data", "branch", "True"]


def test_scan_schedule_reads_the_data_checkout(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "settings.json").write_text(json.dumps({"scan_interval_hours": 12}))
    (tmp_path / "data" / "companies.json").write_text("[]")
    code = (f"import sys; sys.path.insert(0, {str(REPO / 'src')!r}); import scan_schedule; "
            "print(scan_schedule._load('settings.json', {}))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                         env={**os.environ, "JT_DATA_ROOT": str(tmp_path)}).stdout
    assert "12" in out


# ---------------------------------------------------------------------------
# workflows
# ---------------------------------------------------------------------------

def _jobs(text: str) -> dict[str, str]:
    body = text[text.index("\njobs:\n") + len("\njobs:\n"):]
    out, name, lines = {}, None, []
    for line in body.split("\n"):
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if m:
            if name:
                out[name] = "\n".join(lines)
            name, lines = m.group(1), [line]
        elif name:
            lines.append(line)
    if name:
        out[name] = "\n".join(lines)
    return out


def _steps(job: str) -> list[str]:
    return job.split("\n      - ")[1:]


def _all_jobs():
    for wf in WORKFLOWS:
        text = wf.read_text("utf-8").replace("\r\n", "\n")
        for name, job in _jobs(text).items():
            yield wf.name, name, job


def test_every_job_that_reads_a_secret_runs_in_the_production_environment():
    with_secrets = [(wf, name) for wf, name, job in _all_jobs() if "${{ secrets." in job]
    assert with_secrets == [("application_retention.yml", "purge"), ("check_jobs.yml", "publish"),
                            ("check_jobs.yml", "personal-alerts")]
    for wf, name, job in _all_jobs():
        if "${{ secrets." in job:
            assert "\n    environment: production\n" in job, (wf, name)
        else:
            assert "environment:" not in job, (wf, name)        # nothing else needs it


def test_code_checkouts_never_store_credentials_and_data_is_checked_out_apart():
    for wf, name, job in _all_jobs():
        for step in _steps(job):
            if CHECKOUT not in step:
                continue
            if "path: jt-data" in step:
                # the data checkout: the data branch (or this run's), sparse
                # unless it is the one that pushes
                assert "ref: ${{ vars.JT_DATA_BRANCH || github.ref" in step, (wf, name)
                assert ("persist-credentials: false" in step) != ("fetch-depth: 0" in step), (wf, name)
            else:
                # the code checkout: the run's own commit, never the data branch
                assert "persist-credentials: false" in step, (wf, name)
                assert "ref:" not in step and "vars." not in step, (wf, name)


def test_only_publishing_jobs_hold_a_credentialed_checkout_and_write_access():
    writers = sorted((wf, name) for wf, name, job in _all_jobs() if "contents: write" in job)
    assert writers == [("check_jobs.yml", "publish"), ("logos.yml", "publish")]
    for wf, name, job in _all_jobs():
        credentialed = [s for s in _steps(job) if CHECKOUT in s and "persist-credentials: false" not in s]
        if (wf, name) in writers:
            assert len(credentialed) == 1 and "path: jt-data" in credentialed[0], (wf, name)
            # the push is made in the data checkout, the code runs from the code checkout
            assert "git -C jt-data config user.name" in job and "PUBLISH_BRANCH: ${{ vars.JT_DATA_BRANCH" in job
            assert "\n          git config" not in job
        else:
            assert credentialed == [], (wf, name)


def test_no_step_runs_from_the_data_checkout():
    for wf, name, job in _all_jobs():
        for step in _steps(job):
            assert "working-directory: jt-data" not in step, (wf, name)
            run = step.split("run:", 1)[1] if "run:" in step else ""
            assert "jt-data/src" not in run and "cd jt-data" not in run, (wf, name)
            if "JT_DATA_ROOT" in step:
                assert "JT_DATA_ROOT: jt-data" in step, (wf, name)


def test_steps_that_read_data_use_the_data_checkout():
    for wf, name, job in _all_jobs():
        uses_data = "path: jt-data" in job
        for step in _steps(job):
            run = step.split("run:", 1)[1] if "run:" in step else ""
            if re.search(r"python3? src/(scraper|alerts|user_alerts|scan_schedule|scan_handoff|company_logos)\.py", run):
                assert uses_data and "JT_DATA_ROOT: jt-data" in step, (wf, name, run.strip()[:60])


def test_no_untrusted_expression_reaches_a_shell():
    """Expressions a data writer or dispatcher controls (variables, inputs,
    event payloads, ref names) go through env:, never into a run: script."""
    risky = re.compile(r"\$\{\{\s*(vars\.|inputs\.|github\.event\.|github\.head_ref|github\.ref_name|"
                       r"steps\.[^}]*outputs)")
    for wf, name, job in _all_jobs():
        for step in _steps(job):
            if "run:" not in step:
                continue
            script = step.split("run:", 1)[1]
            assert not risky.search(script), (wf, name, script.strip()[:80])


def test_no_workflow_runs_untrusted_triggers_or_grants_by_default():
    for wf in WORKFLOWS:
        text = wf.read_text("utf-8")
        assert "pull_request_target" not in text and "workflow_run" not in text, wf.name
        top = re.search(r"^permissions:(.*)$", text, re.M)
        assert top and top.group(1).strip() in ("{}", ""), wf.name
        if top.group(1).strip() == "":
            assert re.search(r"^permissions:\n  contents: read\n", text, re.M), wf.name


def test_artifacts_carry_only_data_files():
    for wf, name, job in _all_jobs():
        for step in _steps(job):
            if "actions/upload-artifact@" in step:
                paths = re.findall(r"^\s+(\S+\.json)\s*$", step.split("path:", 1)[1], re.M)
                assert paths and all(p.startswith("jt-data/data/") for p in paths), (wf, name, paths)


def test_handoff_writes_only_into_the_data_checkout(tmp_path, monkeypatch):
    import scan_handoff
    src = tmp_path / "artifact"
    src.mkdir()
    (src / "seen_jobs.json").write_text("[]")
    dest = tmp_path / "data"
    dest.mkdir()
    scan_handoff.take_over(src, ["seen_jobs.json"], dest_dir=dest)
    assert (dest / "seen_jobs.json").read_text() == "[]"
    for hostile in ("../seen_jobs.json", "streamlit_app.py", "alerts.py", ".github/workflows/x.yml"):
        with pytest.raises(scan_handoff.HandoffError):
            scan_handoff.take_over(src, [hostile], dest_dir=dest)
