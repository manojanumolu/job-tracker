"""Job intelligence (Milestone 5): local, deterministic, never a gate,
facts kept apart from estimates, résumés never stored. See src/job_intel.py."""
import ast
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import job_filters
import job_intel as J
import scraper
from config_store import alert_pending
from user_store import InvalidInput, MemoryBackend, UserStore

REPO = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 11, 12, 6, 0, tzinfo=timezone.utc)
GATE = {"real_job_posting": True, "india_location": True, "detail_read": True, "detail_is_this_job": True,
        "no_conflicting_experience": True, "not_programme_story_talent_recruiter": True,
        "evidence_not_staff_context": True, "fresher_or_entry_evidence": True}


def job(title="Software Engineer", lines=(), skills=None, **extra):
    ev = {"checks": dict(GATE), "experience_lines": list(lines), "fresher_evidence": "0-2 years"}
    if skills is not None:
        ev["skills"] = skills
    return {"title": title, "url": "https://x.example/1", "company": "X", "category": "FRESHER",
            "first_seen": (NOW - timedelta(days=2)).isoformat(), "evidence": ev, **extra}


# ── extraction ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, expected", [
    ("Experience with Java or a similar OO language", {"Java"}),
    ("Strong JavaScript and TypeScript", {"JavaScript", "TypeScript"}),
    ("C++ and C# developers", {"C++", "C#"}),
    ("Python, SQL and Power BI dashboards", {"Python", "SQL", "Power BI", "Data analysis"} - {"Data analysis"}),
    ("Knowledge of data structures, algorithms, object-oriented design", {"Data structures & algorithms",
                                                                         "OOP / design patterns"}),
    ("AWS or Azure cloud platforms; Docker and Kubernetes", {"AWS", "Azure", "Cloud", "Docker", "Kubernetes"}),
    ("Excellent verbal and written communication skills", {"Communication"}),
    ("Let's go to the office", set()),                      # "go" alone is not Go
    ("Reactive maintenance of pumps", set()),               # not React
    ("Rusty bolts", set()), ("Our Java-script free stack", {"Java"} - {"Java"} | set()),
    ("", set()), (None, set()),
])
def test_extract_skills(text, expected):
    got = J.extract_skills(text)
    if text == "Our Java-script free stack":
        assert "JavaScript" not in got
    else:
        assert got == expected


def test_extraction_is_deterministic_and_vocabulary_ordered():
    text = "Python SQL AWS Docker Git Agile communication skills"
    assert J.extract_skills(text) == J.extract_skills(text)
    assert J.clean_skills(["Git", "Python", "nonsense", 3, "Python"]) == ["Python", "Git"]
    assert len(J.clean_skills(list(J.SKILLS) * 2)) == J.MAX_SKILLS


# ── facts vs estimates ──────────────────────────────────────────────────────

def test_job_skills_are_quotes_from_the_posting():
    j = job("Graduate Java Developer", ["Experience with SQL and REST APIs"])
    named = J.job_skills(j)
    assert named == {"Java": "Graduate Java Developer", "SQL": "Experience with SQL and REST APIs",
                     "REST APIs": "Experience with SQL and REST APIs"}
    for skill, quote in named.items():
        assert quote in J.job_lines(j)                      # every fact carries its source


def test_stored_skills_are_reverified_not_trusted():
    j = job("Analyst", skills={"Python": "Python and pandas", "AWS": "a line that names nothing", "Bogus": "x",
                               "Docker": 5})
    assert J.job_skills(j) == {"Python": "Python and pandas"}


def test_match_is_an_explained_estimate():
    j = job("Graduate Java Developer", ["Experience with SQL and REST APIs"])
    m = J.match(["Java", "SQL"], j)
    assert m["kind"] == "estimate" and m["score"] == 67 and m["named"] == 3
    assert [s for s, _ in m["matched"]] == ["Java", "SQL"] and [s for s, _ in m["missing"]] == ["REST APIs"]
    assert J.match(["Java"], job("Associate", ["Good communication"]))["score"] is None    # too little to judge
    assert J.match_label(None) == "Not enough detail" and J.match_label(80) == "Strong"


