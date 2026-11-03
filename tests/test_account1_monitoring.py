"""Account 1 (the primary admin): Hyderabad fresher monitoring (Nov 2026).

The primary admin's monitoring is the shared scope: the catalogue companies
in the shared alerts, scanned by the Check Jobs workflow and emailed to
ALERT_RECIPIENT by alerts.py. ``shared_alert_tailoring`` in settings.json
narrows that email the way a person's own tailoring narrows theirs (same
vocabulary, same matcher): Account 1 asks for Hyderabad and three job
families. Discovery, the job lists and the other admin's monitoring are
untouched.

Everything runs on fakes: data files in tmp_path, a fake publisher and
mailer, user_store.MemoryBackend — nothing reaches a real service."""
import json

import httpx
import pytest

import alerts
import config_store
import job_filters
import scraper
import sources
import user_alerts
from config_store import job_key, shared_alert_pending, shared_tailoring
from job_classifier import classify_job
from user_store import MemoryBackend, UserStore

from test_alerts import GATE, FakeRemote, Mailer
from test_user_alerts import Identity, Mailer as PersonalMailer, _person

ACCOUNT1_TAILORING = {"locations": ["Hyderabad"],
                      "job_families": ["Software engineering", "Data & analytics", "Cloud & DevOps"]}
B_UID, B_MAIL = "AdminBbbbbbbbbbbbbbbbbbbbbbb2", "ben@example.org"
# Account 2's companies stay in the catalogue, outside the shared alerts
CATALOGUE = [{"id": "accenture", "name": "Accenture"}, {"id": "micron", "name": "Micron"},
             {"id": "sanofi", "name": "Sanofi", "shared_alerts": False},
             {"id": "pwc", "name": "PwC", "shared_alerts": False}]


def rec(n, title, location="Hyderabad, Telangana, India", company="micron", category="FRESHER", **extra):
    return {"title": title, "url": f"https://{company}.example/job/{n}", "company": company.title(),
            "company_id": company, "location": location, "category": category, "reason": "r",
            "id": f"{company}_{n}", "first_seen": "2030-01-02T00:00:00+00:00",
            "evidence": {"checks": dict(GATE)}, **extra}


HYD_SW = rec(1, "Software Engineer")
HYD_PY = rec(2, "Python Developer")
HYD_AI = rec(3, "AI/ML Engineer")
HYD_DE = rec(4, "Data Engineer")
HYD_GET = rec(5, "Graduate Engineer Trainee")
HYD_SENIOR = rec(6, "Senior Software Engineer", category="SENIOR")          # 5 years: rejected by the classifier
BLR_SW = rec(7, "Software Engineer", location="Bengaluru, Karnataka, India")
HYD_FIN = rec(8, "Finance Associate - Accounts Payable")
HYD_SALES = rec(9, "Inside Sales Representative")
SHOULD_SEND = [HYD_SW, HYD_PY, HYD_AI, HYD_DE, HYD_GET]
ALL = SHOULD_SEND + [HYD_SENIOR, BLR_SW, HYD_FIN, HYD_SALES]


@pytest.fixture
def shared(tmp_path, monkeypatch):
    """alerts.py's data files; ``settings`` is what the primary admin saved."""
    monkeypatch.setenv("ALERT_RECIPIENT", "me@example.com")
    monkeypatch.setattr(config_store, "SEEN_FILE", tmp_path / "seen_jobs.json")
    monkeypatch.setattr(config_store, "COMPANIES_FILE", tmp_path / "companies.json")
    monkeypatch.setattr(config_store, "SETTINGS_FILE", tmp_path / "settings.json")
    (tmp_path / "companies.json").write_text(json.dumps(CATALOGUE))

    def write(seen, settings):
        (tmp_path / "seen_jobs.json").write_text(json.dumps(seen))
        (tmp_path / "settings.json").write_text(json.dumps(settings))
    return write


# ── C: the tailored shared email ─────────────────────────────────────────────

