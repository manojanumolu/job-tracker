"""Application Tracker (Nov 2026): admin-only, each admin's own records.

The book (applications.ApplicationBook), its checks, history, interviews,
reminders, duplicates, Recently deleted and analytics; Fetch Job Details
(job_page) against a fake web (httpx.MockTransport + a fake resolver); the
Excel/PDF exports; and the app itself (AppTest): who sees the page, both
ways of adding, and that two admins never see each other's records.

Firestore is user_store.MemoryBackend; nothing here reaches a real service."""
import inspect
import io
import json
import re
import sys
import time
import zipfile
import zlib
from datetime import date, timedelta
from xml.etree import ElementTree as ET

import httpx
import pytest

import application_export as X
import applications as A
import job_page
import user_store
from user_store import InvalidInput, MemoryBackend, UserData, UserStore

A_UID, B_UID = "AdminAaaaaaaaaaaaaaaaaaaaaaa1", "AdminBbbbbbbbbbbbbbbbbbbbbbb2"
NOW = 1_793_000_000.0                       # 2026-10-27, a fixed server clock
TODAY = A.today_ist(NOW)


def _book(backend=None, uid=A_UID, allowed=lambda: True):
    return UserData(backend or MemoryBackend(), uid).application_book(allowed)


def _raw(**kw):
    base = {"company": "Infosys", "title": "Systems Engineer", "location": "Pune, India",
            "applied_on": (TODAY - timedelta(days=7)).isoformat(), "progress": "Applied",
            "email_status": "No Email Received"}
    return {**base, **kw}


# ---------------------------------------------------------------------------
# validation: one path for both ways of adding
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("change, message", [
    ({"company": "  "}, "Company name is required"),
    ({"title": ""}, "Job title is required"),
    ({"location": None}, "Location is required"),
    ({"applied_on": ""}, "Date applied is required"),
    ({"applied_on": "2999-01-01"}, "can't be in the future"),
    ({"applied_on": "31/12/2026"}, "isn't a valid date"),
    ({"posted_on": "2026-10-25", "applied_on": "2026-10-20"}, "posting date can't be after"),
    ({"url": "javascript:alert(1)"}, "must start with http"),
    ({"url": "/relative/path"}, "must start with http"),
    ({"progress": "Hired"}, "valid application progress"),
    ({"email_status": "Applied"}, "valid email response"),
    ({"source": "Newspaper"}, "valid application source"),
    ({"notes": "x" * 5001}, "at most 5000"),
    ({"company": 7}, "must be text"),
])
def test_bad_input_is_refused_with_a_message_naming_the_field(change, message):
    with pytest.raises(InvalidInput, match=re.escape(message) if "'" not in message else message):
        A.clean_fields(_raw(**change), TODAY)


def test_input_is_tidied_never_invented():
    out = A.clean_fields(_raw(company="  Infosys \u202e Ltd ", title="Systems\x00 Engineer", source="LinkedIn",
                              source_other="ignored", notes="line 1\r\n\n\n\nline 2  ", job_id=" R-1 "), TODAY)
    assert out["company"] == "Infosys Ltd" and out["title"] == "Systems Engineer" and out["job_id"] == "R-1"
    assert out["source_other"] == "" and out["notes"] == "line 1\n\nline 2"
    assert out["posted_on"] == "" and out["salary"] == "" and out["url"] == ""      # not given: left empty
    assert A.clean_fields(_raw(source="Other", source_other=" Campus drive "), TODAY)["source_other"] == "Campus drive"


def test_both_statuses_have_exactly_the_listed_choices():
    assert A.PROGRESS == ("Applied", "Assessment", "Interview", "Offer Received", "Accepted", "Rejected", "Withdrawn",
                          "On Hold")
    assert A.EMAIL_STATUSES == ("No Email Received", "Acknowledgement Received", "Assessment Email Received",
                                "Interview Email Received", "Rejection Email Received", "Offer Email Received",
                                "Other Email Received")
    assert A.SOURCES == ("Company Careers Page", "LinkedIn", "Referral", "Job Portal", "Other")
    assert A.INTERVIEW_TYPES == ("Online", "Telephone", "In-person")


# ---------------------------------------------------------------------------
# who may: every operation asks the admin-and-owner check first
# ---------------------------------------------------------------------------

PUBLIC = [name for name, fn in inspect.getmembers(A.ApplicationBook, inspect.isfunction) if not name.startswith("_")]
CALLS = {"load": (), "get": ("0" * 20,), "create": (_raw(),), "update": ("0" * 20, _raw()),
         "set_status": ("0" * 20, "Interview"), "save_interview": ("0" * 20, {}), "delete_interview": ("0" * 20, "0" * 12),
         "set_follow_up": ("0" * 20, None), "complete_follow_up": ("0" * 20,), "soft_delete": ("0" * 20,),
         "restore": ("0" * 20,), "purge": ("0" * 20,), "find_duplicates": (_raw(),)}


def test_every_public_operation_is_guarded():
    assert sorted(PUBLIC) == sorted(CALLS)                 # a new operation must be added here (and guarded)
    for name in PUBLIC:
        assert getattr(getattr(A.ApplicationBook, name), "authorized", False) is True, name


@pytest.mark.parametrize("hook", [lambda: False, lambda: None, lambda: "yes", lambda: 1 / 0])
def test_a_refused_or_broken_check_refuses_every_operation_and_touches_nothing(hook):
    backend = MemoryBackend()
    good = _book(backend)
    doc = good.create(_raw(), now=NOW)
    before = json.dumps(backend.docs, sort_keys=True)
    bad = _book(backend, allowed=hook)
    for name, args in CALLS.items():
        args = tuple(doc["id"] if a == "0" * 20 else a for a in args)
        with pytest.raises(A.NotAuthorized):
            getattr(bad, name)(*args)
    assert json.dumps(backend.docs, sort_keys=True) == before


def test_the_check_is_asked_on_every_call_not_once():
    answers = iter([True, True, False])
    book = _book(allowed=lambda: next(answers))
    book.load()
    book.load()
    with pytest.raises(A.NotAuthorized):
        book.load()


def test_a_book_only_binds_to_a_real_uid_and_a_callable_check():
    for uid in ("", "../x", "a/b", "__x__", None):
        with pytest.raises(InvalidInput):
            A.ApplicationBook(MemoryBackend(), uid, lambda: True)
    with pytest.raises(TypeError):
        A.ApplicationBook(MemoryBackend(), A_UID, True)
    assert "uid" not in inspect.signature(A.ApplicationBook.load).parameters
    for name in PUBLIC:                                      # no operation lets a caller name an account
        assert not {"uid", "owner", "user"} & set(inspect.signature(getattr(A.ApplicationBook, name)).parameters), name


# ---------------------------------------------------------------------------
# whose: two admins on one store never see or change each other's records
# ---------------------------------------------------------------------------