def test_match_never_invents_skills_from_nothing():
    m = J.match(["Python"], job("Associate"))
    assert m["named"] == 0 and m["matched"] == [] and m["missing"] == [] and m["score"] is None


# ── never a gate ────────────────────────────────────────────────────────────

def test_job_intel_never_changes_eligibility():
    """Every real record: the global gate and the shared alert gate give the
    same answer with and without the intelligence fields, and the module
    neither imports nor is imported by the gates."""
    seen = json.loads((REPO / "data" / "seen_jobs.json").read_text("utf-8"))
    for rec in seen:
        before = (job_filters.passes_global_gate(rec), alert_pending(rec))
        enriched = json.loads(json.dumps(rec))
        if isinstance(enriched.get("evidence"), dict):
            enriched["evidence"]["skills"] = {"Python": "Python"}
        J.match(["Python"], enriched), J.job_skills(enriched), J.eligibility_explanation(enriched)
        assert (job_filters.passes_global_gate(enriched), alert_pending(enriched)) == before
    for name in ("job_filters.py", "job_classifier.py", "config_store.py"):
        assert "job_intel" not in (REPO / "src" / name).read_text("utf-8")


def test_the_scraper_adds_skills_only_after_every_gate():
    src = (REPO / "src" / "scraper.py").read_text("utf-8")
    decide = src[src.index("def decide("):src.index("\ndef ", src.index("def decide(") + 10)]
    assert decide.index("failed = [k for k, ok in checks.items() if not ok]") < decide.index("_posting_skills(")
    assert scraper._posting_skills("Requirements:\n• Python and SQL\n• Good communication skills") == {
        "Python": "Python and SQL", "SQL": "Python and SQL", "Communication": "Good communication skills"}
    assert scraper._posting_skills(None) == {}                                    # never raises


def test_no_network_and_no_ai_service():
    tree = ast.parse((REPO / "src" / "job_intel.py").read_text("utf-8"))
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert imported <= {"__future__", "io", "re", "zipfile", "collections", "datetime"}


# ── gaps, interview topics, freshness, explanations ─────────────────────────

def test_skill_gaps_count_jobs_and_suggest_next_steps():
    jobs = [job("Java Developer", ["SQL and Git"]), job("Backend Engineer", ["Java, Docker"]), job("Data Analyst", ["SQL"])]
    gaps = J.skill_gaps(["Java"], jobs)
    assert [(g["skill"], g["jobs"]) for g in gaps] == [("SQL", 2), ("Data analysis", 1), ("Docker", 1), ("Git", 1)]
    assert all(g["next_step"] for g in gaps)


def test_interview_topics_come_from_the_curated_bank():
    basic = J.interview_topics(["Python", "SQL", "Nonsense"], "basic")
    assert set(basic) == {"Python", "SQL"} and all(basic.values())
    assert J.interview_topics(["Python"], "intermediate")["Python"] != basic["Python"]
    assert J.interview_topics(["Python"], "expert") == J.interview_topics(["Python"], "basic")


def test_freshness_and_eligibility_explanations_are_facts():
    f = J.freshness(job(), NOW)
    assert f["days"] == 2 and f["label"] == "This week" and f["source"] == "when this tracker first found it"
    posted = J.freshness(job(posted_at=(NOW - timedelta(days=40)).isoformat()), NOW)
    assert posted["label"] == "Older" and posted["source"] == "the company's posting date"
    assert J.freshness({"title": "x"}, NOW)["label"] == "Unknown"
    why = J.eligibility_explanation(job())
    assert len([x for x in why if x["passed"]]) == len(GATE) + 1 and why[-1]["text"].startswith("Quoted from the posting")


# ── résumés: read in memory, never stored ───────────────────────────────────

