"""Nov 2026 fixes from daily use, with the regressions around them.

1. Sign out left the open account menu on top of the login page. Streamlit
   removes an earlier run's elements only when the next run finishes, so the
   menu stayed until the login page's run did (seconds, on Community Cloud).
   The account bar now sits in a placeholder that Sign out empties first.
   (The timing itself was checked in a real browser; AppTest has no DOM.)
2. The primary admin (Account 1) listed every catalogue company, including
   the five another admin (Account 2) monitors on their own ("shared_alerts":
   false). Account 1's Companies, Home, Monitoring, Settings and job lists
   now cover the shared alerts' companies only; the catalogue, the scanner
   and Account 2 are unchanged.
3. The Excel export becomes a report: a Summary sheet, then Applications
   and Interviews as Excel tables — every value kept in full.

Firestore is user_store.MemoryBackend; nothing reaches Google or Firebase."""
import json
from app_source import app_source  # noqa: E402  (streamlit_app.py + the modules it was split into)
import re
from html import unescape

import pytest

from test_admin_scopes import (  # noqa: F401  (fixture re-export)
    A_MAIL, A_UID, B_MAIL, B_UID, FIVE, FIVE_NAMES, M_MAIL, M_UID, _mon_table, _tile, rec, two_admins,
)
from test_firebase_auth import _buttons, _login_page, admins, google_env, no_env  # noqa: F401
from test_phase3_user_data import _sign_in, store  # noqa: F401
from test_streamlit_app import CATALOGUE_FILE, REPO, _companies, _html, _nav, app  # noqa: F401

SRC = app_source()
OWN = ("Accenture", "NPCI")                          # Account 1's own two of the frozen seven


@pytest.fixture
def split(tmp_path):
    """The frozen catalogue as production has it since 3 Nov 2026: the five
    left out of the shared alerts. Returns the catalogue written."""
    cat = [{**c, "shared_alerts": False} if c["id"] in FIVE else c
           for c in json.loads(CATALOGUE_FILE.read_text("utf-8"))]
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "companies.json").write_text(json.dumps(cat), "utf-8")
    return cat


def _jobs():
    return [rec("metlife", 1, company="MetLife"), rec("sanofi", 2, company="Sanofi"),
            rec("accenture", 3, company="Accenture"), rec("c1783687965", 4, company="NPCI")]


def _stat_value(html, label):
    i = html.index(f'<div class="k">{label}</div>')
    return re.search(r'<div class="v[^"]*">([^<]*)</div>', html[i:]).group(1)


def _card_names(html):
    return {unescape(n) for n in re.findall(r'<div class="t">([^<]+)', html)}


# ---------------------------------------------------------------------------
# 1. sign out
# ---------------------------------------------------------------------------

def test_sign_out_happens_before_the_page_is_drawn():
    """Sign out is the button's callback, so the run the click starts never
    draws the menu. It was done in the page's body: the menu was redrawn, then
    st.rerun() discarded the unsent message removing it, and the open menu
    (name and email) stayed on the next account's pages until a reload."""
    menu = SRC[SRC.index("def account_menu()"):SRC.index("# A new destination opens at its top.")]
    assert 'key="btn_account_sign_out"' in menu and "on_click=_menu_sign_out" in menu
    assert 'if st.button("Sign out"' not in menu and "account_sign_out()" not in menu and "bar.empty()" not in menu


def test_the_signed_in_bar_and_menu_never_show_next_to_the_sign_in_form():
    """What the signed-in page drew stays until the login page's run finishes."""
    rule = ('body:has(.st-key-login_card) :is(.st-key-acct_bar, [data-testid="stPopoverBody"]'
            ':has(.st-key-btn_account_sign_out)) { display: none !important; }')
    assert rule in SRC


@pytest.mark.parametrize("who", ["primary", "second", "member"])
def test_sign_out_shows_only_the_login_page(app, google_env, store, two_admins, who):
    uid, mail = {"primary": (A_UID, A_MAIL), "second": (B_UID, B_MAIL), "member": (M_UID, M_MAIL)}[who]
    at = _sign_in(app, google_env, uid, mail)
    assert "btn_account_sign_out" in _buttons(at)
    at.button(key="btn_account_sign_out").click().run()
    assert not at.exception and _login_page(at)
    html = _html(at)
    assert '<div class="acct-head">' not in html and mail not in html            # no menu, no account details
    assert not [b.key for b in at.button if (b.key or "").startswith(("acct_", "nav_", "btn_account"))]
    at.run()                                                         # the next run (a reload) stays signed out
    assert _login_page(at) and "btn_account_sign_out" not in _buttons(at)


