"""M6 Part 12 F — security: authorisation enforced by the storage layer
itself (independent of the UI), the two admins' separation, direct access,
injection, malicious input, logging and session isolation."""
import inspect
import logging
from datetime import date
from pathlib import Path

import pytest

import resume_parse
import resume_profile
import resume_store as RS
from user_store import InvalidInput, MemoryBackend, UserData, UserStoreError

FIX = Path(__file__).parent / "fixtures" / "m6"
A_UID, B_UID = "AdminAaaaaaaaaaaaaaaaaaaaaaa1", "AdminBbbbbbbbbbbbbbbbbbbbbbb2"
CV = (FIX / "resume_fresher_swe.txt").read_bytes()


def _doc(data=CV, name="cv.txt"):
    d = resume_parse.read_document(name, data)
    return d, resume_profile.build_profile(d.text, today=date(2026, 11, 15))


def _lib(backend, uid=A_UID, allowed=lambda: True):
    return UserData(backend, uid).resume_library(allowed)


def _public(cls):
    return [n for n, f in inspect.getmembers(cls, inspect.isfunction) if not n.startswith("_")]


# every public operation with arguments that would succeed for the owner
def _calls(rid):
    d, p = _doc()
    return {
        "list_resumes": (), "get": (rid,), "find_duplicate": (d.sha256,), "create": ("Second", d, p),
        "replace": (rid, d, p), "rename": (rid, "Renamed"), "set_default": (rid,),
        "save_corrections": (rid, {"remove_skills": ["Python"]}), "confirmation": (rid,), "delete": (rid,),
        "export": (rid,), "job_text": ("job-1",), "job_texts": (), "set_job_text": ("job-1", "Python required"),
        "save_matches": (rid, [("job-1", "k", {"overall": 1})]), "matches": (), "add_history": (rid, "m", "x", {}),
        "history": (), "clear_history": (), "practice": (), "set_practice": ("py-b1", "Confident"),
        "decisions": (rid, "job-1"), "decide": (rid, "job-1", "a" * 12, "accepted"), "weights": (),
        "set_weights": ({"mandatory": 40, "preferred": 15, "experience": 20, "education": 15, "evidence": 10},),
        "delete_all": (),
    }


def test_every_public_operation_is_guarded_and_has_a_test_call():
    names = _public(RS.ResumeLibrary)
    assert names and all(getattr(RS.ResumeLibrary, n).authorized for n in names)
    assert set(names) == set(_calls("x"))


@pytest.mark.parametrize("hook", [lambda: False, lambda: None, lambda: 1, lambda: 1 / 0, lambda: "yes"])
def test_a_refused_or_broken_check_refuses_everything_and_writes_nothing(hook):
    backend = MemoryBackend()
    rid = _lib(backend).create("Mine", *_doc())["id"]
    before = dict(backend.docs)
    lib = _lib(backend, allowed=hook)
    for name, args in _calls(rid).items():
        with pytest.raises(RS.NotAuthorized):
            getattr(lib, name)(*args)
    assert backend.docs == before


def test_the_check_is_asked_on_every_call():
    n = []
    lib = _lib(MemoryBackend(), allowed=lambda: n.append(1) or True)
    lib.list_resumes()
    lib.list_resumes()
    lib.weights()
    assert len(n) == 3


def test_a_library_binds_only_to_a_real_uid_and_a_callable_check():
    for bad in ("", "../x", "a/b", "x" * 200, None, "users/x"):
        with pytest.raises((InvalidInput, TypeError)):
            RS.ResumeLibrary(MemoryBackend(), bad, lambda: True)
    with pytest.raises(TypeError):
        RS.ResumeLibrary(MemoryBackend(), A_UID, True)
    assert "uid" not in inspect.signature(RS.ResumeLibrary.get).parameters


def test_admin_one_and_admin_two_never_reach_each_other():
    backend = MemoryBackend()
    a, b = _lib(backend, A_UID), _lib(backend, B_UID)
    rid = a.create("Anna's", *_doc())["id"]
    a.set_job_text("job-1", "Anna's pasted text")
    a.set_practice("py-b1", "Confident")
    a.add_history(rid, "match", "Anna's analysis", {})
    before = dict(backend.docs)
    assert b.list_resumes() == [] and b.job_texts() == {} and b.practice() == {} and b.history() == [] and b.matches() == []
    for op, args in (("get", (rid,)), ("rename", (rid, "x")), ("delete", (rid,)), ("export", (rid,)),
                     ("replace", (rid, *_doc())), ("set_default", (rid,)), ("save_corrections", (rid, {})),
                     ("decisions", (rid, "job-1")), ("add_history", (rid, "m", "x", {}))):
        with pytest.raises(RS.NotFound):
            getattr(b, op)(*args)
    assert b.find_duplicate(_doc()[0].sha256) is None
    assert b.delete_all() == 0 and backend.docs == before
    assert all(p.startswith(f"users/{A_UID}/") for p in backend.docs)