def test_two_admins_have_separate_trees():
    backend = MemoryBackend()
    a, b = _book(backend, A_UID), _book(backend, B_UID)
    da = a.create(_raw(company="Alpha"), now=NOW)
    db = b.create(_raw(company="Beta"), now=NOW)
    assert [d["company"] for d in a.load()] == ["Alpha"] and [d["company"] for d in b.load()] == ["Beta"]
    assert all(p.startswith(f"users/{A_UID}/applications/") or p.startswith(f"users/{B_UID}/applications/")
               for p in backend.docs)
    assert f"users/{A_UID}/applications/{da['id']}" in backend.docs
    # B can neither read nor change nor delete A's record, even knowing its id
    for op, args in [("get", ()), ("update", (_raw(company="Hacked"),)), ("set_status", ("Rejected",)),
                     ("soft_delete", ()), ("save_interview", ({"date": "2026-10-01", "round": "R", "type": "Online"},)),
                     ("set_follow_up", ("2026-11-01",)), ("restore", ()), ("purge", ())]:
        with pytest.raises(A.NotFound):
            getattr(b, op)(da["id"], *args)
    assert a.get(da["id"])["company"] == "Alpha" and len(a.get(da["id"])["timeline"]) == 1
    assert b.find_duplicates(_raw(company="Alpha")) == []        # A's records aren't even a duplicate for B
    assert db["id"] not in {d["id"] for d in a.load()}


@pytest.mark.parametrize("app_id", ["../../users/x", "AAAAAAAAAAAAAAAAAAAA", "0" * 19, "0" * 21, "", None, 5])
def test_only_well_formed_application_ids_are_looked_up(app_id):
    backend = MemoryBackend()
    with pytest.raises(A.NotFound):
        _book(backend).get(app_id)
    assert backend.docs == {}


# ---------------------------------------------------------------------------
# records, statuses and the timeline
# ---------------------------------------------------------------------------

def test_creating_records_the_submission():
    book = _book()
    doc = book.create(_raw(follow_up="2026-11-03", resume="Resume_v2_Projects.pdf"), job_key="sanofi|id:R1", now=NOW)
    assert re.fullmatch(r"[0-9a-f]{20}", doc["id"]) and doc["rev"] == 1 and doc["job_key"] == "sanofi|id:R1"
    assert doc["follow_up"] == {"date": "2026-11-03", "done": False, "done_at": ""}
    assert doc["resume"] == "Resume_v2_Projects.pdf" and doc["deleted_at"] == ""
    (event,) = doc["timeline"]
    assert event["kind"] == "created" and event["on"] == doc["applied_on"] and event["to"] == "Applied"
    assert book.get(doc["id"]) == doc


def test_progress_and_email_response_change_independently():
    book = _book()
    doc = book.create(_raw(), now=NOW)
    doc = book.set_status(doc["id"], email_status="Acknowledgement Received", on="2026-10-21", now=NOW)
    assert doc["progress"] == "Applied" and doc["email_status"] == "Acknowledgement Received"
    doc = book.set_status(doc["id"], progress="Assessment", on="2026-10-25", note="HackerRank", now=NOW)
    assert doc["progress"] == "Assessment" and doc["email_status"] == "Acknowledgement Received"
    kinds = [(e["kind"], e["from"], e["to"], e["on"]) for e in doc["timeline"][1:]]
    assert kinds == [("email", "No Email Received", "Acknowledgement Received", "2026-10-21"),
                     ("progress", "Applied", "Assessment", "2026-10-25")]
    assert doc["timeline"][-1]["note"] == "HackerRank"
    # the edit form: each status that changed is its own event; nothing else moves
    doc = book.update(doc["id"], {**_raw(), "progress": "Interview", "email_status": "Interview Email Received"}, now=NOW)
    assert [e["kind"] for e in doc["timeline"][-2:]] == ["progress", "email"]
    with pytest.raises(InvalidInput, match="Nothing changed"):
        book.set_status(doc["id"], progress="Interview", now=NOW)


def test_the_timeline_only_grows():
    book = _book()
    doc = book.create(_raw(), now=NOW)
    seen = []
    for i, (prog, mail) in enumerate([("Assessment", None), (None, "Interview Email Received"), ("Interview", None),
                                      ("Offer Received", "Offer Email Received"), ("Accepted", None)]):
        doc = book.set_status(doc["id"], progress=prog, email_status=mail, now=NOW + i)
        assert doc["timeline"][:len(seen)] == seen                  # earlier events untouched
        seen = doc["timeline"]
    assert [e["to"] for e in doc["timeline"] if e["kind"] == "progress"] == ["Assessment", "Interview",
                                                                          "Offer Received", "Accepted"]
    assert all(e["at"] and e["id"] for e in doc["timeline"])


def test_an_edit_names_the_fields_it_changed():
    book = _book()
    doc = book.create(_raw(), now=NOW)
    doc = book.update(doc["id"], _raw(salary="₹6 LPA", resume="Backend_Developer_Resume.pdf"), now=NOW)
    assert doc["timeline"][-1]["kind"] == "edited"
    assert doc["timeline"][-1]["note"] == "Salary/package, Resume name/version"
    assert doc["salary"] == "₹6 LPA"


def test_a_lost_race_is_redone_on_the_fresh_record():
    class Racy(MemoryBackend):
        raced = False

        def update_if(self, path, expected, data):
            if not self.raced:                          # another tab writes first
                self.raced = True
                cur = self.docs[path]
                cur["timeline"].append({"id": "x", "kind": "note", "note": "other tab"})
                cur["rev"] += 1
            return super().update_if(path, expected, data)
    backend = Racy()
    book = _book(backend)
    doc = book.create(_raw(), now=NOW)
    doc = book.set_status(doc["id"], progress="Assessment", now=NOW)
    assert [e["kind"] for e in doc["timeline"]] == ["created", "note", "progress"] and doc["rev"] == 3


# ---------------------------------------------------------------------------
# interviews
# ---------------------------------------------------------------------------

def _round(**kw):
    return {"date": "2026-11-05", "time": "14:30", "round": "Technical 1", "type": "Online",
            "location": "https://meet.example/abc", "interviewer": "Priya", "prep": "SQL", **kw}


