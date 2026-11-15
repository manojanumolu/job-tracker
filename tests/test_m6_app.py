"""M6 Resume & Job Intelligence in the app itself (AppTest): who sees the
page, direct access, the two admins' separation, and every section's main
flow. Firestore is user_store.MemoryBackend; nothing reaches a real service.

Streamlit's AppTest can't drive a file uploader, so an upload is put into
this browser session exactly as _ri_read leaves it (a parsed document and
its profile) and saved through the page's own Save button."""
from datetime import date
from pathlib import Path

import pytest

import resume_parse
import resume_profile
from test_application_tracker import A_MAIL, B_MAIL, M_MAIL, M_UID, two_admins  # noqa: F401  (fixture)
from test_firebase_auth import _buttons, no_env  # noqa: F401
from test_phase3_user_data import _sign_in, admins, app, google_env, store  # noqa: F401  (fixtures)
from test_streamlit_app import NEW_RECORD, _html, _nav

A_UID, B_UID = "AdminAaaaaaaaaaaaaaaaaaaaaaa1", "AdminBbbbbbbbbbbbbbbbbbbbbbb2"
FIX = Path(__file__).parent / "fixtures" / "m6"
FRESHER_JOB = {
    "title": "Associate Software Engineer", "url": "https://careers.example.com/job/101", "company": "ExampleSoft",
    "location": "Hyderabad, India", "category": "FRESHER", "reason": "fresher", "date": "Nov 14, 10:00",
    "id": "ExampleSoft_Associate_Software_Engineer", "first_seen": "2026-11-14T10:00:00+00:00",
    "evidence": {"checks": {"detail_read": True, "fresher_or_entry_evidence": True},
                 "requirements": [{"text": "B.Tech / B.E. in Computer Science or IT (2025 or 2026 batch)", "importance": "mandatory"},
                                  {"text": "0-1 years of experience; freshers welcome", "importance": "mandatory"},
                                  {"text": "Strong knowledge of Python and SQL", "importance": "mandatory"},
                                  {"text": "Hands-on experience building REST APIs", "importance": "mandatory"},
                                  {"text": "Experience with Docker or AWS is a plus", "importance": "preferred"}]},
}
SENIOR_JOB = {
    "title": "Data Engineer", "url": "https://careers.example.com/job/202", "company": "DataCo",
    "location": "Bengaluru, India", "category": "FRESHER", "reason": "test", "date": "Nov 14, 11:00",
    "id": "DataCo_Data_Engineer", "first_seen": "2026-11-14T11:00:00+00:00",
    "evidence": {"checks": {"detail_read": True},
                 "requirements": [{"text": "Minimum 4 years of experience in data engineering", "importance": "mandatory"},
                                  {"text": "Python, SQL and Spark required", "importance": "mandatory"}]},
}
JOBS = [FRESHER_JOB, SENIOR_JOB, NEW_RECORD]


def _jid(job):
    import hashlib

    import config_store
    return hashlib.sha1(config_store.job_key(job).encode("utf-8")).hexdigest()[:12]


def _parsed(name="cv.txt", path="resume_fresher_swe.txt"):
    doc = resume_parse.read_document(name, (FIX / path).read_bytes())
    return doc, resume_profile.build_profile(doc.text, today=date(2026, 11, 15))


def _lib(store, uid=A_UID):
    return store.for_uid(uid).resume_library(lambda: True)


def _seed(store, uid=A_UID, name="Software Engineer Resume"):
    doc, prof = _parsed()
    return _lib(store, uid).create(name, doc, prof)


def _ri(at, section=None):
    _nav(at, "resume_intel")
    assert at.session_state.page == "resume_intel" and not at.exception, at.exception
    if section:
        at.pills(key="ri_section").set_value(section).run()
        assert not at.exception, at.exception
    return at


def _click(at, key):
    at.button(key=key).click().run()
    assert not at.exception, at.exception
    return at


def _m6_docs(store, uid):
    return [p for p in store.backend.docs if p.startswith(f"users/{uid}/resume")]


# ── who sees it ─────────────────────────────────────────────────────────────

def test_admins_see_the_page_below_the_tracker(app, google_env, store, two_admins):
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    order = [b.key[4:] for b in at.button if (b.key or "").startswith("nav_")]
    assert order[:4] == ["home", "jobs", "applications", "resume_intel"]
    assert at.button(key="nav_resume_intel").label == "Resume & Job Intelligence"
    _ri(at)
    html = _html(at)
    assert "Resume &amp; Job Intelligence" in html or "Resume & Job Intelligence" in html
    assert "No résumés yet" in html


