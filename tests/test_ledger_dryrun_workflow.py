"""The one-off ledger dry-run workflow (.github/workflows/ledger_dryrun.yml):
started only by hand, runs one pinned and reviewed commit, and reads
Firestore through a keyless, read-only identity (Workload Identity
Federation to a reader that holds only Cloud Datastore Viewer). It never
touches the Firebase admin key and does nothing but count. Read as text,
like the other workflow tests (no YAML library in CI)."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
WF = (REPO / ".github" / "workflows" / "ledger_dryrun.yml").read_text("utf-8")
STEPS = WF.split("\n      - name: ")[1:]
COUNT = next(s for s in STEPS if s.startswith("Count the personal alert ledgers"))
SCRIPT = "\n".join(line[10:] if line.startswith("          ") else line.strip()
                   for line in COUNT.split("        run: |\n", 1)[1].splitlines())
HEREDOCS = re.findall(r"<<'PY'\n(.*?)\nPY\n", SCRIPT, re.S)
PROVIDER = "projects/123456789/locations/global/workloadIdentityPools/ledger-pool/providers/github-main"
READER = "ledger-reader@demo-job-tracker.iam.gserviceaccount.com"


def test_started_only_by_hand():
    on = WF[WF.index("\non:\n"):WF.index("\npermissions:")]
    assert on.strip() == "on:\n  workflow_dispatch:"                      # no schedule, push, inputs or other trigger


def test_least_privilege():
    assert re.search(r"^permissions: \{\}$", WF, re.M)
    job = WF.split("jobs:", 1)[1].split("steps:", 1)[0]
    assert re.findall(r"^      ([a-z-]+): ([a-z]+)", job, re.M) == [("contents", "read"), ("id-token", "write")]
    assert "environment:" not in WF and "pull_request" not in WF


def test_runs_one_pinned_reviewed_commit_without_stored_credentials():
    checkout = next(s for s in STEPS if "actions/checkout@" in s)
    assert re.search(r"\n          ref: [0-9a-f]{40}\n", checkout)       # a full commit SHA, not a branch
    assert "persist-credentials: false" in checkout
    for s in STEPS:
        if "uses: " in s:
            assert re.search(r"uses: [\w./-]+@[0-9a-f]{40} #", s)          # actions pinned to commit SHAs
    assert [s.split("uses: ")[1].split("@")[0] for s in STEPS if "uses: " in s] == ["actions/checkout",
                                                                                      "actions/setup-python"]


def test_never_the_admin_key_and_only_two_identifiers():
    assert "FIREBASE_SERVICE_ACCOUNT" not in WF and "GMAIL" not in WF and "ALERT_RECIPIENT" not in WF
    assert sorted(re.findall(r"\$\{\{ secrets\.([A-Z_]+) \}\}", WF)) == ["LEDGER_READER_SERVICE_ACCOUNT",
                                                                         "LEDGER_WIF_PROVIDER"]
    assert all("secrets." not in s for s in STEPS if s is not COUNT)       # only the counting step sees them
    assert "${{" not in SCRIPT                                              # values reach the script via env only


def test_temporary_files_are_private_and_removed_and_nothing_is_echoed():
    assert SCRIPT.startswith("umask 077\n")
    assert """trap 'rm -f "$tok" "$cfg"' EXIT""" in SCRIPT and '"$RUNNER_TEMP/' in SCRIPT
    for bad in ("echo", "cat ", "set -x", "printenv", "env\n", "tee ", "--json", "upload-artifact", "cache@"):
        assert bad not in WF, bad


def test_it_only_counts():
    commands = SCRIPT.split("PY\n")[-1]
    assert re.findall(r"\bpython3? \S+", commands) == ["python src/ledger_dryrun.py"]
    for forbidden in ("alerts.py", "user_alerts", "scraper", "notifier", "git ", "gh ", "GITHUB_TOKEN",
                      "github.token", "--key-file"):
        assert forbidden not in WF, forbidden


def _validate(provider, reader):
    env = {k: v for k, v in os.environ.items() if k not in ("WIF_PROVIDER", "READER_SA")}
    return subprocess.run([sys.executable, "-c", HEREDOCS[0]], capture_output=True, text=True,
                          env={**env, "WIF_PROVIDER": provider, "READER_SA": reader})


@pytest.mark.parametrize("provider, reader, refused", [
    (PROVIDER, READER, False),
    (PROVIDER, "firebase-adminsdk-abcde@demo-job-tracker.iam.gserviceaccount.com", True),   # the admin account
    ("", READER, True), (PROVIDER, "", True), (PROVIDER, "someone@gmail.com", True),
    ('projects/1/locations/global/workloadIdentityPools/x"; curl evil #/providers/y', READER, True),
    (PROVIDER, "a@b.iam.gserviceaccount.com; rm -rf /", True),
])
def test_settings_are_checked_before_any_token_is_requested(provider, reader, refused):
    assert SCRIPT.index("<<'PY'") < SCRIPT.index("curl ")                    # the check runs first (set -e)
    r = _validate(provider, reader)
    assert (r.returncode != 0) is refused, r.stderr
    assert provider[:40] not in r.stderr or not provider                    # a refusal never echoes the value


def test_the_identity_config_is_keyless_and_impersonates_only_the_reader(tmp_path):
    tok, cfg = tmp_path / "tok.json", tmp_path / "cfg.json"
    tok.write_text(json.dumps({"value": "header.payload.signature"}))
    r = subprocess.run([sys.executable, "-c", HEREDOCS[1], str(tok), str(cfg)], capture_output=True, text=True,
                       env={**os.environ, "WIF_PROVIDER": PROVIDER, "READER_SA": READER})
    assert r.returncode == 0, r.stderr
    config = json.loads(cfg.read_text())
    assert config["type"] == "external_account" and "private_key" not in json.dumps(config)
    assert config["audience"] == "//iam.googleapis.com/" + PROVIDER
    assert config["service_account_impersonation_url"].endswith(f"/serviceAccounts/{READER}:generateAccessToken")
    assert config["credential_source"] == {"file": str(tok), "format": {"type": "json",
                                                                         "subject_token_field_name": "value"}}
    from google.auth import identity_pool, load_credentials_from_file
    creds, _ = load_credentials_from_file(str(cfg))                         # no network: parsing only
    assert isinstance(creds, identity_pool.Credentials)