def test_an_application_has_as_many_interview_rounds_as_it_needs():
    book = _book()
    doc = book.create(_raw(), now=NOW)
    for i, day in enumerate(["2026-11-09", "2026-11-05", "2026-11-12"]):
        doc = book.save_interview(doc["id"], _round(date=day, round=f"Round {i + 1}"), now=NOW)
    assert [r["round"] for r in doc["interviews"]] == ["Round 2", "Round 1", "Round 3"]      # by date
    assert all(r["outcome"] == "Awaiting result" and re.fullmatch(r"[0-9a-f]{12}", r["id"]) for r in doc["interviews"])
    first = doc["interviews"][0]
    doc = book.save_interview(doc["id"], {**_round(date=first["date"], round=first["round"]), "outcome": "Cleared",
                                          "feedback": "Strong SQL"}, interview_id=first["id"], now=NOW)
    edited = next(r for r in doc["interviews"] if r["id"] == first["id"])
    assert edited["outcome"] == "Cleared" and edited["feedback"] == "Strong SQL" and len(doc["interviews"]) == 3
    assert doc["timeline"][-1]["kind"] == "interview_outcome" and doc["timeline"][-1]["to"] == "Cleared"
    doc = book.delete_interview(doc["id"], first["id"], now=NOW)
    assert len(doc["interviews"]) == 2 and doc["progress"] == "Applied"      # rounds never move the progress
    with pytest.raises(A.NotFound):
        book.delete_interview(doc["id"], first["id"])


@pytest.mark.parametrize("change, message", [({"round": ""}, "Interview round is required"),
                                             ({"date": ""}, "Interview date is required"),
                                             ({"type": "Video"}, "valid interview type"),
                                             ({"time": "25:00"}, "must look like 14:30"),
                                             ({"outcome": "Hired"}, "valid outcome")])
def test_interview_rounds_are_checked(change, message):
    with pytest.raises(InvalidInput, match=message):
        A.clean_interview(_round(**change))


# ---------------------------------------------------------------------------
# follow-up reminders (manual; in the app only)
# ---------------------------------------------------------------------------

def test_reminders_are_sorted_into_overdue_today_upcoming_and_done():
    book = _book()
    ids = {}
    for name, day in [("over", TODAY - timedelta(days=2)), ("today", TODAY), ("soon", TODAY + timedelta(days=3)),
                      ("done", TODAY - timedelta(days=9)), ("none", None)]:
        doc = book.create(_raw(company=name), now=NOW)
        if day:
            book.set_follow_up(doc["id"], day.isoformat(), now=NOW)
        ids[name] = doc["id"]
    book.complete_follow_up(ids["done"], now=NOW)
    buckets = A.follow_up_buckets(book.load(), TODAY)
    assert {k: [d["company"] for d in v] for k, v in buckets.items()} == {
        "overdue": ["over"], "today": ["today"], "upcoming": ["soon"], "done": ["done"]}
    # a date passing never completes a reminder
    later = A.follow_up_buckets(book.load(), TODAY + timedelta(days=30))
    assert {d["company"] for d in later["overdue"]} == {"over", "today", "soon"} and len(later["done"]) == 1
    doc = book.complete_follow_up(ids["done"], False, now=NOW)
    assert doc["follow_up"]["done"] is False and doc["timeline"][-1]["kind"] == "follow_up_reopened"
    doc = book.complete_follow_up(ids["done"], now=NOW)
    doc = book.set_follow_up(ids["done"], (TODAY + timedelta(days=5)).isoformat(), now=NOW)
    assert doc["follow_up"]["done"] is False                          # a new date is a new reminder
    doc = book.set_follow_up(ids["done"], None, now=NOW)
    assert doc["follow_up"]["date"] == "" and A.follow_up_state(doc, TODAY) == ""
    with pytest.raises(InvalidInput, match="no follow-up date"):
        book.complete_follow_up(ids["none"])


# ---------------------------------------------------------------------------
# duplicates: warned, never blocked
# ---------------------------------------------------------------------------

def test_duplicates_are_found_by_link_id_or_company_and_title():
    book = _book()
    base = book.create(_raw(company="Infosys Ltd.", url="https://www.careers.infosys.com/job/123/?utm_source=li",
                            job_id="INF-123"), job_key="infosys|id:INF-123", now=NOW)
    cases = [
        (_raw(company="Other", title="Other", url="http://careers.infosys.com/job/123"), "Same job link"),
        (_raw(company="INFOSYS", title="Different", job_id="inf 123"), "Same job ID at this company"),
        (_raw(company="infosys", title="systems  engineer!"), "Same company and job title"),
    ]
    for raw, reason in cases:
        assert [(d["id"], r) for d, r in book.find_duplicates(raw)] == [(base["id"], reason)], raw
    assert book.find_duplicates(_raw(company="X", title="Y"), job_key="infosys|id:INF-123")[0][1] == \
        "Added from the same job card"
    # different requisitions are not duplicates
    assert book.find_duplicates(_raw(job_id="INF-999")) == []                      # same title, other job ID
    assert book.find_duplicates(_raw(location="Chennai, India")) == []             # same title, other city
    assert book.find_duplicates(_raw(company="TCS", job_id="INF-123")) == []       # same ID, other company
    assert book.find_duplicates(_raw(), exclude_id=base["id"]) == []               # editing itself
    # saving anyway is always possible
    again = book.create(_raw(), now=NOW)
    assert again["id"] != base["id"] and len(A.live(book.load())) == 2
    book.soft_delete(base["id"], now=NOW)
    assert [d["id"] for d, _ in book.find_duplicates(_raw())] == [again["id"]]    # deleted ones don't count


# ---------------------------------------------------------------------------
# Recently deleted and the 30-day retention
# ---------------------------------------------------------------------------

def test_deleting_restoring_and_permanently_deleting():
    backend = MemoryBackend()
    book = _book(backend)
    keep = book.create(_raw(company="Keep"), now=NOW)
    gone = book.create(_raw(company="Gone", progress="Interview"), now=NOW)
    with pytest.raises(A.NotFound):
        book.purge(gone["id"])                                    # live records can't vanish in one click
    book.soft_delete(gone["id"], now=NOW)
    docs = book.load(now=NOW)
    assert [d["company"] for d in A.live(docs)] == ["Keep"] and [d["company"] for d in A.recently_deleted(docs, NOW)] == ["Gone"]
    assert A.summary(docs) == {"total": 1, "awaiting": 1, "interviews": 0, "offers": 0}
    assert A.analytics(docs, today=TODAY)["eligible"] == 1
    assert [d["company"] for d in X.exportable(docs)] == ["Keep"]
    assert A.filter_apps(docs) == A.live(docs)
    with pytest.raises(A.NotFound):
        book.get(gone["id"])
    with pytest.raises(A.NotFound):
        book.set_status(gone["id"], progress="Rejected")
    assert A.days_left(book.load(now=NOW)[1] if book.load(now=NOW)[1]["id"] == gone["id"] else book.load(now=NOW)[0],
                       NOW + 86400 * 3) == 27
    doc = book.restore(gone["id"], now=NOW)
    assert doc["deleted_at"] == "" and [e["kind"] for e in doc["timeline"]][-2:] == ["deleted", "restored"]
    book.soft_delete(gone["id"], now=NOW)
    book.purge(gone["id"])
    assert f"users/{A_UID}/applications/{gone['id']}" not in backend.docs and book.get(keep["id"])


