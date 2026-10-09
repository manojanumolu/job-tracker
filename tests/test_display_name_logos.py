"""Company marks (brand_logos) and the display name a person chooses on the
Account page (users/{uid}.preferred_name).

The rules under test:
  * the ten major employers get their own local SVG mark, everywhere a
    company is drawn; anything else gets initials on a tint, and names that
    merely look alike ("MetLife", "Intellect") never borrow a mark
  * the chosen name is trimmed and validated, saved only to the signed-in
    person's own profile (by the server-side UID), shown in the header
    (first name, ADMIN for admins) and in the menu (full name + email), and
    survives signing in again
  * saving it changes nothing else: not the email, UID, role, companies,
    dismissals, alerts, tailoring or preferences — and never another account
Firestore is user_store.MemoryBackend; Google and Firebase are never
contacted and no email is sent."""
import base64
import inspect
import xml.etree.ElementTree as ET

import pytest

import brand_logos
import user_store
from user_store import InvalidInput, MemoryBackend, UserData, UserStore
from test_account_onboarding import CountingBackend, JOBS, _ready
from test_firebase_auth import _buttons, admins, google_env, no_env  # noqa: F401  (fixtures)
from test_phase3_user_data import (  # noqa: F401
    A_MAIL, A_UID, B_MAIL, B_UID, AccountIdentity, _go, _sign_in, _use_store,
)
from test_streamlit_app import _html, _nav, app  # noqa: F401

TEN = ("google", "microsoft", "amazon", "apple", "meta", "nvidia", "ibm", "intel", "accenture", "deloitte")
C_UID, C_MAIL = "UserCccccccccccccccccccccccc3", "carol@example.org"


# ── company marks ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name, key", [
    ("Google", "google"), ("Google India", "google"), ("Alphabet", "google"),
    ("Microsoft", "microsoft"), ("Microsoft Corporation", "microsoft"),
    ("Amazon", "amazon"), ("Amazon Web Services", "amazon"), ("AWS", "amazon"),
    ("Apple", "apple"), ("Apple Inc.", "apple"),
    ("Meta", "meta"), ("Meta Platforms", "meta"), ("Facebook", "meta"),
    ("NVIDIA", "nvidia"), ("Nvidia", "nvidia"),
    ("IBM", "ibm"), ("IBM India Pvt Ltd", "ibm"), ("International Business Machines", "ibm"),
    ("Intel", "intel"), ("Intel Corporation", "intel"),
    ("Accenture", "accenture"), ("  accenture  ", "accenture"),
    ("Deloitte", "deloitte"), ("Deloitte USI", "deloitte"),
])
def test_each_major_company_gets_its_own_mark(name, key):
    assert brand_logos.brand_key(name) == key


@pytest.mark.parametrize("name", ["MetLife", "Metamorph", "Intellect Design Arena", "Pineapple", "Applied Materials",
                                  "Amazonia Labs", "Googly", "Avalara", "PwC", "NPCI", "Sanofi", "", None, 42])
def test_lookalike_and_unknown_names_get_no_mark(name):
    assert brand_logos.brand_key(name) is None


def test_the_ten_marks_are_distinct_local_svg():
    uris = {k: brand_logos.brand_uri(k) for k in TEN}
    assert len(set(uris.values())) == len(TEN) and set(brand_logos.BRAND_NAMES) == set(TEN)
    for key, uri in uris.items():
        assert uri.startswith("data:image/svg+xml;base64,")
        svg = base64.b64decode(uri.split(",", 1)[1]).decode("utf-8")
        root = ET.fromstring(svg)                                          # well-formed
        assert root.tag == "{http://www.w3.org/2000/svg}svg" and root.get("viewBox")
        flat = svg.replace('xmlns="http://www.w3.org/2000/svg"', "")
        assert "http" not in flat and "href" not in flat and "<script" not in flat and "<image" not in flat, key
        assert brand_logos.brand_tile(key).startswith("#")


