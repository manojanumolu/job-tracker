"""Profile avatars: a person picks a company mark (or keeps their initial)
on the Account page, so accounts with the same name are told apart.

The rules under test:
  * saved to the signed-in person's own profile (users/{uid}.avatar) by the
    server-side UID; only AVATAR_CHOICES or "" (initial) are accepted
  * shown in the compact header control, the account menu and the Account
    page — and it is the viewer's choice, never the employer of a job
  * survives signing in again; profiles without one keep the initial
  * changing it changes nothing else, and never another account — three
    accounts with the same display name stay separate
Firestore is user_store.MemoryBackend; Google and Firebase are never
contacted and no email is sent."""
import inspect

import pytest

import brand_logos
import user_store
from user_store import InvalidInput, MemoryBackend, UserData, UserStore
from test_account_onboarding import CountingBackend, JOBS, _ready
from test_firebase_auth import _buttons, admins, google_env, no_env  # noqa: F401  (fixtures)
from test_phase3_user_data import (  # noqa: F401
    A_MAIL, A_UID, B_MAIL, B_UID, AccountIdentity, _sign_in, _use_store,
)
from test_streamlit_app import _html, _nav, app  # noqa: F401

C_UID, C_MAIL = "UserCccccccccccccccccccccccc3", "carol@example.org"
TRIGGER = '[class*="st-key-acct_menu"] [data-testid="stPopoverButton"]::before'


# ── store ────────────────────────────────────────────────────────────────────

def test_the_choices_are_the_ten_company_marks():
    assert set(user_store.AVATAR_CHOICES) == set(brand_logos.BRAND_NAMES) and len(user_store.AVATAR_CHOICES) == 10


def _person(backend, uid=A_UID, email=A_MAIL):
    u = UserData(backend, uid)
    u.ensure_profile(email, "Manoj", True)
    u.complete_onboarding()
    return u


def test_saving_and_changing_an_avatar():
    backend = MemoryBackend()
    a = _person(backend)
    assert user_store.avatar_from(a.profile()) == ""                       # default: the initial
    assert a.set_avatar("google") == "google" and user_store.avatar_from(a.profile()) == "google"
    assert a.set_avatar("amazon") == "amazon" and user_store.avatar_from(a.profile()) == "amazon"
    assert a.set_avatar("") == "" and user_store.avatar_from(a.profile()) == ""       # back to the initial
    assert a.set_avatar(None) == ""


@pytest.mark.parametrize("bad", ["Google", "metlife", "../x", "google ", 1, ["google"], {"k": 1}, True])
def test_only_known_avatars_are_accepted(bad):
    backend = MemoryBackend()
    a = _person(backend)
    a.set_avatar("ibm")
    with pytest.raises(InvalidInput):
        a.set_avatar(bad)
    assert user_store.avatar_from(a.profile()) == "ibm"


@pytest.mark.parametrize("profile", [{}, None, {"avatar": "metlife"}, {"avatar": 3}, {"avatar": ""}, {"avatar": None}])
def test_profiles_without_a_usable_avatar_fall_back_to_the_initial(profile):
    assert user_store.avatar_from(profile) == ""


def test_an_avatar_changes_only_the_avatar_and_survives_signing_in():
    backend = MemoryBackend()
    a = _person(backend)
    a.watch("pwc", {"pwc", "metlife"})
    a.set_watch_all(False)
    a.dismiss({"job-1|https://x"})
    a.set_notifications(True)
    a.set_email_tailoring(["Hyderabad"], ["Software engineering"])
    a.set_display_name("Manoj A")
    before = {p: dict(d) for p, d in backend.docs.items()}
    a.set_avatar("nvidia")
    for path in before:
        changed = {k for k in set(before[path]) | set(backend.docs[path]) if before[path].get(k) != backend.docs[path].get(k)}
        assert changed == ({"avatar"} if path == f"users/{A_UID}" else set()), path
    prof, created = UserData(backend, A_UID).ensure_profile(A_MAIL, "Manoj", True)          # signing in again
    assert not created and prof["avatar"] == "nvidia" and prof["preferred_name"] == "Manoj A"
    assert set(inspect.signature(UserData.set_avatar).parameters) == {"self", "choice"}        # no uid to point elsewhere


