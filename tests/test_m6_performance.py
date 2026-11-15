"""M6 Part 14 — performance and reliability: large catalogues, many
résumés, repeated analysis and caching, concurrent admins, failures,
malformed records, restarts and expired sessions."""
import random
import threading
import time
from datetime import date
from pathlib import Path

import pytest

import job_requirements as jr
import resume_match as rm
import resume_parse
import resume_profile as rp
import resume_store as RS
from user_store import MemoryBackend, UserData, UserStoreError

FIX = Path(__file__).parent / "fixtures" / "m6"
TODAY = date(2026, 11, 15)
A_UID, B_UID = "AdminAaaaaaaaaaaaaaaaaaaaaaa1", "AdminBbbbbbbbbbbbbbbbbbbbbbb2"
VOCAB = ["Python", "Java", "SQL", "React", "Django", "Spring Boot", "AWS", "Docker", "Kubernetes", "Excel", "Power BI",
         "Tableau", "Selenium", "Kafka", "Spark", "Linux", "Git", "C++", "Node.js", "MongoDB"]


def _profiles():
    return [rp.build_profile((FIX / f).read_text(), today=TODAY) for f in ("resume_fresher_swe.txt", "resume_data_analyst.txt")]


def _catalogue(n, seed=7):
    rnd = random.Random(seed)
    jobs = []
    for i in range(n):
        req = [{"text": f"Must have {' and '.join(rnd.sample(VOCAB, 3))}", "importance": "mandatory"},
               {"text": f"{rnd.choice(VOCAB)} is a plus", "importance": "preferred"},
               {"text": rnd.choice(["0-1 years of experience", "2+ years of experience", "Freshers welcome",
                                    "B.Tech in CS, 2025 or 2026 batch"]), "importance": "mandatory"}]
        jobs.append({"title": rnd.choice(["Software Engineer", "Data Analyst", "QA Engineer", "Cloud Engineer"]) + f" {i}",
                     "company": f"Company {i % 40}", "location": "Hyderabad, India", "evidence": {"requirements": req}})
    return jobs


def test_a_large_catalogue_with_several_resumes_is_fast():
    profiles = _profiles() * 2                                  # four résumés
    jobs = _catalogue(3000)
    t = time.perf_counter()
    reqs = [jr.requirements(j) for j in jobs]
    results = [rm.analyse(p, r) for r in reqs for p in profiles]
    gaps = rm.global_gaps(profiles[0], reqs)
    elapsed = time.perf_counter() - t
    assert len(results) == 12_000 and gaps and elapsed < 30, elapsed


def test_repeated_analysis_is_identical_and_the_cache_key_tracks_every_input():
    p = _profiles()[0]
    r = jr.requirements(_catalogue(1)[0])
    assert rm.analyse(p, r) == rm.analyse(p, r)
    base = rm.cache_key(rm.profile_hash(p), jr.fingerprint(r), rm.DEFAULT_WEIGHTS)
    assert base == rm.cache_key(rm.profile_hash(p), jr.fingerprint(r), dict(rm.DEFAULT_WEIGHTS))
    changed_profile = rp.apply_corrections(p, {"remove_skills": ["Python"]})
    other_weights = {"mandatory": 50, "preferred": 10, "experience": 20, "education": 10, "evidence": 10}
    r2 = jr.requirements(_catalogue(2)[1])
    assert len({base, rm.cache_key(rm.profile_hash(changed_profile), jr.fingerprint(r), rm.DEFAULT_WEIGHTS),
                rm.cache_key(rm.profile_hash(p), jr.fingerprint(r), other_weights),
                rm.cache_key(rm.profile_hash(p), jr.fingerprint(r2), rm.DEFAULT_WEIGHTS)}) == 4


def test_analysis_uses_the_stored_profile_never_re_reading_the_file(monkeypatch):
    backend = MemoryBackend()
    lib = UserData(backend, A_UID).resume_library(lambda: True)
    doc = resume_parse.read_document("cv.txt", (FIX / "resume_fresher_swe.txt").read_bytes())
    rid = lib.create("Mine", doc, rp.build_profile(doc.text))["id"]
    monkeypatch.setattr(resume_parse, "read_document", lambda *a: pytest.fail("re-parsed"))
    monkeypatch.setattr(rp, "build_profile", lambda *a, **k: pytest.fail("re-extracted"))
    for job in _catalogue(50):
        rm.analyse(lib.get(rid)["effective"], jr.requirements(job))


