"""Application Tracker: the job applications one admin has sent (Nov 2026).

Admin-only, and personal. Being an admin opens the feature; it never opens
another admin's records. Two checks guard every operation:

  * who: ApplicationBook calls its ``authorize`` hook at the start of every
    public method. The app's hook re-checks admin rights against the
    sign-in source (is_owner()) AND that the session's signed-in UID is the
    UID this book is bound to. A refusal raises NotAuthorized, and so does
    an error inside the hook (fail closed).
  * whose: a book is bound to ONE uid when it is made (by the app, from the
    server-side signed-in account, through UserData.application_book), and
    every path it touches starts with users/{that uid}/applications. No
    method takes a uid. Application IDs are random hex, checked against a
    strict pattern, so no value from a request can name another person's
    data or step outside the tree.

The browser never reaches Firestore: the server uses the Admin SDK and the
security rules deny all client access (config/firestore.rules).

Storage, one document per application:

  users/{uid}/applications/{id}
      company, title, url, job_id, location, description
      applied_on, posted_on                 dates, "YYYY-MM-DD" ("" = none)
      progress, email_status                two independent statuses
      source, source_other, salary, resume, notes
      follow_up   {date, done, done_at}     a manual in-app reminder
      interviews  [{id, date, time, round, type, location, interviewer,
                    prep, feedback, outcome}]
      timeline    [{id, at, on, kind, from, to, note}]   append-only history
                  (the "created" event also has email: the first email response)
      job_key                               the tracked job it came from, if any
      created_at, updated_at                server clock, UTC
      deleted_at, purge_after               Recently deleted ("" = not deleted)
      rev                                   bumped on every write

The timeline, interviews and reminder live inside the application's own
document: an application and its history change together in one
compare-and-set write (``rev``), and a list of applications is one read.
Sizes are capped (MAX_TIMELINE, MAX_INTERVIEWS, field limits), far below
Firestore's 1 MB document limit.

Deleting is soft: the application moves to Recently deleted for
RETENTION_DAYS and can be restored. After that it is permanently deleted —
when the owner next opens the tracker (load() deletes what has expired, and
nothing expired is ever shown or restorable meanwhile) and by the daily
retention workflow (``python src/applications.py --purge-expired``), which
covers admins who don't come back. Deleted applications are left out of the
lists, the analytics and the exports.

No Streamlit import here, so this is unit-testable on its own.
"""

from __future__ import annotations

import argparse
import calendar
import functools
import logging
import os
import re
import secrets
import sys
import time
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit

import identity
from user_store import InvalidInput, UserStoreError, valid_uid

log = logging.getLogger("applications")

SCHEMA_VERSION = 1
PROGRESS = ("Applied", "Assessment", "Interview", "Offer Received", "Accepted", "Rejected", "Withdrawn", "On Hold")
EMAIL_STATUSES = ("No Email Received", "Acknowledgement Received", "Assessment Email Received",
                  "Interview Email Received", "Rejection Email Received", "Offer Email Received",
                  "Other Email Received")
NO_EMAIL = EMAIL_STATUSES[0]
SOURCES = ("Company Careers Page", "LinkedIn", "Referral", "Job Portal", "Other")
INTERVIEW_TYPES = ("Online", "Telephone", "In-person")
INTERVIEW_OUTCOMES = ("Awaiting result", "Cleared", "Not cleared", "Rescheduled", "Cancelled")
# the stages that mean an application got as far as an interview (an offer
# or an acceptance comes after one) and the ones that mean an offer
INTERVIEW_REACHED = frozenset({"Interview", "Offer Received", "Accepted"})
OFFER_REACHED = frozenset({"Offer Received", "Accepted"})
RESPONSE_WAIT_DAYS = 14
RETENTION_DAYS = 30
MAX_APPLICATIONS = 2000
MAX_TIMELINE = 300
MAX_INTERVIEWS = 30
LIMITS = {"company": 120, "title": 160, "location": 160, "url": 2000, "job_id": 120, "source_other": 80,
          "salary": 80, "resume": 120, "notes": 5000, "description": 20000, "job_key": 300, "event_note": 1000,
          "round": 80, "iv_location": 300, "interviewer": 120, "prep": 4000, "feedback": 4000, "time": 8}
LABELS = {"company": "Company name", "title": "Job title", "url": "Job URL", "job_id": "Job ID",
          "location": "Location", "applied_on": "Date applied", "posted_on": "Job posting date",
          "progress": "Application progress", "email_status": "Email response", "source": "Application source",
          "source_other": "Custom source", "salary": "Salary/package", "resume": "Resume name/version",
          "notes": "Notes", "description": "Job description", "follow_up": "Follow-up date",
          "job_key": "Tracked job", "round": "Interview round", "date": "Interview date", "time": "Interview time",
          "type": "Interview type", "iv_location": "Location or meeting link", "interviewer": "Interviewer",
          "prep": "Preparation notes", "feedback": "Feedback", "outcome": "Outcome", "event_note": "Note",
          "event_on": "Date it happened"}