def test_one_persons_avatar_never_reaches_another():
    backend = MemoryBackend()
    a, b = _person(backend), _person(backend, B_UID, B_MAIL)
    b.set_avatar("apple")
    b_before = backend.get(f"users/{B_UID}")
    a.set_avatar("meta")
    assert backend.get(f"users/{B_UID}") == b_before and user_store.avatar_from(b_before) == "apple"


# ── in the app ───────────────────────────────────────────────────────────────

@pytest.fixture
def ustore(monkeypatch):
    s = UserStore(CountingBackend(), AccountIdentity())
    _use_store(monkeypatch, s)
    return s


def _label(at):
    return next(p.proto.popover.label for p in at.get("popover"))


def _styles(at):
    return "".join(e.proto.body for e in at.get("html") if e.proto.body.startswith("<style>"))


def _shows(at, key):
    """The compact control, the menu head and the Account page all show
    ``key``'s mark ("" = the initial)."""
    html, styles = _html(at), _styles(at)
    if key:
        uri, tile = brand_logos.brand_uri(key), brand_logos.brand_tile(key)
        mark = f'av-mark" style="background:{tile}" aria-hidden="true"><img src="{uri}" alt=""></span>'
        return (f'{TRIGGER} {{ content: ""; background: url("{uri}")' in styles
                and f'<div class="acct-head"><span class="acct-av {mark}' in html
                and f'<div class="set-head"><span class="acct-av acct-av-lg {mark}' in html)
    return (f'{TRIGGER} {{ content: "' in styles and 'av-mark" style=' not in html
            and '<div class="acct-head"><span class="acct-av" aria-hidden="true">' in html)


def _pick(at, key):
    at.button(key=f"av_pick_{key or 'initials'}").click().run()
    assert not at.exception
    return at


def test_existing_accounts_keep_their_initial(app, google_env, ustore):
    _ready(ustore, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS, page="settings")
    assert _shows(at, "") and f'{TRIGGER} {{ content: "A"; }}' in _styles(at)
    assert {f"av_pick_{k}" for k in ("initials",) + user_store.AVATAR_CHOICES} <= _buttons(at)
    assert ".st-key-av_pick_initials button { border-color: var(--accent) !important;" in _styles(at)   # selected


@pytest.mark.parametrize("admin", [False, True])
def test_picking_an_avatar_shows_it_everywhere(app, google_env, ustore, admins, admin):
    admins(A_MAIL if admin else "someone@else.example")
    _ready(ustore, A_UID, A_MAIL).set_display_name("Manoj Anumolu")
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS, page="settings")
    _pick(at, "google")
    assert _shows(at, "google")
    assert _label(at) == ("Manoj **ADMIN**" if admin else "Manoj")          # first name + badge unchanged
    html = _html(at)
    assert '<div class="nm">Manoj Anumolu</div>' in html and A_MAIL in html
    assert 'class="name-msg ok" role="status"' in html and "you&#x27;ll appear with Google." in html
    assert ".st-key-av_pick_google button { border-color: var(--accent) !important;" in _styles(at)
    assert ustore.backend.get(f"users/{A_UID}")["avatar"] == "google"
    _pick(at, "amazon")
    assert _shows(at, "amazon") and not _shows(at, "google")
    _pick(at, "")
    assert _shows(at, "") and ustore.backend.get(f"users/{A_UID}")["avatar"] == ""


def test_the_avatar_persists_into_a_new_session(app, google_env, ustore):
    _ready(ustore, A_UID, A_MAIL)
    _pick(_sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS, page="settings"), "deloitte")
    again = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)                # another browser session
    _nav(again, "settings")
    assert _shows(again, "deloitte")


def test_the_account_avatar_is_the_viewers_choice_not_the_employers(app, google_env, ustore):
    """Jobs from Accenture show Accenture's mark on their cards; the header
    still shows the viewer's own pick (Microsoft)."""
    from test_streamlit_app import NEW_RECORD
    _ready(ustore, A_UID, A_MAIL).set_avatar("microsoft")
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=({**NEW_RECORD, "company": "Accenture"},))
    _nav(at, "jobs")
    html, styles = _html(at), _styles(at)
    assert f'<img src="{brand_logos.brand_uri("accenture")}" alt=""></div>' in html             # the employer
    assert f'{TRIGGER} {{ content: ""; background: url("{brand_logos.brand_uri("microsoft")}")' in styles   # the viewer
    assert f'<div class="acct-head"><span class="acct-av av-mark" style="background:{brand_logos.brand_tile("microsoft")}"' in html


