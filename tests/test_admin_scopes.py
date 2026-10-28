"""Two admins, two monitoring scopes (access.py, Oct 2026).

The primary admin (A) keeps the shared scope exactly as before: the
ALERT_RECIPIENT email, the shared dismissals and alert ledger in
seen_jobs.json, the catalogue companies and the scan interval. Another
admin (B) monitors through their own account: the companies they follow,
their own dismissals and delivery ledger, their own verified email. One
shared scanner and one job store serve both; ``company_id`` on each job and
``shared_alerts: false`` on B's catalogue entries keep them apart.

Firestore is user_store.MemoryBackend, Firebase/Google are fakes, mail is
captured: nothing here reaches a real service."""
import json
import time

import pytest

import access
import alerts
import config_store
import notifier
import repo_sync
import user_alerts
from access import ACCOUNT_KEY, OWNER_SESSION_KEY
from config_store import job_key
from user_store import MemoryBackend, UserStore, UserStoreError

from test_alerts import FakeRemote, GATE
from test_alerts import Mailer as SharedMailer
from test_phase3_user_data import (  # noqa: F401  (fixture re-export)
    _company, _sign_in, admins, app, google_env, store,
)
from test_firebase_auth import _buttons, no_env  # noqa: F401  (google_env needs no_env)
from test_streamlit_app import REPO, _companies, _html, _key, _nav, _seen
from test_user_alerts import Identity, Mailer as PersonalMailer, _person

A_UID, B_UID, M_UID = "AdminAaaaaaaaaaaaaaaaaaaaaaa1", "AdminBbbbbbbbbbbbbbbbbbbbbbb2", "Memberccccccccccccccccccccc3"
A_MAIL, B_MAIL, M_MAIL = "anna@example.org", "ben@example.org", "mia@example.org"
SHARED_RECIPIENT = "me@example.com"          # the app fixture's ALERT_RECIPIENT


def rec(cid, n, company=None, **extra):
    return {"title": f"{(company or cid).title()} Graduate {n}", "url": f"https://{cid}.example/job/{n}",
            "company": company or cid.title(), "company_id": cid, "location": "Pune · India",
            "category": "ENTRY_LEVEL", "reason": "entry-level signal", "date": "Oct 20, 10:00",
            "id": f"{cid}_{n}", "first_seen": "2030-01-02T00:00:00+00:00",
            "evidence": {"checks": dict(GATE)}, **extra}


def _account(email, verified=True, provider="password"):
    return {"provider": provider, "email": email, "email_verified": verified, "uid": "x"}


# ---------------------------------------------------------------------------
# who is the primary admin
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("admins_env, primary_env, expected", [
    ("ben@example.org anna@example.org", "Anna@Example.org", "anna@example.org"),   # wherever it is listed
    ("anna@example.org, ben@example.org", "ben@example.org", "ben@example.org"),
    ("anna@example.org, ben@example.org", "typo@example.org", ""),      # named but not an admin: nobody
    ("anna@example.org", "", ""),                                       # not set: no separation
])
def test_primary_admin_email(monkeypatch, admins_env, primary_env, expected):
    monkeypatch.setenv("JT_ADMIN_EMAILS", admins_env)
    monkeypatch.setenv("JT_PRIMARY_ADMIN_EMAIL", primary_env)
    assert access.primary_admin_email() == expected


def test_until_the_primary_admin_is_named_every_admin_keeps_todays_rights(monkeypatch):
    """Deploying the split changes nobody's access, in any list order."""
    monkeypatch.setenv("JT_ADMIN_EMAILS", f"{B_MAIL}, {A_MAIL}")
    monkeypatch.delenv("JT_PRIMARY_ADMIN_EMAIL", raising=False)
    for mail in (A_MAIL, B_MAIL):
        assert access.primary_session_valid({ACCOUNT_KEY: _account(mail)})
    assert not access.primary_session_valid({ACCOUNT_KEY: _account(M_MAIL)})


