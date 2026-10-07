"""The personal notifier inside the Check Jobs workflow: the global scan
and shared alerts stay exactly as they were, the personal step runs after
them and can never fail the job, and its CLI behaves safely in every state
(not set up, broken credential, broken data, failing sends)."""
import hashlib
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import notifier
import user_alerts
import user_store
from config_store import job_key
from user_store import MemoryBackend, UserStore, UserStoreError

REPO = Path(__file__).resolve().parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "check_jobs.yml"
STEP_MARK = "\n      # Personal alerts (per-user"
# sha256 of check_jobs.yml as it was on main (f77b76f) before the personal
# step existed, line endings normalised: the global scan must stay exactly so
GLOBAL_WORKFLOW_SHA256 = "250a1e9b50c756c60ead5723b74fd5f3e63c9519e8fd6db8ca01340d52d1f51c"


def _wf() -> str:
    return WORKFLOW.read_text("utf-8").replace("\r\n", "\n")


def _steps(text: str) -> dict[str, str]:
    """name -> the step's text (a tiny parser: no YAML library in CI)."""
    parts = text.split("\n      - name: ")[1:]
    return {p.split("\n", 1)[0].strip(): p for p in parts}


# ---------------------------------------------------------------------------
# the workflow
# ---------------------------------------------------------------------------

def test_the_global_scan_is_byte_for_byte_unchanged():
    wf = _wf()
    assert STEP_MARK in wf
    assert hashlib.sha256(wf[:wf.index(STEP_MARK)].encode()).hexdigest() == GLOBAL_WORKFLOW_SHA256


def test_the_personal_step_runs_last_and_can_never_fail_the_job():
    steps = _steps(_wf())
    names = list(steps)
    assert names == ["Checkout repo", "Set up Python", "Install dependencies", "Run scraper",
                     "Publish results and send email alerts", "Send personal email alerts"]
    step = steps["Send personal email alerts"]
    assert "continue-on-error: true" in step                         # its failure leaves the job green
    assert "timeout-minutes: 10" in step                             # and can't hang the job
    # it runs whenever the shared step may run — not only when the shared
    # email succeeded (one must never depend on the other)
    publish_if = steps["Publish results and send email alerts"].split("if: ", 1)[1].split("\n", 1)[0]
    assert step.split("if: ", 1)[1].split("\n", 1)[0] == publish_if
    assert step.rstrip().endswith("run: python user_alerts.py --send")


def test_the_personal_step_gets_only_the_secrets_it_needs():
    step = _steps(_wf())["Send personal email alerts"]
    env = [line.strip() for line in step.split("env:", 1)[1].split("run:", 1)[0].splitlines() if line.strip()]
    assert env == ["FIREBASE_SERVICE_ACCOUNT: ${{ secrets.FIREBASE_SERVICE_ACCOUNT }}",
                   "GMAIL_ADDRESS: ${{ secrets.GMAIL_ADDRESS }}",
                   "GMAIL_APP_PASSWORD: ${{ secrets.GMAIL_APP_PASSWORD }}"]
    wf = _wf()
    assert wf.count("secrets.FIREBASE_SERVICE_ACCOUNT") == 1          # no other step sees the key
    assert "GITHUB_TOKEN" not in step and "ALERT_RECIPIENT" not in step and "git " not in step
    assert "echo" not in wf.lower()


def test_the_workflow_still_serialises_runs():
    wf = _wf()
    assert "group: check-jobs" in wf and "cancel-in-progress: false" in wf    # no overlapping runs


def test_the_shared_notifier_does_not_know_the_personal_one():
    src = (REPO / "alerts.py").read_text("utf-8")
    assert "user_alerts" not in src and "user_store" not in src and "firebase" not in src.lower()


# ---------------------------------------------------------------------------
# the CLI, run exactly as the step runs it
# ---------------------------------------------------------------------------

def _cli(env_extra: dict, args=("--send",)) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()
           if k not in ("FIREBASE_SERVICE_ACCOUNT", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "FIREBASE_PROJECT_ID")}
    env.update(env_extra)
    env["PYTHONWARNINGS"] = "ignore"
    return subprocess.run([sys.executable, "user_alerts.py", *args], cwd=REPO, env=env, capture_output=True,
                          text=True, timeout=120)


def test_without_the_secret_the_step_does_nothing_and_succeeds():
    r = _cli({})
    assert r.returncode == 0 and "not set up — nothing to do" in r.stdout + r.stderr


def test_an_empty_secret_is_the_same_as_none():
    r = _cli({"FIREBASE_SERVICE_ACCOUNT": ""})
    assert r.returncode == 0


