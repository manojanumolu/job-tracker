"""Notification idempotency: a job is emailed at most once, only when it is
accepted, not dismissed and its record has been published first."""
import json
import shutil
import subprocess

import pytest

import alerts
import config_store
import repo_sync


def _rec(title, url, category="FRESHER", **extra):
    return {"title": title, "url": url, "company": "Sanofi", "location": "Hyderabad", "category": category,
            "reason": "r", "notified": False, **extra}


LEGACY = {"title": "Associate – HEVA (Evidence Synthesis)", "company": "Sanofi", "notified": True, "dismissed": True,
          "url": "https://sanofi.wd3.myworkdayjobs.com/SanofiCareers/job/Hyderabad/Associate---HEVA--Evidence-Synthesis-_R2852866"}
NEW = _rec("Source-to-Pay Strategic Support Associate",
           "https://sanofi.wd3.myworkdayjobs.com/SanofiCareers/job/Hyderabad/Source-to-Pay-Strategic-Support-Associate_R2872514-1")


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(config_store, "SEEN_FILE", tmp_path / "seen_jobs.json")
    monkeypatch.setattr(config_store, "COMPANIES_FILE", tmp_path / "companies.json")
    monkeypatch.setattr(config_store, "SETTINGS_FILE", tmp_path / "settings.json")
    (tmp_path / "companies.json").write_text("[]")
    (tmp_path / "settings.json").write_text(json.dumps({"recipient_email": "me@example.com"}))

    def write(seen):
        (tmp_path / "seen_jobs.json").write_text(json.dumps(seen))
    return write


class FakeRemote:
    """publish() stand-in: merges into an in-memory remote and writes the
    merged copy locally, exactly like repo_sync.publish; can be told to fail."""
    def __init__(self, seen=None):
        self.seen = list(seen or [])
        self.fail = set()   # message prefixes that fail
        self.calls = []

    def __call__(self, message, local):
        self.calls.append(message)
        if any(message.startswith(f) for f in self.fail):
            return False
        self.seen = repo_sync.merge_seen(self.seen, local["seen_jobs.json"])
        config_store.SEEN_FILE.write_text(json.dumps(self.seen))
        return True


class Mailer:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def __call__(self, jobs, recipient):
        if self.fail:
            raise RuntimeError("SMTP down")
        self.sent.append([j["title"] for j in jobs])


# ---------------------------------------------------------------------------
# eligibility
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("record, eligible", [
    (NEW, True),
    (_rec("x", "u", category="ENTRY_LEVEL"), True),
    (_rec("x", "u", category="EXPERIENCED"), False),
    (_rec("x", "u", category="UNKNOWN"), False),
    (_rec("x", "u", category="NOT_A_JOB"), False),
    ({**NEW, "category": None}, False),                 # legacy / uncategorised
    ({**NEW, "dismissed": True}, False),                # dismissed -> never emailed
    ({**NEW, "notified": True}, False),
    ({**NEW, "notify_state": "claimed"}, False),
    ({**NEW, "notify_state": "sent"}, False),
    (LEGACY, False),
])
def test_alert_eligibility(record, eligible):
    assert config_store.alert_pending(record) is eligible


# ---------------------------------------------------------------------------
# the pipeline
# ---------------------------------------------------------------------------

def test_new_accepted_job_emailed_once(data):
    data([LEGACY, dict(NEW)])
    remote, mail = FakeRemote([LEGACY]), Mailer()
    assert alerts.run(send=mail, publisher=remote) == 0
    assert mail.sent == [["Source-to-Pay Strategic Support Associate"]]
    rec = [j for j in remote.seen if j["title"].startswith("Source")][0]
    assert rec["notified"] is True and rec["notify_state"] == "sent"
    # next run: nothing new
    assert alerts.run(send=mail, publisher=remote) == 0
    assert len(mail.sent) == 1
    # the legacy dismissed record is untouched and never emailed
    assert [j for j in remote.seen if j["title"].startswith("Associate")][0] == LEGACY


def test_no_email_when_results_cannot_be_published(data):
    data([dict(NEW)])
    remote, mail = FakeRemote(), Mailer()
    remote.fail.add(alerts.DATA_MESSAGE)
    assert alerts.run(send=mail, publisher=remote) == 1
    assert mail.sent == []


def test_final_push_failure_never_causes_a_resend(data):
    data([dict(NEW)])
    remote, mail = FakeRemote(), Mailer()
    remote.fail.add(alerts.SENT_MESSAGE)
    assert alerts.run(send=mail, publisher=remote) == 1
    assert len(mail.sent) == 1
    # the durable state says "claimed": the next run (fresh checkout of the remote) does not resend
    data(remote.seen)
    remote.fail.clear()
    assert alerts.run(send=mail, publisher=remote) == 0
    assert len(mail.sent) == 1


def test_send_failure_releases_claim_and_retries(data):
    data([dict(NEW)])
    remote = FakeRemote()
    assert alerts.run(send=Mailer(fail=True), publisher=remote) == 1
    rec = remote.seen[0]
    assert rec["notify_state"] == "pending" and not rec.get("notified")
    data(remote.seen)
    mail = Mailer()
    assert alerts.run(send=mail, publisher=remote) == 0
    assert mail.sent == [["Source-to-Pay Strategic Support Associate"]]


def test_job_dismissed_in_the_app_before_sending_is_not_emailed(data):
    data([dict(NEW)])
    remote, mail = FakeRemote(), Mailer()

    def publisher(message, local):
        ok = remote(message, local)
        if message == alerts.DATA_MESSAGE:
            # the user dismisses the job in the app right after it was published
            for j in remote.seen:
                j["dismissed"] = True
            config_store.SEEN_FILE.write_text(json.dumps(remote.seen))
        return ok

    assert alerts.run(send=mail, publisher=publisher) == 0
    assert mail.sent == []
    assert remote.seen[0]["notify_state"] == "skipped" and remote.seen[0]["dismissed"]


