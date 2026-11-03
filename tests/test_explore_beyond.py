"""Home: Tailored Opportunities + Explore Beyond Your Preferences (Nov 2026).

The tailored list is the jobs inside the viewer's primary preferences (for
Account 1: the shared email's Hyderabad + three families). The explore list
is the other technical jobs the scanner already found and qualified on the
portals the viewer monitors. Nothing new is fetched and no email changes.

Data files live in tmp_path; Firestore is user_store.MemoryBackend."""
import json
from datetime import datetime, timezone

import pytest

import alerts
import discovery
import job_filters
from test_account1_monitoring import ACCOUNT1_TAILORING, SHOULD_SEND, shared  # noqa: F401
from test_account1_monitoring import rec as mon_rec
from test_admin_scopes import A_MAIL, A_UID, B_MAIL, B_UID, two_admins  # noqa: F401
from test_alerts import GATE, FakeRemote, Mailer
from test_firebase_auth import admins, google_env, no_env  # noqa: F401
from test_phase3_user_data import _sign_in, store  # noqa: F401
from test_streamlit_app import REPO, _companies, _html, _key, app  # noqa: F401
from test_ui_fixes_2026_11 import split  # noqa: F401

T = ACCOUNT1_TAILORING
_NOW = datetime.now(timezone.utc)
TODAY = f"{_NOW:%b} {_NOW.day}, {_NOW:%H:%M}"


def job(n, title, location, cid="accenture", company="Accenture", category="FRESHER", **extra):
    return {"title": title, "url": f"https://{cid}.example/job/{n}", "company": company, "company_id": cid,
            "location": location, "category": category, "reason": "experience starts at 0: '0-1 years'",
            "date": TODAY, "id": f"{cid}_{n}", "first_seen": _NOW.isoformat(),
            "evidence": {"checks": dict(GATE), "experience": ["0-1 years"]}, **extra}


HYD_SW = job(1, "Software Engineer - Fresher", "Hyderabad, Telangana, India")
BLR_SW = job(2, "Software Engineer - Fresher", "Bengaluru, Karnataka, India")
PUNE_GET = job(3, "Graduate Engineer Trainee", "Pune, Maharashtra, India")
BLR_EMB = job(4, "Embedded Firmware Engineer - New Grad", "Bengaluru, India")
HYD_VLSI = job(5, "ASIC Verification Engineer", "Hyderabad, India")
BLR_SENIOR = job(6, "Senior Software Engineer", "Bengaluru, India", category="SENIOR")
BLR_FIN = job(7, "Finance Associate - Accounts Payable", "Bengaluru, India")
BLR_SALES = job(8, "Inside Sales Representative", "Bengaluru, India")
BLR_ANALYST = job(9, "Analyst", "Bengaluru, India")
BLR_OPS_FIN = job(10, "Operations Finance Analyst", "Bengaluru, India")
REMOTE_QA = job(11, "QA Automation Engineer I", "Remote, India")
BLR_FAILED = job(12, "Software Engineer", "Bengaluru, India",
                 evidence={"checks": {**GATE, "detail_read": False}})   # a safety check failed
OTHERS_JOB = job(13, "Software Engineer - Fresher", "Chennai, India", cid="sanofi", company="Sanofi")


# ── the classification (no Streamlit) ────────────────────────────────────────

def test_hyderabad_software_fresher_is_tailored_not_explore():
    assert discovery.is_tailored(HYD_SW, T)
    assert discovery.beyond([HYD_SW], T) == []


@pytest.mark.parametrize("j, cat, why", [
    (BLR_SW, "Software Engineering", ["Different location"]),
    (PUNE_GET, "Software Engineering", ["Different location"]),
    (BLR_EMB, "Embedded Systems & Firmware", ["Different location", "Broader technical opportunity"]),
    (HYD_VLSI, "VLSI & Semiconductor Engineering", ["Broader technical opportunity"]),
    (REMOTE_QA, "Testing & Quality Assurance", ["Different location"]),
])
def test_other_locations_and_domains_reach_explore(j, cat, why):
    assert discovery.beyond([j], T) == [j]
    assert discovery.category(j) == cat and discovery.reasons(j, T) == why


def test_a_tailored_family_elsewhere_is_only_a_different_location():
    blr_data = job(20, "Associate Data Engineer", "Bengaluru, India")
    assert discovery.reasons(blr_data, T) == ["Different location"]