def test_account1_email_gets_only_hyderabad_tech_fresher_jobs(shared):
    shared([dict(j) for j in ALL], {"scan_interval_hours": 1, "shared_alert_tailoring": ACCOUNT1_TAILORING})
    remote, mail = FakeRemote(), Mailer()
    assert alerts.run(send=mail, publisher=remote) == 0
    assert sorted(mail.sent[0]) == sorted(j["title"] for j in SHOULD_SEND)
    rejected = {j["id"] for j in (HYD_SENIOR, BLR_SW, HYD_FIN, HYD_SALES)}
    for j in remote.seen:
        if j["id"] in rejected:                  # untouched in the ledger: never claimed, never sent
            assert not j.get("notified") and j.get("notify_state") is None


def test_a_qualifying_job_is_emailed_once_however_often_it_is_scanned(shared):
    shared([dict(HYD_SW)], {"shared_alert_tailoring": ACCOUNT1_TAILORING})
    remote, mail = FakeRemote(), Mailer()
    assert alerts.run(send=mail, publisher=remote) == 0
    assert alerts.run(send=mail, publisher=remote) == 0          # the next hourly scan: same job again
    assert mail.sent == [["Software Engineer"]]


def test_the_same_posting_found_again_or_updated_is_not_a_new_job():
    seen = [dict(HYD_SW, uid=scraper.job_uid(HYD_SW["company"], HYD_SW["url"]))]
    again = {k: HYD_SW[k] for k in ("title", "url", "company", "location", "category", "reason")}
    updated = dict(again, title="Software Engineer (Python)")          # the company edited the title
    repost = dict(again, url="https://micron.example/job/1001")         # re-posted under a new ID
    assert scraper.merge_new_jobs(seen, [again, updated, repost]) == []


def test_no_tailoring_keeps_todays_behaviour_every_qualifying_job(shared):
    shared([dict(j) for j in ALL], {"scan_interval_hours": 3})
    remote, mail = FakeRemote(), Mailer()
    assert alerts.run(send=mail, publisher=remote) == 0
    # the shared gate still drops the senior role; everything else goes out
    assert sorted(mail.sent[0]) == sorted(j["title"] for j in ALL if j is not HYD_SENIOR)


@pytest.mark.parametrize("settings", [{}, {"shared_alert_tailoring": None}, {"shared_alert_tailoring": "Hyderabad"},
                                      {"shared_alert_tailoring": {}}, None, []])
def test_a_missing_or_malformed_setting_means_no_tailoring(settings):
    t = shared_tailoring(settings)
    assert all(shared_alert_pending(dict(j), tailoring=t) for j in (HYD_SW, BLR_SW, HYD_FIN))


def test_the_app_shows_outside_tailoring_jobs_as_not_pending():
    """The job page's "Pending — goes out after the next check" uses the
    same test as the email, so a Bengaluru job never claims to be on its way."""
    assert shared_alert_pending(dict(HYD_SW), frozenset(), ACCOUNT1_TAILORING)
    assert not shared_alert_pending(dict(BLR_SW), frozenset(), ACCOUNT1_TAILORING)
    assert not shared_alert_pending(dict(HYD_FIN), frozenset(), ACCOUNT1_TAILORING)


def test_companies_outside_the_shared_alerts_stay_out_whatever_the_tailoring(shared):
    sanofi = rec(20, "Software Engineer", company="sanofi")              # Account 2's company
    shared([dict(HYD_SW), dict(sanofi)], {"shared_alert_tailoring": ACCOUNT1_TAILORING})
    remote, mail = FakeRemote(), Mailer()
    assert alerts.run(send=mail, publisher=remote) == 0
    assert mail.sent == [["Software Engineer"]]
    assert not next(j for j in remote.seen if j["company_id"] == "sanofi").get("notify_state")


