"""Admins' dismissals: the shared GitHub commit runs on the app's background
writer (one thread for the process), so the click doesn't wait for it.

What must hold, and is checked here against a fake GitHub that behaves like
the contents API (every write must name the current sha, else 409):
  * a scan publishing between the commit's read and write is never
    overwritten — the write is refused, the commit re-reads and re-applies
    only its own change (dismissed flags on the chosen jobs)
  * rapid successive dismissals all land, one commit each, in click order,
    and the clicks don't wait for them
  * a failed commit is reported to the admin on their next run; their own
    (Firestore) dismissal stays, so the job stays hidden for them
  * members never reach the shared file
  * a process that shuts down normally finishes the commits already queued
    (only a hard kill can lose one — then the shared copy simply lacks that
    dismissal; the admin's own copy still has it)
Nothing reaches GitHub, Firebase or Gmail."""
import json
import subprocess
import sys
import textwrap
import threading
import time
from types import SimpleNamespace

import pytest
from github import GithubException

import config_store
from config_store import alert_pending, dismiss_jobs, job_key, update_json
from test_firebase_auth import admins, google_env, no_env  # noqa: F401  (fixtures)
from test_phase3_user_data import A_MAIL, A_UID, B_MAIL, B_UID, _sign_in, store  # noqa: F401
from test_streamlit_app import FRESHER_RECORD, NEW_RECORD, _html, _key, _nav, app  # noqa: F401

PATH = "seen_jobs.json"
GOOD = {"evidence": {"checks": {"detail_read": True, "fresher_or_entry_evidence": True}}}
JOBS = [{**NEW_RECORD, **GOOD},
        {**NEW_RECORD, **GOOD, "id": "PwC_Associate", "title": "Associate Analyst", "url": "https://pwc.example/job/3"},
        {**FRESHER_RECORD, **GOOD, "id": "MetLife_Graduate", "title": "Graduate Trainee", "url": "https://metlife.example/job/4"}]


class FakeRepo:
    """GitHub's contents API for one repository, in memory."""

    def __init__(self, files, latency=0.0, fail=None, on_read=None):
        self.files = {k: json.dumps(v) for k, v in files.items()}
        self.sha = {k: 1 for k in files}
        self.latency, self.fail, self.on_read = latency, fail, on_read
        self.commits, self.attempts, self.lock = [], 0, threading.Lock()

    def get_contents(self, path):
        time.sleep(self.latency)
        with self.lock:
            if path not in self.files:
                raise GithubException(404, {"message": "Not Found"}, None)
            got = SimpleNamespace(decoded_content=self.files[path].encode(), sha=str(self.sha[path]))
        if self.on_read:
            self.on_read(self, path)
        return got

    def update_file(self, path, message, content, sha):
        time.sleep(self.latency)
        with self.lock:
            self.attempts += 1
            if self.fail:
                raise GithubException(self.fail, {"message": "server error"}, None)
            if sha != str(self.sha[path]):
                raise GithubException(409, {"message": "sha does not match"}, None)
            self.files[path], self.sha[path] = content, self.sha[path] + 1
            self.commits.append(message)

    def publish(self, path, data):
        """Someone else (the scan's publish job) pushes a new version."""
        with self.lock:
            self.files[path], self.sha[path] = json.dumps(data), self.sha[path] + 1

    def data(self, path=PATH):
        return json.loads(self.files[path])


