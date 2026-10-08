"""Optional email tailoring ("Tailor my email alerts"): a personal narrowing
of the alert EMAILS only, stored in users/{uid}.notifications.tailoring.

The rules under test:
  * nothing chosen = every qualifying job from the person's companies
    (exactly the alerts from before tailoring existed)
  * any chosen location AND any chosen job family (OR within a dimension)
  * after the shared eligibility gate, the person's companies, dismissals
    and alerts on/off — never instead of them
  * one person's tailoring never reaches anyone else, and the job lists,
    the shared data and the browsing filters are untouched
Plus the account header (first name, ADMIN for admins only) and the cost of
clicks. In-memory store; nothing is emailed and Google/Firebase are never
contacted."""
import json

import pytest

import job_filters
import user_alerts
import user_store
from config_store import job_key
from user_store import InvalidInput, MemoryBackend, UserStore
from test_account_onboarding import CountingBackend, JOBS, PWC_TITLE, METLIFE_TITLE, _acct_keys, _pills, _ready
from test_firebase_auth import _buttons, admins, google_env, no_env  # noqa: F401  (fixtures)
from test_phase3_user_data import (  # noqa: F401
    A_MAIL, A_UID, B_MAIL, B_UID, AccountIdentity, _sign_in, _use_store,
)
from test_streamlit_app import REPO, _html, _nav, app  # noqa: F401
from test_user_alerts import A, B, Identity, Mailer, _person, job


def _tailor(store, uid, locations=(), job_families=()):
    return store.for_uid(uid).set_email_tailoring(list(locations), list(job_families))


def _sent(mail, to="alice@example.org"):
    return next((keys for addr, keys in mail.sent if addr == to), [])


# a small catalogue of qualifying jobs (all pass the shared gate)
HYD_SW = job(1, title="Software Engineer", location="Hyderabad · India")
BLR_SW = job(2, title="Graduate Software Developer", location="Bangalore · India")
PUNE_SW = job(3, title="Software Engineer", location="Pune · India")
HYD_DATA = job(4, title="Data Analyst", location="Hyderabad · India")
BLR_DATA = job(5, title="Associate - Data Analyst", location="Bengaluru")
HYD_HR = job(6, title="HR Recruiter", location="Hyderabad")
CATALOGUE = [HYD_SW, BLR_SW, PUNE_SW, HYD_DATA, BLR_DATA, HYD_HR]


@pytest.fixture
def store():
    return UserStore(MemoryBackend(), Identity())


# ── A–F: the matching rules ──────────────────────────────────────────────────

def test_a_no_tailoring_emails_every_qualifying_job(store):
    _person(store, A, "alice@example.org")
    _tailor(store, A)
    mail = Mailer()
    user_alerts.run(store, CATALOGUE, mail)
    assert _sent(mail) == sorted(job_key(j) for j in CATALOGUE)


def test_b_location_only(store):
    _person(store, A, "alice@example.org")
    _tailor(store, A, locations=["Hyderabad"])
    mail = Mailer()
    user_alerts.run(store, CATALOGUE, mail)
    assert _sent(mail) == sorted(job_key(j) for j in (HYD_SW, HYD_DATA, HYD_HR))


def test_c_job_family_only(store):
    _person(store, A, "alice@example.org")
    _tailor(store, A, job_families=["Software engineering"])
    mail = Mailer()
    user_alerts.run(store, CATALOGUE, mail)
    assert _sent(mail) == sorted(job_key(j) for j in (HYD_SW, BLR_SW, PUNE_SW))


def test_d_location_and_family_must_both_match(store):
    _person(store, A, "alice@example.org")
    _tailor(store, A, locations=["Hyderabad"], job_families=["Software engineering"])
    mail = Mailer()
    user_alerts.run(store, CATALOGUE, mail)
    assert _sent(mail) == [job_key(HYD_SW)]


def test_e_several_locations_are_or(store):
    _person(store, A, "alice@example.org")
    _tailor(store, A, locations=["Hyderabad", "Bengaluru"], job_families=["Software engineering"])
    mail = Mailer()
    user_alerts.run(store, CATALOGUE, mail)
    assert _sent(mail) == sorted(job_key(j) for j in (HYD_SW, BLR_SW))