def test_a_mistyped_primary_admin_locks_the_shared_scope_never_hands_it_over(monkeypatch):
    monkeypatch.setenv("JT_ADMIN_EMAILS", f"{A_MAIL}, {B_MAIL}")
    monkeypatch.setenv("JT_PRIMARY_ADMIN_EMAIL", "anna@exampel.org")
    for mail in (A_MAIL, B_MAIL):
        session = {ACCOUNT_KEY: _account(mail)}
        assert access.owner_session_valid(session) and not access.primary_session_valid(session)
        assert access.monitoring_recipient(session) == mail          # never the shared recipient


def test_only_the_primary_admin_session_owns_the_shared_scope(monkeypatch):
    monkeypatch.setenv("JT_ADMIN_EMAILS", f"{A_MAIL}, {B_MAIL}")
    monkeypatch.setenv("JT_PRIMARY_ADMIN_EMAIL", A_MAIL)
    a, b = {ACCOUNT_KEY: _account(A_MAIL)}, {ACCOUNT_KEY: _account(B_MAIL)}
    assert access.owner_session_valid(a) and access.owner_session_valid(b)        # both are admins
    assert access.primary_session_valid(a) and not access.primary_session_valid(b)
    # an unverified address that merely claims the primary email is nobody
    assert not access.primary_session_valid({ACCOUNT_KEY: _account(A_MAIL, verified=False)})
    assert not access.primary_session_valid({ACCOUNT_KEY: _account(M_MAIL)})      # a member


def test_the_legacy_owner_password_is_the_primary_admin(monkeypatch):
    monkeypatch.delenv("JT_ADMIN_EMAILS", raising=False)
    monkeypatch.setenv("JT_OWNER_PASSWORD", "correct horse battery staple")
    assert access.primary_session_valid({OWNER_SESSION_KEY: time.time() + 60})


# ---------------------------------------------------------------------------
# recipients and the test email
# ---------------------------------------------------------------------------

def test_each_admin_has_their_own_recipient_and_test_email(monkeypatch):
    monkeypatch.setenv("JT_ADMIN_EMAILS", f"{A_MAIL}, {B_MAIL}")
    monkeypatch.setenv("JT_PRIMARY_ADMIN_EMAIL", A_MAIL)
    monkeypatch.setenv("ALERT_RECIPIENT", SHARED_RECIPIENT)
    sent = []
    monkeypatch.setattr(notifier, "test_mail", sent.append)
    a, b = {ACCOUNT_KEY: _account(A_MAIL)}, {ACCOUNT_KEY: _account(B_MAIL)}
    assert access.monitoring_recipient(a) == SHARED_RECIPIENT
    assert access.monitoring_recipient(b) == B_MAIL                           # never the shared one
    assert access.monitoring_recipient({ACCOUNT_KEY: _account(M_MAIL)}) == ""  # members have no admin recipient
    assert access.send_test_email(b) == B_MAIL and access.send_test_email(a) == SHARED_RECIPIENT
    assert sent == [B_MAIL, SHARED_RECIPIENT]
    with pytest.raises(access.OwnerRequired):
        access.send_test_email({ACCOUNT_KEY: _account(M_MAIL)})


# ---------------------------------------------------------------------------
# the scanner's two notification paths
# ---------------------------------------------------------------------------

CATALOGUE = [{"id": "sanofi", "name": "Sanofi"}, {"id": "pwc", "name": "PwC"},
             {"id": "bens", "name": "Bens Co", "shared_alerts": False}]       # added by B


@pytest.fixture
def shared(tmp_path, monkeypatch):
    """alerts.py's data files (as in test_alerts.data), with B's company in the catalogue."""
    monkeypatch.setenv("ALERT_RECIPIENT", SHARED_RECIPIENT)
    monkeypatch.setattr(config_store, "SEEN_FILE", tmp_path / "seen_jobs.json")
    monkeypatch.setattr(config_store, "COMPANIES_FILE", tmp_path / "companies.json")
    monkeypatch.setattr(config_store, "SETTINGS_FILE", tmp_path / "settings.json")
    (tmp_path / "companies.json").write_text(json.dumps(CATALOGUE))
    (tmp_path / "settings.json").write_text("{}")

    def write(seen):
        (tmp_path / "seen_jobs.json").write_text(json.dumps(seen))
    return write