@pytest.mark.parametrize("rid", ["../" + "a" * 17, "A" * 20, "a" * 19, "a" * 21, "", None, 5, "users/x/resumes/y",
                                 "aaaaaaaaaaaaaaaaaaa/"])
def test_only_well_formed_resume_ids_are_looked_up(rid):
    seen = []

    class Spy(MemoryBackend):
        def get(self, path):
            seen.append(path)
            return super().get(path)
    lib = _lib(Spy())
    with pytest.raises(RS.NotFound):
        lib.get(rid)
    assert seen == []


def test_job_and_question_keys_are_hashes_never_paths():
    backend = MemoryBackend()
    lib = _lib(backend)
    lib.set_job_text("../../users/B/x", "text")
    with pytest.raises(InvalidInput):
        lib.set_practice("../../x", "Confident")
    assert all(p.startswith(f"users/{A_UID}/") and ".." not in p for p in backend.docs)


def test_deleting_an_account_removes_every_m6_document():
    backend = MemoryBackend()
    lib = _lib(backend)
    rid = lib.create("Mine", *_doc())["id"]
    lib.set_job_text("job-1", "x")
    lib.save_matches(rid, [("job-1", "k", {})])
    lib.add_history(rid, "m", "x", {})
    lib.set_practice("py-b1", "Practising")
    lib.decide(rid, "job-1", "a" * 12, "accepted")
    lib.set_weights(dict(RS.resume_profile and {"mandatory": 40, "preferred": 15, "experience": 20, "education": 15,
                                                "evidence": 10}))
    _lib(backend, B_UID).create("Other", *_doc())
    UserData(backend, A_UID).delete_personal_data()
    assert not [p for p in backend.docs if p.startswith(f"users/{A_UID}")]
    assert [p for p in backend.docs if p.startswith(f"users/{B_UID}/resumes/")]


def test_deleting_a_resume_removes_what_was_derived_from_it_and_nothing_else():
    backend = MemoryBackend()
    lib = _lib(backend)
    keep = lib.create("Keep", *_doc())["id"]
    gone = lib.create("Gone", *_doc(CV.replace(b"PRIYA", b"ASHA")))["id"]
    for rid in (keep, gone):
        lib.save_matches(rid, [("job-1", "k", {})])
        lib.add_history(rid, "m", "x", {})
        lib.decide(rid, "job-1", "b" * 12, "rejected")
    lib.delete(gone)
    assert not [p for p, d in backend.docs.items() if gone in p or (isinstance(d, dict) and d.get("resume_id") == gone)]
    assert len(lib.matches()) == 1 and len(lib.history()) == 1 and lib.decisions(keep, "job-1")


def test_names_and_text_inputs_are_validated():
    lib = _lib(MemoryBackend())
    d, p = _doc()
    for bad in ("", "   ", None, 5, "x" * 81):
        with pytest.raises(InvalidInput):
            lib.create(bad, d, p)
    meta = lib.create("  My ‮ CV \x00 ", d, p)
    assert meta["name"] == "My CV"
    with pytest.raises(InvalidInput):
        lib.create("my cv", d, p)                               # names are unique per admin
    with pytest.raises(InvalidInput):
        lib.set_job_text("job", "x" * (RS.JOB_TEXT_MAX + 1))
    with pytest.raises(InvalidInput):
        lib.save_corrections(meta["id"], {"add_skills": ["Telepathy"]})
    with pytest.raises(InvalidInput):
        lib.save_corrections(meta["id"], {"experience_kind": {"99": "employment"}})
    with pytest.raises(InvalidInput):
        lib.set_weights({"mandatory": 100, "preferred": 100})
    for _ in range(RS.MAX_RESUMES - 1):
        lib.create(f"R{_}", d, p)
    with pytest.raises(InvalidInput, match="at most"):
        lib.create("One too many", d, p)


def test_the_uploaded_file_and_raw_text_are_never_stored():
    backend = MemoryBackend()
    d, p = _doc()
    _lib(backend).create("Mine", d, p)
    stored = str(backend.docs)
    assert d.text not in stored and "TECHNICAL SKILLS" not in stored and "RELEVANT COURSEWORK" not in stored
    assert "priya.sharma@example.com" not in stored and "98765 43210" not in stored
    assert "Example Fintech" in stored                           # the structured profile is what's kept


def test_a_storage_failure_becomes_a_safe_error_logged_by_type_only(caplog):
    class Broken(MemoryBackend):
        def list(self, collection):
            raise RuntimeError(f"permission denied on {collection} for priya.sharma@example.com")
    lib = _lib(Broken())
    with caplog.at_level(logging.DEBUG), pytest.raises(UserStoreError) as e:
        lib.list_resumes()
    assert str(e.value) == RS.UNAVAILABLE and e.value.kind == "RuntimeError"
    assert "priya" not in caplog.text and A_UID not in caplog.text