def test_members_never_see_or_reach_it_directly(app, google_env, store, two_admins):
    m = _sign_in(app, google_env, M_UID, M_MAIL, seen=JOBS)
    assert "nav_resume_intel" not in _buttons(m)
    m.session_state["page"] = "resume_intel"          # a direct page address
    m.run()
    assert m.session_state.page == "home" and "Your résumés compared" not in _html(m)
    _nav(m, "jobs")
    assert "btn_ri_jp" not in _buttons(m)              # no résumé panel on job pages either
    assert not [p for p in store.backend.docs if "/resume" in p]


def test_a_url_with_the_page_doesnt_open_it_for_a_member(app, google_env, store, two_admins):
    google_env["firebase"]  # noqa: B018  (fixture ready)
    m = _sign_in(app, google_env, M_UID, M_MAIL, seen=JOBS)
    m.query_params["page"] = "resume_intel"
    m.run()
    assert m.session_state.page != "resume_intel"


def test_without_the_sign_in_gate_there_is_no_page(app):
    at = app([NEW_RECORD])
    assert "nav_resume_intel" not in _buttons(at)
    at.session_state["page"] = "resume_intel"
    at.run()
    assert at.session_state.page == "home"


# ── the two admins ────────────────────────────────────────────────────────────

def test_two_admins_never_see_each_others_resumes(app, google_env, store, two_admins):
    meta = _seed(store, A_UID, "Anna's Data Resume")
    b = _ri(_sign_in(app, google_env, B_UID, B_MAIL, seen=JOBS), "resumes")
    assert "Anna's Data Resume" not in _html(b) and "No résumés yet" in _html(b)
    # B's own library can't name A's résumé: its paths are bound to B's uid
    with pytest.raises(Exception, match="doesn't exist"):
        _lib(store, B_UID).get(meta["id"])
    a = _ri(_sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS), "resumes")
    assert "Anna&#x27;s Data Resume" in _html(a) or "Anna's Data Resume" in _html(a)


# ── My Resumes: save an upload, review, correct, rename, delete ───────────────

def test_an_upload_is_previewed_then_saved_without_the_file(app, google_env, store, two_admins):
    at = _ri(_sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS), "resumes")
    doc, prof = _parsed()
    at.session_state["_ri_pending"] = {"parsed": doc, "profile": prof, "dup": None}
    at.session_state["ri_new_name"] = "Software Engineer Resume"
    at.run()
    html = _html(at)
    assert "Preview — not saved yet" in html and "Skills and their evidence" in html and "p•••@example.com" in html
    assert "priya.sharma@example.com" not in html                 # contact details only masked
    _click(at, "btn_ri_save")
    (meta,) = _lib(store).list_resumes()
    assert meta["name"] == "Software Engineer Resume" and meta["is_default"] and meta["version"] == 1
    stored = str(store.backend.docs)
    assert "Example Fintech" in stored                            # the profile is kept ...
    assert "priya.sharma@example.com" not in stored and "98765 43210" not in stored   # ... contact details masked
    assert "TECHNICAL SKILLS" not in stored                       # ... and never the raw text
    assert at.session_state.get("_ri_pending") is None
    assert at.session_state.ri_review == meta["id"] and "Review &amp; correct" in _html(at)


def test_a_duplicate_upload_needs_a_confirmation(app, google_env, store, two_admins):
    _seed(store)
    at = _ri(_sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS), "resumes")
    doc, prof = _parsed()
    at.session_state["_ri_pending"] = {"parsed": doc, "profile": prof, "dup": _lib(store).find_duplicate(doc.sha256)}
    at.session_state["ri_new_name"] = "Copy"
    at.run()
    assert "already saved as" in _html(at)
    _click(at, "btn_ri_save")
    assert len(_lib(store).list_resumes()) == 1 and "Tick the box" in _html(at)


def test_replacing_needs_a_confirmation_and_bumps_the_version(app, google_env, store, two_admins):
    meta = _seed(store)
    at = _ri(_sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS), "resumes")
    doc, prof = _parsed("v2.txt", "resume_data_analyst.txt")
    at.session_state["_ri_pending"] = {"parsed": doc, "profile": prof, "dup": None}
    at.run()
    at.selectbox(key="ri_target").set_value(meta["id"]).run()
    _click(at, "btn_ri_save")
    assert _lib(store).get(meta["id"])["meta"]["version"] == 1          # not without the tick
    at.checkbox(key="ri_confirm").check().run()
    _click(at, "btn_ri_save")
    after = _lib(store).get(meta["id"])["meta"]
    assert after["version"] == 2 and [v["version"] for v in after["versions"]] == [1, 2]
    assert len(_lib(store).list_resumes()) == 1