@pytest.mark.parametrize("j", [BLR_SENIOR, BLR_FAILED])
def test_senior_or_unqualified_jobs_are_rejected(j):
    assert not discovery.is_eligible(j) and discovery.beyond([j], T) == []


@pytest.mark.parametrize("j", [BLR_FIN, BLR_SALES, BLR_ANALYST, BLR_OPS_FIN,
                               job(21, "Content Marketing Analyst", "Bengaluru, India"),
                               job(22, "Customer Support Associate", "Pune, India"),
                               job(23, "Scientist I, Protein Biology", "Bengaluru, India")])
def test_non_technical_roles_are_not_technical(j):
    assert discovery.categories(j) == [] and discovery.beyond([j], T) == []


def test_technical_words_keep_a_mixed_title():
    assert discovery.category(job(24, "Sales Data Engineer", "Pune, India")) == "Data Engineering & Analytics"
    assert discovery.category(job(25, "Data Scientist - Computer Vision", "Pune, India")) == "AI & Machine Learning"


def test_without_preferences_there_is_nothing_to_go_beyond():
    assert discovery.beyond([BLR_SW], {}) == [] and discovery.beyond([BLR_SW], None) == []


def test_the_discovery_vocabulary():
    assert discovery.CATEGORIES == (
        "VLSI & Semiconductor Engineering", "Embedded Systems & Firmware", "AI & Machine Learning", "Cybersecurity",
        "Testing & Quality Assurance", "Cloud & DevOps", "Data Engineering & Analytics", "IT Support & Infrastructure",
        "Software Engineering", "Other Graduate Engineering Roles")
    assert set(discovery.LOCATIONS) == {"Hyderabad", "Bengaluru", "Pune", "Chennai", "Mumbai", "Delhi NCR", "Kolkata",
                                         "Ahmedabad", "Kochi", "Coimbatore", "Remote"}
    assert "Embedded Systems & Firmware" not in job_filters.JOB_FAMILIES      # the email vocabulary is unchanged


# ── the email alerts are untouched ───────────────────────────────────────────

def test_explore_jobs_never_enter_the_hyderabad_email(shared):  # noqa: F811
    outside = [mon_rec(30, "Software Engineer", location="Bengaluru, Karnataka, India"),
               mon_rec(31, "Graduate Engineer Trainee", location="Pune, India"),
               mon_rec(32, "ASIC Verification Engineer"),                 # Hyderabad, outside the three families
               mon_rec(33, "Embedded Firmware Engineer")]
    shared([dict(j) for j in SHOULD_SEND + outside], {"scan_interval_hours": 1, "shared_alert_tailoring": T})
    remote, mail = FakeRemote(), Mailer()
    assert alerts.run(send=mail, publisher=remote) == 0
    (sent,) = mail.sent                                    # the titles emailed
    assert sorted(sent) == sorted(j["title"] for j in SHOULD_SEND)


def test_the_alert_code_does_not_use_discovery():
    for name in ("alerts.py", "user_alerts.py", "notifier.py", "config_store.py", "job_filters.py", "scraper.py"):
        src = (REPO / "src" / name).read_text("utf-8")
        assert "import discovery" not in src and "discovery." not in src


# ── the Home page ────────────────────────────────────────────────────────────

SEEN = [HYD_SW, BLR_SW, PUNE_GET, BLR_EMB, HYD_VLSI, BLR_SENIOR, BLR_FIN, BLR_SALES, OTHERS_JOB]


def _with_tailoring(tmp_path, tailoring=T):
    (tmp_path / "data" / "settings.json").write_text(json.dumps({"scan_interval_hours": 1,
                                                                 "shared_alert_tailoring": tailoring}))


def _sections(html):
    i = html.index("Explore Beyond Your Preferences")
    return html[:i], html[i:]