@pytest.mark.parametrize("secret", [
    "{not json",
    json.dumps({"type": "service_account", "project_id": "p", "private_key": "pk-SECRET-VALUE"}),
    json.dumps({"type": "service_account", "project_id": "p", "private_key": "-----BEGIN PRIVATE KEY-----\npk-SECRET-VALUE\n",
                "client_email": "svc-SECRET@p.iam.gserviceaccount.com"}),
])
def test_a_broken_secret_fails_the_step_without_leaking_it(secret):
    r = _cli({"FIREBASE_SERVICE_ACCOUNT": secret})
    out = r.stdout + r.stderr
    assert r.returncode == 1 and "Traceback" not in out
    assert "SECRET" not in out and "BEGIN PRIVATE KEY" not in out


# ---------------------------------------------------------------------------
# failure isolation inside one run
# ---------------------------------------------------------------------------

A, B, C = "UserAaaaaaaaaaaaaaaaaaaaaaaa1", "UserBbbbbbbbbbbbbbbbbbbbbbbb2", "UserCcccccccccccccccccccccccc3"
PASSED = {"checks": {"india": True, "experience": True}}


def _job(n, company="pwc", title="Software Engineer", location="Pune · India", category="FRESHER",
         first_seen="2030-01-02T00:00:00+00:00"):
    return {"id": f"{company}_{n}", "url": f"https://jobs.example/{company}/{n}", "company_id": company,
            "company": company, "title": title, "location": location, "category": category, "evidence": PASSED,
            "first_seen": first_seen}


class Identity:
    def __init__(self):
        self.accounts = {}

    def account(self, uid):
        return self.accounts.get(uid)

    def google_account(self, sub):
        return None


def _subscriber(store, uid, email, *, mode="general", prefs=None, follow=None, since="2030-01-01T00:00:00Z",
                state="ok"):
    u = store.for_uid(uid)
    u.ensure_profile(email, "", True)
    if follow is not None:
        for c in follow:
            u.watch(c, {"pwc", "sanofi", "metlife"})
        u.set_watch_all(False)
    if prefs:
        u.set_preferences(mode, **prefs)
    store.backend.set(f"users/{uid}", {"notifications": {"enabled": True, "mode": mode, "enabled_at": since}},
                      merge=True)
    store.identity.accounts[uid] = None if state == "deleted" else {
        "uid": uid, "email": email, "email_verified": True, "disabled": state == "disabled"}


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    """main() with an in-memory store, a seen_jobs.json of our own and a
    recording mailer in place of Gmail."""
    store = UserStore(MemoryBackend(), Identity())
    monkeypatch.setattr(user_alerts, "configure", lambda secrets, project_id="": (store, ""))
    monkeypatch.setattr(user_alerts, "BASE", tmp_path)
    sent, fail_for, lock = [], set(), threading.Lock()

    def send_alerts(jobs, to):
        if to in fail_for:
            raise RuntimeError(f"smtp refused {to}")
        with lock:
            sent.append((to, sorted(job_key(j) for j in jobs)))
    monkeypatch.setattr(notifier, "send_alerts", send_alerts)

    def seen(jobs):
        (tmp_path / "seen_jobs.json").write_text(json.dumps(jobs), "utf-8")
    return {"store": store, "sent": sent, "fail_for": fail_for, "seen": seen}


def test_many_people_one_failure_the_others_still_get_their_email(cli_env, caplog):
    s = cli_env["store"]
    _subscriber(s, A, "a@example.org")
    _subscriber(s, B, "b@example.org")
    _subscriber(s, C, "c@example.org")
    cli_env["seen"]([_job(1)])
    cli_env["fail_for"].add("b@example.org")
    assert user_alerts.main(["--send"]) == 1                          # reported as a failed step...
    assert sorted(to for to, _ in cli_env["sent"]) == ["a@example.org", "c@example.org"]   # ...others delivered
    assert s.for_uid(B).delivery(job_key(_job(1)))["state"] == "pending"
    assert "b@example.org" not in caplog.text and B not in caplog.text
    cli_env["fail_for"].clear()
    assert user_alerts.main(["--send"]) == 0                           # the retry
    assert sorted(to for to, _ in cli_env["sent"]) == ["a@example.org", "b@example.org", "c@example.org"]


def test_disabled_deleted_and_unsubscribed_people_get_nothing(cli_env):
    s = cli_env["store"]
    _subscriber(s, A, "a@example.org", state="disabled")
    _subscriber(s, B, "b@example.org", state="deleted")
    _subscriber(s, C, "c@example.org")
    s.backend.set(f"users/{C}", {"notifications": {"enabled": False, "mode": "general", "enabled_at": None}},
                  merge=True)
    cli_env["seen"]([_job(1)])
    assert user_alerts.main(["--send"]) == 0 and cli_env["sent"] == []