def test_review_corrections_are_saved_and_applied(app, google_env, store, two_admins):
    meta = _seed(store)
    at = _ri(_sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS), "resumes")
    _click(at, f"btn_ri_rev_{meta['id']}")
    rid = meta["id"]
    at.multiselect(key=f"ri_c_rm_{rid}").set_value(["Kubernetes"]).run()
    at.multiselect(key=f"ri_c_add_{rid}").set_value(["Kafka"]).run()
    at.radio(key=f"ri_c_xk_{rid}_0").set_value("employment").run()
    _click(at, f"btn_ri_conf_{rid}")
    full = _lib(store).get(rid)
    eff = full["effective"]
    assert "Kubernetes" not in eff["skills"] and eff["skills"]["Kafka"]["status"] == "needs_verification"
    assert eff["experience"][0]["kind"] == "employment" and full["profile"]["experience"][0]["kind"] == "internship"
    assert _lib(store).confirmation(rid)["confirmed"] is True


def test_rename_set_default_and_delete(app, google_env, store, two_admins):
    first = _seed(store, name="General Fresher Resume")
    doc, prof = _parsed("da.txt", "resume_data_analyst.txt")
    second = _lib(store).create("Data Analyst Resume", doc, prof)
    at = _ri(_sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS), "resumes")
    _click(at, f"btn_ri_ren_{second['id']}")
    at.text_input(key=f"ri_ren_{second['id']}").set_value("Data Analyst Resume v2").run()
    _click(at, f"btn_ri_ren_save_{second['id']}")
    _click(at, f"btn_ri_def_{second['id']}")
    metas = {m["id"]: m for m in _lib(store).list_resumes()}
    assert metas[second["id"]]["name"] == "Data Analyst Resume v2" and metas[second["id"]]["is_default"]
    assert not metas[first["id"]]["is_default"]
    _click(at, f"btn_ri_del_{second['id']}")
    _click(at, f"btn_ri_del_yes_{second['id']}")
    left = _lib(store).list_resumes()
    assert [m["id"] for m in left] == [first["id"]] and left[0]["is_default"]
    assert not [p for p in store.backend.docs if second["id"] in p]


def test_export_is_offered_only_after_an_authorised_read(app, google_env, store, two_admins):
    meta = _seed(store)
    at = _ri(_sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS), "resumes")
    assert not [d for d in at.get("download_button")]
    _click(at, f"btn_ri_exp_{meta['id']}")
    assert at.session_state["_ri_export"]["rid"] == meta["id"] and '"profile"' in at.session_state["_ri_export"]["json"]


def test_skills_are_copied_into_settings_not_duplicated(app, google_env, store, two_admins):
    meta = _seed(store)
    at = _ri(_sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS), "resumes")
    _click(at, f"btn_ri_rev_{meta['id']}")
    _click(at, f"btn_ri_m5_{meta['id']}")
    skills = store.for_uid(A_UID).profile()["skills"]
    assert {"Python", "SQL", "Django / Flask", "Databases"} <= set(skills)
    assert not [p for p in store.backend.docs if p.startswith(f"users/{A_UID}/") and "skill" in p.split("/")[2]
                and p.split("/")[2] != "resume_skills"]


# ── Job Match, eligibility and history ────────────────────────────────────────

def test_job_match_ranks_and_shows_a_critical_issue_separately(app, google_env, store, two_admins):
    _seed(store)
    at = _ri(_sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS), "match")
    html = _html(at)
    assert "Associate Software Engineer" in html and "Data Engineer" in html and "Eligibility issue" in html
    # the fresher job ranks above the one with a critical (experience) issue
    assert html.index("Associate Software Engineer") < html.index("Data Engineer")
    keys = [b.key for b in at.button if (b.key or "").startswith("btn_ri_open_")]
    _click(at, next(k for k in keys if _jid(SENIOR_JOB) in k))
    html = _html(at)
    assert "Critical · Experience" in html and "at least 4 year" in html and "Evidence for each skill" in html
    _click(at, "btn_ri_save1")
    lib = _lib(store)
    assert len(lib.matches()) == 1 and lib.history()[0]["kind"] == "match"


def test_job_match_filters(app, google_env, store, two_admins):
    _seed(store)
    at = _ri(_sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS), "match")
    at.selectbox(key="ri_m_el").set_value("Has a critical issue").run()
    html = _html(at)
    assert "Data Engineer" in html and "Associate Software Engineer" not in html
    at.selectbox(key="ri_m_el").set_value("Any").run()
    at.text_input(key="ri_m_q").set_value("associate").run()
    html = _html(at)
    assert "Associate Software Engineer" in html and "Data Engineer" not in html
    at.selectbox(key="ri_m_exp").set_value("3+ yrs").run()
    assert "1 result" not in _html(at) and "0 results" in _html(at)


