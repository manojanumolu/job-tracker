"""Account menu, first-sign-in onboarding, personal preferences, followed
companies and the member/admin split. Firestore is user_store.MemoryBackend;
Google and Firebase are never contacted and no email is sent."""
import json
import re

import pytest

import user_store
from user_store import MemoryBackend, UserStore
from test_firebase_auth import _buttons, _login_page, admins, google_env, no_env  # noqa: F401  (fixtures)
from test_phase3_user_data import (  # noqa: F401
    A_MAIL, A_UID, B_MAIL, B_UID, METLIFE_JOB, PWC_JOB, AccountIdentity, _profiles, _sign_in, _use_store,
)
from test_streamlit_app import REPO, _html, _nav, app  # noqa: F401

JOBS = (PWC_JOB, METLIFE_JOB)          # Software · Pune · entry level / Data · Hyderabad · fresher
PWC_TITLE, METLIFE_TITLE = PWC_JOB["title"], METLIFE_JOB["title"]


class CountingBackend(MemoryBackend):
    def __init__(self):
        super().__init__()
        self.reads = 0

    def get(self, path):
        self.reads += 1
        return super().get(path)

    def list(self, collection):
        self.reads += 1
        return super().list(collection)


@pytest.fixture
def store(monkeypatch):
    s = UserStore(CountingBackend(), AccountIdentity())
    _use_store(monkeypatch, s)
    return s


def _ready(store, uid, email, **prefs):
    """A returning person: onboarding done, optional saved preferences."""
    u = store.for_uid(uid)
    u.ensure_profile(email, "", True)
    u.complete_onboarding()
    if prefs:
        u.set_preferences("general", prefs.get("job_families", []), prefs.get("locations", []),
                          prefs.get("experience", []))
    return u


def _pills(at, key):
    return next(b for b in at.button_group if b.key == key)


def _ms(at, key):
    return next(m for m in at.multiselect if m.key == key)


def _acct_keys(at):
    return {b.key for b in at.button if (b.key or "").startswith("acct_")}


# ── a new member ─────────────────────────────────────────────────────────────

def test_a_new_member_starts_with_no_companies_alerts_off_and_onboarding(app, google_env, store):
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS, onboard=True)
    assert at.session_state.page == "onboarding" and "Where do you want to work?" in _html(at)
    profile = store.backend.get(f"users/{A_UID}")
    assert profile["watch_all"] is False and profile["onboarding"]["status"] == "pending"
    assert store.for_uid(A_UID).watchlist() == set()
    assert user_store.notification_settings_from(profile)["enabled"] is False
    for key in ("nav_jobs", "nav_settings"):                 # nothing else until onboarding is done or skipped
        at.button(key=key).click().run()
        assert at.session_state.page == "onboarding"


def test_onboarding_saves_every_answer_and_persists(app, google_env, store):
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS, onboard=True)
    answers = [("locations", ["Hyderabad"]), ("job_types", ["internship"]), ("job_families", ["Data & analytics"]),
               ("experience", ["fresher"]), ("work_modes", ["remote"])]
    for field, value in answers:
        _pills(at, f"ob_{field}").set_value(value).run()
        at.button(key="btn_ob_next").click().run()
        assert not at.exception
    html = _html(at)
    assert "You’re ready" in html and "Hyderabad" in html and "Internship" in html and "Remote" in html
    at.button(key="btn_ob_back").click().run()                       # back keeps the answer
    assert _pills(at, "ob_work_modes").value == ["remote"]
    at.button(key="btn_ob_next").click().run()
    at.button(key="btn_ob_finish").click().run()
    assert at.session_state.page == "home" and not at.exception
    prof = store.backend.get(f"users/{A_UID}")
    assert prof["onboarding"]["status"] == "complete"
    assert prof["preferences"] == {"mode": "general", "job_families": ["Data & analytics"], "locations": ["Hyderabad"],
                                   "experience": ["fresher"], "job_types": ["internship"], "work_modes": ["remote"]}
    assert user_store.notification_settings_from(prof)["enabled"] is False          # still off
    html = _html(at)                                                  # member home: their matches only
    assert METLIFE_TITLE in html and PWC_TITLE not in html
    again = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)       # a later visit: no onboarding, same answers
    assert again.session_state.page == "home" and "Where do you want to work?" not in _html(again)
    _nav(again, "settings")
    assert "Data &amp; analytics" in _html(again) and "Hyderabad" in _html(again)