def test_after_sign_out_the_next_account_signs_in_clean(app, google_env, store, two_admins, split):
    a = _sign_in(app, google_env, A_UID, A_MAIL, seen=_jobs())
    a.button(key="btn_account_sign_out").click().run()
    assert _login_page(a)
    b = _sign_in(app, google_env, B_UID, B_MAIL, seen=_jobs())
    html = _html(b)
    assert B_MAIL in html and A_MAIL not in html


# ---------------------------------------------------------------------------
# 2. Account 1 / Account 2 separation
# ---------------------------------------------------------------------------

def test_account1_companies_page_leaves_out_account2s_five(app, google_env, store, two_admins, split):
    a = _sign_in(app, google_env, A_UID, A_MAIL, seen=_jobs(), page="companies")
    html = _html(a)
    names = _card_names(html)
    assert set(OWN) <= names and not names & set(FIVE_NAMES)
    assert "2 career portals monitored" in html
    assert "All (2)" in str(a.pills(key="co_status").proto)          # the status filter counts the same two
    assert _companies(a) == split                                    # the catalogue itself is unchanged


def test_account1_counts_and_monitoring_cover_its_own_companies(app, google_env, store, two_admins, split):
    a = _sign_in(app, google_env, A_UID, A_MAIL, seen=_jobs())
    assert _stat_value(_html(a), "Companies monitored") == "2"        # Home
    _nav(a, "monitoring")
    table = _mon_table(a)
    assert table.count('<td class="c">') == 2 and not [n for n in FIVE_NAMES if n in table]
    assert "2 portals tracked" in _html(a)
    _nav(a, "settings")
    assert _tile(a, "Companies monitored") == "2"


def test_account1_job_lists_leave_out_account2s_companies(app, google_env, store, two_admins, split):
    jobs = _jobs()
    a = _sign_in(app, google_env, A_UID, A_MAIL, seen=jobs)
    home = _html(a)
    assert jobs[2]["title"] in home and jobs[3]["title"] in home
    assert jobs[0]["title"] not in home and jobs[1]["title"] not in home
    assert _stat_value(home, "Active jobs") == "2"
    _nav(a, "jobs")
    page = _html(a)
    assert jobs[0]["title"] not in page and jobs[1]["title"] not in page and "2 jobs found" in page


def test_account1_can_still_open_the_whole_catalogue(app, google_env, store, two_admins, split):
    """?view=all is the catalogue (every scanned company), as before."""
    a = _sign_in(app, google_env, A_UID, A_MAIL, seen=_jobs())
    a.session_state["page"], a.session_state["company_view"] = "companies", "all"
    a.run()
    names = _card_names(_html(a))
    assert set(FIVE_NAMES) | set(OWN) <= names


def test_account2_keeps_and_monitors_all_five(app, google_env, store, two_admins, split):
    for cid in FIVE:
        store.for_uid(B_UID).watch(cid, [c["id"] for c in split])
    jobs = _jobs()
    b = _sign_in(app, google_env, B_UID, B_MAIL, seen=jobs)
    feed = _html(b)
    assert jobs[0]["title"] in feed and jobs[1]["title"] in feed       # MetLife and Sanofi jobs
    assert jobs[2]["title"] not in feed                                # not Account 1's Accenture
    _nav(b, "monitoring")
    table = _mon_table(b)
    assert all(n in table for n in FIVE_NAMES) and table.count('<td class="c">') == 5
    _nav(b, "settings")
    assert _tile(b, "Companies monitored") == "5"
    assert store.for_uid(B_UID).watchlist() == set(FIVE)
    assert _companies(b) == split                                     # same URLs, scan settings and flags


def test_without_a_separated_company_account1_sees_the_whole_catalogue(app, google_env, store, two_admins):
    """No "shared_alerts": false anywhere (every deployment before Nov 2026): nothing changes."""
    a = _sign_in(app, google_env, A_UID, A_MAIL, page="companies")
    assert "7 career portals monitored" in _html(a)
    assert set(FIVE_NAMES) | set(OWN) <= _card_names(_html(a))


def test_the_shared_email_still_leaves_out_the_five(split):
    """Alert eligibility (unchanged): the five never reach Account 1's email."""
    from config_store import excluded_from_shared_alerts, shared_alert_pending
    excluded = excluded_from_shared_alerts(split)
    assert excluded == set(FIVE)
    for job in _jobs():
        assert shared_alert_pending(job, excluded) is (job["company_id"] not in FIVE)


# ---------------------------------------------------------------------------
# 3. the Excel export: Summary, Applications and Interviews, nothing lost
# ---------------------------------------------------------------------------

import io  # noqa: E402
import zipfile  # noqa: E402
from datetime import timedelta  # noqa: E402