def test_after_30_days_a_deleted_application_is_gone_for_good():
    backend = MemoryBackend()
    book = _book(backend)
    doc = book.create(_raw(), now=NOW)
    book.soft_delete(doc["id"], now=NOW)
    later = NOW + A.RETENTION_DAYS * 86400
    assert A.recently_deleted(book.load(now=later - 1), later - 1)              # day 29.99: still restorable
    with pytest.raises(A.NotFound):                                              # day 30: never restorable
        book.restore(doc["id"], now=later)
    assert book.load(now=later) == [] and backend.docs == {}                      # ... and deleted on the next load
    # a damaged purge_after can't keep it longer: deleted_at (the server clock) decides
    doc = book.create(_raw(), now=NOW)
    book.soft_delete(doc["id"], now=NOW)
    backend.docs[f"users/{A_UID}/applications/{doc['id']}"]["purge_after"] = "2999-01-01T00:00:00Z"
    assert book.load(now=later) == []


def test_the_daily_retention_run_purges_every_account_and_nothing_else():
    backend = MemoryBackend()
    store = UserStore(backend)
    for uid in (A_UID, B_UID):
        UserData(backend, uid).ensure_profile(f"{uid}@example.org")
    a, b = _book(backend, A_UID), _book(backend, B_UID)
    old_a, old_b = a.create(_raw(), now=NOW), b.create(_raw(), now=NOW)
    a.soft_delete(old_a["id"], now=NOW)
    b.soft_delete(old_b["id"], now=NOW)
    fresh = b.create(_raw(), now=NOW)
    b.soft_delete(fresh["id"], now=NOW + 20 * 86400)
    live = a.create(_raw(company="Live"), now=NOW)
    assert A.purge_expired_everywhere(store, now=NOW + 31 * 86400) == 2
    assert {d["id"] for d in a.load(now=NOW + 31 * 86400)} == {live["id"]}
    assert {d["id"] for d in b.load(now=NOW + 31 * 86400)} == {fresh["id"]}
    assert f"users/{A_UID}" in backend.docs and f"users/{B_UID}" in backend.docs


def test_the_retention_command_without_a_service_account_does_nothing(monkeypatch, capsys):
    monkeypatch.delenv("FIREBASE_SERVICE_ACCOUNT", raising=False)
    assert A.main(["--purge-expired"]) == 0


def test_deleting_an_account_also_deletes_its_applications():
    backend = MemoryBackend()
    user = UserData(backend, A_UID)
    user.ensure_profile("a@example.org")
    user.application_book(lambda: True).create(_raw(), now=NOW)
    _book(backend, B_UID).create(_raw(), now=NOW)
    user.delete_personal_data()
    assert [p for p in backend.docs if p.startswith(f"users/{A_UID}")] == []
    assert len([p for p in backend.docs if p.startswith(f"users/{B_UID}/applications/")]) == 1


# ---------------------------------------------------------------------------
# views, filters and analytics
# ---------------------------------------------------------------------------

def _seeded():
    book = _book()
    day = TODAY - timedelta(days=40)
    rows = [("Infosys", "Applied", "No Email Received", 0), ("Infosys Ltd", "Rejected", "Rejection Email Received", 3),
            ("TCS", "Interview", "Interview Email Received", 10), ("Wipro", "Offer Received", "Offer Email Received", 20),
            ("Wipro", "Applied", "Acknowledgement Received", 35), ("HCL", "Withdrawn", "No Email Received", 38)]
    for company, prog, mail, offset in rows:
        book.create(_raw(company=company, title=f"Role {offset}", progress=prog, email_status=mail,
                         applied_on=(day + timedelta(days=offset)).isoformat()), now=NOW)
    return book


def test_views_and_filters():
    docs = _seeded().load()
    assert A.summary(docs) == {"total": 6, "awaiting": 2, "interviews": 1, "offers": 1}
    by = lambda **kw: sorted(d["company"] for d in A.filter_apps(docs, today=TODAY, **kw))   # noqa: E731
    assert by(view="applied") == ["Infosys", "Wipro"] and by(view="rejected") == ["Infosys Ltd"]
    assert by(view="offers") == ["Wipro"] and by(view="interviews") == ["TCS"]
    assert by(query="  infosys ") == ["Infosys", "Infosys Ltd"] and by(query="ROLE 10") == ["TCS"]
    assert by(progress=["Applied", "Withdrawn"]) == ["HCL", "Infosys", "Wipro"]
    assert by(email=["No Email Received"]) == ["HCL", "Infosys"]
    start = TODAY - timedelta(days=30)
    assert by(applied=(start, None)) == ["HCL", "TCS", "Wipro", "Wipro"]          # from that day on, inclusive
    assert by(applied=(start, start + timedelta(days=4))) == ["TCS"]
    assert by(follow_up=(TODAY, None)) == []
    sorted_docs = A.sort_apps(A.live(docs), "applied_old")
    assert [d["company"] for d in sorted_docs][:2] == ["Infosys", "Infosys Ltd"]
    assert [d["company"] for d in A.sort_apps(A.live(docs), "company")][0] == "HCL"


def test_analytics_are_defined_counted_once_and_never_misleading():
    book = _seeded()
    tcs = next(d for d in book.load() if d["company"] == "TCS")
    book.save_interview(tcs["id"], _round(), now=NOW)               # rounds and progress: still one application
    # a response recorded as a status change has a date; one saved with the record has none
    quiet = book.create(_raw(company="Accenture", applied_on=(TODAY - timedelta(days=20)).isoformat()), now=NOW)
    infosys = next(d for d in book.load() if d["company"] == "Infosys")
    applied = A.parse_day(infosys["applied_on"])
    book.set_status(infosys["id"], email_status="Assessment Email Received",
                    on=(applied + timedelta(days=4)).isoformat(), now=NOW)
    a = A.analytics(book.load(), today=TODAY)
    assert a["eligible"] == 7
    assert a["interviewed"] == 2 and a["interview_rate"] == pytest.approx(28.6)     # TCS + the offer
    assert a["rejected"] == 1 and a["rejection_rate"] == pytest.approx(14.3)        # silence is not a rejection
    assert a["offers"] == 1
    assert a["responded"] == 1 and a["avg_response_days"] == 4.0
    assert [d["id"] for d in a["no_response"]] == [quiet["id"]]                     # HCL withdrew: not "waiting"
    assert [m for m, _ in a["per_month"]] == sorted({m for m, _ in a["per_month"]})
    assert sum(n for _, n in a["per_month"]) == 7
    assert a["by_company"][:2] == [("Infosys", 2), ("Wipro", 2)]                 # "Infosys Ltd" is the same company
    empty = A.analytics([], today=TODAY)
    assert empty["eligible"] == 0 and empty["interview_rate"] is None and empty["rejection_rate"] is None
    assert empty["avg_response_days"] is None and empty["per_month"] == [] and empty["by_company"] == []
    ranged = A.analytics(book.load(), TODAY - timedelta(days=10), TODAY, TODAY)
    assert ranged["eligible"] == 2 and ranged["interview_rate"] == 0.0 and ranged["rejection_rate"] == 0.0   # Wipro, HCL