def test_two_admins_working_at_the_same_time_stay_separate():
    backend = MemoryBackend()
    doc = resume_parse.read_document("cv.txt", (FIX / "resume_fresher_swe.txt").read_bytes())
    prof = rp.build_profile(doc.text)
    errors = []

    def work(uid, tag):
        try:
            lib = UserData(backend, uid).resume_library(lambda: True)
            for i in range(10):
                rid = lib.create(f"{tag} {i}", doc, prof)["id"]
                lib.add_history(rid, "m", f"{tag} analysis {i}", {})
                lib.set_job_text(f"job-{i}", f"{tag} text {i}")
        except Exception as e:                                       # pragma: no cover - reported below
            errors.append(e)
    threads = [threading.Thread(target=work, args=(A_UID, "A")), threading.Thread(target=work, args=(B_UID, "B"))]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    a = UserData(backend, A_UID).resume_library(lambda: True)
    b = UserData(backend, B_UID).resume_library(lambda: True)
    assert {m["name"][0] for m in a.list_resumes()} == {"A"} and {m["name"][0] for m in b.list_resumes()} == {"B"}
    assert all(t.startswith("A ") for t in a.job_texts().values()) and len(a.history()) == 10
    assert sum(m["is_default"] for m in a.list_resumes()) >= 1


def test_history_is_capped():
    lib = UserData(MemoryBackend(), A_UID).resume_library(lambda: True)
    doc = resume_parse.read_document("cv.txt", (FIX / "resume_fresher_swe.txt").read_bytes())
    rid = lib.create("Mine", doc, rp.build_profile(doc.text))["id"]
    for i in range(RS.MAX_HISTORY + 15):
        lib.add_history(rid, "m", f"entry {i}", {}, now=1_800_000_000 + i)
    h = lib.history(1000)
    assert len(h) == RS.MAX_HISTORY and h[0]["label"] == f"entry {RS.MAX_HISTORY + 14}"


def test_an_interrupted_upload_saves_nothing():
    whole = (FIX / "resume_fresher_swe.txt").read_bytes()
    from m6_docs import docx, pdf
    for name, data in (("cv.pdf", pdf([whole.decode()[:1500]])), ("cv.docx", docx(whole.decode().split("\n")))):
        with pytest.raises(resume_parse.ResumeFileError):
            resume_parse.read_document(name, data[: len(data) // 3])


@pytest.mark.parametrize("method", ["list", "get", "set", "set_many", "delete_many"])
def test_firestore_failures_surface_as_a_safe_error(method):
    class Broken(MemoryBackend):
        pass

    def boom(*a, **k):
        raise ConnectionError("unavailable")
    backend = Broken()
    lib = UserData(backend, A_UID).resume_library(lambda: True)
    doc = resume_parse.read_document("cv.txt", (FIX / "resume_fresher_swe.txt").read_bytes())
    rid = lib.create("Mine", doc, rp.build_profile(doc.text))["id"]
    setattr(backend, method, boom)
    with pytest.raises(UserStoreError):
        {"list": lambda: lib.list_resumes(), "get": lambda: lib.get(rid), "set": lambda: lib.rename(rid, "New"),
         "set_many": lambda: lib.create("Other", doc, rp.build_profile(doc.text)),
         "delete_many": lambda: lib.delete(rid)}[method]()


def test_the_library_survives_an_application_restart():
    backend = MemoryBackend()
    doc = resume_parse.read_document("cv.txt", (FIX / "resume_fresher_swe.txt").read_bytes())
    first = UserData(backend, A_UID).resume_library(lambda: True)
    rid = first.create("Mine", doc, rp.build_profile(doc.text))["id"]
    first.save_corrections(rid, {"remove_skills": ["Python"]}, confirmed=True)
    again = UserData(backend, A_UID).resume_library(lambda: True)            # a new process, the same database
    assert "Python" not in again.get(rid)["effective"]["skills"] and again.confirmation(rid)["confirmed"]


def test_a_session_that_expires_is_refused_from_its_next_operation():
    state = {"ok": True}
    lib = UserData(MemoryBackend(), A_UID).resume_library(lambda: state["ok"])
    lib.list_resumes()
    state["ok"] = False
    with pytest.raises(RS.NotAuthorized):
        lib.list_resumes()


@pytest.mark.parametrize("profile", [{}, {"skills": None}, {"skills": {"Python": {}}}, {"education": [{}]},
                                     {"skills": {"Telepathy": {"status": "demonstrated"}}},
                                     {"totals": {"employment_months": "x"}}])
def test_malformed_profiles_never_break_the_engine(profile):
    r = jr.requirements(_catalogue(1)[0])
    try:
        rm.analyse(profile, r)
    except (TypeError, ValueError) as e:                  # a corrupted record may be refused, never crash the page
        pytest.fail(f"{type(e).__name__}: {e}")
