"""JT-01 (independent audit of c0b8ca9): dependency and GitHub Actions supply
chain.

* every third-party action is pinned to a full commit SHA (the tag in a
  comment), and the SHAs are the reviewed ones below
* every workflow grants nothing by default and every job asks for exactly
  its permissions
* the jobs that load external websites (the scan, the logo finder) hold no
  secret, a read-only token and no stored git credentials; the jobs that
  can push never load an external site and take over only checked JSON
* every Python install is hash-locked (pip --require-hashes) from lockfiles
  that agree with their top-level .in files and with each other
"""
import json
import re
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

import scan_handoff

REPO = Path(__file__).resolve().parent.parent
WF_DIR = REPO / ".github" / "workflows"
WORKFLOWS = {p.name: p.read_text("utf-8").replace("\r\n", "\n") for p in sorted(WF_DIR.glob("*.yml"))}

# reviewed 2026-10-13: what each tag pointed to in the official repositories
# (the same versions the floating @v4 / @v5 tags ran before pinning)
REVIEWED_ACTIONS = {
    "actions/checkout": ("11d5960a326750d5838078e36cf38b85af677262", "v4.4.0"),
    "actions/setup-python": ("a26af69be951a213d495a4c3e4e4022e16d87065", "v5.6.0"),
    "actions/upload-artifact": ("ea165f8d65b6e75b540449e92b4886f43607fa02", "v4.6.2"),
    "actions/download-artifact": ("d3f86a106a0bac45b974a628896c90dbdf5c8093", "v4.3.0"),
}
# every job, and exactly the permissions it may have
EXPECTED_PERMISSIONS = {
    ("check_jobs.yml", "plan"): {"contents": "read"},
    ("check_jobs.yml", "scrape"): {"contents": "read"},
    ("check_jobs.yml", "publish"): {"contents": "write"},
    ("check_jobs.yml", "scheduler"): {"actions": "write"},
    ("check_jobs.yml", "personal-alerts"): {"contents": "read"},
    ("logos.yml", "find"): {"contents": "read"},
    ("logos.yml", "publish"): {"contents": "write"},
    ("scan_scheduler.yml", "wait-and-dispatch"): {"contents": "read", "actions": "write"},
}
LOCKS = {"requirements.txt": "requirements.in", "requirements-dev.txt": "requirements-dev.in",
         "requirements-logos.txt": "requirements-logos.in"}


def jobs(text: str) -> dict[str, str]:
    """job name -> its block (a tiny parser: no YAML library needed)."""
    body = text.split("\njobs:\n", 1)[1]
    names = re.findall(r"^  ([A-Za-z0-9_-]+):\n", body, re.M)
    out = {}
    for i, name in enumerate(names):
        start = body.index(f"\n  {name}:\n") + 1 if i or not body.startswith(f"  {name}:\n") else 0
        end = body.index(f"\n  {names[i + 1]}:\n") if i + 1 < len(names) else len(body)
        # comments are documentation, not configuration: leave them out
        out[name] = "\n".join(line for line in body[start:end].split("\n") if not line.lstrip().startswith("#"))
    return out


def job_permissions(block: str) -> dict[str, str]:
    m = re.search(r"^    permissions:\n((?:      [a-z-]+: [a-z]+.*\n)+)", block, re.M)
    return dict(re.findall(r"^      ([a-z-]+): ([a-z]+)", m.group(1), re.M)) if m else {}


def run_blocks(text: str) -> list[str]:
    """Every run: script (single line or block)."""
    out = []
    for m in re.finditer(r"^(\s*)(?:- )?run: (.*)$", text, re.M):
        indent, first = len(m.group(1)), m.group(2)
        if first.strip() in ("|", ">"):
            lines = []
            for line in text[m.end() + 1:].split("\n"):
                if line.strip() and len(line) - len(line.lstrip()) <= indent:
                    break
                lines.append(line)
            out.append("\n".join(lines))
        else:
            out.append(first)
    return out


# ---------------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------------