def test_f_several_families_are_or(store):
    _person(store, A, "alice@example.org")
    _tailor(store, A, locations=["Bengaluru"], job_families=["Software engineering", "Data & analytics"])
    mail = Mailer()
    user_alerts.run(store, CATALOGUE, mail)
    assert _sent(mail) == sorted(job_key(j) for j in (BLR_SW, BLR_DATA))


@pytest.mark.parametrize("tailoring, expected", [
    (None, True), ({}, True), ({"locations": [], "job_families": []}, True),
    ({"locations": ["Hyderabad"]}, True), ({"locations": ["Pune"]}, False),
    ({"job_families": ["Cybersecurity"]}, False),
    ({"locations": ["Pune", "Hyderabad"], "job_families": ["Data & analytics", "Software engineering"]}, True),
    ({"locations": ["Hyderabad"], "job_families": ["HR & recruiting"]}, False),
])
def test_matcher_truth_table(tailoring, expected):
    assert job_filters.matches_tailoring(HYD_SW, tailoring) is expected


def test_the_same_vocabulary_and_matchers_as_everywhere_else():
    """No second vocabulary or matcher: tailoring uses job_filters'
    LOCATIONS / JOB_FAMILIES and the same job_location_choices /
    job_families as the job lists."""
    titles = ["Software Engineer", "DevOps Engineer", "Cloud Security Analyst", "Social Security Associate",
              "Data Analyst", "QA Automation Tester", "HR Recruiter", "Process Associate", "Trainee"]
    for title in titles:
        j = {"title": title, "location": "Hyderabad"}
        for fam in job_filters.JOB_FAMILIES:
            assert job_filters.matches_tailoring(j, {"job_families": [fam]}) is (fam in job_filters.job_families(j))
    for loc in ("Bangalore · India", "Gurugram", "Navi Mumbai", "Remote - India", "India"):
        j = {"title": "x", "location": loc}
        for choice in job_filters.LOCATIONS:
            assert job_filters.matches_tailoring(j, {"locations": [choice]}) is (choice in job_filters.job_location_choices(j))
    assert {"Cloud & DevOps", "Cybersecurity"} <= set(job_filters.JOB_FAMILIES)


# ── G: backward compatibility ────────────────────────────────────────────────

def test_g_a_profile_from_before_tailoring_behaves_exactly_as_before(store):
    """An existing subscriber's profile has no tailoring field at all."""
    _person(store, A, "alice@example.org")
    prof = store.backend.get(f"users/{A}")
    assert "tailoring" not in prof["notifications"]
    assert user_store.tailoring_from(prof) == {"locations": [], "job_families": []}
    mail = Mailer()
    user_alerts.run(store, CATALOGUE, mail)
    assert _sent(mail) == sorted(job_key(j) for j in CATALOGUE)


def test_g_an_older_tailored_mode_keeps_working_and_tailoring_narrows_it_further(store):
    _person(store, A, "alice@example.org", mode="tailored",
            prefs={"job_families": ["Software engineering"], "locations": [], "experience": []})
    mail = Mailer()
    user_alerts.run(store, CATALOGUE, mail, run_id="r1")
    assert _sent(mail) == sorted(job_key(j) for j in (HYD_SW, BLR_SW, PUNE_SW))
    _tailor(store, A, locations=["Pune"])
    more = [job(10, title="Software Engineer", location="Pune"), job(11, title="Software Engineer", location="Chennai")]
    mail = Mailer()
    user_alerts.run(store, more, mail, run_id="r2")
    assert _sent(mail) == [job_key(more[0])]