def test_the_shared_scan_alerts_only_the_primary_admins_companies(shared):
    """A's email: every catalogue company as before, never B's."""
    seen = [rec("sanofi", 1), rec("pwc", 2), rec("bens", 3)]
    shared([dict(j) for j in seen])
    remote, mail = FakeRemote(), SharedMailer()
    assert alerts.run(send=mail, publisher=remote) == 0
    assert mail.sent == [[seen[0]["title"], seen[1]["title"]]]
    bens = next(j for j in remote.seen if j["company_id"] == "bens")
    assert "notify_state" not in bens and not bens.get("notified")               # untouched in the shared ledger
    # a later run never sends it either
    assert alerts.run(send=mail, publisher=remote) == 0 and len(mail.sent) == 1


def test_without_a_second_admin_the_shared_alerts_are_unchanged(shared, tmp_path):
    (tmp_path / "companies.json").write_text(json.dumps(CATALOGUE[:2]))    # no flag anywhere: today's data
    seen = [rec("sanofi", 1), rec("pwc", 2), rec("legacy", 3, company="Removed Co")]
    shared([dict(j) for j in seen])
    remote, mail = FakeRemote(), SharedMailer()
    assert alerts.run(send=mail, publisher=remote) == 0
    assert mail.sent == [[j["title"] for j in seen]]


def test_the_personal_scan_alerts_only_the_other_admins_companies():
    """B's email: the companies B follows, to B's own verified address; the
    primary admin's account (alerts off in their profile) gets nothing from it."""
    store = UserStore(MemoryBackend(), Identity())
    _person(store, A_UID, A_MAIL, enabled=False)
    b = store.for_uid(B_UID)
    b.ensure_profile(B_MAIL, "", True)
    b.watch("bens", {"bens", "sanofi"})
    b.set_watch_all(False)
    store.backend.set(f"users/{B_UID}", {"notifications": {"enabled": True, "mode": "general",
                                                           "enabled_at": "2030-01-01T00:00:00Z"}}, merge=True)
    store.identity.accounts[B_UID] = {"uid": B_UID, "email": B_MAIL, "email_verified": True, "disabled": False}
    seen = [rec("sanofi", 1), rec("pwc", 2), rec("bens", 3)]
    mail = PersonalMailer()
    user_alerts.run(store, seen, mail, run_id="r1")
    assert mail.sent == [(B_MAIL, [job_key(seen[2])])]


def test_shared_and_personal_dismissals_never_cross():
    """A's shared dismissal doesn't stop B's email; B's dismissal is only B's."""
    store = UserStore(MemoryBackend(), Identity())
    _person(store, B_UID, B_MAIL, watch_all=False, follow=["sanofi"])
    dismissed_by_a = rec("sanofi", 1, dismissed=True)                 # the shared flag (A's dismissal)
    dismissed_by_b = rec("sanofi", 2)
    store.for_uid(B_UID).dismiss({job_key(dismissed_by_b)})
    mail = PersonalMailer()
    user_alerts.run(store, [dismissed_by_a, dismissed_by_b], mail, run_id="r1")
    assert mail.sent == [(B_MAIL, [job_key(dismissed_by_a)])]
    # ... and the shared alerts never see B's dismissal
    assert config_store.shared_alert_pending(dismissed_by_b) and not config_store.shared_alert_pending(dismissed_by_a)


def test_the_scope_flag_survives_the_scan_and_the_publish_merge():
    """The remote catalogue keeps its configuration; a scan only adds its results."""
    remote = [dict(c) for c in CATALOGUE]
    scanned = [{**{k: v for k, v in c.items() if k != "shared_alerts"}, "status": "active",
                "last_checked": "2030-01-02T00:00:00+00:00"} for c in CATALOGUE]   # a scan of an older copy
    merged = repo_sync.merge_companies(remote, scanned)
    assert [c.get("shared_alerts", True) for c in merged] == [True, True, False]
    assert all(c["status"] == "active" for c in merged)
    assert config_store.excluded_from_shared_alerts(merged) == {"bens"}