def test_every_action_is_pinned_to_a_reviewed_commit_sha():
    seen = set()
    for name, text in WORKFLOWS.items():
        for m in re.finditer(r"uses:\s*(\S+)(.*)$", text, re.M):
            ref, comment = m.group(1), m.group(2)
            assert not ref.startswith("docker://"), (name, ref)
            if ref.startswith("./"):
                continue                                       # a local action (none today)
            action, _, sha = ref.partition("@")
            assert re.fullmatch(r"[0-9a-f]{40}", sha), (name, ref)          # never a tag or branch
            assert action in REVIEWED_ACTIONS and REVIEWED_ACTIONS[action][0] == sha, (name, ref)
            assert re.search(rf"#\s*{re.escape(REVIEWED_ACTIONS[action][1])}\b", comment), (name, ref)
            seen.add(action)
    assert seen == set(REVIEWED_ACTIONS)                      # no stale entry in the table either


# ---------------------------------------------------------------------------
# permissions and triggers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(WORKFLOWS))
def test_nothing_is_granted_by_default(name):
    head = WORKFLOWS[name].split("\njobs:\n", 1)[0]
    assert "write-all" not in WORKFLOWS[name] and "read-all" not in WORKFLOWS[name]
    if name == "tests.yml":
        assert re.search(r"^permissions:\n  contents: read\n(?!  )", head, re.M)
    else:
        assert "\npermissions: {}\n" in head


def test_every_job_asks_for_exactly_its_permissions():
    found = {}
    for name, text in WORKFLOWS.items():
        if name == "tests.yml":
            continue
        for job, block in jobs(text).items():
            found[(name, job)] = job_permissions(block)
    assert found == EXPECTED_PERMISSIONS


@pytest.mark.parametrize("name", sorted(WORKFLOWS))
def test_no_risky_trigger_or_script_injection(name):
    text = WORKFLOWS[name]
    assert "pull_request_target" not in text and "workflow_run" not in text
    for script in run_blocks(text):
        # values from events or inputs reach scripts only through env:
        assert "${{" not in script, (name, script)


# ---------------------------------------------------------------------------
# the jobs that read the web vs the jobs that can push
# ---------------------------------------------------------------------------

READS_THE_WEB = {("check_jobs.yml", "scrape"), ("logos.yml", "find")}
CAN_PUSH = {("check_jobs.yml", "publish"), ("logos.yml", "publish")}


@pytest.mark.parametrize("wf, job", sorted(READS_THE_WEB))
def test_jobs_that_load_external_sites_hold_nothing_worth_stealing(wf, job):
    block = jobs(WORKFLOWS[wf])[job]
    assert job_permissions(block) == {"contents": "read"}
    assert "secrets." not in block and "GITHUB_TOKEN" not in block and "github.token" not in block
    assert "persist-credentials: false" in block                       # no git token left in .git/config
    assert "download-artifact" not in block and "git push" not in block and "--publish" not in block


@pytest.mark.parametrize("wf, job", sorted(CAN_PUSH))
def test_jobs_that_can_push_never_load_an_external_site(wf, job):
    block = jobs(WORKFLOWS[wf])[job]
    assert job_permissions(block) == {"contents": "write"}
    for forbidden in ("scraper.py", "playwright", "company_logos.py --limit", "upload-artifact"):
        assert forbidden not in block, forbidden
    assert re.search(r"run: python company_logos\.py(?! --publish-only)", block) is None
    # they take over the other job's data through the checked hand-over only
    assert "download-artifact" in block and "python scan_handoff.py" in block
    assert block.index("python scan_handoff.py") < block.index("python ", block.index("python scan_handoff.py") + 1)