def test_g_saving_tailoring_keeps_alerts_on_and_saving_alerts_keeps_tailoring(store):
    u = _person(store, A, "alice@example.org")
    before = store.backend.get(f"users/{A}")["notifications"]
    _tailor(store, A, locations=["Pune"])
    after = store.backend.get(f"users/{A}")["notifications"]
    assert {k: after[k] for k in ("enabled", "mode", "enabled_at")} == {k: before[k] for k in ("enabled", "mode", "enabled_at")}
    u.set_notifications(True, "general")
    u.set_notifications(False, "general")
    assert store.backend.get(f"users/{A}")["notifications"]["tailoring"] == {"locations": ["Pune"], "job_families": []}


def test_a_new_profile_starts_with_no_tailoring_and_alerts_off(store):
    prof, created = store.for_uid(A).ensure_profile("alice@example.org", "Alice", True)
    assert created and prof["notifications"]["enabled"] is False and prof["watch_all"] is False
    assert user_store.tailoring_from(prof) == {"locations": [], "job_families": []}


def test_tailoring_only_accepts_the_shared_vocabulary(store):
    u = store.for_uid(A)
    u.ensure_profile("alice@example.org", "", True)
    for bad in ({"locations": ["Atlantis"]}, {"job_families": ["Security"]}, {"locations": "Hyderabad"},
                {"locations": ["x" * 200]}, {"job_families": [None]}):
        with pytest.raises(InvalidInput):
            u.set_email_tailoring(**bad)
    assert u.set_email_tailoring([" Pune ", "Pune"], ["Cloud & DevOps", "Cybersecurity"]) == {
        "locations": ["Pune"], "job_families": ["Cloud & DevOps", "Cybersecurity"]}


# ── H, I, J, K, L: the rest of the pipeline still decides first ──────────────

def test_h_one_persons_tailoring_never_affects_another(store):
    _person(store, A, "alice@example.org")
    _person(store, B, "bob@example.org")
    _tailor(store, A, locations=["Hyderabad"], job_families=["Software engineering"])
    mail = Mailer()
    user_alerts.run(store, CATALOGUE, mail)
    assert _sent(mail, "alice@example.org") == [job_key(HYD_SW)]
    assert _sent(mail, "bob@example.org") == sorted(job_key(j) for j in CATALOGUE)
    assert user_store.tailoring_from(store.backend.get(f"users/{B}")) == {"locations": [], "job_families": []}
    assert [p for p in store.backend.docs if "tailoring" in json.dumps(store.backend.docs[p])] == [f"users/{A}"]


def test_i_jobs_outside_the_persons_companies_are_never_emailed(store):
    _person(store, A, "alice@example.org", watch_all=False, follow=["pwc"])
    _tailor(store, A)                                            # no tailoring: still only PwC
    seen = [job(1, "pwc", location="Hyderabad"), job(2, "sanofi", location="Hyderabad"), job(3, "accenture")]
    mail = Mailer()
    user_alerts.run(store, seen, mail, run_id="r1")
    assert _sent(mail) == [job_key(seen[0])]
    _tailor(store, A, locations=["Hyderabad"])                   # tailoring can't widen the companies
    more = [job(4, "sanofi", location="Hyderabad"), job(5, "pwc", location="Hyderabad")]
    mail = Mailer()
    user_alerts.run(store, more, mail, run_id="r2")
    assert _sent(mail) == [job_key(more[1])]


def test_i_following_nothing_means_nothing_is_emailed_even_with_tailoring(store):
    _person(store, A, "alice@example.org", watch_all=False)
    _tailor(store, A, locations=["Hyderabad"])
    mail = Mailer()
    user_alerts.run(store, CATALOGUE, mail)
    assert mail.sent == []


def test_j_tailoring_never_lets_an_ineligible_job_through(store):
    _person(store, A, "alice@example.org")
    _tailor(store, A, locations=["Hyderabad"], job_families=["Software engineering"])
    bad = [job(1, title="Software Engineer", location="Hyderabad", category="EXPERIENCED"),
           job(2, title="Software Engineer", location="Hyderabad", evidence={"checks": {"india": True, "experience": False}}),
           job(3, title="Software Engineer", location="Hyderabad", evidence={}),
           job(4, title="Software Engineer", location="Hyderabad", category=None)]
    mail = Mailer()
    user_alerts.run(store, bad, mail)
    assert mail.sent == []