def test_followed_by_other_admin():
    store = UserStore(MemoryBackend(), Identity())
    _person(store, A_UID, A_MAIL, follow=["sanofi"])
    _person(store, B_UID, B_MAIL, follow=["pwc"])
    _person(store, M_UID, M_MAIL, follow=["accenture"])                # a member
    admins = {A_MAIL, B_MAIL}
    assert store.followed_by_other_admin("pwc", admins, A_UID) is True
    assert store.followed_by_other_admin("pwc", admins, B_UID) is False         # their own follow doesn't count
    assert store.followed_by_other_admin("accenture", admins, A_UID) is False   # members don't hold companies
    store.backend.set(f"users/{B_UID}", {"email_verified": False}, merge=True)
    assert store.followed_by_other_admin("pwc", admins, A_UID) is False         # an unverified email isn't an admin

    class Broken(MemoryBackend):
        def list(self, collection):
            raise RuntimeError("down")
    with pytest.raises(UserStoreError):
        UserStore(Broken(), Identity()).followed_by_other_admin("pwc", admins, A_UID)


# ---------------------------------------------------------------------------
# the app, with both admins signed in
# ---------------------------------------------------------------------------

@pytest.fixture
def two_admins(admins, monkeypatch):
    admins(f"{B_MAIL}, {A_MAIL}")         # list order doesn't matter
    monkeypatch.setenv("JT_PRIMARY_ADMIN_EMAIL", A_MAIL)


def _wait_for(cond, timeout=5.0):
    deadline = time.time() + timeout
    while not cond() and time.time() < deadline:
        time.sleep(0.05)
    return cond()


def test_both_admins_sign_in_with_their_own_controls(app, google_env, store, two_admins):
    a = _sign_in(app, google_env, A_UID, A_MAIL, seen=[rec("sanofi", 1)])
    b = _sign_in(app, google_env, B_UID, B_MAIL, seen=[rec("sanofi", 1)])
    for at in (a, b):
        assert 'class="role admin"' in _html(at) and "nav_email" in _buttons(at) and "nav_monitoring" in _buttons(at)
    _nav(a, "email")
    assert SHARED_RECIPIENT in _html(a)                                        # A: the shared recipient, in full
    _nav(b, "email")
    html = _html(b)
    assert B_MAIL in html and "Your monitoring" in html
    assert SHARED_RECIPIENT not in html and "m•••@example.com" not in html     # not even masked
    _nav(a, "settings")
    _nav(b, "settings")
    # the shared scan interval is A's to set; B never gets the control
    assert "Global job scan interval" in _html(a) and "Global job scan interval" not in _html(b)


def test_test_emails_go_to_each_admins_own_recipient(app, google_env, store, two_admins, monkeypatch):
    import streamlit as st
    sent = []
    monkeypatch.setattr(notifier, "test_mail", sent.append)
    b = _sign_in(app, google_env, B_UID, B_MAIL, page="email")
    b.button(key="btn_test").click().run()
    assert not b.exception and sent == [B_MAIL]
    st.cache_resource.clear()                              # the app-wide 30 s test-email cooldown
    a = _sign_in(app, google_env, A_UID, A_MAIL, page="email")
    a.button(key="btn_test").click().run()
    assert sent == [B_MAIL, SHARED_RECIPIENT]