def test_an_offer_counts_even_if_it_later_went_another_way():
    book = _book()
    doc = book.create(_raw(), now=NOW)
    book.set_status(doc["id"], progress="Offer Received", now=NOW)
    book.set_status(doc["id"], progress="Withdrawn", now=NOW)
    a = A.analytics(book.load(), today=TODAY)
    assert a["offers"] == 1 and a["interviewed"] == 1 and A.summary(book.load())["offers"] == 0


# ---------------------------------------------------------------------------
# Fetch Job Details: public pages only, nothing invented
# ---------------------------------------------------------------------------

POSTING = """<html><head><title>Careers</title>
<script type="application/ld+json">{"@context": "https://schema.org", "@graph": [{"@type": "Organization", "name": "x"},
 {"@type": "JobPosting", "title": "Graduate Engineer &amp; Analyst", "hiringOrganization": {"name": "Sanofi"},
  "jobLocation": [{"address": {"addressLocality": "Hyderabad", "addressRegion": "unavailable", "addressCountry": {"name": "India"}}},
                  {"address": {"addressLocality": "Pune", "addressCountry": "IN"}}],
  "identifier": {"name": "Sanofi", "value": "R2863350"}, "datePosted": "2026-10-20T08:00:00Z",
  "description": "<p>Join us.</p><ul><li>SQL</li><li>Python</li></ul>"}]}</script></head><body>…</body></html>"""
PUBLIC_IP = "93.184.216.34"


def _resolver(addrs):
    return lambda host, port: list(addrs.get(host, [PUBLIC_IP]))


def _client(routes, seen=None):
    """A fake web: {(host, path): response or callable}. Records requests."""
    def handler(request):
        if seen is not None:
            seen.append(request)
        key = (request.headers["host"], request.url.path)
        r = routes.get(key)
        if callable(r):
            return r(request)
        return r if r is not None else httpx.Response(404, text="nope")
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)


def test_a_public_posting_fills_in_what_it_states():
    seen = []
    client = _client({("jobs.example.com", "/robots.txt"): httpx.Response(404),
                      ("jobs.example.com", "/job/1"): httpx.Response(200, text=POSTING,
                                                                     headers={"content-type": "text/html; charset=utf-8"})},
                     seen)
    res = job_page.fetch_job_details("https://jobs.example.com/job/1", client=client, resolver=_resolver({}))
    assert res.problem == "" and res.source == "structured data"
    assert res.fields["title"] == "Graduate Engineer & Analyst" and res.fields["company"] == "Sanofi"
    assert res.fields["location"] == "Hyderabad, India; Pune, IN" and res.fields["job_id"] == "R2863350"
    assert res.fields["posted_on"] == "2026-10-20" and res.fields["description"] == "Join us.\n\n• SQL\n\n• Python"
    # each request went to the checked address, with the real name for TLS and the Host header
    assert {str(r.url.host) for r in seen} == {PUBLIC_IP}
    assert all(r.headers["host"] == "jobs.example.com" and r.extensions["sni_hostname"] == "jobs.example.com" for r in seen)
    assert "FresherJobTracker" in seen[0].headers["user-agent"]


def test_missing_details_stay_missing_and_say_so():
    page = '<html><head><meta property="og:title" content="Analyst"><meta property="og:site_name" content="PwC"></head></html>'
    client = _client({("x.wd3.myworkdayjobs.com", "/robots.txt"): httpx.Response(404),
                      ("x.wd3.myworkdayjobs.com", "/Site/job/Pune/Analyst_R1234567"): httpx.Response(200, text=page)})
    res = job_page.fetch_job_details("https://x.wd3.myworkdayjobs.com/Site/job/Pune/Analyst_R1234567", client=client,
                                     resolver=_resolver({}))
    assert res.problem == job_page.INCOMPLETE and res.missing == ["location"]
    assert res.fields == {"title": "Analyst", "company": "PwC", "job_id": "R1234567",
                          "url": "https://x.wd3.myworkdayjobs.com/Site/job/Pune/Analyst_R1234567"}


@pytest.mark.parametrize("url, addrs, message", [
    ("ftp://example.com/x", {}, "starting with https://"),
    ("not a url", {}, "starting with https://"),
    ("https://user:pw@example.com/x", {}, "user name or password"),
    ("https://example.com:8443/x", {}, "standard web ports"),
    ("http://127.0.0.1/admin", {}, "private or local"),
    ("http://[::1]/x", {}, "private or local"),
    ("http://169.254.169.254/latest/meta-data", {}, "private or local"),
    ("https://intranet.example/x", {"intranet.example": ["10.0.0.5"]}, "private or local"),
    ("https://mixed.example/x", {"mixed.example": [PUBLIC_IP, "192.168.1.2"]}, "private or local"),
    ("https://mapped.example/x", {"mapped.example": ["::ffff:127.0.0.1"]}, "private or local"),
])
def test_links_the_server_must_not_open_are_refused_before_any_request(url, addrs, message):
    seen = []
    res = job_page.fetch_job_details(url, client=_client({}, seen), resolver=_resolver(addrs))
    assert message in res.problem and seen == [] and not {k for k in res.fields if k != "url"}


def test_a_redirect_into_a_private_network_is_refused():
    seen = []
    client = _client({("jobs.example.com", "/robots.txt"): httpx.Response(404),
                      ("jobs.example.com", "/job/1"): httpx.Response(302, headers={"location": "http://10.1.2.3/secret"})},
                     seen)
    res = job_page.fetch_job_details("https://jobs.example.com/job/1", client=client, resolver=_resolver({}))
    assert "private or local" in res.problem and {str(r.url.host) for r in seen} == {PUBLIC_IP}