def test_k_dismissed_jobs_stay_out(store):
    u = _person(store, A, "alice@example.org")
    _tailor(store, A, locations=["Hyderabad"])
    u.dismiss({job_key(HYD_DATA)})
    mail = Mailer()
    user_alerts.run(store, CATALOGUE, mail)
    assert _sent(mail) == sorted(job_key(j) for j in (HYD_SW, HYD_HR))


def test_l_alerts_off_still_means_nothing(store):
    _person(store, A, "alice@example.org", enabled=False)
    _tailor(store, A, locations=["Hyderabad"])
    mail = Mailer()
    assert user_alerts.run(store, CATALOGUE, mail)["off"] == 1 and mail.sent == []


def test_tailored_jobs_still_go_through_claim_send_finalize(store):
    _person(store, A, "alice@example.org")
    _tailor(store, A, locations=["Hyderabad"])
    fail = Mailer(fail_for={"alice@example.org"})
    assert user_alerts.run(store, CATALOGUE, fail, run_id="r1")["failed"] == 1
    assert store.for_uid(A).delivery(job_key(HYD_SW))["state"] == "pending"        # released for a retry
    assert store.for_uid(A).delivery(job_key(PUNE_SW)) is None                    # never claimed
    mail = Mailer()
    user_alerts.run(store, CATALOGUE, mail, run_id="r2")
    assert _sent(mail) == sorted(job_key(j) for j in (HYD_SW, HYD_DATA, HYD_HR))
    mail = Mailer()
    user_alerts.run(store, CATALOGUE, mail, run_id="r3")
    assert mail.sent == []                                                       # exactly once


def test_cybersecurity_tailoring_keeps_the_social_security_fix(store):
    _person(store, A, "alice@example.org")
    _tailor(store, A, job_families=["Cybersecurity"])
    seen = [job(1, title="Social Security Associate"), job(2, title="Security Guard"),
            job(3, title="SOC Analyst"), job(4, title="Cyber Security Engineer - Fresher")]
    mail = Mailer()
    user_alerts.run(store, seen, mail)
    assert _sent(mail) == sorted(job_key(j) for j in seen[2:])


# ── the app: the card, storage per UID, isolation from browsing ─────────────

@pytest.fixture
def ustore(monkeypatch):
    s = UserStore(CountingBackend(), AccountIdentity())
    _use_store(monkeypatch, s)
    return s


def _tl_keys(at):
    return {f: next(k for k in at.session_state if str(k).startswith(f"tl_{f}_")) for f in ("locations", "job_families")}


def _choose(at, locations=(), families=()):
    keys = _tl_keys(at)
    _pills(at, keys["locations"]).set_value(list(locations))
    _pills(at, keys["job_families"]).set_value(list(families))
    at.run()


def test_the_card_saves_to_the_signed_in_uid_only(app, google_env, ustore):
    _ready(ustore, A_UID, A_MAIL)
    _ready(ustore, B_UID, B_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS, page="alerts")
    html = _html(at)
    assert "Tailor my email alerts" in html and "Optional" in html
    assert "Leave everything unselected to receive all qualifying jobs from your monitored companies." in html
    assert "All qualifying jobs from your monitored companies." in html                 # the no-filter state
    assert at.button(key="btn_tl_save").disabled                                          # nothing to save yet
    _choose(at, ["Hyderabad", "Bengaluru"], ["Software engineering"])
    assert "Unsaved changes" in _html(at)
    at.button(key="btn_tl_save").click().run()
    assert ustore.for_uid(A_UID).personal_view()["tailoring"] == {
        "locations": ["Hyderabad", "Bengaluru"], "job_families": ["Software engineering"]}
    assert "Your email alerts are tailored to your preferences." in _html(at)
    assert user_store.tailoring_from(ustore.backend.get(f"users/{B_UID}")) == {"locations": [], "job_families": []}
    # job preferences (the job lists' defaults) are a different thing and stay as they were
    assert ustore.backend.get(f"users/{A_UID}")["preferences"]["locations"] == []
    at.button(key="btn_tl_clear").click().run()
    assert ustore.for_uid(A_UID).personal_view()["tailoring"] == {"locations": [], "job_families": []}
    assert "All qualifying jobs from your monitored companies." in _html(at)


