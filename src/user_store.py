"""Per-user data, in Firestore, keyed only by the Firebase UID.

Shared (unchanged, in the repository): job facts and their job_key
identity, the company catalogue, scanner state and the eligibility rules.
Personal (here), one tree per person:

  users/{uid}                    profile: email, display_name (from the sign-in
                                 provider), preferred_name (chosen by the
                                 person), avatar (chosen by the person),
                                 created_at,
                                 last_login_at, onboarding, notifications
                                 (incl. the email tailoring), preferences,
                                 watch_all
  users/{uid}/companies/{id}     watchlist: company IDs from the shared catalogue
  users/{uid}/dismissed/{id}     jobs this person dismissed (job_key inside)
  users/{uid}/deliveries/{id}    per-user email ledger: claim -> send -> finalize
  users/{uid}/applications/{id}  Application Tracker (admins only; applications.py)

Isolation is structural: a UserData object is bound to ONE uid when it is
created (by the app, from the server-side signed-in account) and every
path it touches starts with users/{that uid}. No method takes a uid, and
document IDs for jobs and companies are SHA-256 hashes, so no value from a
request can name another person's data or escape the user's tree.

The server talks to Firestore with the Firebase Admin SDK (a service
account kept in the app's secrets, never sent to a browser). Direct client
access stays denied by the Firestore security rules (firestore.rules).

Failures never fall back to shared data: every Firestore error surfaces as
UserStoreError with a message that is safe to show.

Account state (Firebase Authentication is the source of truth, by UID):
  disabled  refused at sign-in (Firebase itself refuses a fresh sign-in),
            and an open session is signed out at its next check (every
            few minutes); the notifier skips the account
  deleted   no personal data is read or written for that UID any more and
            the notifier skips it; existing data stays, unreachable
  deleted, then signs in with Google again
            Firebase creates a NEW account with a NEW UID, so the person
            starts with an empty profile — the old users/{old uid} data is
            never reattached (UIDs are never reused, and email is never
            used to find data). They come back as a Member; admin rights
            still follow JT_ADMIN_EMAILS (a verified email), exactly as for
            anyone else. Policy (approved by the owner, 2026-10-07):
            deleting an account is a RESET, not a block — disabling is the
            way to keep someone out, and there is no deletion blocklist.

No Streamlit import here, so this is unit-testable on its own.
"""

from __future__ import annotations

import calendar
import functools
import hashlib
import json
import logging
import random
import re
import secrets
import threading
import time
from collections.abc import Iterable, Mapping

log = logging.getLogger("user_store")

SCHEMA_VERSION = 1
UID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
# A browser session of an email sign-in: "<uid>.<token>" in the jt_session
# cookie. Only the token's sha256 is stored (users/{uid}/sessions/{hash}),
# so the database never holds a usable token.
SESSION_TOKEN_RE = re.compile(r"^([A-Za-z0-9_-]{1,128})\.([A-Za-z0-9_-]{40,64})$")
SESSION_MAX_S = 30 * 86400              # never longer than Streamlit's own Google sign-in cookie
NOTIFY_MODES = ("general", "tailored")
ONBOARDING_PENDING, ONBOARDING_COMPLETE = "pending", "complete"
DELIVERY_CLAIMED, DELIVERY_SENT, DELIVERY_PENDING = "claimed", "sent", "pending"
# Nov 2026: the same delivery states as the shared email (config_store):
# "sending" is written just before the message goes to Gmail; an abandoned
# "sending" becomes "uncertain" and is never resent; definite failures retry
# after a backoff and end as "failed".
DELIVERY_SENDING, DELIVERY_UNCERTAIN, DELIVERY_FAILED = "sending", "uncertain", "failed"
DELIVERY_SCHEMA = 2                     # claims made by this code carry it; older ones can't be trusted as unsent
DELIVERY_CLAIM_TTL_S = 2 * 3600
DELIVERY_MAX_ATTEMPTS = 5
DELIVERY_BACKOFF_S = (15 * 60, 30 * 60, 3600, 2 * 3600)
MAX_PREF_ITEMS, MAX_PREF_LEN = 50, 80
DISPLAY_NAME_MAX = 60                                      # characters, after trimming
# profile avatars a person can pick: the company marks in brand_logos.py
# ("" = their initials, the default)
AVATAR_CHOICES = ("google", "amazon", "microsoft", "apple", "meta", "nvidia", "ibm", "intel", "accenture", "deloitte")
SERVICE_ACCOUNT_SECTION = "firebase_service_account"     # [firebase_service_account] in st.secrets
SERVICE_ACCOUNT_KEY = "FIREBASE_SERVICE_ACCOUNT"          # or the JSON as one string
APP_NAME = "job-tracker-user-data"
UNAVAILABLE = "Your personal data is unavailable right now."


class UserStoreError(Exception):
    """The personal-data store failed. ``str(e)`` is safe to show; ``kind``
    is the underlying error's class name (e.g. "PermissionDenied"), never its
    message — a category an admin can act on, with no data in it."""

    def __init__(self, message: str = "", kind: str = ""):
        super().__init__(message)
        self.kind = kind


class InvalidInput(ValueError):
    """A value this module refuses (unknown company, bad preference, ...).
    Only these pass through as ValueError: any other error — including a
    ValueError raised inside the Firebase SDK — becomes UserStoreError."""


def _hash_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now_iso(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if now is None else now))


