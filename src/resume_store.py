"""Résumé & Job Intelligence storage (M6): one admin's résumé library,
analyses and preparation progress — private to that admin.

Same isolation model as the Application Tracker (applications.py):

  * **who** — ResumeLibrary asks its ``authorize`` hook at the start of
    every public method. The app's hook re-checks admin rights against the
    sign-in source and that the session's signed-in UID is the UID this
    library is bound to. A refusal, or an error inside the hook, raises
    NotAuthorized (fail closed). Reads, writes, exports and deletes alike.
  * **whose** — a library is bound to ONE uid when it is made (by the app,
    from the server-side signed-in account, through
    UserData.resume_library). Every path starts with users/{that uid}/; no
    method takes a uid. Résumé IDs are random hex checked against a strict
    pattern; job and question keys are SHA-256 hashes — so nothing from a
    request can name another admin's data or leave this admin's tree.

The browser never reaches Firestore (Admin SDK on the server; the security
rules deny every client: config/firestore.rules).

What is stored — and what is NOT:
  * the uploaded **file is never stored** (not in Firestore, not on disk,
    not in Git): it is read in memory (resume_parse) and dropped. Only its
    name, type, size, SHA-256 (duplicate detection) and the structured
    profile built from it are kept. "Replace" uploads a new file and builds
    a new profile; "Preview" shows the extracted information.
  * contact details are kept masked in the profile.

Collections, all under users/{uid}/:
  resumes/{rid}            metadata: name, file_type, file_name, size, sha256,
                           uploaded_at, updated_at, version, versions[],
                           extraction_status, extraction_warnings, pages,
                           is_default, rev
  resume_profiles/{rid}    the structured profile as extracted (schema'd),
                           plus the extracted text's ATS hints (no text)
  resume_skills/{rid}      the person's corrections and confirmations
  resume_matches/{mid}     match summaries (rid x job), with the cache key
  resume_history/{hid}     analysis history (capped at MAX_HISTORY)
  resume_jobtext/{jid}     job descriptions the admin pasted (public job text)
  resume_practice/{qid}    interview practice status
  resume_suggestions/{sid} accepted / rejected improvement suggestions
  resume_settings/main     scoring weights

Retention and deletion: kept until the admin deletes them. Deleting a
résumé deletes its profile, corrections, match summaries, suggestion
decisions and history entries in one go. "Delete all" removes every M6
document of this admin, and deleting the account (UserData.
delete_personal_data) removes all of them too.

No Streamlit import here, so this is unit-testable on its own.
"""

from __future__ import annotations

import functools
import hashlib
import logging
import re
import secrets
import time
import unicodedata
from collections.abc import Callable

import resume_profile
import skill_taxonomy as tax
from user_store import InvalidInput, UserStoreError, valid_uid

log = logging.getLogger("resume_store")

SCHEMA_VERSION = 1
COLLECTIONS = ("resumes", "resume_profiles", "resume_skills", "resume_matches", "resume_history", "resume_jobtext",
               "resume_practice", "resume_suggestions", "resume_settings")
MAX_RESUMES = 20
MAX_HISTORY = 300
MAX_VERSIONS = 10
NAME_MAX = 80
JOB_TEXT_MAX = 20_000
RID_RE = re.compile(r"^[0-9a-f]{20}$")
KEY_RE = re.compile(r"^[0-9a-f]{24}$")
UNAVAILABLE = "Your résumé library is unavailable right now."


class NotAuthorized(PermissionError):
    """Refused: not an admin, or not this library's owner."""


class NotFound(LookupError):
    """No such résumé in this admin's library."""


def _now_iso(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if now is None else now))


def key_of(*parts: str) -> str:
    """A document ID for a job / question / pair: a hash, never the raw value."""
    return hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:24]