def test_h_another_person_in_the_same_browser_never_sees_the_first_ones_picks(app, google_env, ustore):
    _ready(ustore, A_UID, A_MAIL)
    _ready(ustore, B_UID, B_MAIL)
    _tailor(ustore, A_UID, ["Pune"], ["Design"])
    a = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS, page="alerts")
    assert "Pune — Design" in _html(a)
    b = _sign_in(app, google_env, B_UID, B_MAIL, seen=JOBS, page="alerts")
    assert "Pune — Design" not in _html(b) and "All qualifying jobs from your monitored companies." in _html(b)
    assert all(not b.session_state[k] for k in _tl_keys(b).values())


def test_a_tailoring_save_is_one_read_and_one_write(app, google_env, ustore):
    _ready(ustore, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS, page="alerts")
    _choose(at, ["Pune"])
    ustore.backend.reads = 0
    at.button(key="btn_tl_save").click().run()
    at.button(key="nav_jobs").click().run()
    assert ustore.backend.reads == 1            # the save reads the profile once; no full re-read afterwards


def test_browsing_filters_never_change_email_tailoring(app, google_env, ustore):
    _ready(ustore, A_UID, A_MAIL, locations=["Pune"])
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    _nav(at, "jobs")
    html = _html(at)
    assert PWC_TITLE in html and METLIFE_TITLE not in html                    # browsing starts from preferences
    for ms in at.multiselect:
        if "loc" in (ms.key or ""):
            ms.set_value(["Hyderabad"]).run()
            break
    assert ustore.for_uid(A_UID).personal_view()["tailoring"] == {"locations": [], "job_families": []}
    assert ustore.backend.get(f"users/{A_UID}")["preferences"]["locations"] == ["Pune"]


def test_the_admin_monitoring_page_has_the_card_and_members_never_reach_it(app, google_env, ustore, admins):
    admins(A_MAIL)
    _ready(ustore, A_UID, A_MAIL)
    _ready(ustore, B_UID, B_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS, page="monitoring")
    html = _html(at)
    assert "Tailor my email alerts" in html and "shared alert email" in html
    m = _sign_in(app, google_env, B_UID, B_MAIL, seen=JOBS)
    assert "nav_monitoring" not in _buttons(m)
    m.session_state["page"] = "monitoring"
    m.run()
    assert m.session_state.page == "home" and "shared alert email" not in _html(m)


def test_my_companies_shows_the_card_once_companies_are_chosen(app, google_env, ustore):
    u = _ready(ustore, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS, page="companies")
    assert "Tailor my email alerts" not in _html(at)
    u.watch("pwc", {"pwc"})
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS, page="companies")
    assert "Tailor my email alerts" in _html(at) and "the 1 company you follow" in _html(at)


# ── M, N, O: the account header and menu ─────────────────────────────────────

def _menu_label(at):
    return next(e for e in at.get("popover") if e.proto.popover.label).proto.popover.label


@pytest.mark.parametrize("admin", [True, False])
def test_m_n_the_header_shows_the_first_name_and_admin_only_for_admins(app, google_env, admins, admin):
    from test_firebase_auth import EMAIL, GATED, FakeUser, NEW_RECORD, _fresh_google_token, ok_google_answer
    admins(EMAIL if admin else "someone@else.example")
    google_env["firebase"](ok_google_answer())
    google_env["set_user"](FakeUser(token=_fresh_google_token(), name="Manoj Anumolu"))
    at = app([NEW_RECORD], secrets=GATED, owner=False)
    label = next(p.proto.popover.label for p in at.get("popover"))
    assert label == ("Manoj **ADMIN**" if admin else "Manoj")                 # first name only in the header
    html = _html(at)
    assert "Manoj Anumolu" in html and EMAIL.lower() in html                   # full name + email inside the menu
    assert ('class="role admin"' in html) is admin