@pytest.mark.parametrize("name, expected", [
    ("Morgan Stanley", "MS"), ("MetLife", "Me"), ("Myntra", "My"), ("Mastercard", "Ma"), ("NPCI", "NP"),
    ("PwC", "Pw"), ("Sanofi", "Sa"), ("The Hershey Company", "He"), ("Tata Consultancy Services", "TC"),
    ("x", "X"), ("", ""), ("  ", ""), (None, ""), ("—", ""),
])
def test_fallback_initials(name, expected):
    assert brand_logos.initials(name) == expected


def test_unrelated_m_companies_never_share_an_icon():
    names = ["MetLife", "Myntra", "Mastercard", "Morgan Stanley", "Microsoft", "Meta"]
    icons = {n: brand_logos.brand_key(n) or brand_logos.initials(n) for n in names}
    assert len(set(icons.values())) == len(names)


def test_company_pages_show_marks_and_initials(app):
    """The tracked catalogue: Accenture shows its mark; MetLife (no mark)
    shows "Me" on a tint — never Meta's mark."""
    at = app([])
    _nav(at, "companies")
    html = _html(at)
    assert f'<img src="{brand_logos.brand_uri("accenture")}" alt="">' in html
    assert brand_logos.brand_uri("meta") not in html
    assert '<div class="logo ini2" style="--h:' in html and '" aria-hidden="true">Me</div>' in html


def test_job_cards_use_the_same_mark(app):
    from test_streamlit_app import NEW_RECORD
    job = {**NEW_RECORD, "company": "Accenture"}
    at = app([job])
    _nav(at, "jobs")
    assert html_has_mark(_html(at), "accenture")


def html_has_mark(html, key):
    return f'<div class="logo brand" style="--tile:{brand_logos.brand_tile(key)}" aria-hidden="true">' \
           f'<img src="{brand_logos.brand_uri(key)}" alt=""></div>' in html


# ── display name: validation and storage ─────────────────────────────────────

@pytest.mark.parametrize("raw, clean", [
    ("Ada", "Ada"), ("  Ada Lovelace  ", "Ada Lovelace"), ("Ada \t  Lovelace", "Ada Lovelace"),
    ("x" * user_store.DISPLAY_NAME_MAX, "x" * user_store.DISPLAY_NAME_MAX), ("Zoë O'Brien-Núñez", "Zoë O'Brien-Núñez"),
    ("  " + "y" * user_store.DISPLAY_NAME_MAX + "  ", "y" * user_store.DISPLAY_NAME_MAX),
])
def test_valid_names_are_trimmed(raw, clean):
    assert user_store.clean_display_name(raw) == clean


@pytest.mark.parametrize("raw", ["", "   ", "\t\n", "x" * (user_store.DISPLAY_NAME_MAX + 1), "Ada\x00", "Ada\x07x",
                                 "Ada‮Lovelace", None, 42, ["Ada"]])
def test_invalid_names_are_refused(raw):
    with pytest.raises(InvalidInput):
        user_store.clean_display_name(raw)


def _person(backend, uid=A_UID, email=A_MAIL):
    u = UserData(backend, uid)
    u.ensure_profile(email, "Provider Name", True)
    u.complete_onboarding()
    return u


def test_saving_a_name_changes_only_the_name():
    backend = MemoryBackend()
    a = _person(backend)
    a.watch("pwc", {"pwc", "metlife"})
    a.set_watch_all(False)
    a.dismiss({"job-1|https://x"})
    a.set_notifications(True)
    a.set_email_tailoring(["Hyderabad"], ["Software engineering"])
    a.set_preferences("general", ["Software engineering"], ["Pune"], ["fresher"])
    before = {p: d for p, d in backend.docs.items()}
    assert a.set_display_name("  Ada   Lovelace ") == "Ada Lovelace"
    after = backend.docs
    assert set(after) == set(before)
    for path in before:
        changed = {k for k in set(before[path]) | set(after[path]) if before[path].get(k) != after[path].get(k)}
        assert changed == ({"preferred_name"} if path == f"users/{A_UID}" else set()), path
    prof = a.profile()
    assert prof["preferred_name"] == "Ada Lovelace" and prof["email"] == A_MAIL and prof["uid"] == A_UID
    assert prof["display_name"] == "Provider Name"                        # the provider's name is kept apart
    assert user_store.display_name_from(prof) == "Ada Lovelace"