def test_account1_home_splits_tailored_and_explore(app, google_env, store, two_admins, split, tmp_path):  # noqa: F811
    _with_tailoring(tmp_path)
    a = _sign_in(app, google_env, A_UID, A_MAIL, seen=SEEN)
    assert not a.exception
    html = _html(a)
    assert "Tailored Opportunities" in html and "Hyderabad — Software engineering · Data &amp; analytics" in html
    assert ("Discover fresher and entry-level opportunities from other locations, technical domains "
            "and companies you may have overlooked.") in html
    top, bottom = _sections(html)
    assert HYD_SW["url"] in top and HYD_SW["url"] not in bottom                  # each job in one section only
    for j in (BLR_SW, PUNE_GET, BLR_EMB, HYD_VLSI):
        assert j["url"] in bottom and j["url"] not in top
    for j in (BLR_SENIOR, BLR_FIN, BLR_SALES, OTHERS_JOB):                      # rejected, or Account 2's company
        assert j["url"] not in html
    assert "Broader technical opportunity" in bottom and "Different location" in bottom
    assert "only from the 2 portals you monitor" in bottom
    keys = [b.key for b in a.button if (b.key or "").startswith("crit_")]
    assert len(keys) == len(set(keys)) == 5


def test_explore_filters_narrow_by_area_and_location(app, google_env, store, two_admins, split, tmp_path):  # noqa: F811
    _with_tailoring(tmp_path)
    a = _sign_in(app, google_env, A_UID, A_MAIL, seen=SEEN)
    a.selectbox(key="x_cat").set_value("Embedded Systems & Firmware").run()
    _, bottom = _sections(_html(a))
    assert BLR_EMB["url"] in bottom and BLR_SW["url"] not in bottom
    a.selectbox(key="x_cat").set_value("all").run()
    a.selectbox(key="x_loc").set_value("Pune").run()
    _, bottom = _sections(_html(a))
    assert PUNE_GET["url"] in bottom and BLR_SW["url"] not in bottom


def test_explore_card_actions_work(app, google_env, store, two_admins, split, tmp_path):  # noqa: F811
    _with_tailoring(tmp_path)
    a = _sign_in(app, google_env, A_UID, A_MAIL, seen=SEEN)
    _, bottom = _sections(_html(a))
    assert f'href="{BLR_SW["url"]}"' in bottom                                  # Apply opens the posting
    a.button(key=_key("applied", BLR_SW)).click().run()                         # the Application Tracker form
    assert a.session_state.page == "applications" and a.text_input(key="atf_title").value == BLR_SW["title"]
    assert store.for_uid(A_UID).application_book(lambda: True).load() == []     # nothing saved until Save
    a.button(key="nav_home").click().run()
    a.button(key=_key("crit", BLR_SW)).click().run()                            # Details
    assert not a.exception and BLR_SW["title"] in _html(a)


def test_preferences_and_data_are_not_changed_by_viewing(app, google_env, store, two_admins, split, tmp_path):  # noqa: F811
    _with_tailoring(tmp_path)
    settings = (tmp_path / "data" / "settings.json").read_text("utf-8")
    a = _sign_in(app, google_env, A_UID, A_MAIL, seen=SEEN)
    before = json.dumps(store.backend.docs, sort_keys=True, default=str)
    a.selectbox(key="x_loc").set_value("Bengaluru").run()
    assert json.dumps(store.backend.docs, sort_keys=True, default=str) == before
    assert (tmp_path / "data" / "settings.json").read_text("utf-8") == settings
    assert _companies(a) == split
    assert json.loads((tmp_path / "data" / "seen_jobs.json").read_text("utf-8")) == SEEN


def test_account2_does_not_see_account1s_preferences(app, google_env, store, two_admins, split, tmp_path):  # noqa: F811
    _with_tailoring(tmp_path)
    b = _sign_in(app, google_env, B_UID, B_MAIL, seen=SEEN)
    html = _html(b)
    assert not b.exception
    # Account 2 saved no tailoring of its own: its Home is as before, with
    # none of the primary admin's shared-email preferences
    assert "Explore Beyond Your Preferences" not in html and "Tailored Opportunities" not in html
    assert "Hyderabad — Software engineering" not in html and A_MAIL not in html


def test_old_postings_stay_out_of_explore(app, google_env, store, two_admins, split, tmp_path):  # noqa: F811
    _with_tailoring(tmp_path)
    old = job(40, "Software Engineer - Fresher", "Mumbai, India", date="Jan 1, 00:00")
    a = _sign_in(app, google_env, A_UID, A_MAIL, seen=[HYD_SW, old])
    assert not a.exception
    html = _html(a)
    assert old["url"] not in _sections(html)[1]