EDITABLE = ("company", "title", "url", "job_id", "location", "applied_on", "posted_on", "progress", "email_status",
            "source", "source_other", "salary", "resume", "notes", "description")
ID_RE = re.compile(r"^[0-9a-f]{20}$")
SUB_ID_RE = re.compile(r"^[0-9a-f]{12}$")
EARLIEST = date(2000, 1, 1)
IST = timezone(timedelta(hours=5, minutes=30), "IST")
UNAVAILABLE = "Your applications are unavailable right now."
VIEWS = ("all", "applied", "follow_up_due", "interviews", "offers", "rejected")


class NotAuthorized(PermissionError):
    """The Application Tracker refused: not an admin, or not this book's owner."""


class NotFound(LookupError):
    """No such application in this person's tracker (or it was deleted)."""


class Conflict(RuntimeError):
    """The application kept changing underneath this write (another tab)."""


def today_ist(now: float | None = None) -> date:
    """The tracker's "today": the date in India, where its users are."""
    return datetime.fromtimestamp(time.time() if now is None else now, IST).date()


def _now_iso(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if now is None else now))


def _iso_seconds(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return float(calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")))
    except ValueError:
        return None


def parse_day(value: object) -> date | None:
    """A stored "YYYY-MM-DD" (or a date) as a date; None for "" or junk."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Validation: one path for every way an application is made or changed
# ---------------------------------------------------------------------------

_INVISIBLE = "​‎‏‪‫‬‭‮⁦⁧⁨⁩﻿"


def _text(field: str, value: object, required: bool = False, multiline: bool = False, limit_key: str = "") -> str:
    """A cleaned string: control and direction-override characters dropped,
    whitespace tidied (single-line fields collapse it), within its limit."""
    label = LABELS.get(limit_key or field, field)
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise InvalidInput(f"{label} must be text.")
    keep = "\n\t" if multiline else ""
    value = "".join(ch for ch in value if ch not in _INVISIBLE
                    and (ch in keep or not (ord(ch) < 32 or 0x7F <= ord(ch) < 0xA0)))
    if multiline:
        value = "\n".join(line.rstrip() for line in value.replace("\r", "").strip().split("\n"))
        value = re.sub(r"\n{3,}", "\n\n", value)
    else:
        value = " ".join(value.split())
    if required and not value:
        raise InvalidInput(f"{label} is required.")
    limit = LIMITS[limit_key or field]
    if len(value) > limit:
        raise InvalidInput(f"{label} can be at most {limit} characters.")
    return value


def clean_url(value: object, field: str = "url") -> str:
    """"" or an http(s) address with a host — nothing else is ever stored
    as a link (no javascript:, no relative paths)."""
    url = _text(field, value)
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        raise InvalidInput(f"{LABELS[field]} isn't a valid web address.") from None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise InvalidInput(f"{LABELS[field]} must start with http:// or https://.")
    return url


def _day(field: str, value: object, required: bool = False, latest: date | None = None) -> str:
    label = LABELS[field]
    if value in (None, ""):
        if required:
            raise InvalidInput(f"{label} is required.")
        return ""
    day = parse_day(value)
    if day is None:
        raise InvalidInput(f"{label} isn't a valid date.")
    if day < EARLIEST:
        raise InvalidInput(f"{label} is too far in the past.")
    if latest is not None and day > latest:
        raise InvalidInput(f"{label} can't be in the future.")
    return day.isoformat()


def _choice(field: str, value: object, options: tuple[str, ...], required: bool = True) -> str:
    if value in (None, "") and not required:
        return ""
    if value not in options:
        raise InvalidInput(f"Choose a valid {LABELS[field].lower()}.")
    return value


def clean_fields(raw: dict, today: date | None = None) -> dict:
    """The application's own fields from a form, checked and tidied:
    company, job title and location are required; the date applied is
    required and not in the future; a posting date is optional and not
    after the date applied; progress and email response are each one of
    their own list; the source is one of SOURCES (a custom name only with
    "Other"); text has length limits. Raises InvalidInput with a message
    that names the field."""
    if not isinstance(raw, dict):
        raise InvalidInput("Nothing to save.")
    today = today or today_ist()
    latest = today + timedelta(days=1)            # a browser a few hours ahead of India is fine
    out = {
        "company": _text("company", raw.get("company"), required=True),
        "title": _text("title", raw.get("title"), required=True),
        "location": _text("location", raw.get("location"), required=True),
        "url": clean_url(raw.get("url")),
        "job_id": _text("job_id", raw.get("job_id")),
        "applied_on": _day("applied_on", raw.get("applied_on"), required=True, latest=latest),
        "posted_on": _day("posted_on", raw.get("posted_on"), latest=latest),
        "progress": _choice("progress", raw.get("progress"), PROGRESS),
        "email_status": _choice("email_status", raw.get("email_status"), EMAIL_STATUSES),
        "source": _choice("source", raw.get("source"), SOURCES, required=False),
        "source_other": _text("source_other", raw.get("source_other")),
        "salary": _text("salary", raw.get("salary")),
        "resume": _text("resume", raw.get("resume")),
        "notes": _text("notes", raw.get("notes"), multiline=True),
        "description": _text("description", raw.get("description"), multiline=True),
    }
    if out["posted_on"] and out["posted_on"] > out["applied_on"]:
        raise InvalidInput("Job posting date can't be after the date applied.")
    if out["source"] != "Other":
        out["source_other"] = ""                  # a custom name belongs to "Other" only
    return out


def clean_interview(raw: dict) -> dict:
    """One interview round: date, round and type are required; time is
    "HH:MM" (24 h) or empty; the rest is optional text."""
    if not isinstance(raw, dict):
        raise InvalidInput("Nothing to save.")
    when = raw.get("time")
    if hasattr(when, "strftime"):
        when = when.strftime("%H:%M")
    when = _text("time", when or "")
    if when and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", when):
        raise InvalidInput("Interview time must look like 14:30.")
    return {
        "date": _day("date", raw.get("date"), required=True),
        "time": when,
        "round": _text("round", raw.get("round"), required=True),
        "type": _choice("type", raw.get("type"), INTERVIEW_TYPES),
        "location": _text("location", raw.get("location"), limit_key="iv_location"),
        "interviewer": _text("interviewer", raw.get("interviewer")),
        "prep": _text("prep", raw.get("prep"), multiline=True),
        "feedback": _text("feedback", raw.get("feedback"), multiline=True),
        "outcome": _choice("outcome", raw.get("outcome") or INTERVIEW_OUTCOMES[0], INTERVIEW_OUTCOMES),
    }


# ---------------------------------------------------------------------------
# One admin's applications
# ---------------------------------------------------------------------------

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
            log.warning("application tracker refused: %s", method.__name__)
            raise NotAuthorized("Only admins can use the Application Tracker, and only for their own account.")
        try:
            return method(self, *args, **kwargs)
        except (NotAuthorized, NotFound, Conflict, InvalidInput, UserStoreError):
            raise
        except Exception as e:
            log.warning("application tracker %s failed: %s", method.__name__, type(e).__name__)
            raise UserStoreError(UNAVAILABLE, kind=type(e).__name__) from None
    wrapper.authorized = True
    return wrapper


def _event(kind: str, now: float, on: str, frm: str = "", to: str = "", note: str = "", **extra) -> dict:
    return {"id": secrets.token_hex(6), "at": _now_iso(now), "on": on, "kind": kind,
            "from": frm, "to": to, "note": note, **extra}


def _append(doc: dict, *events: dict) -> None:
    """Add to the history, never rewriting it; past MAX_TIMELINE the oldest
    events after the first (the application's creation) make way."""
    timeline = list(doc.get("timeline") or []) + [e for e in events if e]
    if len(timeline) > MAX_TIMELINE:
        timeline = timeline[:1] + timeline[len(timeline) - MAX_TIMELINE + 1:]
    doc["timeline"] = timeline


class ApplicationBook:
    """Everything one admin tracked. Bound to a single uid; every public
    method asks ``authorize`` first."""

    def __init__(self, backend, uid: str, authorize: Callable[[], bool]):
        if not valid_uid(uid):
            raise InvalidInput("not a Firebase UID")
        if not callable(authorize):
            raise TypeError("authorize must be callable")
        self._backend, self.uid, self._authorize = backend, uid, authorize
        self._col = f"users/{uid}/applications"

    def _path(self, app_id: object) -> str:
        if not isinstance(app_id, str) or not ID_RE.match(app_id):
            raise NotFound("That application doesn't exist.")
        return f"{self._col}/{app_id}"

    def _read(self, app_id: str, deleted: bool | None = False, now: float | None = None) -> dict:
        """One application. ``deleted``: False = only a live one, True = only
        one in Recently deleted, None = either."""
        doc = self._backend.get(self._path(app_id))
        if not isinstance(doc, dict) or doc.get("id") != app_id:
            raise NotFound("That application doesn't exist.")
        if deleted is not None and bool(doc.get("deleted_at")) != deleted:
            raise NotFound("That application was deleted." if doc.get("deleted_at") else
                           "That application isn't in Recently deleted.")
        if expired(doc, now):
            raise NotFound("That application was permanently deleted.")
        return doc

    def _change(self, app_id: str, mutate: Callable[[dict], None], now: float | None = None,
                deleted: bool | None = False) -> dict:
        """Read, change, then write only if nobody wrote since (``rev``):
        a write that lost a race is redone on the fresh copy, so two tabs
        never erase each other's timeline events."""
        now = time.time() if now is None else now
        for _ in range(4):
            cur = self._read(app_id, deleted, now)
            new = {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v)
                   for k, v in cur.items()}
            mutate(new)
            new["updated_at"], new["rev"] = _now_iso(now), int(cur.get("rev") or 0) + 1
            if self._backend.update_if(self._path(app_id), {"rev": cur.get("rev")}, new):
                return new
        raise Conflict("This application kept changing in another tab. Reload and try again.")

    # -- reading ---------------------------------------------------------------
    @_authorized
    def load(self, now: float | None = None) -> list[dict]:
        """Every application this person has, Recently deleted included,
        after permanently deleting those past their RETENTION_DAYS there."""
        docs = [d for d_id, d in self._backend.list(self._col).items()
                if isinstance(d, dict) and d.get("id") == d_id and ID_RE.match(d_id)]
        gone = [d for d in docs if expired(d, now)]
        if gone:
            self._backend.delete_many([self._path(d["id"]) for d in gone])
        return [d for d in docs if not expired(d, now)]

    @_authorized
    def get(self, app_id: str) -> dict:
        """A live application (not one in Recently deleted)."""
        return self._read(app_id)

    # -- creating and editing ------------------------------------------------
    @_authorized
    def create(self, raw: dict, job_key: str = "", now: float | None = None) -> dict:
        """A new application from either way of adding one (a fetched job
        page or typed in): the same checks, the same record. Duplicates are
        the caller's to warn about (find_duplicates) — never blocked here."""
        now = time.time() if now is None else now
        fields = clean_fields(raw, today_ist(now))
        live = [d for d in self._backend.list(self._col).values() if isinstance(d, dict) and not d.get("deleted_at")]
        if len(live) >= MAX_APPLICATIONS:
            raise InvalidInput(f"The tracker holds at most {MAX_APPLICATIONS} applications.")
        app_id = secrets.token_hex(10)
        doc = {"id": app_id, "schema": SCHEMA_VERSION, **fields,
               "job_key": _text("job_key", job_key),
               "follow_up": {"date": "", "done": False, "done_at": ""}, "interviews": [], "timeline": [],
               "created_at": _now_iso(now), "updated_at": _now_iso(now), "deleted_at": "", "purge_after": "",
               "rev": 1}
        _append(doc, _event("created", now, fields["applied_on"], to=fields["progress"], email=fields["email_status"]))
        if follow := _day("follow_up", raw.get("follow_up")):
            doc["follow_up"]["date"] = follow
        if not self._backend.create(self._path(app_id), doc):
            raise Conflict("Couldn't save — please try again.")
        return doc

    @_authorized
    def update(self, app_id: str, raw: dict, now: float | None = None) -> dict:
        """Save the edit form. Changes of progress and of email response
        each add their own timeline event (one never changes the other);
        any other edited field is noted by name. The follow-up date rides
        along when ``raw`` has one."""
        now = time.time() if now is None else now
        fields = clean_fields(raw, today_ist(now))
        follow = _day("follow_up", raw.get("follow_up")) if "follow_up" in raw else None
        on = today_ist(now).isoformat()

        def mutate(doc):
            events = []
            for field, kind in (("progress", "progress"), ("email_status", "email")):
                if fields[field] != doc.get(field):
                    events.append(_event(kind, now, on, doc.get(field) or "", fields[field]))
            edited = [LABELS[f] for f in EDITABLE if f not in ("progress", "email_status")
                      and fields[f] != (doc.get(f) or "")]
            if edited:
                events.append(_event("edited", now, on, note=", ".join(edited)))
            doc.update(fields)
            if follow is not None and follow != (doc.get("follow_up") or {}).get("date", ""):
                _set_follow_up(doc, follow, now)
                events.append(_event("follow_up", now, on, to=follow or ""))
            _append(doc, *events)
        return self._change(app_id, mutate, now)

    @_authorized
    def set_status(self, app_id: str, progress: str | None = None, email_status: str | None = None,
                   on: object = None, note: str = "", now: float | None = None) -> dict:
        """Record what happened: a new application progress, a new email
        response, or both — each its own timeline event with the date it
        happened (``on``, default today) and an optional note. Passing one
        leaves the other exactly as it is."""
        now = time.time() if now is None else now
        if progress is not None:
            _choice("progress", progress, PROGRESS)
        if email_status is not None:
            _choice("email_status", email_status, EMAIL_STATUSES)
        day = _day("event_on", on, latest=today_ist(now) + timedelta(days=1)) or today_ist(now).isoformat()
        note = _text("note", note, multiline=True, limit_key="event_note")

        def mutate(doc):
            events = []
            if progress is not None and progress != doc.get("progress"):
                events.append(_event("progress", now, day, doc.get("progress") or "", progress, note))
                doc["progress"] = progress
            if email_status is not None and email_status != doc.get("email_status"):
                events.append(_event("email", now, day, doc.get("email_status") or "", email_status,
                                     "" if events else note))
                doc["email_status"] = email_status
            if not events and note:
                events.append(_event("note", now, day, note=note))
            if not events:
                raise InvalidInput("Nothing changed — pick a new status or write a note.")
            _append(doc, *events)
        return self._change(app_id, mutate, now)

    # -- interviews ----------------------------------------------------------
    @_authorized
    def save_interview(self, app_id: str, raw: dict, interview_id: str | None = None,
                       now: float | None = None) -> dict:
        """Add an interview round (``interview_id`` None) or change one. An
        application has as many rounds as it needs, up to MAX_INTERVIEWS."""
        now = time.time() if now is None else now
        iv = clean_interview(raw)
        if interview_id is not None and not (isinstance(interview_id, str) and SUB_ID_RE.match(interview_id)):
            raise NotFound("That interview round doesn't exist.")

        def mutate(doc):
            rounds = [dict(r) for r in doc.get("interviews") or [] if isinstance(r, dict)]
            on = today_ist(now).isoformat()
            if interview_id is None:
                if len(rounds) >= MAX_INTERVIEWS:
                    raise InvalidInput(f"An application can have at most {MAX_INTERVIEWS} interview rounds.")
                rounds.append({"id": secrets.token_hex(6), **iv})
                _append(doc, _event("interview", now, on, to=iv["round"], note=f"Scheduled for {iv['date']}"))
            else:
                idx = next((i for i, r in enumerate(rounds) if r.get("id") == interview_id), None)
                if idx is None:
                    raise NotFound("That interview round doesn't exist.")
                old = rounds[idx]
                rounds[idx] = {"id": interview_id, **iv}
                if old.get("outcome") != iv["outcome"]:
                    _append(doc, _event("interview_outcome", now, on, old.get("outcome") or "", iv["outcome"],
                                        note=iv["round"]))
            rounds.sort(key=lambda r: (r.get("date") or "", r.get("time") or ""))
            doc["interviews"] = rounds
        return self._change(app_id, mutate, now)

    @_authorized
    def delete_interview(self, app_id: str, interview_id: str, now: float | None = None) -> dict:
        now = time.time() if now is None else now

        def mutate(doc):
            rounds = [r for r in doc.get("interviews") or [] if isinstance(r, dict)]
            keep = [r for r in rounds if r.get("id") != interview_id]
            if len(keep) == len(rounds):
                raise NotFound("That interview round doesn't exist.")
            gone = next(r for r in rounds if r.get("id") == interview_id)
            doc["interviews"] = keep
            _append(doc, _event("interview_removed", now, today_ist(now).isoformat(), note=gone.get("round") or ""))
        return self._change(app_id, mutate, now)

    # -- follow-up reminder (manual; never completed by the calendar) --------
    @_authorized
    def set_follow_up(self, app_id: str, day: object, now: float | None = None) -> dict:
        """Set or change the follow-up date; None/"" clears the reminder."""
        now = time.time() if now is None else now
        value = _day("follow_up", day)

        def mutate(doc):
            _set_follow_up(doc, value, now)
            _append(doc, _event("follow_up", now, today_ist(now).isoformat(), to=value))
        return self._change(app_id, mutate, now)

    @_authorized
    def complete_follow_up(self, app_id: str, done: bool = True, now: float | None = None) -> dict:
        """Mark the reminder done (or open it again). Only this marks it
        done: a date passing never does."""
        now = time.time() if now is None else now

        def mutate(doc):
            fu = dict(doc.get("follow_up") or {})
            if not fu.get("date"):
                raise InvalidInput("This application has no follow-up date.")
            fu.update({"done": done is True, "done_at": _now_iso(now) if done is True else ""})
            doc["follow_up"] = fu
            _append(doc, _event("follow_up_done" if done is True else "follow_up_reopened", now,
                                today_ist(now).isoformat(), to=fu["date"]))
        return self._change(app_id, mutate, now)

    # -- Recently deleted ----------------------------------------------------
    @_authorized
    def soft_delete(self, app_id: str, now: float | None = None) -> dict:
        """Move to Recently deleted, restorable for RETENTION_DAYS."""
        now = time.time() if now is None else now

        def mutate(doc):
            doc["deleted_at"], doc["purge_after"] = _now_iso(now), _now_iso(now + RETENTION_DAYS * 86400)
            _append(doc, _event("deleted", now, today_ist(now).isoformat()))
        return self._change(app_id, mutate, now)

    @_authorized
    def restore(self, app_id: str, now: float | None = None) -> dict:
        now = time.time() if now is None else now

        def mutate(doc):
            doc["deleted_at"], doc["purge_after"] = "", ""
            _append(doc, _event("restored", now, today_ist(now).isoformat()))
        return self._change(app_id, mutate, now, deleted=True)

    @_authorized
    def purge(self, app_id: str, now: float | None = None) -> None:
        """Permanently delete one application from Recently deleted (a live
        one must be deleted first, so nothing vanishes in one click)."""
        self._read(app_id, deleted=True, now=now)
        self._backend.delete(self._path(app_id))

    @_authorized
    def find_duplicates(self, raw: dict, exclude_id: str = "", job_key: str = "") -> list[tuple[dict, str]]:
        """Live applications that may be this one (see find_duplicates)."""
        docs = [d for d in self._backend.list(self._col).values() if isinstance(d, dict)]
        return find_duplicates(docs, raw, exclude_id, job_key)


def _set_follow_up(doc: dict, day: str, now: float) -> None:
    old = doc.get("follow_up") if isinstance(doc.get("follow_up"), dict) else {}
    # a new date is a new reminder: not done yet
    doc["follow_up"] = ({"date": day, "done": False, "done_at": ""} if day != old.get("date")
                        else {"date": day, "done": old.get("done") is True, "done_at": old.get("done_at") or ""})


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------

def expired(doc: dict, now: float | None = None) -> bool:
    """Has this application been in Recently deleted past its RETENTION_DAYS?
    Judged from deleted_at (the server clock), so a damaged purge_after can
    never keep a record longer."""
    deleted = _iso_seconds(doc.get("deleted_at")) if isinstance(doc, dict) else None
    if deleted is None:
        return False
    return (time.time() if now is None else now) >= deleted + RETENTION_DAYS * 86400


def purge_expired_everywhere(store, now: float | None = None) -> int:
    """The daily retention run (a server process, no signed-in person):
    permanently delete, in every account, the applications past their days
    in Recently deleted. Reads nothing else and returns only a count."""
    gone = 0
    for uid in store.user_ids():
        col = f"users/{uid}/applications"
        paths = [f"{col}/{d_id}" for d_id, d in store.backend.list(col).items()
                 if ID_RE.match(d_id) and isinstance(d, dict) and expired(d, now)]
        if paths:
            store.backend.delete_many(paths)
            gone += len(paths)
    return gone


# ---------------------------------------------------------------------------
# Views, reminders, duplicates and analytics — pure functions over records
# already read through an ApplicationBook
# ---------------------------------------------------------------------------

def live(docs: Iterable[dict]) -> list[dict]:
    """Applications not in Recently deleted."""
    return [d for d in docs if isinstance(d, dict) and not d.get("deleted_at")]


def recently_deleted(docs: Iterable[dict], now: float | None = None) -> list[dict]:
    return sorted((d for d in docs if isinstance(d, dict) and d.get("deleted_at") and not expired(d, now)),
                  key=lambda d: d.get("deleted_at") or "", reverse=True)


def days_left(doc: dict, now: float | None = None) -> int:
    """Whole days until a deleted application is permanently deleted."""
    deleted = _iso_seconds(doc.get("deleted_at"))
    if deleted is None:
        return RETENTION_DAYS
    left = deleted + RETENTION_DAYS * 86400 - (time.time() if now is None else now)
    return max(0, int(left // 86400))


def follow_up_state(doc: dict, today: date) -> str:
    """"" (no reminder), "done", "overdue", "today" or "upcoming"."""
    fu = doc.get("follow_up") if isinstance(doc.get("follow_up"), dict) else {}
    day = parse_day(fu.get("date"))
    if day is None:
        return ""
    if fu.get("done") is True:
        return "done"
    return "overdue" if day < today else "today" if day == today else "upcoming"


def follow_up_buckets(docs: Iterable[dict], today: date) -> dict[str, list[dict]]:
    """Live applications with a reminder, by state: overdue (oldest first),
    today, upcoming (soonest first) and done (latest done first)."""
    out = {"overdue": [], "today": [], "upcoming": [], "done": []}
    for d in live(docs):
        state = follow_up_state(d, today)
        if state:
            out[state].append(d)
    for key in ("overdue", "today", "upcoming"):
        out[key].sort(key=lambda d: (d["follow_up"]["date"], (d.get("company") or "").lower()))
    out["done"].sort(key=lambda d: d["follow_up"].get("done_at") or "", reverse=True)
    return out


def awaiting_response(doc: dict) -> bool:
    """Applied, and nothing back yet but (at most) an acknowledgement."""
    return doc.get("progress") == "Applied" and doc.get("email_status") in (NO_EMAIL, "Acknowledgement Received")


def summary(docs: Iterable[dict]) -> dict[str, int]:
    """The four summary cards, over live applications."""
    items = live(docs)
    return {"total": len(items), "awaiting": sum(1 for d in items if awaiting_response(d)),
            "interviews": sum(1 for d in items if d.get("progress") == "Interview"),
            "offers": sum(1 for d in items if d.get("progress") in OFFER_REACHED)}


def in_view(doc: dict, view: str, today: date) -> bool:
    if view == "applied":
        return doc.get("progress") == "Applied"
    if view == "follow_up_due":
        return follow_up_state(doc, today) in ("overdue", "today")
    if view == "interviews":
        return doc.get("progress") == "Interview"
    if view == "offers":
        return doc.get("progress") in OFFER_REACHED
    if view == "rejected":
        return doc.get("progress") == "Rejected"
    return True


def _within(day: date | None, start: date | None, end: date | None) -> bool:
    if start is None and end is None:
        return True
    if day is None:
        return False
    return (start is None or day >= start) and (end is None or day <= end)


def filter_apps(docs: Iterable[dict], query: str = "", progress: Iterable[str] = (), email: Iterable[str] = (),
                applied: tuple = (None, None), follow_up: tuple = (None, None), view: str = "all",
                today: date | None = None) -> list[dict]:
    """Live applications matching the search (company or job title) and
    every chosen filter; an empty filter means "any"."""
    today = today or today_ist()
    q = " ".join((query or "").casefold().split())
    progress, email = set(progress or ()), set(email or ())
    out = []
    for d in live(docs):
        if q and q not in f"{d.get('company') or ''} {d.get('title') or ''}".casefold():
            continue
        if progress and d.get("progress") not in progress:
            continue
        if email and d.get("email_status") not in email:
            continue
        if not _within(parse_day(d.get("applied_on")), *applied):
            continue
        fu = d.get("follow_up") if isinstance(d.get("follow_up"), dict) else {}
        if not _within(parse_day(fu.get("date")), *follow_up):
            continue
        if not in_view(d, view, today):
            continue
        out.append(d)
    return out


SORTS = {"applied_new": "Date applied — newest", "applied_old": "Date applied — oldest",
         "updated": "Last updated", "company": "Company A–Z", "follow_up": "Follow-up date"}


def sort_apps(docs: list[dict], key: str = "applied_new") -> list[dict]:
    if key == "applied_old":
        return sorted(docs, key=lambda d: (d.get("applied_on") or "", d.get("created_at") or ""))
    if key == "updated":
        return sorted(docs, key=lambda d: d.get("updated_at") or "", reverse=True)
    if key == "company":
        return sorted(docs, key=lambda d: ((d.get("company") or "").casefold(), (d.get("title") or "").casefold()))
    if key == "follow_up":
        # reminders that are still open first, soonest first; then the rest
        def k(d):
            fu = d.get("follow_up") if isinstance(d.get("follow_up"), dict) else {}
            return (not fu.get("date") or fu.get("done") is True, fu.get("date") or "9999")
        return sorted(docs, key=k)
    return sorted(docs, key=lambda d: (d.get("applied_on") or "", d.get("created_at") or ""), reverse=True)


_LEGAL = re.compile(r"\b(private|pvt|limited|ltd|llp|llc|inc|incorporated|corp|corporation|co|company|plc|gmbh|"
                    r"technologies|technology|solutions|services|india)\b")


def _norm(text: object) -> str:
    text = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", text).split())


def norm_company(name: object) -> str:
    """"Infosys Ltd." and "infosys" are the same company for duplicates."""
    plain = _norm(name)
    return " ".join(_LEGAL.sub(" ", plain).split()) or plain


def _norm_job_id(value: object) -> str:
    return re.sub(r"[^0-9a-z]", "", _norm(value))


def find_duplicates(docs: Iterable[dict], raw: dict, exclude_id: str = "", job_key: str = "") -> list[tuple[dict, str]]:
    """Live applications that may be the same as ``raw`` — each with the
    reason. Matches, strongest first:

      * the same tracked job (job_key, from "I've applied" on a job card)
      * the same job link (identity.canonical_url: no tracking parameters,
        no trailing slash, no "www.")
      * the same job ID at the same company
      * the same company and the same job title — unless the two have
        different job IDs, or different locations: those are separate
        requisitions, not duplicates

    Never blocks anything: the caller warns, the person decides."""
    url = identity.canonical_url(raw.get("url") or "") if (raw.get("url") or "").strip() else ""
    job_id, company, title = _norm_job_id(raw.get("job_id")), norm_company(raw.get("company")), _norm(raw.get("title"))
    location = _norm(raw.get("location"))
    out = []
    for d in live(docs):
        if d.get("id") == exclude_id:
            continue
        same_company = bool(company) and norm_company(d.get("company")) == company
        other_id, other_loc = _norm_job_id(d.get("job_id")), _norm(d.get("location"))
        if job_key and d.get("job_key") == job_key:
            reason = "Added from the same job card"
        elif url and (d.get("url") or "").strip() and identity.canonical_url(d["url"]) == url:
            reason = "Same job link"
        elif job_id and other_id == job_id and same_company:
            reason = "Same job ID at this company"
        elif (same_company and title and _norm(d.get("title")) == title
              and not (job_id and other_id and job_id != other_id)
              and not (location and other_loc and location != other_loc)):
            reason = "Same company and job title"
        else:
            continue
        out.append((d, reason))
    return out


def _history(doc: dict, field: str) -> list[str]:
    """Every value ``field`` has had, oldest first, the current one last."""
    kind = "progress" if field == "progress" else "email"
    seen = []
    for e in doc.get("timeline") or []:
        if not isinstance(e, dict):
            continue
        if e.get("kind") == "created" and field == "progress":
            seen.append(e.get("to") or "")
        elif e.get("kind") == "created" and field == "email_status":
            seen.append(e.get("email") or NO_EMAIL)
        elif e.get("kind") == kind:
            seen += [e.get("from") or "", e.get("to") or ""]
    seen.append(doc.get(field) or "")
    return [s for s in seen if s]


def reached_interview(doc: dict) -> bool:
    """Got as far as an interview at any time: progress was ever Interview,
    Offer Received or Accepted, or an interview round is recorded."""
    return bool(set(_history(doc, "progress")) & INTERVIEW_REACHED) or bool(doc.get("interviews"))


def received_offer(doc: dict) -> bool:
    """Received an offer at any time (progress Offer Received or Accepted,
    or an offer email) — even if it later went another way."""
    return (bool(set(_history(doc, "progress")) & OFFER_REACHED)
            or "Offer Email Received" in _history(doc, "email_status"))


def first_response_days(doc: dict) -> int | None:
    """Days from applying to the first recorded email response, from the
    date given when the response was recorded. None when no response was
    recorded through a status change (an application saved with a response
    already set has no known response date)."""
    applied = parse_day(doc.get("applied_on"))
    for e in doc.get("timeline") or []:
        if isinstance(e, dict) and e.get("kind") == "email" and e.get("from") == NO_EMAIL and e.get("to") != NO_EMAIL:
            day = parse_day(e.get("on"))
            if applied is None or day is None or day < applied:
                return None
            return (day - applied).days
    return None


def no_response(doc: dict, today: date) -> bool:
    """Applied at least RESPONSE_WAIT_DAYS ago with nothing recorded since:
    no email response and progress still Applied. Never counted as a
    rejection."""
    applied = parse_day(doc.get("applied_on"))
    return (applied is not None and (today - applied).days >= RESPONSE_WAIT_DAYS
            and doc.get("email_status") == NO_EMAIL and doc.get("progress") == "Applied")


def _pct(part: int, whole: int) -> float | None:
    return None if not whole else round(100.0 * part / whole, 1)


def analytics(docs: Iterable[dict], start: date | None = None, end: date | None = None,
              today: date | None = None) -> dict:
    """The analytics section, over live applications applied within
    [start, end] (None = no bound; the default is all time). Each
    application counts once in every figure.

      eligible               live applications in the range
      interview_rate         reached an interview / eligible × 100
      rejection_rate         progress now Rejected / eligible × 100
      offers                 applications that received an offer
      avg_response_days      mean days to the first recorded email response,
                             over the ``responded`` applications that have one
      no_response            applied 14+ days ago, no response recorded
      per_month              [("YYYY-MM", n)], every month in the range shown
      by_company             [(company, n)], most applications first

    Rates are None with no eligible applications (never a misleading 0 %).
    An application with no response is never counted as rejected."""
    today = today or today_ist()
    items = [d for d in live(docs) if _within(parse_day(d.get("applied_on")), start, end)]
    n = len(items)
    interviewed = sum(1 for d in items if reached_interview(d))
    rejected = sum(1 for d in items if d.get("progress") == "Rejected")
    waits = [w for w in (first_response_days(d) for d in items) if w is not None]
    months = Counter((d.get("applied_on") or "")[:7] for d in items if parse_day(d.get("applied_on")))
    per_month = []
    if months:
        first, last = min(months), max(months)
        y, m = int(first[:4]), int(first[5:7])
        while f"{y:04d}-{m:02d}" <= last:
            per_month.append((f"{y:04d}-{m:02d}", months.get(f"{y:04d}-{m:02d}", 0)))
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    names: dict[str, Counter] = {}
    for d in items:
        names.setdefault(norm_company(d.get("company")), Counter())[(d.get("company") or "").strip()] += 1
    by_company = sorted(((c.most_common(1)[0][0], sum(c.values())) for c in names.values()),
                        key=lambda x: (-x[1], x[0].casefold()))
    return {"eligible": n, "interviewed": interviewed, "interview_rate": _pct(interviewed, n),
            "rejected": rejected, "rejection_rate": _pct(rejected, n),
            "offers": sum(1 for d in items if received_offer(d)),
            "responded": len(waits), "avg_response_days": round(sum(waits) / len(waits), 1) if waits else None,
            "no_response": [d for d in items if no_response(d, today)],
            "per_month": per_month, "by_company": by_company}


# ---------------------------------------------------------------------------
# The daily retention run
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Application Tracker maintenance.")
    parser.add_argument("--purge-expired", action="store_true",
                        help=f"permanently delete applications deleted more than {RETENTION_DAYS} days ago")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="[applications] %(message)s")
    if not args.purge_expired:
        parser.print_help()
        return 0
    from user_store import configure
    store, problem = configure({"FIREBASE_SERVICE_ACCOUNT": os.environ.get("FIREBASE_SERVICE_ACCOUNT", "")},
                               os.environ.get("FIREBASE_PROJECT_ID", ""))
    if store is None:
        log.info("Personal data is not set up%s — nothing to do", f" ({problem})" if problem else "")
        return 1 if problem else 0
    try:
        gone = purge_expired_everywhere(store)
    except Exception as e:                    # never a traceback (it could carry a path, and paths hold UIDs)
        log.error("retention run stopped: %s", type(e).__name__)
        return 1
    log.info("permanently deleted %d expired application(s)", gone)
    return 0


if __name__ == "__main__":
    sys.exit(main())