def _wait(cond, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


# ── the commit itself: never overwrites newer shared state ───────────────────

def test_a_scan_publishing_mid_commit_is_never_overwritten(tmp_path):
    first = [dict(JOBS[0]), dict(JOBS[1])]
    newer = [{**JOBS[0], "notified": True, "notify_state": "sent"}, dict(JOBS[1]), dict(JOBS[2])]   # +1 job, 1 emailed
    published = []

    def scan_publishes_once(repo, path):
        if not published:
            published.append(path)
            repo.publish(path, newer)                    # lands between our read and our write
    repo = FakeRepo({PATH: first}, on_read=scan_publishes_once)
    data, saved, err = update_json(tmp_path / PATH, PATH, dismiss_jobs({job_key(JOBS[1])}), [], "chore: dismiss 1 alert(s)",
                                   token="t", repo=repo)
    assert saved and err == "" and repo.attempts == 2   # the stale write was refused, the retry applied to the newer copy
    final = {j["id"]: j for j in repo.data()}
    assert set(final) == {JOBS[0]["id"], JOBS[1]["id"], JOBS[2]["id"]}             # the scan's new job kept
    assert final[JOBS[0]["id"]]["notify_state"] == "sent" and final[JOBS[0]["id"]]["notified"] is True
    assert final[JOBS[1]["id"]]["dismissed"] is True                                # our change, and only it
    assert not final[JOBS[0]["id"]].get("dismissed") and not final[JOBS[2]["id"]].get("dismissed")
    assert not alert_pending(final[JOBS[1]["id"]])                                  # the shared email skips it


def test_a_failed_commit_changes_nothing_on_github(tmp_path):
    repo = FakeRepo({PATH: JOBS}, fail=502)
    data, saved, err = update_json(tmp_path / PATH, PATH, dismiss_jobs({job_key(JOBS[0])}), [], "m", token="t", repo=repo)
    assert not saved and err and repo.data() == JOBS


# ── through the app ──────────────────────────────────────────────────────────

@pytest.fixture
def github(monkeypatch):
    def make(**kw):
        repo = FakeRepo({PATH: JOBS}, **kw)
        monkeypatch.setenv("GITHUB_TOKEN", "test-token")
        monkeypatch.setattr(config_store, "_get_repo", lambda token: repo)
        return repo
    return make


def _admin_on_jobs(app, google_env, admins):
    admins(A_MAIL)
    a = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    _nav(a, "jobs")
    return a


def test_rapid_dismissals_all_land_in_order_and_the_clicks_do_not_wait(app, google_env, store, admins, github):
    repo = github(latency=0.15)                                  # a commit = 2 round trips = 0.3 s here
    a = _admin_on_jobs(app, google_env, admins)
    t0 = time.time()
    for job in JOBS:
        a.button(key=_key("dismiss", job)).click().run()
        assert not a.exception
    clicks = time.time() - t0
    assert _wait(lambda: len(repo.commits) == 3), repo.commits
    assert repo.commits == ["chore: dismiss 1 alert(s)"] * 3
    assert all(j.get("dismissed") for j in repo.data())          # none lost
    assert store.for_uid(A_UID).dismissed() == {job_key(j) for j in JOBS}
    assert clicks < 3 * 0.3 + 3 * 1.5, clicks                     # (AppTest runs themselves take ~1 s each)
    a.run()
    assert "not in the shared list" not in _html(a)               # nothing failed: nothing reported


def test_a_failed_shared_commit_is_reported_and_the_admins_own_dismissal_stays(app, google_env, store, admins, github):
    repo = github(fail=502, latency=0.3)                         # the commit fails after the click returned
    a = _admin_on_jobs(app, google_env, admins)
    a.button(key=_key("dismiss", JOBS[0])).click().run()
    assert "Removed 1 alert(s)" in _html(a)                      # the click answered at once
    assert _wait(lambda: repo.attempts >= 1)
    time.sleep(0.3)                                               # the failure is recorded after the attempt
    a.run()                                                       # the admin's next click / page
    msg = "A dismissal was saved for you, but not in the shared list."
    assert msg in _html(a)
    assert store.for_uid(A_UID).dismissed() == {job_key(JOBS[0])}  # their own copy has it ...
    assert JOBS[0]["title"] not in _html(a) and JOBS[1]["title"] in _html(a)   # ... so it stays hidden for them
    assert not repo.data()[0].get("dismissed") and repo.commits == []
    a.run()
    assert msg not in _html(a)                                    # reported once


def test_members_never_write_the_shared_file(app, google_env, store, admins, github):
    repo = github()
    admins("someone.else@example.org")
    b = _sign_in(app, google_env, B_UID, B_MAIL, seen=JOBS)
    _nav(b, "jobs")
    b.button(key=_key("dismiss", JOBS[0])).click().run()
    time.sleep(0.5)
    assert repo.attempts == 0 and repo.commits == [] and not repo.data()[0].get("dismissed")
    assert store.for_uid(B_UID).dismissed() == {job_key(JOBS[0])}


def test_a_normal_shutdown_finishes_the_queued_commits(tmp_path):
    """Python waits for ThreadPoolExecutor threads when the interpreter exits
    normally (the way Streamlit stops on a restart or redeploy), so a commit
    queued just before is still made. Only a hard kill can drop it."""
    out = tmp_path / "committed.txt"
    script = textwrap.dedent(f"""
        import time
        from concurrent.futures import ThreadPoolExecutor
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jt-shared-write")
        def commit(n):
            time.sleep(0.3)                       # a commit still in flight at exit
            with open({str(out)!r}, "a") as f:
                f.write(f"{{n}}\\n")
        for n in range(3):
            pool.submit(commit, n)
        # the main thread ends here (no shutdown() call), as a stopping server's does
    """)
    subprocess.run([sys.executable, "-c", script], check=True, timeout=30)
    assert out.read_text().split() == ["0", "1", "2"]