def test_three_accounts_with_the_same_name_stay_separate(app, google_env, ustore):
    people = ((A_UID, A_MAIL, "google"), (B_UID, B_MAIL, "amazon"), (C_UID, C_MAIL, "microsoft"))
    for uid, mail, _ in people:
        u = _ready(ustore, uid, mail)
        u.set_display_name("Manoj")
    ustore.for_uid(B_UID).watch("pwc", {"pwc", "metlife"})
    ustore.for_uid(B_UID).set_watch_all(False)
    ustore.for_uid(C_UID).set_email_tailoring(["Pune"], [])
    for uid, mail, pick in people:                                         # each picks their own, in the app
        at = _sign_in(app, google_env, uid, mail, seen=JOBS, page="settings")
        before = {u: ustore.backend.get(f"users/{u}") for u, _, _ in people if u != uid}
        _pick(at, pick)
        assert _label(at) == "Manoj" and _shows(at, pick)
        assert {u: ustore.backend.get(f"users/{u}") for u, _, _ in people if u != uid} == before
    assert {uid: user_store.avatar_from(ustore.backend.get(f"users/{uid}")) for uid, _, _ in people} == \
           {A_UID: "google", B_UID: "amazon", C_UID: "microsoft"}
    assert ustore.for_uid(B_UID).watchlist() == {"pwc"} and ustore.for_uid(A_UID).watchlist() == set()
    assert user_store.tailoring_from(ustore.backend.get(f"users/{C_UID}"))["locations"] == ["Pune"]
    for uid, mail, pick in people:                                         # and each sees only their own
        at = _sign_in(app, google_env, uid, mail, seen=JOBS, page="settings")
        assert _shows(at, pick) and all(not _shows(at, other) for _, _, other in people if other != pick)


def test_a_url_cannot_point_the_picker_at_another_account(app, google_env, ustore):
    _ready(ustore, A_UID, A_MAIL)
    _ready(ustore, B_UID, B_MAIL)
    b_before = ustore.backend.get(f"users/{B_UID}")
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS, page="settings",
                  query={"uid": B_UID, "user": B_UID, "email": B_MAIL, "avatar": "apple"})
    _pick(at, "intel")
    assert ustore.backend.get(f"users/{B_UID}") == b_before
    assert ustore.backend.get(f"users/{A_UID}")["avatar"] == "intel"


def test_an_avatar_change_leaves_every_other_setting_alone(app, google_env, ustore):
    u = _ready(ustore, A_UID, A_MAIL, job_families=["Software engineering"], locations=["Pune"])
    u.watch("pwc", {"pwc", "metlife"})
    u.set_watch_all(False)
    u.set_notifications(True)
    u.set_email_tailoring(["Hyderabad"], ["Data & analytics"])
    u.dismiss({"some-job|https://x"})
    u.set_display_name("Manoj")
    before = u.snapshot()
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS, page="settings")
    _pick(at, "apple")
    after = u.snapshot()
    assert after["watchlist"] == before["watchlist"] and after["dismissed"] == before["dismissed"]
    drop = {"avatar", "last_login_at"}
    assert {k: v for k, v in after["profile"].items() if k not in drop} == \
           {k: v for k, v in before["profile"].items() if k not in drop}


def test_no_picker_without_personal_data(app, google_env):
    from test_firebase_auth import GATED, FakeUser, NEW_RECORD, _fresh_google_token, ok_google_answer
    from test_phase3_user_data import _go
    google_env["firebase"](ok_google_answer())
    google_env["set_user"](FakeUser(token=_fresh_google_token(), name="Manoj Anumolu"))
    at = app([NEW_RECORD], secrets=GATED, owner=False)
    _go(at, "settings")
    assert not [k for k in _buttons(at) if k.startswith("av_pick_")] and _shows(at, "")