def test_nothing_from_a_resume_is_ever_logged(caplog):
    import job_requirements
    import resume_improve
    import resume_match
    with caplog.at_level(logging.DEBUG):
        d, p = _doc()
        lib = _lib(MemoryBackend())
        rid = lib.create("Mine", d, p)["id"]
        r = job_requirements.requirements({"title": "Engineer"}, (FIX / "job_backend_fresher.txt").read_text())
        resume_match.analyse(lib.get(rid)["effective"], r)
        resume_improve.suggestions(p, r, [])
        with pytest.raises(resume_parse.ResumeFileError):
            resume_parse.read_document("cv.pdf", b"%PDF-1.4 PRIYA SHARMA secret " * 10)
    for needle in ("Priya", "PRIYA", "Example Fintech", "98765", "priya.sharma"):
        assert needle not in caplog.text


def test_no_m6_module_sends_anything_over_the_network():
    import interview_prep
    import job_requirements
    import resume_improve
    import resume_match
    import resume_worker
    import skill_taxonomy
    for mod in (resume_parse, resume_profile, RS, interview_prep, job_requirements, resume_improve, resume_match,
                resume_worker, skill_taxonomy):
        src = inspect.getsource(mod)
        for bad in ("httpx", "requests", "urllib.request", "socket", "openai", "anthropic", "smtplib"):
            assert f"import {bad}" not in src and f"from {bad}" not in src, (mod.__name__, bad)


def test_html_and_script_in_a_resume_are_escaped_in_the_app():
    src = (Path(__file__).parent.parent / "streamlit_app.py").read_text("utf-8")
    body = src[src.index("# RESUME & JOB INTELLIGENCE (M6)"):src.index("# ROUTER")]
    # every value interpolated into HTML in the M6 UI goes through escape() (or is a number / a fixed label)
    import re
    body = "\n".join(line for line in body.splitlines() if "<" in line)    # lines that build HTML
    for m in re.finditer(r"\{([^{}]*(?:\[\"(?:name|title|text|quote|evidence|before|after|why|label|question|"
                         r"requirement|note|file_name|company|location|skill)\"\])\s*)\}", body):
        expr = m.group(1)
        assert "escape(" in expr or expr.strip().startswith(("_ri_", "_ms(")), expr


# ── in the app: injection, direct access, session isolation ──────────────────

from test_application_tracker import A_MAIL, B_MAIL, two_admins  # noqa: E402,F401  (fixture)
from test_firebase_auth import _buttons, no_env  # noqa: E402,F401
from test_phase3_user_data import _sign_in, admins, app, google_env, store  # noqa: E402,F401
from test_streamlit_app import NEW_RECORD, _html, _nav  # noqa: E402


def test_injected_markup_in_a_resume_is_shown_as_text(app, google_env, store, two_admins):
    evil = (b"<img src=x onerror=alert(1)> Name\nSKILLS\nPython, <script>alert(2)</script>\n"
            b"PROJECTS\n<b>Bold</b> project\n* Built a <iframe> thing with Flask\n")
    d, p = _doc(evil)
    store.for_uid(A_UID).resume_library(lambda: True).create("<svg onload=alert(3)>", d, p)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=[NEW_RECORD])
    _nav(at, "resume_intel")
    at.pills(key="ri_section").set_value("resumes").run()
    html = _html(at)
    assert "<svg onload" not in html and "&lt;svg onload=alert(3)&gt;" in html
    rid = store.for_uid(A_UID).resume_library(lambda: True).list_resumes()[0]["id"]
    at.button(key=f"btn_ri_rev_{rid}").click().run()
    html = _html(at)
    assert "<iframe>" not in html and "&lt;iframe&gt;" in html and "<script>alert" not in html


def test_another_accounts_cached_library_is_never_shown(app, google_env, store, two_admins):
    import time
    meta = store.for_uid(A_UID).resume_library(lambda: True).create("Anna's secret résumé", *_doc())
    at = _sign_in(app, google_env, "AdminBbbbbbbbbbbbbbbbbbbbbbb2", B_MAIL, seen=[NEW_RECORD])
    # a stale cache from another account in this browser session (e.g. after switching accounts)
    at.session_state["_ri_cache"] = {"uid": A_UID, "at": time.time(), "resumes": [meta], "full": {}, "texts": {},
                                     "weights": {}, "practice": {}, "history": [], "hashes": {}}
    _nav(at, "resume_intel")
    at.pills(key="ri_section").set_value("resumes").run()
    assert not at.exception and "Anna" not in _html(at) and "No résumés yet" in _html(at)


def test_an_expired_or_downgraded_session_is_refused_at_the_next_operation(app, google_env, store, two_admins, monkeypatch):
    store.for_uid(A_UID).resume_library(lambda: True).create("Mine", *_doc())
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=[NEW_RECORD])
    _nav(at, "resume_intel")
    monkeypatch.setenv("JT_ADMIN_EMAILS", B_MAIL)               # A is no longer an admin
    at.run()
    assert at.session_state.page != "resume_intel" or "unavailable" in _html(at)
    assert "Mine" not in _html(at)