def test_a_second_admins_dismissal_never_touches_the_shared_alerts(app, google_env, store, two_admins):
    jobs = [rec("sanofi", 1), rec("pwc", 2)]
    a = _sign_in(app, google_env, A_UID, A_MAIL, seen=jobs)
    b = _sign_in(app, google_env, B_UID, B_MAIL, seen=jobs)
    _nav(b, "jobs")
    b.button(key=_key("dismiss", jobs[0])).click().run()
    assert not b.exception
    assert store.for_uid(B_UID).dismissed() == {job_key(jobs[0])}
    time.sleep(0.3)                                                            # (no background commit is queued)
    assert not any(j.get("dismissed") for j in _seen(b))                      # the shared ledger is untouched
    _nav(a, "jobs")
    assert jobs[0]["title"] in _html(a)                                        # A still sees it
    a.button(key=_key("dismiss", jobs[1])).click().run()
    assert _wait_for(lambda: any(j.get("dismissed") for j in _seen(a)))       # A's goes to the shared alerts, as before
    assert [j["id"] for j in _seen(a) if j.get("dismissed")] == ["pwc_2"]
    assert store.for_uid(B_UID).dismissed() == {job_key(jobs[0])}            # and not to B's list


def test_a_second_admin_adds_a_company_to_their_own_monitoring(app, google_env, store, two_admins):
    before = json.loads((REPO / "data" / "companies.json").read_text("utf-8"))
    b = _sign_in(app, google_env, B_UID, B_MAIL)
    b.session_state["page"], b.session_state["company_view"] = "companies", "add"
    b.run()
    b.text_input(key="new_name").input("Bens Co")
    b.text_input(key="new_url").input("https://careers.bens.example/jobs")
    b.button(key="btn_add").click().run()
    assert not b.exception
    cat = _companies(b)
    new = next(c for c in cat if c["name"] == "Bens Co")
    assert new["shared_alerts"] is False                                      # out of A's email
    assert store.for_uid(B_UID).watchlist() == {new["id"]}                    # in B's monitoring
    assert [c for c in cat if c["name"] != "Bens Co"] == before                # A's companies exactly as they were
    # A adding one keeps today's behaviour: in the shared alerts, nobody's follow changes
    a = _sign_in(app, google_env, A_UID, A_MAIL)
    a.session_state["page"], a.session_state["company_view"] = "companies", "add"
    a.run()
    a.text_input(key="new_name").input("Annas Co")
    a.text_input(key="new_url").input("https://careers.annas.example/jobs")
    a.button(key="btn_add").click().run()
    annas = next(c for c in _companies(a) if c["name"] == "Annas Co")
    assert "shared_alerts" not in annas and store.for_uid(A_UID).watchlist() == set()


def test_a_second_admin_cannot_remove_or_change_the_shared_scope(app, google_env, store, two_admins):
    before = json.loads((REPO / "data" / "companies.json").read_text("utf-8"))
    b = _sign_in(app, google_env, B_UID, B_MAIL)
    _company(b, "sanofi")
    assert "btn_remove_company" not in _buttons(b) and "btn_shared_alerts" not in _buttons(b)
    b.session_state["confirm_remove"] = "sanofi"           # even if the confirmation were forced open
    b.run()
    b.button(key="btn_remove").click().run()
    assert "only the primary admin changes" in _html(b)
    assert _companies(b) == before


def test_the_primary_admin_never_drops_the_other_admins_monitoring(app, google_env, store, two_admins):
    """A's "Stop tracking" of a company B follows takes it out of A's alerts
    only; it stays scanned for B, and B's follow is untouched."""
    b = _sign_in(app, google_env, B_UID, B_MAIL)
    _company(b, "pwc")
    b.button(key="btn_follow").click().run()
    assert store.for_uid(B_UID).watchlist() == {"pwc"}
    a = _sign_in(app, google_env, A_UID, A_MAIL)
    _company(a, "pwc")
    a.button(key="btn_remove_company").click().run()
    a.button(key="btn_remove").click().run()
    assert not a.exception
    pwc = next(c for c in _companies(a) if c["id"] == "pwc")
    assert pwc["shared_alerts"] is False                                     # still tracked, out of A's email
    assert store.for_uid(B_UID).watchlist() == {"pwc"}
    # ... and A can bring it back into the shared alerts
    _company(a, "pwc")
    a.button(key="btn_shared_alerts").click().run()
    assert "shared_alerts" not in next(c for c in _companies(a) if c["id"] == "pwc")
    # a company nobody else follows is removed as before
    _company(a, "avalara")
    a.button(key="btn_remove_company").click().run()
    a.button(key="btn_remove").click().run()
    assert all(c["id"] != "avalara" for c in _companies(a))


