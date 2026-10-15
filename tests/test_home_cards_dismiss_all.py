"""Member Home: the three summary cards line up, and Dismiss all is back
(2026-10-15 report).

Cards: each card is a keyed st.container inside a column, wrapped in its own
stLayoutWrapper. The old rule put height: 100% on the card alone, which
resolved against that auto-height wrapper, so every card kept its content
height: Job preferences (several lines) ran ~200 px taller than Companies and
Alert emails and its button sat lower. Measured in Chromium (local harness,
1280-1600 px): tops already matched, bottoms and buttons now match too; on
phones the cards stack exactly as before. AppTest can't measure layout, so
these tests pin the structure and the CSS that does it.

Dismiss all: members never had one. The single-user app's "Clear all" sat in
the job toolbar until 2026-09-29, then moved to the admin-only Settings
danger zone, which also changes the shared listings. The member version asks
first and writes only this person's dismissed list. data/seen_jobs.json and
every other account are untouched. Firestore is MemoryBackend; Google and
Firebase are never contacted."""
from pathlib import Path

import pytest

from user_store import MemoryBackend, UserStore
from test_firebase_auth import _buttons, admins, google_env, no_env  # noqa: F401
from test_phase3_user_data import A_MAIL, A_UID, B_MAIL, B_UID, METLIFE_JOB, PWC_JOB, AccountIdentity, _sign_in, _use_store
from test_phase3_user_data import _job_key
from test_streamlit_app import _html, _key, _nav, _seen, app  # noqa: F401

APP_SRC = (Path(__file__).resolve().parent.parent / "streamlit_app.py").read_text("utf-8")
JOBS = (PWC_JOB, METLIFE_JOB)          # Software · Pune · entry level / Data · Hyderabad · fresher


@pytest.fixture
def store(monkeypatch):
    s = UserStore(MemoryBackend(), AccountIdentity())
    _use_store(monkeypatch, s)
    return s


def _ready(store, uid, email, **prefs):
    u = store.for_uid(uid)
    u.ensure_profile(email, "", True)
    u.complete_onboarding()
    if prefs:
        u.set_preferences("general", [], prefs.get("locations", []), [])
    return u


def _labels(at):
    return {b.key: b.label for b in at.button}


# ── the three cards ──────────────────────────────────────────────────────────

def test_cards_share_one_row_container(app, google_env, store):
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    keys = _buttons(at)
    assert {"btn_set_mh_prefs", "btn_set_mh_companies", "btn_set_mh_alerts"} <= keys
    labels = _labels(at)
    assert (labels["btn_set_mh_prefs"], labels["btn_set_mh_companies"], labels["btn_set_mh_alerts"]) == \
        ("Edit preferences", "Choose companies", "Manage alerts")              # content unchanged
    assert 'cards_row = st.container(key="mh_cards")' in APP_SRC and "cols = cards_row.columns(3)" in APP_SRC


def test_cards_stretch_to_the_row_and_buttons_sit_at_the_bottom():
    css = APP_SRC
    # every level from the row down to the card stretches...
    assert ':is(.st-key-mh_cards, .st-key-mh_cards > [data-testid="stLayoutWrapper"]) > [data-testid="stHorizontalBlock"] { align-items: stretch !important; }' in css
    assert '.st-key-mh_cards [data-testid="stColumn"] { display: flex !important; flex-direction: column !important; }' in css
    assert '.st-key-mh_cards [data-testid="stColumn"] > [data-testid="stVerticalBlock"] { flex: 1 1 auto !important; }' in css
    assert '.st-key-mh_cards [data-testid="stLayoutWrapper"]:has(> [class*="st-key-set_mh_"]) { flex: 1 1 auto !important;' in css
    assert '[class*="st-key-set_mh_"] { flex: 1 1 auto !important;' in css
    # ...and the button is pushed down by flexible space, not a pixel offset
    assert '[class*="st-key-set_mh_"] > [data-testid="stElementContainer"]:last-child { margin-top: auto; }' in css
    assert '[class*="st-key-set_mh_"] { height: 100%; justify-content: space-between; }' not in css