def clean_name(value: object) -> str:
    """A résumé name: trimmed, single-spaced, no control/bidi characters,
    1..NAME_MAX characters."""
    if not isinstance(value, str):
        raise InvalidInput("Enter a name for the résumé.")
    name = " ".join("".join(ch for ch in value if unicodedata.category(ch)[0] != "C").split())
    if not name:
        raise InvalidInput("Enter a name for the résumé.")
    if len(name) > NAME_MAX:
        raise InvalidInput(f"Use at most {NAME_MAX} characters for the résumé name.")
    return name


def clean_corrections(c: object, profile: dict) -> dict:
    """Validate a person's corrections against the profile they apply to."""
    if not isinstance(c, dict):
        raise InvalidInput("Corrections must be a mapping.")
    out: dict = {}
    for key in ("remove_skills", "add_skills", "confirm_skills"):
        vals = c.get(key) or []
        if not isinstance(vals, (list, tuple, set)) or not all(tax.known(v) for v in vals):
            raise InvalidInput("Only skills from the skill dictionary can be corrected.")
        out[key] = tax.sort_skills(vals)
    n_exp, n_proj, n_edu = (len(profile.get(k) or []) for k in ("experience", "projects", "education"))
    for key, allowed, n in (("experience_kind", ("employment", "internship"), n_exp),
                            ("project_kind", ("academic_project", "personal_project", "project"), n_proj)):
        m = c.get(key) or {}
        if not isinstance(m, dict):
            raise InvalidInput("Invalid correction.")
        clean = {}
        for idx, kind in m.items():
            i = int(idx) if str(idx).isdigit() else -1
            if not 0 <= i < n or kind not in allowed:
                raise InvalidInput("Invalid correction.")
            clean[str(i)] = kind
        out[key] = clean
    edu = c.get("education") or {}
    if not isinstance(edu, dict):
        raise InvalidInput("Invalid correction.")
    out["education"] = {}
    for idx, fields in edu.items():
        i = int(idx) if str(idx).isdigit() else -1
        if not 0 <= i < n_edu or not isinstance(fields, dict):
            raise InvalidInput("Invalid correction.")
        f = {}
        if "level" in fields:
            if fields["level"] not in resume_profile.LEVELS:
                raise InvalidInput("Invalid degree level.")
            f["level"] = fields["level"]
        if "specialization" in fields:
            if fields["specialization"] not in resume_profile.SPECIALISATIONS and fields["specialization"] != "":
                raise InvalidInput("Invalid specialisation.")
            f["specialization"] = fields["specialization"]
        if "end_year" in fields:
            y = fields["end_year"]
            if not isinstance(y, int) or not 1980 <= y <= 2045:
                raise InvalidInput("Enter a graduation year between 1980 and 2045.")
            f["end_year"] = y
        out["education"][str(i)] = f
    if "name" in c:
        if not isinstance(c["name"], str) or len(c["name"]) > 60:
            raise InvalidInput("Invalid name.")
        out["name"] = " ".join(c["name"].split())
    return out