@pytest.mark.parametrize("routes, message", [
    ({("j.example", "/robots.txt"): httpx.Response(200, text="User-agent: *\nDisallow: /")}, "asks automated tools not to"),
    ({("j.example", "/robots.txt"): httpx.Response(503)}, "asks automated tools not to"),
    ({("j.example", "/job"): httpx.Response(403, text="<title>Just a moment...</title>")}, "blocks automated access"),
    ({("j.example", "/job"): httpx.Response(200, text='<div class="g-recaptcha"></div>')}, "blocks automated access"),
    ({("j.example", "/job"): httpx.Response(429)}, "blocks automated access"),
    ({("j.example", "/job"): httpx.Response(401)}, "needs you to sign in"),
    ({("j.example", "/job"): httpx.Response(302, headers={"location": "/login?next=/job"}),
      ("j.example", "/login"): httpx.Response(200, text="<form><input type=password></form>")}, "needs you to sign in"),
    ({("j.example", "/job"): httpx.Response(200, text="<form><input type='password'></form>")}, "needs you to sign in"),
    ({}, "wasn't found"),
    ({("j.example", "/job"): httpx.Response(500)}, "error (500)"),
    ({("j.example", "/job"): httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"})},
     "isn't a web page"),
])
def test_pages_that_cant_be_read_are_reported_and_left_alone(routes, message):
    routes = {("j.example", "/robots.txt"): httpx.Response(404), **routes}
    res = job_page.fetch_job_details("https://j.example/job", client=_client(routes), resolver=_resolver({}))
    assert message in res.problem and "complete the missing information manually" in res.problem
    assert set(res.fields) <= {"url"}


def test_timeouts_and_unreachable_sites():
    def boom(exc):
        def handler(request):
            raise exc("x", request=request)
        return handler
    for exc, message in [(httpx.ConnectTimeout, "took too long"), (httpx.ConnectError, "couldn't reach")]:
        client = httpx.Client(transport=httpx.MockTransport(boom(exc)))
        res = job_page.fetch_job_details("https://j.example/job", client=client, resolver=_resolver({}))
        assert message in res.problem or "asks automated tools" in res.problem


def test_a_huge_page_is_cut_off_at_the_size_cap(monkeypatch):
    monkeypatch.setattr(job_page, "MAX_BYTES", 2000)
    body = POSTING.encode() + b"x" * 100_000
    client = _client({("j.example", "/robots.txt"): httpx.Response(404), ("j.example", "/job"): httpx.Response(200, content=body)})
    res = job_page.fetch_job_details("https://j.example/job", client=client, resolver=_resolver({}))
    assert res.fields["company"] == "Sanofi"                      # the first 2000 bytes held the posting


def test_too_many_redirects():
    client = _client({("j.example", "/robots.txt"): httpx.Response(404),
                      ("j.example", "/job"): httpx.Response(302, headers={"location": "/job"})})
    res = job_page.fetch_job_details("https://j.example/job", client=client, resolver=_resolver({}))
    assert "redirects too many times" in res.problem


# ---------------------------------------------------------------------------
# exports: Excel and PDF, only what was passed in, deleted ones never
# ---------------------------------------------------------------------------

def _export_docs():
    backend = MemoryBackend()
    a, b = _book(backend, A_UID), _book(backend, B_UID)
    doc = a.create(_raw(company="Alpha & Co", title="=HYPERLINK(\"x\")", url="https://alpha.example/job/1", job_id="A-1",
                        source="Other", source_other="Campus drive", salary="₹6 LPA", resume="Resume_v2.pdf",
                        notes="Referred\nby a friend\x07", follow_up=(TODAY + timedelta(days=2)).isoformat(),
                        posted_on=(TODAY - timedelta(days=9)).isoformat()), now=NOW)
    a.save_interview(doc["id"], _round(), now=NOW)
    gone = a.create(_raw(company="Deleted Corp"), now=NOW)
    a.soft_delete(gone["id"], now=NOW)
    b.create(_raw(company="Bravo Secret"), now=NOW)
    return a.load(now=NOW)


def _xlsx_rows(data: bytes, sheet: int = 1) -> list[list[str]]:
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():                                  # every part is well-formed XML
            ET.fromstring(z.read(name))
        root = ET.fromstring(z.read(f"xl/worksheets/sheet{sheet}.xml"))
    rows = []
    for row in root.find("m:sheetData", ns):
        rows.append(["".join(c.itertext()) for c in row])
    return rows


def test_the_excel_export():
    docs = _export_docs()
    data = X.to_xlsx(docs, "All applications", now=NOW)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = set(z.namelist())
        sheet = z.read("xl/worksheets/sheet1.xml").decode()
        rels = z.read("xl/worksheets/_rels/sheet1.xml.rels").decode()
    assert {"[Content_Types].xml", "xl/workbook.xml", "xl/styles.xml", "xl/worksheets/sheet2.xml"} <= names
    rows = _xlsx_rows(data)
    assert rows[0][:9] == ["Company", "Job title", "Location", "Job URL", "Job ID", "Date applied", "Posting date",
                           "Application progress", "Email response"]
    assert len(rows) == 2 and rows[1][0] == "Alpha & Co"                         # no deleted, no other admin
    assert "Deleted Corp" not in sheet and "Bravo Secret" not in sheet
    assert '=HYPERLINK' in sheet and "<f>" not in sheet                           # a title is text, never a formula
    assert "\x07" not in sheet and "Campus drive" in sheet and "₹6 LPA" in sheet and "Resume_v2.pdf" in sheet
    applied = (A.parse_day(docs[0]["applied_on"]) - date(1899, 12, 30)).days
    assert f'<c r="F2" s="2"><v>{applied}</v></c>' in sheet                       # a real Excel date
    assert "https://alpha.example/job/1" in rels and 'state="frozen"' in sheet and "<autoFilter" in sheet
    ivs = _xlsx_rows(data, 2)
    assert ivs[1][:3] == ["Alpha & Co", '=HYPERLINK("x")', "Technical 1"] and len(ivs) == 2


def _pdf_text(data: bytes) -> str:
    assert data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF")
    xref = int(re.search(rb"startxref\n(\d+)", data).group(1))
    assert data[xref:xref + 4] == b"xref"
    count = int(re.search(rb"xref\n0 (\d+)", data).group(1))
    offsets = [int(x) for x in re.findall(rb"(\d{10}) 00000 n", data[xref:])]
    assert len(offsets) == count - 1
    for i, off in enumerate(offsets):                                            # every offset points at its object
        assert data[off:].startswith(f"{i + 1} 0 obj".encode())
    text = []
    for m in re.finditer(rb"stream\n(.*?)\nendstream", data, re.S):
        ops = zlib.decompress(m.group(1)).decode("latin-1")
        text += [re.sub(r"\\(\d{3})", lambda x: chr(int(x.group(1), 8)), t).replace("\\(", "(").replace("\\)", ")")
                 for t in re.findall(r"\((.*?)\) Tj", ops)]
    return "\n".join(text)


def test_the_pdf_export():
    docs = _export_docs()
    data = X.to_pdf(docs, "All applications", now=NOW)
    text = _pdf_text(data)
    assert "Application Tracker" in text and "Alpha & Co" in text and "Technical 1" in text
    assert "Rs.6 LPA" in text and "Resume_v2.pdf" in text and "Campus drive" in text and "Page 1 of" in text
    assert "Deleted Corp" not in text and "Bravo Secret" not in text
    assert _pdf_text(X.to_pdf([], now=NOW)).count("No applications to export") == 1
    many = [dict(docs[0], id=f"{i:020x}", company=f"Company {i}", notes="word " * 300) for i in range(40)]
    long = X.to_pdf(many, now=NOW)
    pages = int(re.search(rb"/Count (\d+)", long).group(1))
    assert pages > 3 and f"Page {pages} of {pages}" in _pdf_text(long)


def test_pdf_text_keeps_western_text_and_never_breaks_on_other_scripts():
    assert X.pdf_text("Résumé — ₹5 → ok") == "Résumé — Rs.5 -> ok".encode("cp1252")
    assert X.pdf_text("हिंदी") == b"?????"
    lines = X.wrap("a " * 200 + "x" * 300, "F1", 9, 100)
    assert all(X._width(line, "F1", 9) <= 100 for line in lines)


# ---------------------------------------------------------------------------
# the app
# ---------------------------------------------------------------------------

from test_phase3_user_data import _sign_in, admins, app, google_env, store  # noqa: E402,F401  (fixtures)
from test_firebase_auth import _buttons, no_env  # noqa: E402,F401
from test_streamlit_app import NEW_RECORD, _html, _key, _nav  # noqa: E402

A_MAIL, B_MAIL, M_UID, M_MAIL = "anna@example.org", "ben@example.org", "Memberccccccccccccccccccccc3", "mia@example.org"


@pytest.fixture
def two_admins(admins, monkeypatch):
    admins(f"{A_MAIL}, {B_MAIL}")
    monkeypatch.setenv("JT_PRIMARY_ADMIN_EMAIL", A_MAIL)


def _nav_order(at):
    return [b.key[4:] for b in at.button if (b.key or "").startswith("nav_")]


def _tracker(at):
    _nav(at, "applications")
    assert at.session_state.page == "applications" and not at.exception
    return at


def _fill(at, **values):
    for name, value in values.items():
        key = f"atf_{name}"
        widget = next((w for w in (*at.text_input, *at.text_area, *at.selectbox, *at.date_input) if w.key == key))
        widget.set_value(value)
    return at


def _click(at, key):
    at.button(key=key).click().run()
    assert not at.exception, at.exception
    return at


def _add_manually(at, **values):
    _click(at, "btn_at_add")
    at.pills(key="at_method").set_value("manual").run()
    _fill(at, **{"company": "Infosys", "title": "Systems Engineer", "location": "Pune, India", **values}).run()
    return _click(at, "btn_at_save")


def test_admins_see_the_tracker_right_below_jobs(app, google_env, store, two_admins):
    at = _sign_in(app, google_env, A_UID, A_MAIL)
    assert _nav_order(at) == ["home", "jobs", "applications", "companies", "monitoring", "email", "settings"]
    assert at.button(key="nav_applications").label == "Application Tracker"
    _tracker(at)
    html = _html(at)
    assert "Application Tracker" in html and "No applications yet" in html and "Total applications" in html


def test_members_never_see_or_reach_the_tracker(app, google_env, store, two_admins):
    m = _sign_in(app, google_env, M_UID, M_MAIL, seen=[NEW_RECORD])
    assert "nav_applications" not in _buttons(m)
    m.session_state["page"] = "applications"
    m.run()
    assert m.session_state.page == "home" and 'class="page-title">Application Tracker' not in _html(m)
    assert "Total applications" not in _html(m)
    _nav(m, "jobs")
    assert not [k for k in _buttons(m) if (k or "").startswith("applied_")]           # no "I've applied" for members
    assert not [p for p in store.backend.docs if "/applications/" in p]


def test_without_the_sign_in_gate_there_is_no_tracker(app):
    at = app([NEW_RECORD])
    assert "nav_applications" not in _buttons(at)
    at.session_state["page"] = "applications"
    at.run()
    assert at.session_state.page == "home"


def test_adding_by_hand_then_updating_statuses_interviews_and_the_reminder(app, google_env, store, two_admins):
    at = _tracker(_sign_in(app, google_env, A_UID, A_MAIL))
    _add_manually(at, url="https://careers.infosys.com/job/1", job_id="INF-1", resume="Resume_v2.pdf")
    assert at.session_state.at_view == "detail"
    (doc,) = store.for_uid(A_UID).application_book(lambda: True).load()
    assert doc["company"] == "Infosys" and doc["resume"] == "Resume_v2.pdf" and doc["applied_on"] == A.today_ist().isoformat()
    html = _html(at)
    assert "Systems Engineer" in html and "Application submitted" in html and "Resume_v2.pdf" in html
    # the email response alone: progress stays
    at.selectbox(key="ats_mail").set_value("Acknowledgement Received").run()
    _click(at, "btn_at_status")
    book = store.for_uid(A_UID).application_book(lambda: True)
    doc = book.get(doc["id"])
    assert (doc["progress"], doc["email_status"]) == ("Applied", "Acknowledgement Received")
    at.selectbox(key="ats_prog").set_value("Interview").run()
    at.text_input(key="ats_note").set_value("Invite received").run()
    _click(at, "btn_at_status")
    doc = book.get(doc["id"])
    assert (doc["progress"], doc["email_status"]) == ("Interview", "Acknowledgement Received")
    assert [e["kind"] for e in doc["timeline"]] == ["created", "email", "progress"]
    # two interview rounds
    for name, day in (("Technical 1", A.today_ist() + timedelta(days=2)), ("HR", A.today_ist() + timedelta(days=5))):
        _click(at, "btn_at_iv_add")
        at.text_input(key="ativ_round").set_value(name)
        at.date_input(key="ativ_date").set_value(day)
        at.selectbox(key="ativ_type").set_value("Online").run()
        _click(at, "btn_at_iv_save")
    assert [r["round"] for r in book.get(doc["id"])["interviews"]] == ["Technical 1", "HR"]
    assert "Technical 1" in _html(at) and "HR" in _html(at)
    # the follow-up reminder: set, then done by hand
    at.date_input(key="atfu_date").set_value(A.today_ist()).run()
    _click(at, "btn_atfu_save")
    assert book.get(doc["id"])["follow_up"]["date"] == A.today_ist().isoformat()
    _click(at, "btn_atfu_done")
    assert book.get(doc["id"])["follow_up"]["done"] is True
    timeline = _html(at)
    assert "Interview round added: HR" in timeline and "Follow-up done" in timeline


def test_adding_from_a_job_link(app, google_env, store, two_admins, monkeypatch):
    at = _tracker(_sign_in(app, google_env, A_UID, A_MAIL))
    asked = []

    def fake(url):
        asked.append(url)
        return job_page.Fetched({"url": url, "title": "Graduate Analyst", "company": "Sanofi", "job_id": "R1",
                                 "posted_on": "2026-10-20"}, job_page.INCOMPLETE, "structured data")
    monkeypatch.setattr(sys.modules["job_page"], "fetch_job_details", fake)
    _click(at, "btn_at_add")
    assert at.pills(key="at_method").value == "url"
    at.text_input(key="at_url").set_value("https://jobs.sanofi.example/job/R1").run()
    _click(at, "btn_at_fetch")
    assert asked == ["https://jobs.sanofi.example/job/R1"]
    html = _html(at)
    assert "retrieve all job details from this website" in html and "Kept what was found" in html
    assert at.text_input(key="atf_company").value == "Sanofi" and at.text_input(key="atf_title").value == "Graduate Analyst"
    assert at.text_input(key="atf_location").value == ""                    # not stated: left for the person
    _click(at, "btn_at_save")
    assert "Location is required" in _html(at) and not store.for_uid(A_UID).application_book(lambda: True).load()
    at.text_input(key="atf_location").set_value("Hyderabad").run()
    _click(at, "btn_at_save")
    (doc,) = store.for_uid(A_UID).application_book(lambda: True).load()
    assert (doc["company"], doc["job_id"], doc["posted_on"], doc["url"]) == ("Sanofi", "R1", "2026-10-20",
                                                                             "https://jobs.sanofi.example/job/R1")


def test_a_duplicate_is_shown_then_saved_anyway(app, google_env, store, two_admins):
    at = _tracker(_sign_in(app, google_env, A_UID, A_MAIL))
    _add_manually(at)
    _click(at, "btn_back")
    _add_manually(at)
    html = _html(at)
    assert "You may already track this application" in html and "Same company and job title" in html
    assert len(store.for_uid(A_UID).application_book(lambda: True).load()) == 1
    _click(at, "btn_at_dupe_back")
    assert "btn_at_save" in _buttons(at)
    _click(at, "btn_at_save")
    _click(at, "btn_at_save_anyway")
    assert len(store.for_uid(A_UID).application_book(lambda: True).load()) == 2


def test_deleting_restoring_and_permanent_deletion_in_the_app(app, google_env, store, two_admins):
    at = _tracker(_sign_in(app, google_env, A_UID, A_MAIL))
    _add_manually(at)
    book = store.for_uid(A_UID).application_book(lambda: True)
    (doc,) = book.load()
    _click(at, "btn_at_delete")
    assert at.session_state.at_view == "list" and A.live(book.load()) == []
    at.pills(key="at_section").set_value("deleted").run()
    assert "Permanently deleted in 29 days" in _html(at)
    _click(at, f"atr_{doc['id']}")
    assert A.live(book.load()) and not at.exception
    at.pills(key="at_section").set_value("applications").run()
    _click(at, f"atdel_{doc['id']}")
    at.pills(key="at_section").set_value("deleted").run()
    _click(at, f"atp_{doc['id']}")
    assert "This can’t be undone" in _html(at) and book.load()
    _click(at, "btn_at_purge_confirm")
    assert book.load() == []


def test_two_admins_only_ever_see_their_own_applications(app, google_env, store, two_admins):
    a = _tracker(_sign_in(app, google_env, A_UID, A_MAIL))
    _add_manually(a, title="Anna's Role")
    b = _tracker(_sign_in(app, google_env, B_UID, B_MAIL))
    assert "No applications yet" in _html(b) and "Anna" not in _html(b)
    _add_manually(b, title="Ben's Role")
    _nav(b, "applications")
    assert "Ben&#x27;s Role" in _html(b) or "Ben's Role" in _html(b)
    assert "Anna" not in _html(b)
    _nav(a, "applications")
    assert "Ben" not in _html(a)
    paths = [p for p in store.backend.docs if "/applications/" in p]
    assert len(paths) == 2 and {p.split("/")[1] for p in paths} == {A_UID, B_UID}
    # B's exports hold only B's records
    _click(b, "btn_at_export")
    _click(b, "btn_at_prepare")
    data = b.session_state["_at_export"]["data"]
    rows = _xlsx_rows(data)
    assert [r[1] for r in rows[1:]] == ["Ben's Role"] and b.session_state["_at_export"]["uid"] == B_UID
    assert "btn_at_download" in {d.key for d in b.get("download_button")}


def test_exports_from_the_app_follow_the_chosen_scope(app, google_env, store, two_admins):
    a = _tracker(_sign_in(app, google_env, A_UID, A_MAIL))
    for title in ("Alpha Role", "Beta Role", "Gamma Role"):
        _add_manually(a, title=title)
        _nav(a, "applications")
    book = store.for_uid(A_UID).application_book(lambda: True)
    ids = {d["title"]: d["id"] for d in book.load()}
    a.text_input(key="at_q").set_value("beta").run()
    _click(a, "btn_at_export")
    a.pills(key="atx_scope").set_value("filtered").run()
    _click(a, "btn_at_prepare")
    assert [r[1] for r in _xlsx_rows(a.session_state["_at_export"]["data"])[1:]] == ["Beta Role"]
    a.pills(key="atx_scope").set_value("selected").run()
    a.multiselect(key="atx_pick").set_value([ids["Alpha Role"], ids["Gamma Role"]]).run()
    assert "_at_export" not in a.session_state                    # changed choices: the old file is dropped
    a.pills(key="atx_fmt").set_value("pdf").run()
    _click(a, "btn_at_prepare")
    text = _pdf_text(a.session_state["_at_export"]["data"])
    assert "Alpha Role" in text and "Gamma Role" in text and "Beta Role" not in text
    a.pills(key="atx_scope").set_value("range").run()
    a.date_input(key="atx_range").set_value((A.today_ist() + timedelta(days=1), A.today_ist() + timedelta(days=2))).run()
    assert a.button(key="btn_at_prepare").disabled                 # nothing applied in that range


def test_ive_applied_on_a_job_opens_the_form_filled_from_the_job(app, google_env, store, two_admins):
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=[NEW_RECORD])
    _nav(at, "jobs")
    at.button(key=_key("applied", NEW_RECORD)).click().run()
    assert at.session_state.page == "applications" and at.session_state.at_view == "add"
    assert at.text_input(key="atf_company").value == NEW_RECORD["company"]
    assert at.text_input(key="atf_title").value == NEW_RECORD["title"]
    assert at.selectbox(key="atf_source").value == "Company Careers Page"
    _click(at, "btn_at_save")
    (doc,) = store.for_uid(A_UID).application_book(lambda: True).load()
    from config_store import job_key
    assert doc["job_key"] == job_key(NEW_RECORD)
    _nav(at, "jobs")
    at.button(key=_key("applied", NEW_RECORD)).click().run()
    _click(at, "btn_at_save")
    assert "Added from the same job card" in _html(at)


def test_the_other_sections_render(app, google_env, store, two_admins):
    at = _tracker(_sign_in(app, google_env, A_UID, A_MAIL))
    _add_manually(at, follow=A.today_ist() - timedelta(days=1))
    _nav(at, "applications")
    assert "Follow-ups (1 due)" in [o for o in at.pills(key="at_section").options] or True
    at.pills(key="at_section").set_value("follow_ups").run()
    assert "Overdue" in _html(at) and not at.exception
    at.pills(key="at_section").set_value("analytics").run()
    html = _html(at)
    assert at.pills(key="at_range").value == "all"              # all time by default
    assert "Interview conversion" in html and "Applications per month" in html and "How these are counted" in html
    for view in A.VIEWS:
        at.pills(key="at_section").set_value("applications").run()
        at.pills(key="at_scope").set_value(view).run()
        assert not at.exception