def test_a_bad_name_saves_nothing():
    backend = MemoryBackend()
    a = _person(backend)
    before = backend.get(f"users/{A_UID}")
    for bad in ("", "   ", "z" * 61):
        with pytest.raises(InvalidInput):
            a.set_display_name(bad)
    assert backend.get(f"users/{A_UID}") == before


def test_the_name_survives_signing_in_again():
    backend = MemoryBackend()
    _person(backend).set_display_name("Ada")
    prof, created = UserData(backend, A_UID).ensure_profile(A_MAIL, "Google Name Changed", True)
    assert not created and prof["preferred_name"] == "Ada" and prof["display_name"] == "Google Name Changed"


def test_one_persons_name_never_reaches_another():
    backend = MemoryBackend()
    a, b = _person(backend), _person(backend, B_UID, B_MAIL)
    b_before = backend.get(f"users/{B_UID}")
    a.set_display_name("Ada")
    assert backend.get(f"users/{B_UID}") == b_before and "preferred_name" not in b_before
    assert set(inspect.signature(UserData.set_display_name).parameters) == {"self", "name"}   # no uid to point elsewhere


@pytest.mark.parametrize("profile", [{}, {"preferred_name": ""}, {"preferred_name": "   "}, {"preferred_name": 7},
                                     {"preferred_name": "x" * 99}, None])
def test_profiles_without_a_usable_chosen_name_fall_back(profile):
    assert user_store.display_name_from(profile) == ""


# ── display name: in the app ─────────────────────────────────────────────────

@pytest.fixture
def ustore(monkeypatch):
    s = UserStore(CountingBackend(), AccountIdentity())
    _use_store(monkeypatch, s)
    return s


def _label(at):
    return next(p.proto.popover.label for p in at.get("popover"))


def _save_name(at, name):
    at.text_input(key="display_name_input").set_value(name)
    at.button(key="btn_save_display_name").click().run()
    assert not at.exception
    return at


def test_an_account_without_a_chosen_name_keeps_the_fallback(app, google_env, ustore):
    _ready(ustore, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS, page="settings")
    assert _label(at) == "Alice"                                            # from the email, as before
    assert at.text_input(key="display_name_input").value == ""


@pytest.mark.parametrize("admin", [False, True])
def test_changing_the_name_updates_header_menu_and_account_page(app, google_env, ustore, admins, admin):
    admins(A_MAIL if admin else "someone@else.example")
    _ready(ustore, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS, page="settings")
    _save_name(at, "   Ada    Lovelace  ")
    assert _label(at) == ("Ada **ADMIN**" if admin else "Ada")              # first name only in the header
    html = _html(at)
    assert ('<div class="who"><div class="nm">Ada Lovelace</div>'
            f'<div class="em">{A_MAIL}</div></div>') in html                 # full name + email in the menu
    assert '<h2 class="section-title">Ada Lovelace</h2>' in html            # the Account page
    assert 'class="name-msg ok" role="status"' in html and "Saved — you&#x27;ll appear as Ada Lovelace." in html
    assert at.text_input(key="display_name_input").value == "Ada Lovelace"
    assert ustore.backend.get(f"users/{A_UID}")["preferred_name"] == "Ada Lovelace"
    assert ('class="role admin"' in html) is admin                          # the role is untouched


def test_an_empty_name_is_refused_with_a_message(app, google_env, ustore):
    _ready(ustore, A_UID, A_MAIL)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS, page="settings")
    _save_name(at, "     ")
    html = _html(at)
    assert 'class="name-msg err" role="alert"' in html and "can&#x27;t be empty" in html
    assert "preferred_name" not in ustore.backend.get(f"users/{A_UID}") and _label(at) == "Alice"