def _iso_seconds(value: object) -> float | None:
    """Seconds since the epoch for a stamp written by _now_iso, else None."""
    if not isinstance(value, str):
        return None
    try:
        return float(calendar.timegm(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")))
    except ValueError:
        return None


def valid_uid(uid: object) -> bool:
    """A Firebase-generated UID: one safe path segment (Firestore reserves
    IDs shaped like __x__)."""
    return (isinstance(uid, str) and bool(UID_RE.match(uid))
            and not (uid.startswith("__") and uid.endswith("__")))


# ---------------------------------------------------------------------------
# Storage backends: Firestore (production) and in-memory (tests, local runs)
# ---------------------------------------------------------------------------

class MemoryBackend:
    """Documents in a dict, with Firestore's create-if-absent and
    compare-and-set semantics. Thread-safe, like concurrent runs."""

    def __init__(self):
        self.docs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def get(self, path: str) -> dict | None:
        with self._lock:
            doc = self.docs.get(path)
            return json.loads(json.dumps(doc)) if doc is not None else None

    def create(self, path: str, data: dict) -> bool:
        with self._lock:
            if path in self.docs:
                return False
            self.docs[path] = json.loads(json.dumps(data))
            return True

    def set(self, path: str, data: dict, merge: bool = False) -> None:
        with self._lock:
            base = self.docs.get(path, {}) if merge else {}
            self.docs[path] = {**base, **json.loads(json.dumps(data))}

    def delete(self, path: str) -> None:
        with self._lock:
            self.docs.pop(path, None)

    def set_many(self, docs: dict[str, dict]) -> None:
        """Several whole-document writes in one go (Firestore: one batch)."""
        with self._lock:
            for path, data in docs.items():
                self.docs[path] = json.loads(json.dumps(data))

    def delete_many(self, paths: Iterable[str]) -> None:
        with self._lock:
            for path in paths:
                self.docs.pop(path, None)

    def list(self, collection: str) -> dict[str, dict]:
        prefix = collection.rstrip("/") + "/"
        with self._lock:
            return {p[len(prefix):]: json.loads(json.dumps(d)) for p, d in self.docs.items()
                    if p.startswith(prefix) and "/" not in p[len(prefix):]}

    def update_if(self, path: str, expected: dict, data: dict) -> bool:
        with self._lock:
            cur = self.docs.get(path)
            if cur is None or any(cur.get(k) != v for k, v in expected.items()):
                return False
            self.docs[path] = {**cur, **json.loads(json.dumps(data))}
            return True


_ABORT_RETRIES = 8


def _retry_aborted(attempt):
    """Run ``attempt()`` again when Firestore aborts it for contention (its
    documented "retry" signal), with jittered backoff. Each attempt re-reads
    state, so a loser ends with a clean "not mine" instead of an error."""
    from google.api_core.exceptions import Aborted
    for n in range(_ABORT_RETRIES):
        try:
            return attempt()
        except Aborted:
            if n == _ABORT_RETRIES - 1:
                raise
            time.sleep(random.uniform(0.01, 0.05) * (n + 1))


class FirestoreBackend:
    """The same operations on a google.cloud.firestore client (from the
    Firebase Admin SDK). create() is atomic (fails if the document exists);
    update_if() is a compare-and-set (a last_update_time precondition)."""

    def __init__(self, client):
        self.client = client

    def get(self, path):
        snap = self.client.document(path).get()
        return snap.to_dict() if snap.exists else None

    def create(self, path, data):
        from google.api_core.exceptions import AlreadyExists

        def attempt():
            try:
                self.client.document(path).create(data)
            except AlreadyExists:
                return False
            return True
        return _retry_aborted(attempt)

    def set(self, path, data, merge=False):
        self.client.document(path).set(data, merge=merge)

    def delete(self, path):
        self.client.document(path).delete()

    # Firestore commits at most 500 writes per batch
    _BATCH = 500

    def set_many(self, docs):
        """Whole-document writes as batched commits: one round trip per 500
        documents instead of one per document (dismissing 27 jobs was 27
        sequential round trips)."""
        items = list(docs.items())
        for i in range(0, len(items), self._BATCH):
            batch = self.client.batch()
            for path, data in items[i:i + self._BATCH]:
                batch.set(self.client.document(path), data)
            batch.commit()

    def delete_many(self, paths):
        paths = list(paths)
        for i in range(0, len(paths), self._BATCH):
            batch = self.client.batch()
            for path in paths[i:i + self._BATCH]:
                batch.delete(self.client.document(path))
            batch.commit()

    def list(self, collection):
        return {s.id: s.to_dict() for s in self.client.collection(collection).stream()}

    def update_if(self, path, expected, data):
        """Compare-and-set on one document: read, check, then write only if
        nobody changed it since (a last_update_time precondition). Of
        several concurrent writers exactly one commits; the rest get False.
        Nothing is locked, so contention never leaves everyone empty-handed."""
        from google.api_core.exceptions import FailedPrecondition, NotFound
        ref = self.client.document(path)

        def attempt():
            snap = ref.get()
            cur = snap.to_dict() if snap.exists else None
            if cur is None or any(cur.get(k) != v for k, v in expected.items()):
                return False
            try:
                ref.update(data, option=self.client.write_option(last_update_time=snap.update_time))
            except (FailedPrecondition, NotFound):
                return False
            return True
        return _retry_aborted(attempt)


class FirebaseIdentity:
    """Server-side lookups in Firebase Authentication (Admin SDK)."""

    def __init__(self, app):
        self.app = app

    def google_account(self, google_sub: str) -> dict | None:
        """The Firebase account linked to this Google account (by Google's
        stable subject ID, never by email): {"uid", "disabled"} or None."""
        from firebase_admin import auth
        result = auth.get_users([auth.ProviderIdentifier("google.com", google_sub)], app=self.app)
        for user in result.users:
            return {"uid": user.uid, "disabled": bool(user.disabled)}
        return None

    def account(self, uid: str) -> dict | None:
        """The Firebase account with this UID: {"uid", "email",
        "email_verified", "disabled", "tokens_valid_after"}, or None if it no
        longer exists. tokens_valid_after is in seconds (0 = never revoked):
        Firebase moves it when a password changes or tokens are revoked."""
        from firebase_admin import auth
        try:
            user = auth.get_user(uid, app=self.app)
        except auth.UserNotFoundError:
            return None
        return {"uid": user.uid, "email": (user.email or "").strip().lower(),
                "email_verified": bool(user.email_verified), "disabled": bool(user.disabled),
                "tokens_valid_after": (user.tokens_valid_after_timestamp or 0) / 1000}

    def delete_account(self, uid: str) -> None:
        """Delete the Firebase Authentication account with this UID (Admin
        SDK, server side). An account that is already gone counts as done."""
        from firebase_admin import auth
        try:
            auth.delete_user(uid, app=self.app)
        except auth.UserNotFoundError:
            pass


# ---------------------------------------------------------------------------
# One person's data
# ---------------------------------------------------------------------------

def _guard(method):
    """Turn any storage failure into UserStoreError (logged by type only:
    a message could echo a path, and paths contain the uid)."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except (UserStoreError, InvalidInput):
            raise
        except Exception as e:
            log.warning("user store %s failed: %s", method.__name__, type(e).__name__)
            raise UserStoreError(UNAVAILABLE, kind=type(e).__name__) from None
    return wrapper


def session_revoked(created_at: float | None, account: dict | None) -> bool:
    """Did Firebase revoke this account's sign-ins (a password change, say)
    after the session began? A session whose start is unknown counts as
    revoked once there has been a revocation. No revocation: False."""
    try:
        valid_after = float((account or {}).get("tokens_valid_after") or 0)
    except (TypeError, ValueError):
        return True
    return valid_after > 0 and (created_at is None or created_at < valid_after)


def clean_display_name(value: object) -> str:
    """A name a person chose for themselves: surrounding whitespace trimmed
    and runs of whitespace collapsed to one space. Empty names, names over
    DISPLAY_NAME_MAX characters and control characters are refused."""
    if not isinstance(value, str):
        raise InvalidInput("Enter a name.")
    name = " ".join(value.split())
    if not name:
        raise InvalidInput("Enter a name — it can't be empty.")
    if len(name) > DISPLAY_NAME_MAX:
        raise InvalidInput(f"Use at most {DISPLAY_NAME_MAX} characters.")
    if any(ord(ch) < 32 or 0x7F <= ord(ch) < 0xA0 or ch in "\u200b\u200e\u200f\u202a\u202b\u202c\u202d\u202e\ufeff"
           for ch in name):
        raise InvalidInput("The name contains characters that can't be shown.")
    return name


def _clean_list(values: object) -> list[str]:
    if not isinstance(values, (list, tuple, set, frozenset)):
        raise InvalidInput("expected a list")
    out = []
    for v in values:
        if not isinstance(v, str) or not v.strip() or len(v) > MAX_PREF_LEN:
            raise InvalidInput("invalid preference value")
        if v.strip() not in out:
            out.append(v.strip())
    if len(out) > MAX_PREF_ITEMS:
        raise InvalidInput("too many preference values")
    return out


class UserData:
    """Everything one signed-in person owns. Bound to a single uid."""

    def __init__(self, backend, uid: str):
        if not valid_uid(uid):
            raise InvalidInput("not a Firebase UID")
        self._backend, self.uid = backend, uid
        self._root = f"users/{uid}"

    # -- profile -------------------------------------------------------------
    @_guard
    def ensure_profile(self, email: str, display_name: str = "", email_verified: bool = False,
                       now: float | None = None) -> tuple[dict, bool]:
        """Create users/{uid} on first sign-in (atomically: two sessions
        signing in at once still make one document); afterwards only the
        login time and identity fields are refreshed. Returns (profile,
        created)."""
        stamp = _now_iso(now)
        identity = {"email": (email or "").strip().lower(), "display_name": display_name or "",
                    "email_verified": email_verified is True, "last_login_at": stamp}
        fresh = {"uid": self.uid, "schema": SCHEMA_VERSION, "created_at": stamp, **identity,
                 "onboarding": {"status": ONBOARDING_PENDING},
                 # alerts off; no email tailoring (= every qualifying job, once alerts are on)
                 "notifications": {"enabled": False, "mode": "general", "enabled_at": None,
                                   "tailoring": {"locations": [], "job_families": []}},
                 "preferences": {"mode": "general", "job_families": [], "locations": [], "experience": [],
                                 "job_types": [], "work_modes": []},
                 # a new person follows no company yet: the shared catalogue isn't theirs
                 "watch_all": False}
        if self._backend.create(self._root, fresh):
            return fresh, True
        self._backend.set(self._root, identity, merge=True)
        return self._backend.get(self._root) or fresh, False

    @_guard
    def profile(self) -> dict | None:
        return self._backend.get(self._root)

    @_guard
    def set_display_name(self, name: object) -> str:
        """The name this person wants shown (users/{uid}.preferred_name).
        Kept apart from display_name, which every sign-in refreshes from
        the provider, so a chosen name survives signing in again. Touches
        nothing else: not the email, UID, role, companies, dismissals,
        alerts, tailoring or preferences. Returns what was saved."""
        clean = clean_display_name(name)
        self._backend.set(self._root, {"preferred_name": clean}, merge=True)
        return clean

    @_guard
    def set_avatar(self, choice: object) -> str:
        """The profile avatar this person picked (users/{uid}.avatar): one
        of AVATAR_CHOICES, or "" for their initials. Display only — it
        identifies nothing and touches nothing else. Returns what was saved."""
        if choice in (None, ""):
            choice = ""
        elif choice not in AVATAR_CHOICES:
            raise InvalidInput("That avatar isn't available.")
        self._backend.set(self._root, {"avatar": choice}, merge=True)
        return choice

    # -- browser sessions of an email sign-in ------------------------------------
    def _session_path(self, token_hash: str) -> str:
        if not isinstance(token_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", token_hash):
            raise InvalidInput("not a session")
        return f"{self._root}/sessions/{token_hash}"

    @_guard
    def start_session(self, email: str, name: str = "", email_verified: bool = False,
                      now: float | None = None) -> tuple[str, str]:
        """A new browser session for this person's email sign-in: returns
        (cookie value "<uid>.<token>", the token's hash). The token is random
        (secrets.token_urlsafe) and only its hash is stored."""
        now = time.time() if now is None else now
        token = secrets.token_urlsafe(32)
        token_hash = _hash_id(token)
        self._backend.set(self._session_path(token_hash), {
            "uid": self.uid, "provider": "password", "email": (email or "").strip().lower(), "name": name or "",
            "email_verified": email_verified is True, "created_at": _now_iso(now), "last_active_at": _now_iso(now),
            "expires_at": _now_iso(now + SESSION_MAX_S)})
        return f"{self.uid}.{token}", token_hash

    @_guard
    def touch_session(self, token_hash: str, now: float | None = None) -> None:
        """This session was just used (only if it still exists: an ended
        session is never brought back)."""
        path = self._session_path(token_hash)
        if self._backend.get(path) is not None:
            self._backend.set(path, {"last_active_at": _now_iso(now)}, merge=True)

    @_guard
    def end_session(self, token_hash: str) -> None:
        """Sign out: this browser session can't be used again."""
        self._backend.delete(self._session_path(token_hash))

    @_guard
    def delete_personal_data(self) -> int:
        """Delete everything this person owns in the store — users/{uid} and
        its companies, dismissed, deliveries, sessions and applications — and nothing
        else (shared listings, catalogue and settings live in GitHub, not
        here). Returns how many documents were deleted."""
        paths = [f"{self._root}/{sub}/{doc_id}" for sub in ("companies", "dismissed", "deliveries", "sessions", "applications")
                 for doc_id in self._backend.list(f"{self._root}/{sub}")]
        if paths:
            self._backend.delete_many(paths)
        existed = self._backend.get(self._root) is not None
        self._backend.delete(self._root)
        return len(paths) + (1 if existed else 0)

    def application_book(self, authorize):
        """This person's Application Tracker (applications.ApplicationBook),
        bound to this same uid. ``authorize`` is asked before every
        operation; the app passes its admin-and-owner check."""
        from applications import ApplicationBook
        return ApplicationBook(self._backend, self.uid, authorize)

    @_guard
    def record_activity(self, now: float | None = None) -> str:
        """This person just used the app (users/{uid}.last_active_at, a
        server clock time — never one the browser sent). The app's 48-hour
        inactivity sign-out reads it when a sign-in is restored in a new
        browser session. Touches nothing else. Returns the time."""
        stamp = _now_iso(now)
        self._backend.set(self._root, {"last_active_at": stamp}, merge=True)
        return stamp

    @_guard
    def complete_onboarding(self, now: float | None = None) -> None:
        self._backend.set(self._root, {"onboarding": {"status": ONBOARDING_COMPLETE,
                                                      "completed_at": _now_iso(now)}}, merge=True)

    # -- watchlist (company IDs from the shared catalogue) ---------------------
    @_guard
    def watchlist(self) -> set[str]:
        return {d.get("company_id") for d in self._backend.list(f"{self._root}/companies").values()
                if isinstance(d.get("company_id"), str)}

    @_guard
    def watch(self, company_id: str, catalogue_ids: Iterable[str], now: float | None = None) -> None:
        """Follow a company. Only IDs in the shared catalogue are accepted;
        the catalogue itself is never copied."""
        if not isinstance(company_id, str) or company_id not in set(catalogue_ids):
            raise InvalidInput("unknown company")
        self._backend.set(f"{self._root}/companies/{_hash_id(company_id)}",
                          {"company_id": company_id, "added_at": _now_iso(now)})

    @_guard
    def set_watch_all(self, watch_all: bool) -> None:
        """Company scope: every tracked company (True, the default) or only
        the companies in the watchlist (False)."""
        self._backend.set(self._root, {"watch_all": watch_all is True}, merge=True)

    @_guard
    def unwatch(self, company_id: str) -> None:
        if isinstance(company_id, str):
            self._backend.delete(f"{self._root}/companies/{_hash_id(company_id)}")

    # -- dismissed jobs (personal; the shared scanner history is untouched) ---
    @_guard
    def dismissed(self) -> set[str]:
        return {d.get("job_key") for d in self._backend.list(f"{self._root}/dismissed").values()
                if isinstance(d.get("job_key"), str)}

    @_guard
    def dismiss(self, job_keys: Iterable[str], now: float | None = None) -> None:
        stamp = _now_iso(now)
        docs = {f"{self._root}/dismissed/{_hash_id(key)}": {"job_key": key, "dismissed_at": stamp}
                for key in job_keys if isinstance(key, str) and key}
        if len(docs) > 1 and hasattr(self._backend, "set_many"):
            self._backend.set_many(docs)              # "Dismiss all": one batched write, not one per job
        else:
            for path, data in docs.items():
                self._backend.set(path, data)

    @_guard
    def restore(self, job_keys: Iterable[str]) -> None:
        paths = [f"{self._root}/dismissed/{_hash_id(key)}" for key in job_keys if isinstance(key, str) and key]
        if len(paths) > 1 and hasattr(self._backend, "delete_many"):
            self._backend.delete_many(paths)
        else:
            for path in paths:
                self._backend.delete(path)

    # -- notification preferences --------------------------------------------
    @_guard
    def notification_settings(self) -> dict:
        """{"enabled", "mode", "email"}. The address is always the verified
        email of the signed-in identity — it can't be set to anything else."""
        return notification_settings_from(self._backend.get(self._root) or {})

    @_guard
    def set_notifications(self, enabled: bool, mode: str = "general") -> dict:
        if mode not in NOTIFY_MODES:
            raise InvalidInput("unknown notification mode")
        prof = self._backend.get(self._root) or {}
        if enabled and prof.get("email_verified") is not True:
            raise InvalidInput("alerts need a verified email address")
        old = prof.get("notifications") if isinstance(prof.get("notifications"), dict) else {}
        # alerts cover jobs found from the moment they were turned on — never a
        # backlog of everything found before
        since = old.get("enabled_at") if old.get("enabled") is True and old.get("enabled_at") else _now_iso()
        # the email tailoring rides along unchanged (a merge may replace the whole map)
        self._backend.set(self._root, {"notifications": {**old, "enabled": enabled is True, "mode": mode,
                                                         "enabled_at": since if enabled is True else None}},
                          merge=True)
        return self.notification_settings()

    @_guard
    def set_email_tailoring(self, locations=(), job_families=()) -> dict:
        """Tailor this person's alert EMAILS (users/{uid}.notifications.
        tailoring): only jobs in any of ``locations`` and any of
        ``job_families``; empty lists mean no tailoring (every qualifying job
        from their companies). Values must come from the shared vocabulary.
        Touches nothing else — not the job lists' preferences, not alerts on
        or off, not anyone else's settings. Returns what was saved."""
        from job_filters import VOCABULARY
        chosen = {"locations": _clean_list(locations), "job_families": _clean_list(job_families)}
        for field, values in chosen.items():
            if any(v not in VOCABULARY[field] for v in values):
                raise InvalidInput(f"unknown {field} value")
        prof = self._backend.get(self._root) or {}
        old = prof.get("notifications") if isinstance(prof.get("notifications"), dict) else {}
        self._backend.set(self._root, {"notifications": {**old, "tailoring": chosen}}, merge=True)
        return chosen

    # -- general / tailored preferences ----------------------------------------
    @_guard
    def set_preferences(self, mode: str = "general", job_families=(), locations=(), experience=(),
                        job_types=(), work_modes=()) -> dict:
        """Save the job-list preferences; returns exactly what was stored
        (users/{uid}.preferences)."""
        if mode not in NOTIFY_MODES:
            raise InvalidInput("unknown preference mode")
        from job_filters import VOCABULARY
        chosen = {"job_families": _clean_list(job_families), "locations": _clean_list(locations),
                  "experience": _clean_list(experience), "job_types": _clean_list(job_types),
                  "work_modes": _clean_list(work_modes)}
        for field, values in chosen.items():
            if any(v not in VOCABULARY[field] for v in values):
                raise InvalidInput(f"unknown {field} value")
        saved = {"mode": mode, **chosen}
        self._backend.set(self._root, {"preferences": saved}, merge=True)
        return saved

    @_guard
    def personal_view(self) -> dict:
        """What this person asked to see: {"mode", "prefs", "watch_all",
        "watchlist", "notifications"}. Missing fields get the defaults."""
        prof = self._backend.get(self._root) or {}
        return view_from(prof, self.watchlist() if prof.get("watch_all") is False else set())

    @_guard
    def snapshot(self) -> dict:
        """Everything the app shows about this person, in three reads:
        {"profile", "dismissed", "watchlist"} — so a page can be drawn
        without asking Firestore again for each piece. The reads run side by
        side (one round trip of waiting instead of three)."""
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=3) as pool:
            prof = pool.submit(self._backend.get, self._root)
            dismissed, watchlist = pool.submit(self.dismissed), pool.submit(self.watchlist)
            return {"profile": prof.result() or {}, "dismissed": dismissed.result(), "watchlist": watchlist.result()}

    # -- per-user delivery ledger: claim -> send -> finalize --------------------
    def _delivery_path(self, job_key: str) -> str:
        if not isinstance(job_key, str) or not job_key:
            raise InvalidInput("job_key required")
        return f"{self._root}/deliveries/{_hash_id(job_key)}"

    @_guard
    def claim_delivery(self, job_key: str, run_id: str, now: float | None = None,
                       stats: dict | None = None) -> bool:
        """Claim the right to email this person about this job — a
        compare-and-set, so exactly one concurrent claimant wins. Claimable:
        no ledger entry yet; "pending" once its retry backoff is over; or a
        "claimed" entry abandoned for DELIVERY_CLAIM_TTL_S by a run that
        never started sending (recovered). Never: "sending" (an abandoned one
        becomes "uncertain"), "sent", "failed", "uncertain", or a claim from
        before DELIVERY_SCHEMA (the old code could have sent it: "uncertain")."""
        now = time.time() if now is None else now
        stats = stats if stats is not None else {}
        path = self._delivery_path(job_key)
        claimed = {"job_key": job_key, "state": DELIVERY_CLAIMED, "claim_id": run_id, "claimed_at": _now_iso(now),
                   "v": DELIVERY_SCHEMA}
        if self._backend.create(path, claimed):
            return True
        cur = self._backend.get(path) or {}
        state = cur.get("state")
        if state == DELIVERY_PENDING:
            due = _iso_seconds(cur.get("next_attempt_at"))
            if due is not None and now < due:
                return False                              # still in its retry backoff
            return self._backend.update_if(path, {"state": DELIVERY_PENDING, "claim_id": cur.get("claim_id")}, claimed)
        if state not in (DELIVERY_CLAIMED, DELIVERY_SENDING):
            return False
        expected = {"state": state, "claim_id": cur.get("claim_id")}
        if state == DELIVERY_CLAIMED and cur.get("v") != DELIVERY_SCHEMA:
            if self._backend.update_if(path, expected, {"state": DELIVERY_UNCERTAIN, "settled_at": _now_iso(now)}):
                stats["legacy_claims_uncertain"] = stats.get("legacy_claims_uncertain", 0) + 1
            return False
        since = _iso_seconds(cur.get("sending_at" if state == DELIVERY_SENDING else "claimed_at"))
        if since is not None and now - since <= DELIVERY_CLAIM_TTL_S:
            return False                                  # a live run may still own it
        if state == DELIVERY_SENDING:
            if self._backend.update_if(path, expected, {"state": DELIVERY_UNCERTAIN, "settled_at": _now_iso(now)}):
                stats["abandoned_sending_uncertain"] = stats.get("abandoned_sending_uncertain", 0) + 1
            return False
        won = self._backend.update_if(path, expected, {**claimed, "recovered_from": cur.get("claim_id")})
        if won:
            stats["abandoned_claims_recovered"] = stats.get("abandoned_claims_recovered", 0) + 1
        return won

    @_guard
    def mark_sending(self, job_key: str, run_id: str, now: float | None = None) -> bool:
        """Just before the message goes to Gmail: only the run holding the
        claim can mark it, and only a marked job may be sent."""
        return self._backend.update_if(self._delivery_path(job_key), {"state": DELIVERY_CLAIMED, "claim_id": run_id},
                                       {"state": DELIVERY_SENDING, "sending_at": _now_iso(now)})

    @_guard
    def save_alert_settings(self, enabled: bool, mode: str, watch_all: bool) -> dict:
        """The "Alert emails" form in one read and one write: on/off, the
        mode and the company scope — exactly what set_watch_all() followed by
        set_notifications() store (the same verified-email rule, the same
        enabled_at "no backlog" stamp, tailoring kept). Returns the fields
        written, so the caller can show them without reading them back."""
        if mode not in NOTIFY_MODES:
            raise InvalidInput("unknown notification mode")
        prof = self._backend.get(self._root) or {}
        if enabled and prof.get("email_verified") is not True:
            raise InvalidInput("alerts need a verified email address")
        old = prof.get("notifications") if isinstance(prof.get("notifications"), dict) else {}
        since = old.get("enabled_at") if old.get("enabled") is True and old.get("enabled_at") else _now_iso()
        fields = {"watch_all": watch_all is True,
                  "notifications": {**old, "enabled": enabled is True, "mode": mode,
                                    "enabled_at": since if enabled is True else None}}
        self._backend.set(self._root, fields, merge=True)
        return fields

    @_guard
    def record_alert_sent(self, now: float | None = None) -> str:
        """A personal alert email was just sent: start the next cadence
        window (see scan_schedule.MEMBER_ALERT_HOURS). Returns the time."""
        stamp = _now_iso(now)
        self._backend.set(self._root, {"alert_cadence": {"last_sent_at": stamp}}, merge=True)
        return stamp

    @_guard
    def finalize_delivery(self, job_key: str, run_id: str, sent: bool | str, now: float | None = None) -> bool:
        """Only the run holding the claim can settle it. ``sent``: True or
        "sent" -> "sent" (final); "uncertain" -> "uncertain" (final, never
        resent); False or "not_sent" -> back to "pending" after a backoff,
        or "failed" after DELIVERY_MAX_ATTEMPTS. Works from "sending" (the
        normal path) and from "claimed" (nothing was marked or sent)."""
        now = time.time() if now is None else now
        outcome = "sent" if sent is True else "not_sent" if sent is False else sent
        path = self._delivery_path(job_key)
        cur = self._backend.get(path) or {}
        if cur.get("claim_id") != run_id or cur.get("state") not in (DELIVERY_CLAIMED, DELIVERY_SENDING):
            return False
        if outcome == "sent":
            data = {"state": DELIVERY_SENT, "sent_at": _now_iso(now)}
        elif outcome == "uncertain":
            data = {"state": DELIVERY_UNCERTAIN, "settled_at": _now_iso(now)}
        else:
            prior = cur.get("attempts")
            prior = prior if isinstance(prior, int) and not isinstance(prior, bool) and prior > 0 else 0
            attempts = prior + (1 if cur.get("state") == DELIVERY_SENDING else 0)
            if attempts >= DELIVERY_MAX_ATTEMPTS:
                data = {"state": DELIVERY_FAILED, "attempts": attempts, "settled_at": _now_iso(now)}
            else:
                wait = DELIVERY_BACKOFF_S[min(max(attempts, 1), len(DELIVERY_BACKOFF_S)) - 1] if attempts else 0
                data = {"state": DELIVERY_PENDING, "attempts": attempts, "released_at": _now_iso(now),
                        "next_attempt_at": _now_iso(now + wait)}
        return self._backend.update_if(path, {"state": cur.get("state"), "claim_id": run_id}, data)

    @_guard
    def delivery(self, job_key: str) -> dict | None:
        return self._backend.get(self._delivery_path(job_key))


PREFERENCE_KEYS = ("job_families", "locations", "experience", "job_types", "work_modes")


def view_from(profile: dict, watchlist: set) -> dict:
    """personal_view() computed from an already-read profile + watchlist."""
    prof = profile if isinstance(profile, dict) else {}
    prefs = prof.get("preferences") if isinstance(prof.get("preferences"), dict) else {}
    notif = prof.get("notifications") if isinstance(prof.get("notifications"), dict) else {}
    mode = notif.get("mode") if notif.get("mode") in NOTIFY_MODES else "general"
    watch_all = prof.get("watch_all") is not False
    return {"mode": mode,
            "prefs": {k: [v for v in prefs.get(k) or [] if isinstance(v, str)] for k in PREFERENCE_KEYS},
            "watch_all": watch_all, "watchlist": set() if watch_all else set(watchlist),
            "notifications": notif, "tailoring": tailoring_from(prof),
            "alerts_last_sent": alerts_last_sent_from(prof)}


def alerts_last_sent_from(profile: dict) -> str:
    """When this person's last personal alert email went out (ISO time), or
    "" — users/{uid}.alert_cadence.last_sent_at, written only by
    user_alerts.py after a successful send. Its own top-level field: the app
    never writes it, so no settings save can wipe it."""
    prof = profile if isinstance(profile, dict) else {}
    cad = prof.get("alert_cadence") if isinstance(prof.get("alert_cadence"), dict) else {}
    value = cad.get("last_sent_at")
    return value if isinstance(value, str) else ""


TAILORING_KEYS = ("locations", "job_families")


def tailoring_from(profile: dict) -> dict:
    """The email tailoring saved in a profile: {"locations", "job_families"}.
    A profile from before tailoring existed has none: both lists empty,
    which means every qualifying job — exactly the alerts it had before."""
    prof = profile if isinstance(profile, dict) else {}
    notif = prof.get("notifications") if isinstance(prof.get("notifications"), dict) else {}
    t = notif.get("tailoring") if isinstance(notif.get("tailoring"), dict) else {}
    return {k: [v for v in t.get(k) or [] if isinstance(v, str)] if isinstance(t.get(k), list) else []
            for k in TAILORING_KEYS}


def display_name_from(profile: dict) -> str:
    """The name a person chose (preferred_name), or "" when they haven't
    chosen one (or the saved value isn't a usable name) — the app then
    falls back to the sign-in provider's name, as before."""
    prof = profile if isinstance(profile, dict) else {}
    try:
        return clean_display_name(prof.get("preferred_name"))
    except InvalidInput:
        return ""


def avatar_from(profile: dict) -> str:
    """The avatar a person picked, or "" (their initials) when they haven't
    picked one or the saved value isn't a current choice."""
    prof = profile if isinstance(profile, dict) else {}
    return prof.get("avatar") if prof.get("avatar") in AVATAR_CHOICES else ""


def notification_settings_from(profile: dict) -> dict:
    """notification_settings() computed from an already-read profile."""
    prof = profile if isinstance(profile, dict) else {}
    notif = prof.get("notifications") if isinstance(prof.get("notifications"), dict) else {}
    email = prof.get("email") if prof.get("email_verified") is True else ""
    return {"enabled": notif.get("enabled") is True and bool(email),
            "mode": notif.get("mode") if notif.get("mode") in NOTIFY_MODES else "general",
            "email": email or ""}


class UserStore:
    """The per-user store of this app: storage plus identity lookups."""

    def __init__(self, backend, identity=None):
        self.backend, self.identity = backend, identity

    def for_uid(self, uid: str) -> UserData:
        """Only the app's sign-in gate calls this, with the uid of the
        server-side signed-in account."""
        return UserData(self.backend, uid)

    def account(self, uid: str) -> dict | None:
        """Firebase's current state of the account with this UID (Admin SDK):
        None if it was deleted. Raises UserStoreError if it can't be read."""
        if self.identity is None or not valid_uid(uid):
            raise UserStoreError(UNAVAILABLE)
        try:
            return self.identity.account(uid)
        except Exception as e:
            log.warning("Firebase account lookup failed: %s", type(e).__name__)
            raise UserStoreError(UNAVAILABLE, kind=type(e).__name__) from None

    def resolve_session(self, cookie: object, idle_limit_s: float, now: float | None = None) -> dict | None:
        """The live email sign-in session named by a jt_session cookie, or
        None (malformed, unknown, ended, idle for ``idle_limit_s`` or past
        SESSION_MAX_S, or begun before Firebase last revoked the account's
        sign-ins). The UID in the cookie is only an address: the session is
        accepted only if the token's hash is stored under that very UID.
        Raises UserStoreError if the store or Firebase can't be read."""
        m = SESSION_TOKEN_RE.match(cookie) if isinstance(cookie, str) else None
        if not m or not valid_uid(m.group(1)):
            return None
        uid, token_hash = m.group(1), _hash_id(m.group(2))
        try:
            doc = self.backend.get(f"users/{uid}/sessions/{token_hash}")
        except Exception as e:
            log.warning("session lookup failed: %s", type(e).__name__)
            raise UserStoreError(UNAVAILABLE, kind=type(e).__name__) from None
        now = time.time() if now is None else now
        if not isinstance(doc, dict) or doc.get("uid") != uid or doc.get("provider") != "password":
            return None
        expires, last = _iso_seconds(doc.get("expires_at")), _iso_seconds(doc.get("last_active_at"))
        if expires is None or last is None or now >= expires or now - last >= idle_limit_s:
            return None
        created = _iso_seconds(doc.get("created_at"))
        session = {"uid": uid, "key": token_hash, "email": doc.get("email") or "", "name": doc.get("name") or "",
                   "email_verified": doc.get("email_verified") is True, "last_active": last, "created_at": created}
        if self.identity is not None:            # (production always has one; a failed lookup raises: not restored)
            session["account"] = self.account(uid)
            if session_revoked(created, session["account"]):
                return None
        return session

    def end_session(self, uid: str, token_hash: str) -> None:
        """End a session that resolve_session found but the account no longer
        matches (the hash came from that session's own token)."""
        try:
            self.for_uid(uid).end_session(token_hash)
        except InvalidInput:
            pass

    def delete_account(self, uid: str) -> None:
        """Delete the Firebase Authentication account (Admin SDK). Raises
        UserStoreError if Firebase couldn't do it."""
        if self.identity is None or not valid_uid(uid):
            raise UserStoreError(UNAVAILABLE)
        try:
            self.identity.delete_account(uid)
        except Exception as e:
            log.warning("Firebase account deletion failed: %s", type(e).__name__)
            raise UserStoreError("Your account couldn't be deleted right now.", kind=type(e).__name__) from None

    def user_ids(self) -> list[str]:
        """Every users/{uid} document (for the notifier, a server process)."""
        try:
            return sorted(uid for uid in self.backend.list("users") if valid_uid(uid))
        except Exception as e:
            log.warning("listing users failed: %s", type(e).__name__)
            raise UserStoreError(UNAVAILABLE, kind=type(e).__name__) from None

    def followed_by_other_admin(self, company_id: str, admins: Iterable[str], exclude_uid: str) -> bool:
        """Does an admin other than ``exclude_uid`` follow this company? (An
        admin = a profile whose verified email is in ``admins``.) Asked by
        the app before a company leaves the catalogue, so one admin can't
        silently drop another admin's monitoring. Only a yes/no comes back:
        nothing of the other account is returned or shown. Raises
        UserStoreError if it can't tell — callers then keep the company."""
        admins = {a.strip().lower() for a in admins if isinstance(a, str)}
        if not isinstance(company_id, str) or not admins:
            return False
        path_id = _hash_id(company_id)
        try:
            for uid, prof in self.backend.list("users").items():
                if uid == exclude_uid or not valid_uid(uid) or not isinstance(prof, dict):
                    continue
                if prof.get("email_verified") is not True or (prof.get("email") or "").strip().lower() not in admins:
                    continue
                if self.backend.get(f"users/{uid}/companies/{path_id}") is not None:
                    return True
        except Exception as e:
            log.warning("checking other admins' companies failed: %s", type(e).__name__)
            raise UserStoreError(UNAVAILABLE, kind=type(e).__name__) from None
        return False

    def google_account(self, google_sub: str) -> dict | None:
        if self.identity is None or not isinstance(google_sub, str) or not google_sub:
            return None
        try:
            return self.identity.google_account(google_sub)
        except Exception as e:
            log.warning("Firebase account lookup failed: %s", type(e).__name__)
            raise UserStoreError(UNAVAILABLE, kind=type(e).__name__) from None


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _service_account(secrets: Mapping | None) -> dict | None:
    if not isinstance(secrets, Mapping):
        return None
    section = secrets.get(SERVICE_ACCOUNT_SECTION)
    if isinstance(section, Mapping):
        return dict(section)
    raw = secrets.get(SERVICE_ACCOUNT_KEY)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return None


def configured(secrets: Mapping | None) -> bool:
    return _service_account(secrets) is not None


def _credential_identity(info: Mapping) -> str:
    """Who a credential is — project, service-account email, key ID — as a
    SHA-256 digest. Never the private key, which isn't part of it."""
    return _hash_id("|".join(str(info.get(k) or "") for k in ("project_id", "client_email", "private_key_id")))


def credential_fingerprint(secrets: Mapping | None, project_id: str = "") -> str:
    """A non-secret fingerprint of the configured credential (plus the
    project it must belong to), for cache keys. A different key (a new key
    ID) or project gives a different fingerprint; it is never logged."""
    return _hash_id(_credential_identity(_service_account(secrets) or {}) + "|" + (project_id or ""))


def configure(secrets: Mapping | None, project_id: str = "") -> tuple[UserStore | None, str]:
    """(store, problem). (None, "") when per-user data isn't set up — the
    app then works exactly as before. ``problem`` never contains a value
    from the credential."""
    info = _service_account(secrets)
    if info is None:
        return None, ""
    if info.get("type") != "service_account" or not info.get("private_key") or not info.get("client_email"):
        return None, "the Firebase service account secret is incomplete"
    if project_id and info.get("project_id") != project_id:
        return None, "the Firebase service account belongs to a different project"
    # one Admin SDK app per credential (keyed by its identity, never by the key
    # itself), so a different credential can never reuse an app built from another
    name = f"{APP_NAME}-{_credential_identity(info)[:16]}"
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore
        try:
            app = firebase_admin.get_app(name)
        except ValueError:
            app = firebase_admin.initialize_app(credentials.Certificate(info),
                                                {"projectId": info.get("project_id")}, name=name)
        client = firestore.client(app=app)
    except Exception as e:
        log.warning("Firestore unavailable: %s", type(e).__name__)
        return None, "Firestore couldn't be started"
    return UserStore(FirestoreBackend(client), FirebaseIdentity(app)), ""
