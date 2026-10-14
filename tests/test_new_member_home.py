"""A new member's first dashboard (2026-10-15 production report).

A freshly registered account saw "35 jobs match your preferences right now"
with no preferences and no followed companies. The jobs were legitimate:
the scanner's listings are shared by everyone (data/seen_jobs.json), and a
person who hasn't narrowed anything sees all of them, by design. The wording
was wrong: it claimed matches for preferences that didn't exist. These tests
pin the honest wording, the per-account defaults and isolation, the full
sign-up -> verify -> dashboard -> sign out -> sign in path, and the member
hero's discovery-feed animation (the original Home hero's, which the member
Home never had). Firestore is user_store.MemoryBackend; Google and Firebase
are never contacted and no email is sent."""
import re

import pytest

import user_store
from user_store import MemoryBackend, UserStore
from test_firebase_auth import GATED, _buttons, _login, _login_page, admins, google_env, no_env  # noqa: F401
from test_phase3_user_data import (  # noqa: F401
    A_MAIL, A_UID, B_MAIL, B_UID, METLIFE_JOB, PWC_JOB, AccountIdentity, _password, _sign_in, _use_store,
)
from test_security_audit_2026_10_13 import firebase_replies  # noqa: F401
from test_signup_flow import NEW_EMAIL, NEW_PW, NEW_UID, _sign_up, ok_replies
from test_streamlit_app import _html, _nav, app  # noqa: F401

JOBS = (PWC_JOB, METLIFE_JOB)          # Software · Pune · entry level / Data · Hyderabad · fresher
PWC_TITLE, METLIFE_TITLE = PWC_JOB["title"], METLIFE_JOB["title"]
SHARED = "discover your next <em>opportunity</em>"
MATCHES = "here are your <em>matches</em>"


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
        u.set_preferences("general", prefs.get("job_families", []), prefs.get("locations", []),
                          prefs.get("experience", []))
    return u


def _hero(html: str) -> str:
    return html.split('<div class="hero mh-hero">', 1)[1].split('<div class="opps-wrap"', 1)[0]


# ── wording: shared listings vs. matches ─────────────────────────────────────

def test_new_member_home_calls_shared_listings_what_they_are(app, google_env, store):
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)                  # onboarding skipped
    html = _html(at)
    hero = _hero(html)
    assert SHARED in hero and MATCHES not in hero and "match your preferences" not in html
    assert ("2 open fresher and entry-level jobs across every company the tracker monitors. "
            "Set preferences or follow companies to narrow them to your matches.") in hero
    assert '<h2 class="section-title">Latest jobs</h2>' in html and "Your matches" not in html
    assert "the same listings every member sees" in html
    assert PWC_TITLE in html and METLIFE_TITLE in html                       # the shared listings stay visible
    assert "Not following any yet" in html and "None yet — every qualifying job is shown." in html


@pytest.mark.parametrize("narrow", ["preferences", "companies"])
def test_preferences_or_follows_turn_home_into_matches(app, google_env, store, narrow):
    u = _ready(store, A_UID, A_MAIL, **({"locations": ["Hyderabad"]} if narrow == "preferences" else {}))
    if narrow == "companies":
        u.watch("metlife", {"metlife", "pwc"})
        u.set_watch_all(False)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    html = _html(at)
    assert MATCHES in _hero(html) and "1 job match your preferences right now." in html
    assert '<h2 class="section-title">Your matches</h2>' in html and "Latest jobs</h2>" not in html
    assert METLIFE_TITLE in html and PWC_TITLE not in html


def test_saved_only_preferences_do_not_count_as_matching(app, google_env, store):
    """Job type and work mode are saved but never matched (postings don't
    state them), so they must not make Home claim "matches"."""
    u = _ready(store, A_UID, A_MAIL)
    u.set_preferences("general", [], [], [], ["internship"], ["remote"])
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    assert SHARED in _hero(_html(at))


# ── the hero animation ────────────────────────────────────────────────────────