def _authorized(method):
    """Every public operation: the owner check first (fail closed), then
    storage failures turned into UserStoreError (logged by type only — a
    message could echo a path, and paths contain the uid)."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            allowed = self._authorize() is True
        except Exception:
            allowed = False
        if not allowed:
            log.warning("resume library refused: %s", method.__name__)
            raise NotAuthorized("Only admins can use Résumé & Job Intelligence, and only for their own account.")
        try:
            return method(self, *args, **kwargs)
        except (NotAuthorized, NotFound, InvalidInput, UserStoreError):
            raise
        except Exception as e:
            log.warning("resume library %s failed: %s", method.__name__, type(e).__name__)
            raise UserStoreError(UNAVAILABLE, kind=type(e).__name__) from None
    wrapper.authorized = True
    return wrapper


class ResumeLibrary:
    """One admin's résumés and analyses. Bound to a single uid; every
    public method asks ``authorize`` first."""

    def __init__(self, backend, uid: str, authorize: Callable[[], bool]):
        if not valid_uid(uid):
            raise InvalidInput("not a Firebase UID")
        if not callable(authorize):
            raise TypeError("authorize must be callable")
        self._backend, self.uid, self._authorize = backend, uid, authorize
        self._root = f"users/{uid}"

    # -- paths ----------------------------------------------------------------
    def _p(self, collection: str, doc_id: str) -> str:
        assert collection in COLLECTIONS
        return f"{self._root}/{collection}/{doc_id}"

    def _rid(self, rid: object) -> str:
        if not isinstance(rid, str) or not RID_RE.match(rid):
            raise NotFound("That résumé doesn't exist.")
        return rid

    def _meta(self, rid: str) -> dict:
        doc = self._backend.get(self._p("resumes", self._rid(rid)))
        if not isinstance(doc, dict) or doc.get("id") != rid:
            raise NotFound("That résumé doesn't exist.")
        return doc

    # -- résumés ----------------------------------------------------------------
    @_authorized
    def list_resumes(self) -> list[dict]:
        docs = [d for d in self._backend.list(f"{self._root}/resumes").values()
                if isinstance(d, dict) and RID_RE.match(str(d.get("id", "")))]
        return sorted(docs, key=lambda d: (not d.get("is_default"), d.get("name", "").lower()))

    @_authorized
    def get(self, rid: str) -> dict:
        """{meta, profile (as extracted), corrections, effective (corrected)}."""
        meta = self._meta(rid)
        prof = self._backend.get(self._p("resume_profiles", rid)) or {}
        corr = (self._backend.get(self._p("resume_skills", rid)) or {}).get("corrections") or {}
        profile = prof.get("profile") or {}
        return {"meta": meta, "profile": profile, "hints": prof.get("hints") or {}, "corrections": corr,
                "effective": resume_profile.apply_corrections(profile, corr) if profile else {}}

    @_authorized
    def find_duplicate(self, sha256: str) -> dict | None:
        """The résumé (metadata) whose current file has this SHA-256, if any."""
        for d in self._backend.list(f"{self._root}/resumes").values():
            if isinstance(d, dict) and d.get("sha256") == sha256:
                return d
        return None

    @_authorized
    def create(self, name: object, parsed, profile: dict, now: float | None = None) -> dict:
        """A new résumé from a parsed file (resume_parse.ParsedDocument) and
        its profile. Never overwrites an existing one."""
        name = clean_name(name)
        existing = self._backend.list(f"{self._root}/resumes")
        if len(existing) >= MAX_RESUMES:
            raise InvalidInput(f"You can keep at most {MAX_RESUMES} résumés. Delete one first.")
        if any(d.get("name", "").lower() == name.lower() for d in existing.values() if isinstance(d, dict)):
            raise InvalidInput("You already have a résumé with that name. Choose another name.")
        now = time.time() if now is None else now
        rid = secrets.token_hex(10)
        meta = {"id": rid, "schema": SCHEMA_VERSION, "name": name, "file_type": parsed.file_type,
                "file_name": parsed.file_name, "size": int(parsed.size), "sha256": parsed.sha256,
                "uploaded_at": _now_iso(now), "updated_at": _now_iso(now), "version": 1,
                "versions": [{"version": 1, "file_name": parsed.file_name, "uploaded_at": _now_iso(now),
                              "sha256": parsed.sha256}],
                "extraction_status": parsed.status, "extraction_warnings": list(parsed.warnings)[:10],
                "pages": int(parsed.pages), "is_default": not existing, "rev": 1}
        self._backend.set_many({
            self._p("resumes", rid): meta,
            self._p("resume_profiles", rid): self._profile_doc(rid, parsed, profile, now),
            self._p("resume_skills", rid): {"id": rid, "corrections": {}, "confirmed": False, "updated_at": _now_iso(now)},
        })
        return meta

    @staticmethod
    def _profile_doc(rid: str, parsed, profile: dict, now: float) -> dict:
        import resume_improve
        meta = {"tables": int(parsed.tables), "header_text": parsed.header_text or "", "file_type": parsed.file_type}
        hints = {"tables": int(parsed.tables), "file_type": parsed.file_type,
                 # the text checks (repetition, formats, ATS) made now, so the text itself is never kept
                 "findings": resume_improve.text_findings(profile, parsed.text, meta)}
        return {"id": rid, "schema": resume_profile.SCHEMA, "profile": profile, "hints": hints,
                "built_at": _now_iso(now)}

    @_authorized
    def replace(self, rid: str, parsed, profile: dict, now: float | None = None) -> dict:
        """A new file for an existing résumé: a new version with a new
        profile. Corrections (which refer to the old file's entries) are
        cleared; the version history keeps the old file's name and date."""
        meta = self._meta(rid)
        now = time.time() if now is None else now
        version = int(meta.get("version") or 1) + 1
        versions = (list(meta.get("versions") or []) + [{"version": version, "file_name": parsed.file_name,
                                                         "uploaded_at": _now_iso(now), "sha256": parsed.sha256}])
        meta.update(file_type=parsed.file_type, file_name=parsed.file_name, size=int(parsed.size), sha256=parsed.sha256,
                    updated_at=_now_iso(now), version=version, versions=versions[-MAX_VERSIONS:],
                    extraction_status=parsed.status, extraction_warnings=list(parsed.warnings)[:10],
                    pages=int(parsed.pages), rev=int(meta.get("rev") or 0) + 1)
        self._backend.set_many({
            self._p("resumes", rid): meta,
            self._p("resume_profiles", rid): self._profile_doc(rid, parsed, profile, now),
            self._p("resume_skills", rid): {"id": rid, "corrections": {}, "confirmed": False, "updated_at": _now_iso(now)},
        })
        return meta

    @_authorized
    def rename(self, rid: str, name: object, now: float | None = None) -> dict:
        meta = self._meta(rid)
        name = clean_name(name)
        if any(d.get("name", "").lower() == name.lower() and d.get("id") != rid
               for d in self._backend.list(f"{self._root}/resumes").values() if isinstance(d, dict)):
            raise InvalidInput("You already have a résumé with that name. Choose another name.")
        meta.update(name=name, updated_at=_now_iso(now), rev=int(meta.get("rev") or 0) + 1)
        self._backend.set(self._p("resumes", rid), meta)
        return meta

    @_authorized
    def set_default(self, rid: str) -> None:
        self._meta(rid)
        docs = {}
        for d in self._backend.list(f"{self._root}/resumes").values():
            if isinstance(d, dict) and RID_RE.match(str(d.get("id", ""))) and bool(d.get("is_default")) != (d["id"] == rid):
                docs[self._p("resumes", d["id"])] = {**d, "is_default": d["id"] == rid}
        if docs:
            self._backend.set_many(docs)

    @_authorized
    def save_corrections(self, rid: str, corrections: object, confirmed: bool = False, now: float | None = None) -> dict:
        self._meta(rid)
        prof = (self._backend.get(self._p("resume_profiles", rid)) or {}).get("profile") or {}
        clean = clean_corrections(corrections, prof)
        doc = {"id": rid, "corrections": clean, "confirmed": bool(confirmed), "updated_at": _now_iso(now)}
        if confirmed:
            doc["confirmed_at"] = _now_iso(now)
        self._backend.set(self._p("resume_skills", rid), doc)
        return doc

    @_authorized
    def confirmation(self, rid: str) -> dict:
        self._meta(rid)
        return self._backend.get(self._p("resume_skills", rid)) or {}

    @_authorized
    def delete(self, rid: str) -> int:
        """Delete a résumé and everything derived from it. Returns how many
        documents were deleted."""
        meta = self._meta(rid)
        paths = [self._p(c, rid) for c in ("resumes", "resume_profiles", "resume_skills")]
        for col in ("resume_matches", "resume_suggestions", "resume_history"):
            for doc_id, d in self._backend.list(f"{self._root}/{col}").items():
                if isinstance(d, dict) and d.get("resume_id") == rid:
                    paths.append(self._p(col, doc_id))
        self._backend.delete_many(paths)
        if meta.get("is_default"):
            rest = sorted((d for d in self._backend.list(f"{self._root}/resumes").values() if isinstance(d, dict)),
                          key=lambda d: d.get("uploaded_at", ""))
            if rest:
                self._backend.set(self._p("resumes", rest[0]["id"]), {**rest[0], "is_default": True})
        return len(paths)

    @_authorized
    def export(self, rid: str) -> dict:
        """Everything stored about one résumé (for the admin to download)."""
        meta = self._meta(rid)
        prof = self._backend.get(self._p("resume_profiles", rid)) or {}
        corr = self._backend.get(self._p("resume_skills", rid)) or {}
        matches = [d for d in self._backend.list(f"{self._root}/resume_matches").values()
                   if isinstance(d, dict) and d.get("resume_id") == rid]
        return {"resume": meta, "profile": prof.get("profile") or {}, "corrections": corr.get("corrections") or {},
                "match_summaries": matches, "exported_at": _now_iso()}

    # -- job text the admin pasted ---------------------------------------------
    @_authorized
    def job_text(self, job_key: str) -> str:
        doc = self._backend.get(self._p("resume_jobtext", key_of("job", job_key))) or {}
        return doc.get("text", "") if doc.get("job_key") == job_key else ""

    @_authorized
    def job_texts(self) -> dict[str, str]:
        return {d["job_key"]: d.get("text", "") for d in self._backend.list(f"{self._root}/resume_jobtext").values()
                if isinstance(d, dict) and isinstance(d.get("job_key"), str)}

    @_authorized
    def set_job_text(self, job_key: str, text: object, now: float | None = None) -> None:
        if not isinstance(job_key, str) or not job_key or len(job_key) > 400:
            raise InvalidInput("Unknown job.")
        if not isinstance(text, str):
            raise InvalidInput("Paste the job description as text.")
        text = "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch)[0] != "C").strip()
        if len(text) > JOB_TEXT_MAX:
            raise InvalidInput(f"Paste at most {JOB_TEXT_MAX:,} characters.")
        path = self._p("resume_jobtext", key_of("job", job_key))
        if not text:
            self._backend.delete(path)
            return
        self._backend.set(path, {"job_key": job_key, "text": text, "updated_at": _now_iso(now)})

    # -- match summaries and history -------------------------------------------
    @_authorized
    def save_matches(self, rid: str, items: list[tuple[str, str, dict]], now: float | None = None) -> int:
        """Store match summaries: [(job_key, cache_key, summary)] for one résumé."""
        self._meta(rid)
        docs = {}
        for job_key, ck, summ in items[:1000]:
            docs[self._p("resume_matches", key_of(rid, job_key))] = {
                "resume_id": rid, "job_key": str(job_key)[:400], "cache_key": str(ck)[:40], "summary": summ,
                "at": _now_iso(now)}
        if docs:
            self._backend.set_many(docs)
        return len(docs)

    @_authorized
    def matches(self) -> list[dict]:
        return [d for d in self._backend.list(f"{self._root}/resume_matches").values()
                if isinstance(d, dict) and RID_RE.match(str(d.get("resume_id", "")))]

    @_authorized
    def add_history(self, rid: str, kind: str, label: str, detail: dict, now: float | None = None) -> None:
        self._meta(rid)
        now = time.time() if now is None else now
        hid = f"{int(now * 1000):014d}{secrets.token_hex(5)}"
        self._backend.set(f"{self._root}/resume_history/{hid}",
                          {"id": hid, "resume_id": rid, "kind": str(kind)[:40], "label": str(label)[:200],
                           "detail": detail, "at": _now_iso(now)})
        docs = self._backend.list(f"{self._root}/resume_history")
        if len(docs) > MAX_HISTORY:
            old = sorted(docs)[: len(docs) - MAX_HISTORY]
            self._backend.delete_many(f"{self._root}/resume_history/{h}" for h in old)

    @_authorized
    def history(self, limit: int = 100) -> list[dict]:
        docs = self._backend.list(f"{self._root}/resume_history")
        return [docs[k] for k in sorted(docs, reverse=True)[:limit] if isinstance(docs[k], dict)]

    @_authorized
    def clear_history(self) -> int:
        docs = list(self._backend.list(f"{self._root}/resume_history"))
        self._backend.delete_many(f"{self._root}/resume_history/{h}" for h in docs)
        return len(docs)

    # -- interview practice and suggestion decisions -----------------------------
    @_authorized
    def practice(self) -> dict[str, str]:
        return {d["question_id"]: d.get("status", "") for d in self._backend.list(f"{self._root}/resume_practice").values()
                if isinstance(d, dict) and isinstance(d.get("question_id"), str)}

    @_authorized
    def set_practice(self, question_id: str, status: str, now: float | None = None) -> None:
        import interview_prep
        if status not in interview_prep.PRACTICE:
            raise InvalidInput("Unknown practice status.")
        if not isinstance(question_id, str) or not re.fullmatch(r"[a-z0-9-]{3,40}", question_id):
            raise InvalidInput("Unknown question.")
        self._backend.set(self._p("resume_practice", key_of("q", question_id)),
                          {"question_id": question_id, "status": status, "at": _now_iso(now)})

    @_authorized
    def decisions(self, rid: str, job_key: str) -> dict[str, str]:
        self._meta(rid)
        doc = self._backend.get(self._p("resume_suggestions", key_of(rid, job_key))) or {}
        return dict(doc.get("decisions") or {}) if doc.get("resume_id") == rid else {}

    @_authorized
    def decide(self, rid: str, job_key: str, suggestion_id: str, decision: str, now: float | None = None) -> dict:
        self._meta(rid)
        if decision not in ("accepted", "rejected", ""):
            raise InvalidInput("Unknown decision.")
        if not isinstance(suggestion_id, str) or not re.fullmatch(r"[0-9a-f]{12}", suggestion_id):
            raise InvalidInput("Unknown suggestion.")
        path = self._p("resume_suggestions", key_of(rid, job_key))
        doc = self._backend.get(path) or {"resume_id": rid, "job_key": str(job_key)[:400], "decisions": {}}
        decisions = dict(doc.get("decisions") or {})
        if decision:
            decisions[suggestion_id] = decision
        else:
            decisions.pop(suggestion_id, None)
        doc.update(decisions=decisions, updated_at=_now_iso(now))
        self._backend.set(path, doc)
        return decisions

    # -- settings ------------------------------------------------------------------
    @_authorized
    def weights(self) -> dict:
        import resume_match
        doc = self._backend.get(self._p("resume_settings", "main")) or {}
        return resume_match.clean_weights(doc.get("weights"))

    @_authorized
    def set_weights(self, weights: object) -> dict:
        import resume_match
        clean = resume_match.clean_weights(weights)
        if clean != weights and weights is not None:
            raise InvalidInput("Weights must be whole numbers from 0 to 100 that add up to 100.")
        self._backend.set(self._p("resume_settings", "main"), {"weights": clean, "updated_at": _now_iso()})
        return clean

    @_authorized
    def delete_all(self) -> int:
        """Every M6 document of this admin (the account itself stays)."""
        paths = [f"{self._root}/{c}/{d}" for c in COLLECTIONS for d in self._backend.list(f"{self._root}/{c}")]
        if paths:
            self._backend.delete_many(paths)
        return len(paths)
