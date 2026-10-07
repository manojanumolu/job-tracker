"""Per-user data, in Firestore, keyed only by the Firebase UID.

Shared (unchanged, in the repository): job facts and their job_key
identity, the company catalogue, scanner state and the eligibility rules.
Personal (here), one tree per person:

  users/{uid}                    profile: email, display_name, created_at,
                                 last_login_at, onboarding, notifications,
                                 preferences, watch_all
  users/{uid}/companies/{id}     watchlist: company IDs from the shared catalogue
  users/{uid}/dismissed/{id}     jobs this person dismissed (job_key inside)
  users/{uid}/deliveries/{id}    per-user email ledger: claim -> send -> finalize

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

import functools
import hashlib
import json
import logging
import random
import re
import threading
import time
from collections.abc import Iterable, Mapping

log = logging.getLogger("user_store")

SCHEMA_VERSION = 1
UID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
NOTIFY_MODES = ("general", "tailored")
ONBOARDING_PENDING, ONBOARDING_COMPLETE = "pending", "complete"
DELIVERY_CLAIMED, DELIVERY_SENT, DELIVERY_PENDING = "claimed", "sent", "pending"
MAX_PREF_ITEMS, MAX_PREF_LEN = 50, 80
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
        "email_verified", "disabled"}, or None if it no longer exists."""
        from firebase_admin import auth
        try:
            user = auth.get_user(uid, app=self.app)
        except auth.UserNotFoundError:
            return None
        return {"uid": user.uid, "email": (user.email or "").strip().lower(),
                "email_verified": bool(user.email_verified), "disabled": bool(user.disabled)}


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
                 "notifications": {"enabled": False, "mode": "general", "enabled_at": None},
                 "preferences": {"mode": "general", "job_families": [], "locations": [], "experience": []},
                 "watch_all": True}
        if self._backend.create(self._root, fresh):
            return fresh, True
        self._backend.set(self._root, identity, merge=True)
        return self._backend.get(self._root) or fresh, False

    @_guard
    def profile(self) -> dict | None:
        return self._backend.get(self._root)

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
        for key in job_keys:
            if isinstance(key, str) and key:
                self._backend.set(f"{self._root}/dismissed/{_hash_id(key)}", {"job_key": key, "dismissed_at": stamp})

    @_guard
    def restore(self, job_keys: Iterable[str]) -> None:
        for key in job_keys:
            if isinstance(key, str) and key:
                self._backend.delete(f"{self._root}/dismissed/{_hash_id(key)}")

    # -- notification preferences --------------------------------------------
    @_guard
    def notification_settings(self) -> dict:
        """{"enabled", "mode", "email"}. The address is always the verified
        email of the signed-in identity — it can't be set to anything else."""
        prof = self._backend.get(self._root) or {}
        notif = prof.get("notifications") if isinstance(prof.get("notifications"), dict) else {}
        email = prof.get("email") if prof.get("email_verified") is True else ""
        return {"enabled": notif.get("enabled") is True and bool(email),
                "mode": notif.get("mode") if notif.get("mode") in NOTIFY_MODES else "general",
                "email": email or ""}

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
        self._backend.set(self._root, {"notifications": {"enabled": enabled is True, "mode": mode,
                                                         "enabled_at": since if enabled is True else None}},
                          merge=True)
        return self.notification_settings()

    # -- general / tailored preferences ----------------------------------------
    @_guard
    def set_preferences(self, mode: str = "general", job_families=(), locations=(), experience=()) -> None:
        if mode not in NOTIFY_MODES:
            raise InvalidInput("unknown preference mode")
        from job_filters import VOCABULARY
        chosen = {"job_families": _clean_list(job_families), "locations": _clean_list(locations),
                  "experience": _clean_list(experience)}
        for field, values in chosen.items():
            if any(v not in VOCABULARY[field] for v in values):
                raise InvalidInput(f"unknown {field} value")
        self._backend.set(self._root, {"preferences": {"mode": mode, **chosen}}, merge=True)

    @_guard
    def personal_view(self) -> dict:
        """What this person asked to see: {"mode", "prefs", "watch_all",
        "watchlist", "notifications"}. Missing fields get the defaults."""
        prof = self._backend.get(self._root) or {}
        prefs = prof.get("preferences") if isinstance(prof.get("preferences"), dict) else {}
        notif = prof.get("notifications") if isinstance(prof.get("notifications"), dict) else {}
        mode = notif.get("mode") if notif.get("mode") in NOTIFY_MODES else "general"
        return {"mode": mode,
                "prefs": {k: [v for v in prefs.get(k) or [] if isinstance(v, str)]
                          for k in ("job_families", "locations", "experience")},
                "watch_all": prof.get("watch_all") is not False,
                "watchlist": self.watchlist() if prof.get("watch_all") is False else set(),
                "notifications": notif}

    # -- per-user delivery ledger: claim -> send -> finalize --------------------
    def _delivery_path(self, job_key: str) -> str:
        if not isinstance(job_key, str) or not job_key:
            raise InvalidInput("job_key required")
        return f"{self._root}/deliveries/{_hash_id(job_key)}"

    @_guard
    def claim_delivery(self, job_key: str, run_id: str, now: float | None = None) -> bool:
        """Claim the right to email this person about this job. Exactly one
        concurrent claimant wins; a sent job can never be claimed again; a
        released claim (failed send) can be claimed by a later run."""
        path = self._delivery_path(job_key)
        claimed = {"job_key": job_key, "state": DELIVERY_CLAIMED, "claim_id": run_id, "claimed_at": _now_iso(now)}
        if self._backend.create(path, claimed):
            return True
        return self._backend.update_if(path, {"state": DELIVERY_PENDING}, claimed)

    @_guard
    def finalize_delivery(self, job_key: str, run_id: str, sent: bool, now: float | None = None) -> bool:
        """Only the run holding the claim can settle it: sent -> "sent"
        (final), not sent -> "pending" (released for a retry)."""
        data = ({"state": DELIVERY_SENT, "sent_at": _now_iso(now)} if sent
                else {"state": DELIVERY_PENDING, "released_at": _now_iso(now)})
        return self._backend.update_if(self._delivery_path(job_key),
                                       {"state": DELIVERY_CLAIMED, "claim_id": run_id}, data)

    @_guard
    def delivery(self, job_key: str) -> dict | None:
        return self._backend.get(self._delivery_path(job_key))


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

    def user_ids(self) -> list[str]:
        """Every users/{uid} document (for the notifier, a server process)."""
        try:
            return sorted(uid for uid in self.backend.list("users") if valid_uid(uid))
        except Exception as e:
            log.warning("listing users failed: %s", type(e).__name__)
            raise UserStoreError(UNAVAILABLE, kind=type(e).__name__) from None

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