@pytest.mark.parametrize("title", [
    "Software Development Engineer", "SDE I", "Software Engineer", "Associate Software Engineer",
    "Graduate Engineer Trainee", "Engineer Trainee", "Trainee Engineer", "Python Developer", "Backend Developer",
    "Application Developer", "Junior Software Developer", "AI/ML Engineer", "Machine Learning Engineer",
    "Data Engineer", "Junior Data Analyst", "Cloud Engineer", "DevOps Engineer", "Associate Engineer",
    "Engineer I - Software QA", "Systems Engineer", "Member of Technical Staff",
    "Packaged App Development Associate", "Technology Analyst", "Programmer Analyst", "Business Analyst",
    "Associate Engineer, IT Eng System Mgr",                       # Micron, Hyderabad (live scan, Nov 2026)
])
def test_every_profile_title_is_in_one_of_the_three_families(title):
    assert job_filters.job_families({"title": title}) & set(ACCOUNT1_TAILORING["job_families"]), title


@pytest.mark.parametrize("title", [
    "Finance Associate - Accounts Payable", "Inside Sales Representative", "HR Recruiter",
    "Customer Support Associate", "Record to Report Ops New Associate", "Engineer II", "Engineer In Training Lead",
    # accepted fresher postings from the live scan of the new companies (Nov 2026): not tech roles
    "Content Marketing Analyst", "Operations Finance Analyst", "Project Controlling Analyst - CCC",
    "Back Office Operations Analyst – Band 1", "Program Management Analyst - Cigna Healthcare",
    "Associate Analyst, Supply Chain Planning", "Safety Reporting Specialist", "Management Trainee",
])
def test_unrelated_titles_stay_outside_the_three_families(title):
    assert not job_filters.job_families({"title": title}) & set(ACCOUNT1_TAILORING["job_families"]), title


# ── experience: the shared classifier (unchanged) ────────────────────────────

D = "Location: Hyderabad. You will build backend services in Python and SQL. "


@pytest.mark.parametrize("title, text, accepted", [
    ("Software Engineer", "Qualifications: 0-2 years of experience. B.Tech 2026 graduates.", True),
    ("Software Engineer", "Qualifications: 0-1 years of experience.", True),
    ("Graduate Engineer Trainee", "Open to freshers / 2026 graduates. No prior experience required.", True),
    ("Associate Software Engineer", "Entry-level role for recent graduates.", True),
    ("Software Engineer", "Requires 5 years of experience.", False),
    ("Software Engineer", "Minimum 3+ years of experience in Java.", False),
    ("Senior Software Engineer", "Open to freshers.", False),
    ("Lead Data Engineer", "0-2 years of experience.", False),
    ("Engineering Manager", "Fresh graduates welcome.", False),
    ("Software Engineering Intern", "Internship for students graduating in 2027.", False),
])
def test_experience_rules(title, text, accepted):
    assert classify_job(title, D + text).accepted is accepted


# ── A/B: the background scan ─────────────────────────────────────────────────

@pytest.fixture
def scan_files(tmp_path, monkeypatch):
    monkeypatch.setattr(config_store, "SEEN_FILE", tmp_path / "seen_jobs.json")
    monkeypatch.setattr(config_store, "COMPANIES_FILE", tmp_path / "companies.json")
    monkeypatch.setattr(scraper.time, "sleep", lambda s: None)
    (tmp_path / "seen_jobs.json").write_text("[]")
    companies = [{"id": "micron", "name": "Micron", "url": "https://micron.wd1.myworkdayjobs.com/External",
                  "last_success": "2020-01-01T00:00:00+00:00"},
                 {"id": "down", "name": "Down Co", "url": "https://down.wd1.myworkdayjobs.com/External",
                  "last_success": "2020-01-01T00:00:00+00:00"},
                 {"id": "accenture", "name": "Accenture", "url": "https://www.accenture.com/in-en/careers/jobsearch"}]
    (tmp_path / "companies.json").write_text(json.dumps(companies))
    return tmp_path