# ── Dismiss all ──────────────────────────────────────────────────────────────

def test_member_dismiss_all_asks_first_and_is_theirs_alone(app, google_env, store):
    _ready(store, A_UID, A_MAIL).set_watch_all(True)                        # both chose "all companies"
    _ready(store, B_UID, B_MAIL).set_watch_all(True)
    a = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    assert _labels(a)["btn_mh_clear"] == "Dismiss all (2)"
    a.button(key="btn_mh_clear").click().run()
    html = _html(a)
    assert "Dismiss all <b>2</b> jobs? They’re hidden for you only — other members still see them." in html
    assert "btn_mh_clear" not in _buttons(a)
    assert store.for_uid(A_UID).dismissed() == set()                       # asking changes nothing

    a.button(key="btn_mh_clear_no").click().run()                          # Cancel
    assert "btn_mh_clear_yes" not in _buttons(a) and store.for_uid(A_UID).dismissed() == set()
    assert PWC_JOB["title"] in _html(a) and METLIFE_JOB["title"] in _html(a)

    a.button(key="btn_mh_clear").click().run()
    a.button(key="btn_mh_clear_yes").click().run()
    assert not a.exception
    assert store.for_uid(A_UID).dismissed() == {_job_key(PWC_JOB), _job_key(METLIFE_JOB)}
    html = _html(a)
    assert PWC_JOB["title"] not in html and METLIFE_JOB["title"] not in html
    assert "You’ve dismissed every job" in html and "No open jobs right now" not in html
    assert "btn_mh_dismissed" in _buttons(a)

    # shared listings and other people untouched
    assert not any(r.get("dismissed") for r in _seen(a))
    assert store.for_uid(B_UID).dismissed() == set()
    b = _sign_in(app, google_env, B_UID, B_MAIL, seen=JOBS)
    assert PWC_JOB["title"] in _html(b) and METLIFE_JOB["title"] in _html(b)


def test_dismissed_jobs_can_be_restored_one_by_one(app, google_env, store):
    _ready(store, A_UID, A_MAIL).set_watch_all(True)
    a = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    a.button(key="btn_mh_clear").click().run()
    a.button(key="btn_mh_clear_yes").click().run()
    a.button(key="btn_mh_dismissed").click().run()                         # View 2 dismissed jobs
    assert a.session_state.page == "jobs" and a.session_state.jobs_tab == "dismissed"
    a.button(key=_key("restore", PWC_JOB)).click().run()
    assert store.for_uid(A_UID).dismissed() == {_job_key(METLIFE_JOB)}
    _nav(a, "home")
    assert PWC_JOB["title"] in _html(a) and _labels(a)["btn_mh_clear"] == "Dismiss all (1)"


def test_dismiss_all_covers_only_the_listed_matches(app, google_env, store):
    _ready(store, A_UID, A_MAIL, locations=["Hyderabad"]).set_watch_all(True)   # matches MetLife only
    a = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    assert _labels(a)["btn_mh_clear"] == "Dismiss all (1)"
    a.button(key="btn_mh_clear").click().run()
    assert "Dismiss all <b>1</b> matching job?" in _html(a)
    a.button(key="btn_mh_clear_yes").click().run()
    assert store.for_uid(A_UID).dismissed() == {_job_key(METLIFE_JOB)}    # PwC was never listed here


def test_no_dismiss_all_when_nothing_is_listed(app, google_env, store):
    store_user = _ready(store, A_UID, A_MAIL)
    store_user.dismiss({_job_key(PWC_JOB), _job_key(METLIFE_JOB)})
    a = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    assert "btn_mh_clear" not in _buttons(a)


def test_admin_home_and_settings_unchanged(app, google_env, store, admins):
    admins(A_MAIL)
    a = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    assert "btn_mh_clear" not in _buttons(a)                               # the member control only
    _nav(a, "settings")
    assert "btn_clear_all" in _buttons(a)                                  # the admin's shared one stays