@pytest.mark.parametrize("account, expected", [
    ({"name": "Manoj Anumolu", "email": "x@example.org"}, "Manoj"),
    ({"name": "  Priya  ", "email": "x@example.org"}, "Priya"),
    ({"name": "", "email": "anvesh.anumolu138@example.org"}, "Anvesh"),
    ({"name": "", "email": "42@example.org"}, "Account"),
    ({}, "Account"),
])
def test_m_first_name(account, expected):
    assert _first_name()(account) == expected


def test_o_menu_items(app, google_env, ustore):
    _ready(ustore, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    labels = {b.key: b.label for b in at.button if (b.key or "").startswith("acct_") or b.key == "btn_account_sign_out"}
    assert labels == {"acct_settings": "Account", "acct_preferences": "Job preferences", "acct_companies": "My companies",
                      "acct_alerts": "My alerts", "btn_account_sign_out": "Sign out"}
    assert _acct_keys(at) == {"acct_settings", "acct_preferences", "acct_companies", "acct_alerts"}


# ── P: clicks don't refetch shared data or re-read personal data ─────────────

def test_p_navigation_and_menu_clicks_fetch_nothing(app, google_env, ustore, admins, monkeypatch):
    import config_store
    calls = []
    catalogue = json.loads((REPO / "companies.json").read_text("utf-8"))

    def fake_fetch(paths, *a, **k):
        calls.append(list(paths))
        return {"companies.json": catalogue, "settings.json": {"recipient_email": "me@example.com"},
                "seen_jobs.json": list(JOBS)}
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(config_store, "fetch_remote_json", fake_fetch)
    admins(A_MAIL)
    _ready(ustore, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    assert len(calls) == 1
    ustore.backend.reads = 0
    lookups = ustore.identity.calls[:]
    for key in ("nav_home", "nav_jobs", "nav_companies", "nav_monitoring", "nav_email", "nav_settings",
                "acct_settings", "acct_preferences", "acct_companies", "acct_alerts", "nav_home"):
        at.button(key=key).click().run()
        assert not at.exception
    assert len(calls) == 1 and ustore.backend.reads == 0 and ustore.identity.calls == lookups


def test_p_a_stale_personal_copy_is_refreshed_without_making_the_click_wait(app, google_env, ustore):
    """After a minute the click is answered from the current copy and a
    background read fetches the next one (adopted on a later click)."""
    import time as _time
    _ready(ustore, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS, page="alerts")
    ustore.for_uid(A_UID).set_email_tailoring(["Kochi"], [])          # changed on another device
    at.session_state["_pcache"] = {**at.session_state["_pcache"], "at": _time.time() - 120}
    at.button(key="nav_alerts").click().run()
    assert "Kochi" not in _html(at)                                   # this click didn't wait for Firestore
    holder = at.session_state["_pcache_next"]
    for _ in range(200):
        if holder.get("done"):
            break
        _time.sleep(0.01)
    at.button(key="nav_alerts").click().run()
    assert "Kochi" in _html(at)                                       # ... the next one shows the fresh copy


def _first_name():
    """streamlit_app.first_name, without running the app module."""
    import ast
    src = (REPO / "streamlit_app.py").read_text("utf-8")
    fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "first_name")
    ns = {"re": __import__("re")}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "streamlit_app.py", "exec"), ns)
    return ns["first_name"]


def test_choosing_a_menu_item_closes_the_menu(app, google_env, ustore):
    """The popover gets a new key after an item is chosen, so the page it
    leads to is drawn with the menu closed (no extra round trip)."""
    _ready(ustore, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    keys = []
    for dest in ("settings", "preferences", "companies", "alerts"):
        at.button(key=f"acct_{dest}").click().run()
        assert not at.exception and at.session_state.page == dest
        keys.append(at.session_state["_acct_menu_n"])
    assert keys == [1, 2, 3, 4]
    _nav(at, "jobs")                                                       # the sidebar doesn't touch the menu
    assert at.session_state["_acct_menu_n"] == 4