def _docx(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f'<w:document><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>')
    return buf.getvalue()


def test_resume_text_reads_txt_and_docx_only():
    assert "Python" in J.resume_text("cv.txt", b"Python and SQL")
    assert J.extract_skills(J.resume_text("CV.DOCX", _docx("Java, Spring Boot, Docker"))) == {"Java", "Spring", "Docker"}
    for name, data in (("cv.pdf", b"%PDF"), ("cv.docx", b"not a zip"), ("cv.txt", b"x" * (J.RESUME_MAX_BYTES + 1)),
                       ("cv.exe", b"MZ")):
        with pytest.raises(J.ResumeError):
            J.resume_text(name, data)


def test_only_confirmed_skill_names_are_stored_per_account():
    store = UserStore(MemoryBackend(), None)
    a, b = store.for_uid("UserAaaaaaaaaaaaaaaaaaaaaaaa1"), store.for_uid("UserBbbbbbbbbbbbbbbbbbbbbbbb2")
    a.ensure_profile("a@example.org", "", True)
    b.ensure_profile("b@example.org", "", True)
    assert a.set_skills(["Python", "SQL", "my secret resume text", "Python"]) == ["Python", "SQL"]
    assert store.backend.get(f"users/{a.uid}")["skills"] == ["Python", "SQL"]
    assert "skills" not in (store.backend.get(f"users/{b.uid}") or {})           # account isolation
    with pytest.raises(InvalidInput):
        a.set_skills("Python")
    assert a.set_skills([]) == []


def test_the_app_never_stores_resume_text():
    app = (REPO / "streamlit_app.py").read_text("utf-8")
    body = app[app.index("def _suggest_skills"):app.index("def skills_card")]
    assert "set_skills(picked)" in body and "resume_text(" in body
    assert "USER.set" not in body.split("def _save_skills")[0]                 # suggesting never writes
    assert "log." not in body and "print(" not in body                          # never logged


# ── in the app (AppTest, owner session, no Firebase: skills kept per session) ─

from test_streamlit_app import _html, app  # noqa: E402,F401


def _accepted(n=1):
    rec = job("Graduate Java Developer", ["Experience with SQL and REST APIs"], uid=f"x|{n}")
    rec.update({"id": f"X_{n}", "url": f"https://x.example/{n}", "location": "Pune · India", "date": "Nov 11, 10:00",
                "notified": True, "reason": "fresher signal"})
    return rec


def test_job_page_shows_quoted_skills_freshness_and_why(app):
    import hashlib
    from config_store import job_key
    rec = _accepted()
    at = app([rec], query={"page": "jobs", "job": hashlib.sha1(job_key(rec).encode()).hexdigest()[:12]})
    assert not at.exception
    html = _html(at)
    assert "Skills this posting names" in html and "“Experience with SQL and REST APIs”" in html
    assert "Freshness:" in html and "Why it is listed" in html
    assert "Add your skills in Settings" in html and "Estimate" not in html      # no estimate without skills


def test_settings_skills_suggest_save_and_estimate(app):
    import hashlib
    from config_store import job_key
    rec = _accepted()
    at = app([rec], page="settings")
    assert not at.exception and "Skills &amp; interview prep" in _html(at)
    at.text_area(key="skills_paste").set_value("Java, SQL, Git and Docker").run()
    at.button(key="btn_skills_suggest").click().run()
    assert set(at.multiselect(key="skills_pick").value) == {"Java", "SQL", "Git", "Docker"}
    at.button(key="btn_skills_save").click().run()
    assert not at.exception
    html = _html(at)
    assert "Using these skills for this session" in html and "Skill gaps" in html and "REST APIs" in html
    at.query_params["job"] = hashlib.sha1(job_key(rec).encode()).hexdigest()[:12]
    at.query_params["page"] = "jobs"
    at.run()
    html = _html(at)
    assert "Match: 67%" in html and "Estimate" in html and "never hides or filters a job" in html