def test_skipping_onboarding_saves_nothing_but_finishes_it(app, google_env, store):
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS, onboard=True)
    _pills(at, "ob_locations").set_value(["Pune"]).run()
    at.button(key="btn_ob_next").click().run()
    at.button(key="btn_onboarding_skip").click().run()
    prof = store.backend.get(f"users/{A_UID}")
    assert prof["onboarding"]["status"] == "complete" and prof["preferences"]["locations"] == []
    html = _html(at)
    assert at.session_state.page == "home" and PWC_TITLE in html and METLIFE_TITLE in html


# ── account menu and roles ────────────────────────────────────────────────────

def test_member_account_menu_has_only_personal_items(app, google_env, store):
    _ready(store, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    (menu,) = at.get("popover")
    assert "ADMIN" not in menu.proto.popover.label and 'class="role admin"' not in _html(at)
    assert _acct_keys(at) == {"acct_settings", "acct_preferences", "acct_companies", "acct_alerts"}
    assert "btn_account_sign_out" in _buttons(at)
    for admin_only in ("nav_monitoring", "nav_email", "mob_monitoring", "mob_email", "btn_add", "btn_run_check"):
        assert admin_only not in _buttons(at)
    assert 'class="acct-head"' in _html(at) and A_MAIL in _html(at)


def test_admin_account_menu_shows_the_badge_and_no_admin_tools(app, google_env, store, admins):
    admins(A_MAIL)
    _ready(store, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    labels = [p.proto.popover.label for p in at.get("popover")]
    assert any(label.endswith("**ADMIN**") for label in labels)
    assert 'class="role admin"' in _html(at)
    assert _acct_keys(at) == {"acct_settings", "acct_preferences", "acct_companies", "acct_alerts"}
    for admin_page in ("nav_monitoring", "nav_email"):                # admin tools stay in the sidebar
        assert admin_page in _buttons(at)


def test_an_account_name_cannot_inject_markdown_into_the_menu(app, google_env, store):
    _ready(store, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    label = next(p.proto.popover.label for p in at.get("popover"))
    assert "**ADMIN**" not in label and not re.search(r"(?<!\\)[*_`\[\]]", label)


@pytest.mark.parametrize("query", [{"page": "monitoring"}, {"page": "email"}, {"page": "companies", "view": "add"}])
def test_a_member_cannot_open_admin_pages_by_address(app, google_env, store, query):
    _ready(store, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS, query=query)
    html = _html(at)
    assert at.session_state.page == "home" and not at.exception
    for marker in ("btn_run_check", "btn_test_email", "add_name", "btn_add_company"):
        assert marker not in _buttons(at) and marker not in {t.key for t in at.text_input}
    assert "here are your <em>matches</em>" in html                     # the member's own Home instead


def test_member_home_shows_no_scanner_internals(app, google_env, store):
    _ready(store, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    html = _html(at)
    assert "here are your <em>matches</em>" in html
    for internal in ("monitored", "Last scan", "Next scheduled", "Portal health", '<div class="side-foot'):
        assert internal not in html
    _nav(at, "settings")
    html = _html(at)
    for internal in ("Data &amp; sync", "Firebase account", A_UID):
        assert internal not in html
    assert "btn_reload" not in _buttons(at) and "btn_clear_all" not in _buttons(at)


def test_admin_keeps_every_capability(app, google_env, store, admins):
    admins(A_MAIL)
    _ready(store, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    assert "Discover jobs" in _html(at)                               # the admin's dashboard
    for page in ("monitoring", "email", "preferences", "alerts", "settings"):
        at.session_state["page"] = page
        at.run()
        assert not at.exception and at.session_state.page == page
    assert "btn_reload" in _buttons(at) and "btn_clear_all" in _buttons(at)


# ── companies ─────────────────────────────────────────────────────────────────

def test_following_writes_only_to_the_persons_own_list(app, google_env, store):
    _ready(store, A_UID, A_MAIL)
    before = (REPO / "companies.json").read_bytes()
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS, page="companies")
    assert "You aren&#x27;t following any companies yet." in _html(at)
    at.button(key="btn_co_pick").click().run()
    follow = next(b for b in at.button if (b.key or "").startswith("fa_"))
    follow.click().run()
    assert not at.exception
    assert len(store.for_uid(A_UID).watchlist()) == 1
    following = [b.key for b in at.button if (b.key or "").startswith("fu_")]
    assert len(following) == 1
    tmp_catalogue = json.loads((at.tmp_path / "companies.json").read_text("utf-8"))
    assert len(tmp_catalogue) == 7 and (REPO / "companies.json").read_bytes() == before
    assert tmp_catalogue == json.loads(before)                       # the shared catalogue is untouched
    assert [p for p in store.backend.docs if "companies" in p] == [
        p for p in store.backend.docs if p.startswith(f"users/{A_UID}/companies/")]
    at.button(key=following[0]).click().run()
    assert store.for_uid(A_UID).watchlist() == set()


def test_admin_still_sees_all_seven_tracked_companies(app, google_env, store, admins):
    admins(A_MAIL)
    _ready(store, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS, page="companies")
    html = _html(at)
    for c in json.loads((REPO / "companies.json").read_text("utf-8")):
        assert (c.get("name") or "") in html
    assert "You aren&#x27;t following any companies yet." not in html


def test_followed_companies_become_the_default_company_filter(app, google_env, store):
    u = _ready(store, A_UID, A_MAIL)
    u.watch("metlife", {"metlife", "pwc"})
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS, page="jobs")
    assert _ms(at, "j_pco").value == ["metlife"]
    assert METLIFE_TITLE in _html(at) and PWC_TITLE not in _html(at)


# ── preferences and filters ──────────────────────────────────────────────────

def test_saved_preferences_are_the_job_filter_defaults(app, google_env, store):
    _ready(store, A_UID, A_MAIL, locations=["Pune"])
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS, page="jobs")
    assert _ms(at, "j_ploc").value == ["Pune"]
    assert PWC_TITLE in _html(at) and METLIFE_TITLE not in _html(at)
    _ms(at, "j_ploc").set_value([]).run()                          # a temporary change ...
    assert PWC_TITLE in _html(at) and METLIFE_TITLE in _html(at)
    assert store.backend.get(f"users/{A_UID}")["preferences"]["locations"] == ["Pune"]   # ... is not saved
    at.button(key="j_clear").click().run()                         # back to the saved ones
    assert _ms(at, "j_ploc").value == ["Pune"] and METLIFE_TITLE not in _html(at)


def test_clear_filters_shows_every_job_without_touching_preferences(app, google_env, store):
    _ready(store, A_UID, A_MAIL, locations=["Kochi"])                  # nothing matches
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS, page="jobs")
    assert PWC_TITLE not in _html(at) and METLIFE_TITLE not in _html(at)
    at.button(key="btn_reset_filters").click().run()
    assert PWC_TITLE in _html(at) and METLIFE_TITLE in _html(at) and _ms(at, "j_ploc").value == []
    assert store.backend.get(f"users/{A_UID}")["preferences"]["locations"] == ["Kochi"]


def test_preferences_page_saves_and_reloads(app, google_env, store):
    _ready(store, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    at.button(key="acct_preferences").click().run()
    assert at.session_state.page == "preferences"
    _pills(at, "pref_job_families").set_value(["Software engineering"])
    _pills(at, "pref_work_modes").set_value(["hybrid"])
    at.button(key="btn_prefs_save").click().run()
    prefs = store.backend.get(f"users/{A_UID}")["preferences"]
    assert prefs["job_families"] == ["Software engineering"] and prefs["work_modes"] == ["hybrid"]
    at.run()
    assert _pills(at, "pref_job_families").value == ["Software engineering"]
    _nav(at, "jobs")
    assert _ms(at, "j_pfam").value == ["Software engineering"]     # the new defaults reach the lists
    assert PWC_TITLE in _html(at) and METLIFE_TITLE not in _html(at)
    assert "aren’t stated in the postings" in _html(at)               # job type / work mode: honest note


def test_invalid_preference_values_are_refused(app, google_env, store):
    u = _ready(store, A_UID, A_MAIL)
    with pytest.raises(user_store.InvalidInput):
        u.set_preferences("general", [], ["Atlantis"], [])
    with pytest.raises(user_store.InvalidInput):
        u.set_preferences("general", [], [], [], ["gig"], [])


# ── alerts ────────────────────────────────────────────────────────────────────

def test_alerts_stay_off_until_turned_on(app, google_env, store, monkeypatch):
    import smtplib
    monkeypatch.setattr(smtplib, "SMTP_SSL", lambda *a, **k: pytest.fail("no email may be sent"))
    monkeypatch.setattr(smtplib, "SMTP", lambda *a, **k: pytest.fail("no email may be sent"))
    _ready(store, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    at.button(key="acct_alerts").click().run()
    assert at.session_state.page == "alerts" and not at.exception
    assert at.toggle(key="my_alerts_on").value is False
    assert at.radio(key="my_alerts_scope").value == "followed"
    assert "btn_test_email" not in _buttons(at)
    at.toggle(key="my_alerts_on").set_value(True)
    at.button(key="btn_my_alerts").click().run()
    n = user_store.notification_settings_from(store.backend.get(f"users/{A_UID}"))
    assert n["enabled"] is True and n["email"] == A_MAIL


# ── isolation, performance, signed-out ───────────────────────────────────────

def test_people_never_see_each_others_choices(app, google_env, store):
    a = _ready(store, A_UID, A_MAIL, locations=["Hyderabad"])
    a.watch("metlife", {"metlife"})
    a.set_notifications(True, "general")
    b_at = _sign_in(app, google_env, B_UID, B_MAIL, seen=JOBS)            # onboarding skipped
    html = _html(b_at)
    assert PWC_TITLE in html and METLIFE_TITLE in html                # B's defaults are B's own (none)
    for page in ("settings", "jobs", "alerts"):
        b_at.session_state["page"] = page
        b_at.run()
        html = _html(b_at)
        assert A_MAIL not in html and A_UID not in html and B_UID not in html
    assert b_at.toggle(key="my_alerts_on").value is False
    assert store.for_uid(B_UID).watchlist() == set()
    assert store.backend.get(f"users/{A_UID}")["preferences"]["locations"] == ["Hyderabad"]


def test_clicks_reuse_the_personal_snapshot(app, google_env, store):
    _ready(store, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    store.backend.reads = 0
    for page in ("jobs", "companies", "settings", "home", "jobs"):     # back to Jobs: its filters were dropped
        _nav(at, page)
    assert store.backend.reads == 0                                   # four clicks, no Firestore reads
    at.button(key="acct_preferences").click().run()
    _pills(at, "pref_locations").set_value(["Pune"])
    at.button(key="btn_prefs_save").click().run()
    assert store.backend.reads <= 4                                   # a save re-reads once (3 reads + the write's own)


def test_signing_out_returns_to_the_login_page(app, google_env, store):
    _ready(store, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    at.button(key="btn_account_sign_out").click().run()
    assert _login_page(at) and "_pcache" not in at.session_state and A_MAIL not in _html(at)


def test_signed_out_visitors_get_the_login_page(app, google_env, store):
    from test_firebase_auth import GATED
    at = app(list(JOBS), secrets=GATED, owner=False)
    assert _login_page(at) and PWC_TITLE not in _html(at)
    assert not _acct_keys(at) and 'class="acct-head"' not in _html(at)   # no account menu
    assert store.backend.docs == {}


def test_legacy_mode_keeps_the_admin_dashboard(app):
    at = app(list(JOBS))
    assert "Discover jobs" in _html(at) and not at.get("popover")
    assert "nav_monitoring" in _buttons(at)


# ── presentation guards (the browser checks are separate) ────────────────────

def test_motion_is_light_and_respects_reduced_motion():
    src = (REPO / "streamlit_app.py").read_text("utf-8")
    blocks = re.findall(r"@media \(prefers-reduced-motion: reduce\) \{(.*?)\n\}", src, re.S)
    joined = "\n".join(blocks)
    for selector in (".lp-card-in", ".st-key-onboard", ".lp-title", ".lp-copy"):
        assert selector in joined
    assert "transition: all" not in src                               # only cheap, named properties animate
