import json

import pytest
from github import GithubException

import config_store
from config_store import dismiss_jobs, fetch_remote_json, job_key, update_json, visible_jobs


class FakeRepo:
    """In-memory stand-in for a PyGithub Repository (contents API only)."""

    def __init__(self, files=None, fail=None):
        self.files = {k: (json.dumps(v), 1) for k, v in (files or {}).items()}
        self.fail = fail  # exception to raise from get_contents
        self.commits = []
        self.conflicts_to_inject = 0

    class _Content:
        def __init__(self, text, sha):
            self.decoded_content = text.encode("utf-8")
            self.sha = sha

    def get_contents(self, path):
        if self.fail:
            raise self.fail
        if path not in self.files:
            raise GithubException(404, {"message": "Not Found"}, None)
        text, sha = self.files[path]
        return self._Content(text, sha)

    def update_file(self, path, message, content, sha):
        if self.conflicts_to_inject:
            self.conflicts_to_inject -= 1
            # another writer committed first: the file moves on
            text, cur = self.files[path]
            data = json.loads(text) + [{"company": "X", "title": f"Concurrent {cur}", "url": "u"}]
            self.files[path] = (json.dumps(data), cur + 1)
            raise GithubException(409, {"message": "sha mismatch"}, None)
        if self.files[path][1] != sha:
            raise GithubException(409, {"message": "sha mismatch"}, None)
        self.files[path] = (content, sha + 1)
        self.commits.append(message)

    def create_file(self, path, message, content):
        self.files[path] = (content, 1)
        self.commits.append(message)

    def data(self, path):
        return json.loads(self.files[path][0])


def _jobs(*titles, **extra):
    return [{"company": "Sanofi", "title": t, "id": f"Sanofi_{t}", "url": f"https://x/{t}", **extra}
            for t in titles]


def test_update_merges_into_latest_remote_not_stale_local(tmp_path):
    """The bug this guards against: the app's local copy is hours old; the
    Actions run has since added jobs. Dismissing an alert must not drop them."""
    local = tmp_path / "seen_jobs.json"
    local.write_text(json.dumps(_jobs("A", "B")))                 # stale
    repo = FakeRepo({"seen_jobs.json": _jobs("A", "B", "C", "D")})  # latest
    target = job_key(_jobs("A")[0])

    data, saved, err = update_json(local, "seen_jobs.json", dismiss_jobs({target}), [], "msg",
                                   token="t", repo=repo)
    assert saved and err == ""
    remote = repo.data("seen_jobs.json")
    assert [j["title"] for j in remote] == ["A", "B", "C", "D"]      # nothing lost
    assert [j.get("dismissed", False) for j in remote] == [True, False, False, False]
    assert json.loads(local.read_text()) == remote                  # local caught up


def test_update_retries_on_conflict(tmp_path):
    repo = FakeRepo({"seen_jobs.json": _jobs("A")})
    repo.conflicts_to_inject = 1
    data, saved, _ = update_json(tmp_path / "s.json", "seen_jobs.json", dismiss_jobs(None), [], "m",
                                 token="t", repo=repo)
    assert saved
    remote = repo.data("seen_jobs.json")
    assert [j["title"] for j in remote] == ["A", "Concurrent 1"]    # concurrent write kept
    assert all(j.get("dismissed") for j in remote)


def test_update_gives_up_after_repeated_conflicts(tmp_path):
    repo = FakeRepo({"seen_jobs.json": _jobs("A")})
    repo.conflicts_to_inject = 10
    local = tmp_path / "s.json"
    local.write_text(json.dumps(_jobs("A")))
    data, saved, err = update_json(local, "seen_jobs.json", dismiss_jobs(None), [], "m",
                                   token="t", repo=repo)
    assert not saved and "try again" in err
    assert json.loads(local.read_text())[0]["dismissed"] is True    # applied locally


def test_update_creates_missing_remote_file(tmp_path):
    repo = FakeRepo({})
    data, saved, _ = update_json(tmp_path / "settings.json", "settings.json",
                                 lambda d: {**d, "recipient_email": "a@b.co"}, {}, "m",
                                 token="t", repo=repo)
    assert saved and repo.data("settings.json") == {"recipient_email": "a@b.co"}


def test_update_without_token_is_local_only(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    local = tmp_path / "settings.json"
    local.write_text(json.dumps({"recipient_email": "old@x.co", "other": 1}))
    data, saved, err = update_json(local, "settings.json", lambda d: {**d, "recipient_email": "new@x.co"},
                                   {}, "m")
    assert not saved and "No GitHub token" in err
    assert json.loads(local.read_text()) == {"recipient_email": "new@x.co", "other": 1}


@pytest.mark.parametrize("status, expected", [
    (401, "invalid or expired"), (403, "permission"), (500, "try again"),
])
def test_github_errors_are_friendly(tmp_path, status, expected):
    repo = FakeRepo(fail=GithubException(status, {"message": "secret internal detail"}, None))
    _, saved, err = update_json(tmp_path / "c.json", "companies.json", lambda d: d, [], "m",
                                token="t", repo=repo)
    assert not saved and expected in err
    assert "secret internal detail" not in err and "GithubException" not in err


def test_malformed_remote_json_falls_back_to_local(tmp_path):
    repo = FakeRepo({})
    repo.files["companies.json"] = ("{not json", 1)
    local = tmp_path / "companies.json"
    local.write_text("[]")
    _, saved, err = update_json(local, "companies.json", lambda d: d + [{"id": "n"}], [], "m",
                                token="t", repo=repo)
    assert not saved and err
    assert json.loads(local.read_text()) == [{"id": "n"}]
    assert repo.files["companies.json"] == ("{not json", 1)          # remote untouched


def test_fetch_remote_json(monkeypatch):
    repo = FakeRepo({"companies.json": [{"id": "a"}], "seen_jobs.json": []})
    repo.files["settings.json"] = ("{broken", 1)
    out = fetch_remote_json(["companies.json", "settings.json", "seen_jobs.json", "missing.json"],
                            token="t", repo=repo)
    assert out == {"companies.json": [{"id": "a"}], "seen_jobs.json": []}
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert fetch_remote_json(["companies.json"]) == {}             # no token -> no network


def test_job_key_distinguishes_same_id_prefix():
    a = {"id": "Sanofi_Associate_–_Evidence_Synthesis,_C", "url": "https://x/R1"}
    b = {"id": "Sanofi_Associate_–_Evidence_Synthesis,_C", "url": "https://x/R2"}
    assert job_key(a) != job_key(b)
    assert job_key({"company": "A", "title": "T"}) == "A_T|"         # old record without id/url


def test_visible_jobs_newest_first_and_skips_dismissed():
    seen = _jobs("old", "mid", "new")
    seen[1]["dismissed"] = True
    assert [j["title"] for j in visible_jobs(seen + ["junk"])] == ["new", "old"]


def test_dismiss_keeps_records_for_dedup():
    seen = _jobs("A", "B")
    out = dismiss_jobs(None)(seen)
    assert len(out) == 2 and all(j["dismissed"] for j in out)
    # scraper dedup still sees them, so they won't be emailed again
    assert {(j["company"], j["title"]) for j in out} == {("Sanofi", "A"), ("Sanofi", "B")}


def test_repo_level_404_is_friendly():
    err = config_store.friendly_github_error(GithubException(404, {"message": "Not Found"}, None))
    assert "can't access the repository" in err


def test_friendly_error_never_echoes_exception_text():
    assert "boom" not in config_store.friendly_github_error(RuntimeError("boom"))