def test_no_recipient_sends_nothing_and_keeps_jobs_pending(data, tmp_path):
    (tmp_path / "settings.json").write_text(json.dumps({"recipient_email": ""}))
    data([dict(NEW)])
    remote, mail = FakeRemote(), Mailer()
    assert alerts.run(send=mail, publisher=remote) == 0
    assert mail.sent == [] and config_store.alert_pending(remote.seen[0])


# ---------------------------------------------------------------------------
# merge rules
# ---------------------------------------------------------------------------

def test_merge_never_undoes_dismissal_or_delivery():
    remote = [{**NEW, "dismissed": True}, {**LEGACY}]
    local = [{**NEW, "notify_state": "claimed", "claimed_at": "t1"}, {**LEGACY, "notified": False}]
    merged = repo_sync.merge_seen(remote, local)
    assert merged[0]["dismissed"] is True and merged[0]["notify_state"] == "claimed"
    assert merged[1]["notified"] is True and merged[1]["dismissed"] is True


def test_only_the_claiming_run_can_release_its_claim():
    remote = [{**NEW, "notify_state": "claimed", "claimed_at": "run-2"}]
    stale_release = [{**NEW, "notify_state": "pending", "released_claim": "run-1"}]
    assert repo_sync.merge_seen(remote, stale_release)[0]["notify_state"] == "claimed"
    own_release = [{**NEW, "notify_state": "pending", "released_claim": "run-2"}]
    assert repo_sync.merge_seen(remote, own_release)[0]["notify_state"] == "pending"


def test_merge_keeps_both_sides_new_records():
    a = _rec("A", "https://x.myworkdayjobs.com/S/job/H/A_R1")
    b = _rec("B", "https://x.myworkdayjobs.com/S/job/H/B_R2")
    assert [j["title"] for j in repo_sync.merge_seen([a], [b])] == ["A", "B"]
    assert [j["title"] for j in repo_sync.merge_seen([a, b], [a])] == ["A", "B"]


def test_merge_companies_respects_app_changes():
    remote = [{"id": "sanofi", "name": "Sanofi", "url": "new-url", "status": "active"},
              {"id": "c9", "name": "Added in app", "url": "u", "status": "unknown"}]
    local = [{"id": "sanofi", "name": "Sanofi", "url": "old-url", "status": "failing", "status_reason": "HTTP 500",
              "last_checked": "t"},
             {"id": "c1", "name": "Removed in app", "url": "u", "status": "active"}]
    merged = repo_sync.merge_companies(remote, local)
    assert [c["id"] for c in merged] == ["sanofi", "c9"]            # removal respected, addition kept
    assert merged[0]["url"] == "new-url"                             # app config wins
    assert merged[0]["status"] == "failing" and merged[0]["status_reason"] == "HTTP 500"   # scan results from the run


# ---------------------------------------------------------------------------
# real git: concurrent app edits are merged, never overwritten
# ---------------------------------------------------------------------------

def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repos(tmp_path):
    if not shutil.which("git"):
        pytest.skip("git not available")
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    clones = []
    for name in ("actions", "app"):
        d = tmp_path / name
        _git(tmp_path, "clone", str(origin), str(d))
        _git(d, "config", "user.email", f"{name}@example.com")
        _git(d, "config", "user.name", name)
        _git(d, "checkout", "-B", "main")
        clones.append(d)
    actions, app = clones
    (actions / "seen_jobs.json").write_text(json.dumps([LEGACY, NEW]))
    (actions / "companies.json").write_text(json.dumps([{"id": "sanofi", "name": "Sanofi", "status": "active"}]))
    _git(actions, "add", ".")
    _git(actions, "commit", "-m", "init")
    _git(actions, "push", "origin", "main")
    _git(app, "pull", "origin", "main")
    return actions, app


def test_publish_merges_a_dismissal_made_during_the_run(repos):
    actions, app = repos
    # the app dismisses the new job while the run is scraping
    seen = json.loads((app / "seen_jobs.json").read_text(encoding="utf-8"))
    seen[1]["dismissed"] = True
    (app / "seen_jobs.json").write_text(json.dumps(seen))
    _git(app, "commit", "-am", "dismiss")
    _git(app, "push", "origin", "main")
    # the run publishes its (older) copy plus a newly found job
    found = _rec("Graduate Trainee", "https://sanofi.wd3.myworkdayjobs.com/SanofiCareers/job/Pune/Graduate-Trainee_R9")
    local = json.loads((actions / "seen_jobs.json").read_text(encoding="utf-8")) + [found]
    companies = [{"id": "sanofi", "name": "Sanofi", "status": "failing", "status_reason": "HTTP 500"}]
    assert repo_sync.publish("data", {"seen_jobs.json": local, "companies.json": companies}, cwd=actions, branch="main")
    _git(app, "pull", "origin", "main")
    result = json.loads((app / "seen_jobs.json").read_text(encoding="utf-8"))
    assert [j["title"] for j in result] == [LEGACY["title"], NEW["title"], "Graduate Trainee"]
    assert result[1]["dismissed"] is True                # the app's dismissal survived
    assert json.loads((app / "companies.json").read_text(encoding="utf-8"))[0]["status"] == "failing"


def test_publish_reports_failure_when_it_cannot_push(repos, tmp_path):
    actions, _ = repos
    _git(actions, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    assert repo_sync.publish("data", {"seen_jobs.json": [NEW]}, cwd=actions, branch="main", attempts=2) is False