import application_export as X  # noqa: E402
import applications as APPS  # noqa: E402
from test_application_tracker import _xlsx_cells, _xlsx_rows  # noqa: E402
from user_store import MemoryBackend, UserData  # noqa: E402

NOW = 1_793_000_000.0                                                   # 2026-10-27 (IST)
TODAY = APPS.today_ist(NOW)
LONG_NOTE = ("Applied after the campus talk; the first round is an online test with aptitude and two coding "
             "questions, then two technical rounds and HR. ") * 12
PREP = "SQL joins and window functions. " * 60


def _on(n):
    return (TODAY + timedelta(days=n)).isoformat()


def _docs():
    backend = MemoryBackend()
    a = UserData(backend, A_UID).application_book(lambda: True)
    b = UserData(backend, B_UID).application_book(lambda: True)
    base = {"location": "Hyderabad", "email_status": "No Email Received"}
    made = [a.create({**base, **r}, now=NOW) for r in (
        dict(company="Micron", title="Associate Engineer", progress="Interview", applied_on=_on(-5),
             email_status="Interview Email Received", follow_up=_on(-2), notes=LONG_NOTE, salary="₹8–10 LPA",
             url="https://micron.example/job/1"),
        dict(company="Infosys", title="Systems Engineer", progress="Offer Received", applied_on=_on(-20),
             follow_up=_on(0)),
        dict(company=TELUGU, title=HINDI, location=TELUGU_CITY, progress="Rejected", applied_on=_on(-40),
             notes=MIXED),
        dict(company="Bare Minimum Ltd", title="Trainee", progress="Applied", applied_on=_on(-1)),  # no optional field
        dict(company="Later Co", title="SDE", progress="Accepted", applied_on=_on(-3), follow_up=_on(9)),
    )]
    for i, outcome in enumerate(("Cleared", "Cleared", "Awaiting result")):
        a.save_interview(made[0]["id"], {"date": _on(i - 2), "time": "14:30", "round": f"Round {i + 1}",
                                         "type": "Online", "location": "https://meet.example/abc",
                                         "interviewer": "Priya", "prep": PREP, "feedback": "Good",
                                         "outcome": outcome}, now=NOW)
    gone = a.create({**base, "company": "Deleted Corp", "title": "X", "progress": "Applied", "applied_on": _on(-1)},
                    now=NOW)
    a.soft_delete(gone["id"], now=NOW)
    b.create({**base, "company": "Other Admin Corp", "title": "Theirs", "progress": "Applied", "applied_on": _on(-1)},
             now=NOW)
    return a.load(now=NOW)


TELUGU, TELUGU_CITY = "టెక్ మహీంద్రా", "హైదరాబాద్"
HINDI = "ग्रेजुएट इंजीनियर ट्रेनी"
MIXED = "Résumé ✓ 面试 \U0001f680"


def _part(data, name):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return z.read(name).decode("utf-8")


def _summary(data):
    return {r[0]: r[1] for r in _xlsx_cells(data, "Summary") if len(r) >= 2 and r[0]}


def test_the_workbook_has_a_summary_first_then_the_two_lists():
    data = X.to_xlsx(_docs(), "All applications", now=NOW)
    wb, t1, t2 = (_part(data, n) for n in ("xl/workbook.xml", "xl/tables/table1.xml", "xl/tables/table2.xml"))
    assert re.findall(r'<sheet name="(\w+)"', wb) == ["Summary", "Applications", "Interviews"]
    assert 'name="ApplicationsTable"' in t1 and 'ref="A4:Q9"' in t1 and 'showRowStripes="1"' in t1   # header + 5
    assert 'name="InterviewsTable"' in t2 and 'ref="A4:K7"' in t2                                   # header + 3 rounds
    assert wb.count("_xlnm.Print_Titles") == 2                       # each list's header repeats on printed pages
    for n in (2, 3):
        assert 'ySplit="4"' in _part(data, f"xl/worksheets/sheet{n}.xml")     # the header row stays in view


def test_the_summary_counts_only_the_exported_applications():
    data = X.to_xlsx(_docs(), "All applications", now=NOW)
    s = _summary(data)
    assert s["Total applications"] == "5"                                # not the deleted one, not Account 2's
    assert s["Applications with interviews"] == "1" and s["Interview rounds"] == "3"
    assert s["Offers received"] == "2" and s["Rejected applications"] == "1"     # Offer Received + Accepted
    assert s["Upcoming follow-ups"] == "2" and s["Overdue follow-ups"] == "1"    # today + in 9 days / 2 days ago
    assert s["Applied in the last 7 days"] == "3" and s["Applied in the last 30 days"] == "4"
    assert s["Interview"] == "1" and s["Withdrawn"] == "0" and s["Interview Email Received"] == "1"
    text = " ".join(" ".join(r) for r in _xlsx_cells(data, "Summary"))
    assert "Deleted Corp" not in text and "Other Admin Corp" not in text
    assert "Overdue" in text and "Due today" in text and "Upcoming" in text   # the open follow-ups list