def test_general_tailored_scope_and_cutoff_through_the_cli(cli_env):
    s = cli_env["store"]
    _subscriber(s, A, "a@example.org", follow=["sanofi"])                               # scope: sanofi only
    _subscriber(s, B, "b@example.org", mode="tailored",
                prefs={"job_families": ["Data & analytics"], "locations": ["Hyderabad"], "experience": ["fresher"]})
    _subscriber(s, C, "c@example.org", since="2030-06-01T00:00:00Z")                     # subscribed later
    jobs = [_job(1, "pwc"), _job(2, "sanofi"),
            _job(3, "metlife", title="Data Analyst", location="Hyderabad · India"),
            _job(4, "metlife", title="Data Analyst", location="Hyderabad", category="ENTRY_LEVEL"),
            _job(5, "pwc", first_seen="2030-07-01T00:00:00+00:00")]
    cli_env["seen"](jobs)
    user_alerts.main(["--send"])
    got = {to: keys for to, keys in cli_env["sent"]}
    assert got["a@example.org"] == [job_key(jobs[1])]
    assert got["b@example.org"] == [job_key(jobs[2])]
    assert got["c@example.org"] == [job_key(jobs[4])]


def test_concurrent_workflow_runs_deliver_each_job_once(cli_env):
    s = cli_env["store"]
    for i, uid in enumerate((A, B, C)):
        _subscriber(s, uid, f"{i}@example.org")
    jobs = [_job(n) for n in range(5)]
    cli_env["seen"](jobs)
    threads = [threading.Thread(target=user_alerts.main, args=(["--send"],)) for _ in range(5)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    per = {}
    for to, keys in cli_env["sent"]:
        per.setdefault(to, []).extend(keys)
    assert {to: sorted(k) for to, k in per.items()} == {f"{i}@example.org": sorted(job_key(j) for j in jobs)
                                                        for i in range(3)}


@pytest.mark.parametrize("break_it", ["user_listing", "corrupt_data", "unexpected_error"])
def test_the_cli_never_crashes(cli_env, monkeypatch, caplog, break_it):
    s = cli_env["store"]
    _subscriber(s, A, "a@example.org")
    cli_env["seen"]([_job(1)])
    if break_it == "user_listing":
        monkeypatch.setattr(s, "user_ids", lambda: (_ for _ in ()).throw(UserStoreError("x")))
    elif break_it == "corrupt_data":
        (user_alerts.BASE / "seen_jobs.json").write_text("{not json", "utf-8")
    else:
        monkeypatch.setattr(user_alerts, "run_for_user", lambda *a, **k: (_ for _ in ()).throw(KeyError("boom")))
    assert user_alerts.main(["--send"]) == 1
    assert cli_env["sent"] == [] and "Traceback" not in caplog.text and "a@example.org" not in caplog.text


def test_dry_run_is_the_default(cli_env):
    s = cli_env["store"]
    _subscriber(s, A, "a@example.org")
    cli_env["seen"]([_job(1)])
    assert user_alerts.main([]) == 0
    assert cli_env["sent"] == [] and s.for_uid(A).delivery(job_key(_job(1))) is None


# ---------------------------------------------------------------------------
# the one credential: same name everywhere, accepted as documented
# ---------------------------------------------------------------------------

def test_the_secret_name_is_the_same_in_code_workflow_and_docs():
    doc = (REPO / "docs" / "PERSONAL_DATA_ACTIVATION.md").read_text("utf-8")
    assert user_store.SERVICE_ACCOUNT_KEY == "FIREBASE_SERVICE_ACCOUNT"
    assert "FIREBASE_SERVICE_ACCOUNT: ${{ secrets.FIREBASE_SERVICE_ACCOUNT }}" in _wf()
    assert "**Secret name** | `FIREBASE_SERVICE_ACCOUNT`" in doc
    assert "BEGIN PRIVATE KEY" not in doc and '"private_key": "' not in doc         # no real-looking values


def test_the_documented_streamlit_form_is_accepted():
    """A TOML literal block ('''...''') around the pasted JSON adds newlines."""
    sa = {"type": "service_account", "project_id": "job-tracker-x", "private_key": "-----BEGIN PRIVATE KEY-----\nX\n",
          "client_email": "svc@job-tracker-x.iam.gserviceaccount.com"}
    pasted = "\n" + json.dumps(sa, indent=2) + "\n"
    assert user_store._service_account({"FIREBASE_SERVICE_ACCOUNT": pasted}) == sa
    assert user_store._service_account({"firebase_service_account": sa}) == sa
    store, why = user_store.configure({"FIREBASE_SERVICE_ACCOUNT": pasted}, "another-project")
    assert store is None and why == "the Firebase service account belongs to a different project"