def test_the_second_admins_home_follows_their_own_companies(app, google_env, store, two_admins):
    """(Already true before the split — Home's filters start from the
    viewer's own follows — and kept that way.)"""
    jobs = [rec("sanofi", 1), rec("pwc", 2)]
    b = _sign_in(app, google_env, B_UID, B_MAIL, seen=jobs)
    _company(b, "sanofi")
    b.button(key="btn_follow").click().run()
    _nav(b, "home")
    assert jobs[0]["title"] in _html(b) and jobs[1]["title"] not in _html(b)
    a = _sign_in(app, google_env, A_UID, A_MAIL, seen=jobs)
    assert "Discover your next <em>opportunity</em>" in _html(a)              # A's dashboard as before
    assert jobs[0]["title"] in _html(a) and jobs[1]["title"] in _html(a)


# ---------------------------------------------------------------------------
# two authenticated admin UIDs: B's selection is B's, A's stays exactly A's
# (28 Oct 2026: an admin who followed nothing was shown every shared listing
# on Home — an empty follow list meant "any company" — and the Companies nav
# showed the global catalogue to both admins, so both looked identical)
# ---------------------------------------------------------------------------

def _docs_of(store, uid):
    """Every Firestore document of one account (its whole users/{uid} tree)."""
    return {p: d for p, d in store.backend.docs.items() if p == f"users/{uid}" or p.startswith(f"users/{uid}/")}


def _without_activity(docs):
    """... minus the sign-in/activity stamps every visit refreshes."""
    skip = {"last_login_at", "last_active_at"}
    return {p: {k: v for k, v in d.items() if k not in skip} for p, d in docs.items() if "/sessions/" not in p}


def test_a_second_admin_who_follows_nothing_sees_no_shared_listings(app, google_env, store, two_admins):
    jobs = [rec("sanofi", 1), rec("pwc", 2)]
    b = _sign_in(app, google_env, B_UID, B_MAIL, seen=jobs)
    assert store.for_uid(B_UID).watchlist() == set() and not b.exception
    html = _html(b)
    assert jobs[0]["title"] not in html and jobs[1]["title"] not in html         # nothing of A's feed
    assert "you don’t follow any yet" in html
    a = _sign_in(app, google_env, A_UID, A_MAIL, seen=jobs)
    assert jobs[0]["title"] in _html(a) and jobs[1]["title"] in _html(a)          # A's Home exactly as before


def test_each_admins_companies_page_is_their_own(app, google_env, store, two_admins):
    b = _sign_in(app, google_env, B_UID, B_MAIL)
    _company(b, "sanofi")
    b.button(key="btn_follow").click().run()
    _nav(b, "companies")
    html = _html(b)
    assert "My companies" in html and "Following 1" in html                       # B's selection, not the catalogue
    assert "btn_cos_all" in _buttons(b)                                           # the catalogue is one click away
    b.button(key="btn_cos_all").click().run()
    assert not b.exception and "career portal" in _html(b) and "Add company" in {x.label for x in b.button}
    a = _sign_in(app, google_env, A_UID, A_MAIL)
    _nav(a, "companies")
    assert "Following 1" not in _html(a) and "Add company" in {x.label for x in a.button}   # A: as before


def test_the_job_page_never_shows_the_primary_admins_alert_history_to_another_admin(app, google_env, store, two_admins):
    """"Email alert: Sent" is the shared email's ledger — A's history, not B's."""
    sent = rec("sanofi", 1, notified=True, notify_state="sent")
    a = _sign_in(app, google_env, A_UID, A_MAIL, seen=[sent])
    b = _sign_in(app, google_env, B_UID, B_MAIL, seen=[sent])
    for at, shown in ((a, True), (b, False)):
        at.session_state["page"], at.session_state["job_id"] = "jobs", _jid(sent)
        at.run()
        assert not at.exception and sent["title"] in _html(at)
        assert ("<dt>Email alert</dt><dd>Sent</dd>" in _html(at)) is shown