def test_pasting_a_job_description_changes_the_analysis(app, google_env, store, two_admins):
    _seed(store)
    at = _ri(_sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS), "match")
    import config_store
    jid = _jid(NEW_RECORD)
    at.session_state["ri_job"] = jid
    at.run()
    assert "Insufficient evidence" in _html(at)
    at.text_area(key=f"ri_jd_{jid}").set_value((FIX / "job_backend_fresher.txt").read_text()).run()
    _click(at, f"btn_ri_jd_{jid}")
    html = _html(at)
    assert "the full description you pasted" in html and "Insufficient evidence" not in html.split("Evidence for each skill")[0]
    assert _lib(store).job_text(config_store.job_key(NEW_RECORD)).startswith("Associate Software Engineer")


def test_the_job_page_offers_an_analysis_to_admins(app, google_env, store, two_admins):
    _seed(store)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    at.session_state["page"] = "jobs"
    at.session_state["job_id"] = _jid(FRESHER_JOB)
    at.run()
    assert not at.exception and "Match with your résumé" in _html(at)
    _click(at, "btn_ri_jp")
    assert "estimate" in _html(at) and "Not a probability of being hired" in _html(at)
    _click(at, "btn_ri_jp_open")
    assert at.session_state.page == "resume_intel" and at.session_state.ri_section == "match"


# ── gaps, interview, improvement, history, overview ───────────────────────────

def test_skill_gaps_for_one_job_and_across_jobs(app, google_env, store, two_admins):
    _seed(store)
    at = _ri(_sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS), "gaps")
    assert "Skill gaps worth closing" in _html(at) or "No gaps to show" in _html(at)
    at.session_state["ri_g_mode"] = "job"
    at.run()
    html = _html(at)
    assert "Missing mandatory skills" in html and "Skills already demonstrated" in html


def test_interview_preparation_and_practice_status(app, google_env, store, two_admins):
    _seed(store)
    at = _ri(_sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS), "interview")
    html = _html(at)
    assert "not questions this employer is known to ask" in html and "Generated from your résumé" in html
    key = next(s.key for s in at.selectbox if (s.key or "").startswith("ri_p_"))
    at.selectbox(key=key).set_value("Confident").run()
    assert not at.exception
    assert _lib(store).practice()[key[5:]] == "Confident"
    at.session_state["ri_i_lvl"] = "advanced"
    at.run()
    labels = " ".join(e.label for e in at.expander)
    assert not at.exception and "Explain the GIL" in labels and "100× more users" in labels


def test_improvement_suggestions_accept_and_reject(app, google_env, store, two_admins):
    meta = _seed(store)
    at = _ri(_sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS), "improve")
    html = _html(at)
    assert "[add a real figure you can verify]" in html and "Unclear technical contribution" in html
    acc = next(b.key for b in at.button if (b.key or "").startswith("btn_ri_acc_"))
    rej = [b.key for b in at.button if (b.key or "").startswith("btn_ri_rej_")][1]
    _click(at, acc)
    _click(at, rej)
    d = _lib(store).decisions(meta["id"], "")
    assert sorted(d.values()) == ["accepted", "rejected"]
    assert [b for b in at.get("download_button")]


def test_history_lists_and_clears(app, google_env, store, two_admins):
    meta = _seed(store)
    _lib(store).add_history(meta["id"], "match", "Software Engineer Resume vs X: 80%", {})
    at = _ri(_sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS), "history")
    assert "Software Engineer Resume vs X: 80%" in _html(at)
    at.checkbox(key="ri_hist_confirm").check().run()
    _click(at, "btn_ri_clear")
    assert _lib(store).history() == []


def test_overview_counts_and_weights(app, google_env, store, two_admins):
    _seed(store)
    at = _ri(_sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS))
    html = _html(at)
    assert "Jobs analysed" in html and "Potential eligibility issues" in html and "Most frequently missing skills" in html
    at.number_input(key="ri_w_mandatory").set_value(50).run()
    at.number_input(key="ri_w_evidence").set_value(0).run()
    _click(at, "btn_ri_weights")
    assert _lib(store).weights()["mandatory"] == 50


def test_a_store_failure_shows_a_message_and_the_rest_of_the_app_works(app, google_env, store, two_admins, monkeypatch):
    _seed(store)
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=JOBS)
    real = store.backend.list

    def broken(collection):
        if "/resume" in collection:
            raise RuntimeError("firestore down")
        return real(collection)
    monkeypatch.setattr(store.backend, "list", broken)
    _ri(at)
    assert "unavailable" in _html(at)
    _nav(at, "jobs")
    assert "Associate Software Engineer" in _html(at) and not at.exception