def test_selected_or_filtered_exports_summarise_only_those():
    chosen = [d for d in _docs() if d.get("company") == "Infosys"]
    data = X.to_xlsx(chosen, "Selected applications", now=NOW)
    assert _summary(data)["Total applications"] == "1"
    assert [r[0] for r in _xlsx_rows(data)[1:]] == ["Infosys"]
    assert not any(any(c for c in r) for r in _xlsx_rows(data, "Interviews")[1:])   # no rounds: an empty table row
    assert "Selected applications" in _xlsx_cells(data, "Summary")[1][0]


def test_every_value_is_kept_in_full():
    data = X.to_xlsx(_docs(), "All applications", now=NOW)
    rows = _xlsx_rows(data)
    by_company = {r[0]: dict(zip(rows[0], r)) for r in rows[1:]}
    micron = by_company["Micron"]
    assert micron["Notes"] == LONG_NOTE.strip() and len(micron["Notes"]) > 1000
    assert micron["Salary / package"] == "₹8–10 LPA" and micron["Interviews"] == "3"
    assert micron["Follow-up status"] == "Overdue" and micron["Status"] == "Interview"
    other = by_company[TELUGU]
    assert other["Job title"] == HINDI and other["Location"] == TELUGU_CITY and other["Notes"] == MIXED
    bare = by_company["Bare Minimum Ltd"]
    assert bare["Notes"] == "" and bare["Job URL"] == "" and bare["Follow-up status"] == "" and bare["Interviews"] == "0"
    ivs = _xlsx_rows(data, "Interviews")
    assert [r[2] for r in ivs[1:]] == ["Round 1", "Round 2", "Round 3"]
    assert all(r[9] == PREP.strip() for r in ivs[1:])                      # long preparation notes kept
    assert [r[8] for r in ivs[1:]] == ["Cleared", "Cleared", "Awaiting result"]
    assert "https://micron.example/job/1" in _part(data, "xl/worksheets/_rels/sheet2.xml.rels")   # clickable
    assert "https://meet.example/abc" in _part(data, "xl/worksheets/_rels/sheet3.xml.rels")


def test_long_text_wraps_and_rows_grow_but_stay_navigable():
    sheet = _part(X.to_xlsx(_docs(), "All applications", now=NOW), "xl/worksheets/sheet2.xml")
    heights = [float(h) for r, h in re.findall(r'<row r="(\d+)" ht="([\d.]+)"', sheet) if int(r) > 4]
    assert max(heights) == X.MAX_LINES * 15 + 6 and min(heights) == 21  # the long note: capped, not 40 lines tall
    widths = [float(w) for w in re.findall(r'<col [^>]*width="([\d.]+)"', sheet)]
    assert max(widths) <= 50 and sum(widths) < 360                       # the old sheet was 419 characters wide


def test_statuses_and_follow_ups_are_coloured():
    sheet = _part(X.to_xlsx(_docs(), "All applications", now=NOW), "xl/worksheets/sheet2.xml")
    for tone in ("amber", "green", "red", "blue", "teal"):
        assert f's="{X.S["tone_" + tone]}"' in sheet, tone
    assert set(X.PROGRESS_TONE) == set(APPS.PROGRESS) and set(X.EMAIL_TONE) == set(APPS.EMAIL_STATUSES)
    assert set(X.OUTCOME_TONE) == set(APPS.INTERVIEW_OUTCOMES)


def test_an_export_from_each_admin_has_only_their_applications():
    backend = MemoryBackend()
    for uid, company in ((A_UID, "Alpha"), (B_UID, "Bravo")):
        UserData(backend, uid).application_book(lambda: True).create(
            {"company": company, "title": "SDE", "location": "Pune", "progress": "Applied",
             "email_status": "No Email Received", "applied_on": _on(-1)}, now=NOW)
    for uid, mine, theirs in ((A_UID, "Alpha", "Bravo"), (B_UID, "Bravo", "Alpha")):
        docs = UserData(backend, uid).application_book(lambda: True).load(now=NOW)
        data = X.to_xlsx(docs, "All applications", now=NOW)
        text = " ".join(" ".join(r) for s in ("Summary", "Applications", "Interviews") for r in _xlsx_cells(data, s))
        assert mine in text and theirs not in text


def test_the_pdf_export_still_works():
    data = X.to_pdf(_docs(), "All applications", now=NOW)
    assert data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF")
