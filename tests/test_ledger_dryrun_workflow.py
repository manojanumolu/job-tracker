"""The one-off ledger dry-run workflow (.github/workflows/ledger_dryrun.yml):
it can only be started by hand, runs one pinned, reviewed commit, gives the
Firebase key to one step only and does nothing but count (read as text,
like the other workflow tests; no YAML library in CI)."""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WF = (REPO / ".github" / "workflows" / "ledger_dryrun.yml").read_text("utf-8")
STEPS = WF.split("\n      - name: ")[1:]


def test_started_only_by_hand():
    on = WF[WF.index("\non:\n"):WF.index("\npermissions:")]
    assert on.strip() == "on:\n  workflow_dispatch:"                      # no schedule, push, inputs or other trigger


def test_least_privilege():
    assert re.search(r"^permissions: \{\}$", WF, re.M)
    assert WF.count("permissions:") == 2 and "\n      contents: read\n" in WF
    assert "write" not in WF.split("jobs:", 1)[1].split("steps:", 1)[0]
    assert "environment:" not in WF and "pull_request" not in WF


def test_runs_one_pinned_reviewed_commit_without_stored_credentials():
    checkout = next(s for s in STEPS if "actions/checkout@" in s)
    assert re.search(r"\n          ref: [0-9a-f]{40}\n", checkout)       # a full commit SHA, not a branch
    assert "persist-credentials: false" in checkout
    for s in STEPS:
        if "uses: " in s:
            assert re.search(r"uses: [\w./-]+@[0-9a-f]{40} #", s)          # actions pinned to commit SHAs


def test_the_key_reaches_one_step_only_and_is_deleted():
    assert WF.count("${{ secrets.") == 1
    step = next(s for s in STEPS if "secrets." in s)
    assert step.startswith("Count the personal alert ledgers")
    assert "FIREBASE_SERVICE_ACCOUNT: ${{ secrets.FIREBASE_SERVICE_ACCOUNT }}" in step
    script = step.split("run: |", 1)[1]
    assert "umask 077" in script and "trap 'rm -f \"$key\"' EXIT" in script and '"$RUNNER_TEMP/' in script
    assert "echo" not in script and "cat " not in script and "set -x" not in script


def test_it_only_counts():
    runs = [s.split("run:", 1)[1] for s in STEPS if "run:" in s]
    commands = " ".join(runs)
    assert re.findall(r"python \S+", commands) == ["python src/ledger_dryrun.py"]
    for forbidden in ("alerts.py", "user_alerts", "scraper", "notifier", "git ", "gh ", "curl", "--json",
                      "GITHUB_TOKEN", "github.token", "GMAIL", "ALERT_RECIPIENT"):
        assert forbidden not in WF, forbidden