def test_the_scan_runs_the_same_commands_in_the_right_jobs():
    wf = jobs(WORKFLOWS["check_jobs.yml"])
    assert "run: python scraper.py" in wf["scrape"] and "scraper.py" not in wf["publish"]
    assert "python alerts.py" in wf["publish"] and "python user_alerts.py --send" in wf["publish"]
    assert "needs: [plan, scrape]" in wf["publish"]
    assert "python scan_handoff.py scan-results companies.json seen_jobs.json" in wf["publish"]
    upload = wf["scrape"].split("upload-artifact", 1)[1]
    assert "companies.json" in upload and "seen_jobs.json" in upload and "retention-days: 1" in upload
    logos = jobs(WORKFLOWS["logos.yml"])
    assert "run: python company_logos.py\n" in logos["find"] + "\n"
    assert "python company_logos.py --publish-only" in logos["publish"]


# ---------------------------------------------------------------------------
# dependencies
# ---------------------------------------------------------------------------

def _lock(name: str) -> dict[str, tuple[str, str, list[str]]]:
    """canonical name -> (version, marker, hashes) for every pin in a lock."""
    text = (REPO / name).read_text("utf-8").replace("\\\n", " ")
    pins = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        assert not line.startswith(("-e", "-r", "-c", "--index-url", "--extra-index-url", "git+", "http://", "https://")), line
        spec = line.split(" --hash=")[0].strip()
        req = Requirement(spec)
        version = str(req.specifier).removeprefix("==")
        assert str(req.specifier).startswith("==") and "*" not in version, line        # exact pins only
        hashes = re.findall(r"--hash=sha256:([0-9a-f]{64})", line)
        assert hashes, f"{name}: {req.name} has no hash"
        key = canonicalize_name(req.name) + (f" ; {req.marker}" if req.marker else "")
        assert key not in pins, f"{name}: {key} pinned twice"
        pins[key] = (version, str(req.marker or ""), hashes)
    return pins


def _top_level(name: str) -> list[Requirement]:
    out = []
    for line in (REPO / name).read_text("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-r "):
            out += _top_level(line[3:].strip())
            continue
        out.append(Requirement(line))
    return out


@pytest.mark.parametrize("lock, source", sorted(LOCKS.items()))
def test_every_lock_is_exact_hashed_and_satisfies_its_inputs(lock, source):
    pins = _lock(lock)
    by_name = {}
    for key, (version, _, _) in pins.items():
        by_name.setdefault(key.split(" ;")[0], []).append(version)
    for req in _top_level(source):
        versions = by_name.get(canonicalize_name(req.name))
        assert versions, f"{lock}: {req.name} from {source} isn't locked"
        assert all(req.specifier.contains(v, prereleases=True) for v in versions), (lock, req, versions)


def test_the_locks_agree_with_each_other():
    main, dev, logos = (_lock(n) for n in ("requirements.txt", "requirements-dev.txt", "requirements-logos.txt"))
    for key, (version, _, hashes) in main.items():
        assert key in dev and dev[key][0] == version and set(dev[key][2]) == set(hashes), key   # tests run what ships
    for key, (version, _, _) in logos.items():
        assert main.get(key, (version,))[0] == version, key
    assert set(dev) - set(main) <= {"pytest", "iniconfig", "pluggy", "pygments", "colorama ; sys_platform == \"win32\""}
    assert (REPO / "requirements-dev.in").read_text("utf-8").count("-r requirements.in") == 1


@pytest.mark.parametrize("name", sorted(WORKFLOWS))
def test_every_workflow_install_is_hash_locked(name):
    for script in run_blocks(WORKFLOWS[name]):
        for line in script.splitlines():
            if "pip install" in line or "pip3 install" in line:
                m = re.fullmatch(r"\s*pip install --require-hashes -r (requirements(?:-dev|-logos)?\.txt)\s*", line)
                assert m, (name, line)
                assert (REPO / m.group(1)).is_file()


def test_the_streamlit_deployment_reads_the_lock():
    """Streamlit Community Cloud installs requirements.txt: that file is the
    hashed lock itself (hashes put pip and uv in hash-checking mode)."""
    text = (REPO / "requirements.txt").read_text("utf-8")
    assert "--hash=sha256:" in text and "streamlit==" in text and "firebase-admin==" in text