def _jid(job):
    import hashlib
    return hashlib.sha1(job_key(job).encode("utf-8")).hexdigest()[:12]


def test_changing_the_second_admins_companies_never_changes_the_first(app, google_env, store, two_admins, tmp_path,
                                                                     monkeypatch):
    """Two UIDs side by side: A follows Sanofi, B follows and unfollows PwC
    and adds a company. A's whole Firestore tree, the shared catalogue's
    existing entries and A's alerts (shared and personal) are identical
    before and after."""
    jobs = [rec("sanofi", 1), rec("pwc", 2)]
    a = _sign_in(app, google_env, A_UID, A_MAIL, seen=jobs)
    _company(a, "sanofi")
    a.button(key="btn_follow").click().run()
    store.backend.set(f"users/{A_UID}", {"notifications": {"enabled": True, "mode": "general",
                                                           "enabled_at": "2030-01-01T00:00:00Z"}}, merge=True)
    a_before = _without_activity(_docs_of(store, A_UID))
    catalogue_before = _companies(a)

    b = _sign_in(app, google_env, B_UID, B_MAIL, seen=jobs)
    _company(b, "pwc")
    b.button(key="btn_follow").click().run()                                    # follow ...
    _company(b, "accenture")
    b.button(key="btn_follow").click().run()
    _company(b, "accenture")
    b.button(key="btn_follow").click().run()                                    # ... and unfollow
    b.session_state["page"], b.session_state["company_view"], b.session_state["company_id"] = "companies", "add", None
    b.run()
    b.text_input(key="new_name").input("Bens Co")
    b.text_input(key="new_url").input("https://careers.bens.example/jobs")
    b.button(key="btn_add").click().run()
    assert not b.exception
    bens = next(c["id"] for c in _companies(b) if c["name"] == "Bens Co")

    # storage: each selection under its own UID, A's tree untouched
    assert store.for_uid(B_UID).watchlist() == {"pwc", bens}
    assert store.for_uid(A_UID).watchlist() == {"sanofi"}
    assert _without_activity(_docs_of(store, A_UID)) == a_before
    assert [c for c in _companies(b) if c["id"] != bens] == catalogue_before

    # alerts: the shared email (A's) and each personal email follow their own scope
    seen = jobs + [rec(bens, 3, company="Bens Co")]
    monkeypatch.setattr(config_store, "SEEN_FILE", tmp_path / "s.json")
    monkeypatch.setattr(config_store, "COMPANIES_FILE", tmp_path / "c.json")
    monkeypatch.setattr(config_store, "SETTINGS_FILE", tmp_path / "st.json")
    (tmp_path / "c.json").write_text(json.dumps(_companies(b)))
    (tmp_path / "st.json").write_text("{}")
    (tmp_path / "s.json").write_text(json.dumps(seen))
    shared_mail = SharedMailer()
    assert alerts.run(send=shared_mail, publisher=FakeRemote()) == 0
    assert shared_mail.sent == [[jobs[0]["title"], jobs[1]["title"]]]           # never B's company

    class Ids(Identity):
        pass
    ids = Ids()
    for uid, mail in ((A_UID, A_MAIL), (B_UID, B_MAIL)):
        ids.accounts[uid] = {"uid": uid, "email": mail, "email_verified": True, "disabled": False}
    store.backend.set(f"users/{B_UID}", {"notifications": {"enabled": True, "mode": "general",
                                                           "enabled_at": "2030-01-01T00:00:00Z"}}, merge=True)
    personal = PersonalMailer()
    user_alerts.run(UserStore(store.backend, ids), seen, personal, run_id="r1")
    assert sorted(personal.sent) == sorted([(A_MAIL, [job_key(seen[0])]),
                                            (B_MAIL, sorted([job_key(seen[1]), job_key(seen[2])]))])