def _scan_result(company):
    scan = scraper.Scan(company["name"])
    if company["id"] == "down":
        scan.fail("failing", "HTTP 503 from Workday API")         # the careers site is down / blocking us
        return scan
    scan.sources_ok = 1
    scan.jobs = [{"title": f"{company['name']} Graduate Engineer Trainee", "company": company["name"],
                  "url": f"https://{company['id']}.example/job/1", "location": "Hyderabad", "category": "FRESHER",
                  "reason": "r", "evidence": {"checks": dict(GATE)}}]
    return scan


def test_one_failing_company_never_stops_the_others(scan_files, monkeypatch):
    monkeypatch.setattr(scraper, "scan_company", _scan_result)
    new = scraper.run_all()
    assert sorted(j["company_id"] for j in new) == ["accenture", "micron"]
    companies = {c["id"]: c for c in json.loads((scan_files / "companies.json").read_text())}
    down = companies["down"]
    # reported as failing, never as a healthy scan with zero jobs; the last good scan is kept
    assert down["status"] == "failing" and "503" in down["status_reason"]
    assert down["last_success"] == "2020-01-01T00:00:00+00:00" and down["last_checked"] > down["last_success"]
    assert companies["micron"]["status"] == "active" and companies["micron"]["last_success"] == companies["micron"]["last_checked"]
    assert all(c["scan"]["run"]["portals_failed"] == 1 for c in companies.values())


def test_a_temporary_failure_is_retried_on_the_next_scan(scan_files, monkeypatch):
    calls = {"n": 0}

    def flaky(company):
        if company["id"] == "down":
            calls["n"] += 1
            if calls["n"] == 1:
                return _scan_result(company)                 # first scan: down
            return _scan_result({**company, "id": "down-recovered"})
        return _scan_result(company)

    monkeypatch.setattr(scraper, "scan_company", flaky)
    scraper.run_all()
    (scan_files / "seen_jobs.json").write_text("[]")
    second = scraper.run_all()
    companies = {c["id"]: c for c in json.loads((scan_files / "companies.json").read_text())}
    assert companies["down"]["status"] == "active" and any(j["company_id"] == "down" for j in second)


@pytest.mark.parametrize("status, kind", [(503, "unavailable"), (403, "blocked"), (429, "rate limited")])
def test_an_unavailable_or_blocking_workday_site_is_a_failed_check(monkeypatch, status, kind):
    def handler(request):
        return httpx.Response(status, text="Access denied" if status == 403 else "")

    real_client = httpx.Client
    monkeypatch.setattr(scraper.httpx, "Client",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(sources.time, "sleep", lambda s: None)
    scan = scraper.scan_company({"id": "micron", "name": "Micron", "url": "https://micron.wd1.myworkdayjobs.com/External"})
    assert scan.status != "active" and scan.jobs == [], kind


# ── D: account isolation ─────────────────────────────────────────────────────

def test_account1s_shared_tailoring_never_changes_account2s_alerts():
    """Account 2's email follows its own companies and its own tailoring;
    the shared setting isn't even read by the personal notifier."""
    store = UserStore(MemoryBackend(), Identity())
    _person(store, B_UID, B_MAIL, watch_all=False)
    b = store.for_uid(B_UID)
    b.watch("sanofi", {"sanofi", "pwc", "micron"})
    b.set_email_tailoring(["Bengaluru"], [])
    blr = rec(30, "Software Engineer", location="Bengaluru, India", company="sanofi")
    hyd = rec(31, "Software Engineer", company="sanofi")
    mine = rec(32, "Software Engineer", company="micron")                    # Account 1's company
    mail = PersonalMailer()
    user_alerts.run(store, [blr, hyd, mine], mail, run_id="r1")
    assert [keys for to, keys in mail.sent if to == B_MAIL] == [[job_key(blr)]]
    assert not [p for p in store.backend.docs if "/users/" in f"/{p}" and B_UID not in p and "deliveries" in p]


def test_the_shared_tailoring_lives_only_in_the_primary_admins_settings():
    src = open(alerts.__file__, encoding="utf-8").read()
    assert "shared_tailoring(settings)" in src                     # alerts.py: the shared email only
    assert "shared_alert_tailoring" not in open(user_alerts.__file__, encoding="utf-8").read()