def test_member_hero_has_the_original_discovery_feed(app, google_env, store):
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    html = _html(at)
    hero = html.split('<div class="hero mh-hero">', 1)[1]
    assert hero.index('<div class="opps-wrap" aria-hidden="true">') < hero.index("</div></div></div></div>") + 30
    for part in ('<span class="glow-bg"></span>', '<div class="feed-q">', 'class="feed-row r1"', 'class="feed-row r2"',
                 'class="feed-row r3"', '<div class="feed-seal">'):
        assert part in hero, part
    # the feed's animations and the card that keeps its glow inside
    assert re.search(r"\.feed-row\.r1 \{[^}]*animation:", html) or re.search(r"\.feed-row[^{]*\{[^}]*animation: jt-feed-row", html)
    assert "@keyframes jt-feed-row" in html and "@keyframes jt-glow-drift" in html
    assert ".st-key-mh_hero { position: relative; overflow: hidden; isolation: isolate;" in html
    reduced = html.split("@media (prefers-reduced-motion: reduce) {")
    assert any(".feed-q .qbar i, .feed-row, .feed-row .ok, .feed-seal, .opps > .glow-bg { animation: none !important;"
               in r for r in reduced)


def test_admin_home_keeps_its_hero(app, google_env, store, admins):
    admins(A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    html = _html(at)
    assert "Discover jobs</div>" in html and '<div class="opps-wrap" aria-hidden="true">' in html
    assert 'class="hero mh-hero"' not in html


# ── isolation and the whole new-member path ──────────────────────────────────

def test_sign_up_to_dashboard_and_back_with_isolated_data(app, google_env, store, firebase_replies):
    other = _ready(store, B_UID, B_MAIL, locations=["Pune"])                 # someone already using the app
    other.watch("pwc", {"pwc", "metlife"})
    other.set_watch_all(False)
    other.set_notifications(True, "general")
    other_before = dict(store.backend.get(f"users/{B_UID}"))
    fake = firebase_replies(**ok_replies(signInWithPassword=_password(NEW_UID, NEW_EMAIL, verified=False)))
    at = app(list(JOBS), secrets=GATED, owner=False)

    # 1-3: register; Firebase is asked for the verification email; nothing personal exists yet
    _sign_up(at)
    assert fake.methods() == ["signUp", "update", "sendOobCode"]
    assert store.backend.get(f"users/{NEW_UID}") is None
    _login(at, email=NEW_EMAIL, password=NEW_PW)                               # not verified yet
    assert _login_page(at) and store.backend.get(f"users/{NEW_UID}") is None

    # 4-5: verified (Firebase now says so), sign in
    fake.by_method["signInWithPassword"] = {**_password(NEW_UID, NEW_EMAIL), "displayName": "Asha Verma"}
    _login(at, email=NEW_EMAIL, password=NEW_PW)
    assert not _login_page(at) and at.session_state.page == "onboarding"
    prof = store.backend.get(f"users/{NEW_UID}")
    assert prof["email"] == NEW_EMAIL and prof["display_name"] == "Asha Verma" and prof["email_verified"] is True
    assert prof["watch_all"] is False and store.for_uid(NEW_UID).watchlist() == set()
    assert prof["preferences"]["locations"] == [] and prof["preferences"]["job_families"] == []
    assert user_store.notification_settings_from(prof)["enabled"] is False
    at.button(key="btn_onboarding_skip").click().run()

    # 6: the dashboard: shared listings, honestly labelled, nothing of the other person
    html = _html(at)
    assert at.session_state.page == "home" and "Hi, Asha" in html and SHARED in _hero(html)
    assert PWC_TITLE in html and METLIFE_TITLE in html
    assert "Not following any yet" in html and B_MAIL not in html and B_UID not in html
    assert 'class="role admin"' not in html and "nav_email" not in _buttons(at)

    # 7: companies: none followed; follow one
    _nav(at, "companies")
    assert "You aren&#x27;t following any companies yet." in _html(at)
    at.button(key="btn_co_pick").click().run()
    next(b for b in at.button if (b.key or "").startswith("fa_")).click().run()
    assert not at.exception and len(store.for_uid(NEW_UID).watchlist()) == 1

    # 8: alerts are this account's own, and off
    at.button(key="acct_alerts").click().run()
    assert at.session_state.page == "alerts" and at.toggle(key="my_alerts_on").value is False

    # 10: sign out, sign back in: the choice persisted, still nobody else's
    at.button(key="btn_account_sign_out").click().run()
    assert _login_page(at)
    _login(at, email=NEW_EMAIL, password=NEW_PW)
    assert not _login_page(at) and at.session_state.page == "alerts"          # the page in the address; no onboarding again
    _nav(at, "home")
    html = _html(at)
    assert MATCHES in _hero(html) and "Following 1" in html
    assert store.backend.get(f"users/{B_UID}") == other_before                 # the other account is untouched
    assert store.for_uid(B_UID).watchlist() == {"pwc"}