# ---------------------------------------------------------------------------
# the hand-over between jobs
# ---------------------------------------------------------------------------

def _write(d: Path, name: str, data) -> Path:
    p = d / name
    p.write_text(json.dumps(data) if not isinstance(data, str) else data, "utf-8")
    return p


def test_handoff_copies_valid_files(tmp_path):
    src, dest = tmp_path / "in", tmp_path / "out"
    src.mkdir(), dest.mkdir()
    _write(src, "companies.json", [{"id": "a", "name": "A"}])
    _write(src, "seen_jobs.json", [{"title": "t"}])
    scan_handoff.take_over(src, ["companies.json", "seen_jobs.json"], dest)
    assert json.loads((dest / "companies.json").read_text()) == [{"id": "a", "name": "A"}]
    assert json.loads((dest / "seen_jobs.json").read_text()) == [{"title": "t"}]


@pytest.mark.parametrize("name, data", [
    ("companies.json", {"not": "a list"}),
    ("companies.json", [{"name": "no id"}]),
    ("seen_jobs.json", ["not an object"]),
    ("seen_jobs.json", "{broken json"),
    ("logos.json", {"logos": []}),
    ("settings.json", {}),                                     # not a file that may be handed over
    ("../streamlit_app.py", "print(1)"),
])
def test_handoff_refuses_bad_files_and_copies_nothing(tmp_path, name, data):
    src, dest = tmp_path / "in", tmp_path / "out"
    src.mkdir(), dest.mkdir()
    _write(src, "companies.json", [{"id": "ok"}])
    if "/" not in name:
        _write(src, name, data)
    with pytest.raises(scan_handoff.HandoffError):
        scan_handoff.take_over(src, ["companies.json", name], dest)
    assert list(dest.iterdir()) == []                          # never half a hand-over


def test_handoff_refuses_missing_oversized_and_linked_files(tmp_path, monkeypatch):
    src, dest = tmp_path / "in", tmp_path / "out"
    src.mkdir(), dest.mkdir()
    with pytest.raises(scan_handoff.HandoffError):
        scan_handoff.take_over(src, ["seen_jobs.json"], dest)                      # missing
    _write(src, "seen_jobs.json", [{"a": 1}])
    monkeypatch.setattr(scan_handoff, "MAX_BYTES", 4)
    with pytest.raises(scan_handoff.HandoffError):
        scan_handoff.take_over(src, ["seen_jobs.json"], dest)                      # too large
    monkeypatch.undo()
    target = _write(tmp_path, "elsewhere.json", [{"a": 1}])
    link = src / "companies.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks aren't available here")
    with pytest.raises(scan_handoff.HandoffError):
        scan_handoff.take_over(src, ["companies.json"], dest)                      # a link
    assert list(dest.iterdir()) == []


def test_handoff_cli_exit_codes(tmp_path, monkeypatch, capsys):
    _write(tmp_path, "seen_jobs.json", "{broken")
    monkeypatch.setattr(scan_handoff, "BASE", tmp_path / "dest")
    assert scan_handoff.main([str(tmp_path), "seen_jobs.json"]) == 1
    assert "refused" in capsys.readouterr().err
    assert scan_handoff.main([]) == 2


def test_logo_publish_only_needs_no_search(monkeypatch, tmp_path):
    import company_logos
    import repo_sync
    calls = []
    monkeypatch.setattr(repo_sync, "publish", lambda message, data: calls.append(data) or True)
    monkeypatch.setattr(company_logos, "load_local", lambda path=None: {"version": 1, "logos": {}})
    monkeypatch.setattr(company_logos, "Fetcher", lambda *a, **k: pytest.fail("publish-only must not search"))
    assert company_logos.main(["--publish-only"]) == 0 and calls == [{"logos.json": {"version": 1, "logos": {}}}]
    monkeypatch.setattr(company_logos, "load_local", lambda path=None: {})
    assert company_logos.main(["--publish-only"]) == 1