def test_the_chosen_name_persists_into_a_new_session(app, google_env, ustore):
    _ready(ustore, A_UID, A_MAIL)
    _save_name(_sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS, page="settings"), "Ada Lovelace")
    again = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS, page="settings")   # a new browser session
    assert _label(again) == "Ada" and '<div class="nm">Ada Lovelace</div>' in _html(again)
    assert again.text_input(key="display_name_input").value == "Ada Lovelace"


def test_three_accounts_stay_separate(app, google_env, ustore):
    """Three accounts with look-alike names: renaming one changes no one
    else, and nothing is merged by name."""
    for uid, mail in ((A_UID, A_MAIL), (B_UID, B_MAIL), (C_UID, C_MAIL)):
        u = _ready(ustore, uid, mail)
        u.set_display_name("Manoj")
    ustore.for_uid(B_UID).watch("pwc", {"pwc", "metlife"})
    ustore.for_uid(B_UID).set_watch_all(False)
    ustore.for_uid(C_UID).set_email_tailoring(["Pune"], [])
    others = {uid: ustore.backend.get(f"users/{uid}") for uid in (B_UID, C_UID)}
    b_companies = ustore.for_uid(B_UID).watchlist()
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS, page="settings")
    assert _label(at) == "Manoj"
    _save_name(at, "Manoj Kumar A")
    assert {uid: ustore.backend.get(f"users/{uid}") for uid in (B_UID, C_UID)} == others
    assert ustore.for_uid(B_UID).watchlist() == b_companies
    profiles = [p for p in ustore.backend.docs if p.count("/") == 1]
    assert sorted(profiles) == sorted(f"users/{u}" for u in (A_UID, B_UID, C_UID))


def test_a_url_cannot_point_the_editor_at_another_account(app, google_env, ustore):
    _ready(ustore, A_UID, A_MAIL)
    _ready(ustore, B_UID, B_MAIL)
    b_before = ustore.backend.get(f"users/{B_UID}")
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS, page="settings",
                  query={"uid": B_UID, "user": B_UID, "email": B_MAIL})
    _save_name(at, "Mallory")
    assert ustore.backend.get(f"users/{B_UID}") == b_before
    assert ustore.backend.get(f"users/{A_UID}")["preferred_name"] == "Mallory"


def test_companies_and_alerts_are_unchanged_after_a_rename(app, google_env, ustore):
    u = _ready(ustore, A_UID, A_MAIL, job_families=["Software engineering"], locations=["Pune"])
    u.watch("pwc", {"pwc", "metlife"})
    u.set_watch_all(False)
    u.set_notifications(True)
    u.set_email_tailoring(["Hyderabad"], ["Data & analytics"])
    u.dismiss({"some-job|https://x"})
    before = u.snapshot()
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS, page="settings")
    _save_name(at, "Ada")
    after = u.snapshot()
    assert after["watchlist"] == before["watchlist"] == {"pwc"}
    assert after["dismissed"] == before["dismissed"]
    drop = {"preferred_name", "last_login_at"}
    assert {k: v for k, v in after["profile"].items() if k not in drop} == \
           {k: v for k, v in before["profile"].items() if k not in drop}
    assert u.notification_settings() == {"enabled": True, "mode": "general", "email": A_MAIL}


def test_no_editor_without_personal_data(app, google_env):
    """Without the per-user store (e.g. the owner password session), there's
    nowhere to keep a name: the page shows no editor and nothing breaks."""
    from test_firebase_auth import EMAIL, GATED, FakeUser, NEW_RECORD, _fresh_google_token, ok_google_answer
    google_env["firebase"](ok_google_answer())
    google_env["set_user"](FakeUser(token=_fresh_google_token(), name="Manoj Anumolu"))
    at = app([NEW_RECORD], secrets=GATED, owner=False)
    _go(at, "settings")
    assert "btn_save_display_name" not in _buttons(at) and _label(at) == "Manoj"
