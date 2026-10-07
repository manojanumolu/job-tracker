"""Who may change things, and alert-recipient privacy.

Admin rights, in order of preference:

  * Signed in (Firebase / Google) with a VERIFIED email listed in the
    JT_ADMIN_EMAILS secret. Once that secret is set it is the only way:
    there is no second password to type.
  * Until it is set: the legacy owner password (JT_OWNER_PASSWORD), a
    break-glass only. Once JT_ADMIN_EMAILS is set the password is inert:
    it can neither sign in nor grant anything.

The signed-in account lives in server-side session state under
ACCOUNT_KEY (written by the app's sign-in gate each run, never by the
browser). Everyone else — visitors and signed-in non-admins — is read-only.

Every change — dismiss/restore, adding or removing a company, dismissing
all, Run check, test email — needs admin rights. The check is made on the
server where each operation happens, not just by hiding buttons.

The alert recipient is personal, and the repository is public, so it comes
from the ALERT_RECIPIENT secret. The ``recipient_email`` field in
settings.json is only a legacy fallback, kept so alerts never stop while the
secret is being set up.

No Streamlit import here, so this is unit-testable on its own.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import threading
import time

OWNER_PASSWORD_ENV = "JT_OWNER_PASSWORD"
RECIPIENT_ENV = "ALERT_RECIPIENT"
MIN_PASSWORD_LEN = 12
OWNER_SESSION_S = 8 * 3600       # an owner sign-in lasts for one working day
OWNER_SESSION_KEY = "_owner_until"  # session-state key: when the owner sign-in expires
ADMIN_EMAILS_ENV = "JT_ADMIN_EMAILS"
ACCOUNT_KEY = "_account"            # session-state key: the signed-in account (see module doc)
MAX_SIGNIN_FAILURES = 10         # per window, across every browser session
SIGNIN_WINDOW_S = 15 * 60


# ---------------------------------------------------------------------------
# Owner password
# ---------------------------------------------------------------------------

def _owner_password() -> str:
    return os.environ.get(OWNER_PASSWORD_ENV, "").strip()


def owner_configured() -> bool:
    """A missing or short password disables every change (fail closed)."""
    return len(_owner_password()) >= MIN_PASSWORD_LEN


def password_matches(candidate: str) -> bool:
    expected = _owner_password()
    if len(expected) < MIN_PASSWORD_LEN or not isinstance(candidate, str):
        return False
    # equal-length digests, so the comparison time says nothing about the password
    return hmac.compare_digest(hashlib.sha256(candidate.encode("utf-8")).digest(),
                               hashlib.sha256(expected.encode("utf-8")).digest())


def admin_emails() -> frozenset[str]:
    """The admin allowlist: JT_ADMIN_EMAILS, comma/space separated."""
    raw = os.environ.get(ADMIN_EMAILS_ENV, "")
    return frozenset(e.strip().lower() for e in re.split(r"[,\s;]+", raw) if "@" in e)


def admins_configured() -> bool:
    return bool(admin_emails())


def owner_password_enabled() -> bool:
    """The legacy owner password works only while no admin list exists."""
    return owner_configured() and not admins_configured()


def account_is_admin(account, now: float | None = None) -> bool:
    """A signed-in account whose email is verified and on the allowlist.
    An unverified email (e.g. a fresh email/password account that merely
    claims an address) never counts, nor does a sign-in past its expiry."""
    if not isinstance(account, dict):
        return False
    if "expires_at" in account:
        try:
            if float(account["expires_at"]) <= (time.time() if now is None else now):
                return False
        except (TypeError, ValueError):
            return False
    email = account.get("email")
    return (account.get("email_verified") is True and isinstance(email, str)
            and email.strip().lower() in admin_emails())


def account_matches_source(account, google, firebase_user) -> bool:
    """Is ``account`` (what the sign-in gate stored for this session) still
    backed by where it came from? Asked again right before a protected
    operation, so stored session state alone never carries a role.
    ``google`` is the identity Streamlit's st.login vouches for now (None if
    signed out); ``firebase_user`` this session's unexpired Firebase sign-in."""
    if not isinstance(account, dict):
        return False
    provider = account.get("provider")
    if provider == "google.com":
        return isinstance(google, dict) and bool(google.get("email")) and google.get("email") == account.get("email")
    if provider == "password":
        return (isinstance(firebase_user, dict) and firebase_user.get("provider") == "password"
                and bool(firebase_user.get("uid")) and firebase_user.get("uid") == account.get("uid")
                and firebase_user.get("email") == account.get("email"))
    if provider == "owner":
        return owner_password_enabled()
    return False


def owner_session_valid(session, now: float | None = None) -> bool:
    """May this session (Streamlit session state, or any mapping) make
    changes? With JT_ADMIN_EMAILS set: only a signed-in, verified admin
    account. Without it: an unexpired legacy owner-password sign-in."""
    try:
        if admins_configured():
            return account_is_admin(session.get(ACCOUNT_KEY))
        if not owner_configured():
            return False
        until = float(session.get(OWNER_SESSION_KEY, 0) or 0)
    except (TypeError, ValueError, AttributeError):
        return False
    return until > (time.time() if now is None else now)


class AttemptLimiter:
    """Failed sign-ins, shared by every session of the process: a new browser
    tab doesn't reset it, so the password can't be guessed at speed."""

    def __init__(self, max_failures: int = MAX_SIGNIN_FAILURES, window_s: float = SIGNIN_WINDOW_S,
                 clock=time.time):
        self.max_failures, self.window_s, self._clock = max_failures, window_s, clock
        self._failures: list[float] = []
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        self._failures = [t for t in self._failures if now - t < self.window_s]

    def locked_for(self) -> float:
        """Seconds until another attempt is allowed (0 = allowed now)."""
        with self._lock:
            now = self._clock()
            self._prune(now)
            if len(self._failures) < self.max_failures:
                return 0.0
            return max(0.0, self.window_s - (now - self._failures[0]))

    def failed(self) -> None:
        with self._lock:
            now = self._clock()
            self._prune(now)
            self._failures.append(now)

    def succeeded(self) -> None:
        with self._lock:
            self._failures.clear()


class Cooldown:
    """A process-wide minimum interval between runs of an action (one
    workflow dispatch for all sessions, not one per browser tab)."""

    def __init__(self, seconds: float, clock=time.time):
        self.seconds, self._clock = seconds, clock
        self._last = float("-inf")
        self._lock = threading.Lock()

    def remaining(self) -> float:
        with self._lock:
            return max(0.0, self.seconds - (self._clock() - self._last))

    def try_start(self) -> bool:
        """Claim the slot; False while the previous run is still cooling down."""
        with self._lock:
            now = self._clock()
            if now - self._last < self.seconds:
                return False
            self._last = now
            return True

    def cancel(self) -> None:
        """Give the slot back (the action didn't actually happen)."""
        with self._lock:
            self._last = float("-inf")


# ---------------------------------------------------------------------------
# Alert recipient
# ---------------------------------------------------------------------------

def alert_recipient(settings: dict | None = None) -> tuple[str, str]:
    """(address, source). The ALERT_RECIPIENT secret wins; the legacy
    settings.json field is used only while the secret isn't set.
    source is "secret", "settings.json" or "" (no recipient)."""
    secret = os.environ.get(RECIPIENT_ENV, "").strip()
    if secret:
        return secret, "secret"
    legacy = (settings or {}).get("recipient_email") if isinstance(settings, dict) else ""
    legacy = legacy.strip() if isinstance(legacy, str) else ""
    return (legacy, "settings.json") if legacy else ("", "")


class OwnerRequired(PermissionError):
    """A protected operation was attempted without a valid owner sign-in."""


def send_test_email(session, settings: dict | None = None) -> str:
    """The test-email operation itself: refuses unless ``session`` holds a
    valid owner sign-in, and only ever writes to the configured alert
    recipient (never to an address supplied by the caller). Returns the
    address it was sent to. The button that calls this checks too — this
    is the second, server-side check."""
    if not owner_session_valid(session):
        raise OwnerRequired("owner sign-in required to send a test email")
    recipient, _ = alert_recipient(settings)
    if not recipient:
        raise LookupError("no alert recipient is configured")
    from notifier import test_mail
    test_mail(recipient)
    return recipient


# the practical address alphabet: quotes/braces around an address in an error
# message ("{'a@b.c': ...}") are not part of it
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")


def mask_email(addr: str) -> str:
    """"manoj@gmail.com" -> "m•••@gmail.com" (enough to recognise, not to use)."""
    addr = (addr or "").strip()
    local, at, domain = addr.partition("@")
    if not at or not local or not domain:
        return "•••" if addr else ""
    return f"{local[0]}•••@{domain}"


def redact_emails(text: object) -> str:
    """Mask every email address in a log line or error message (Actions logs
    of a public repository are public)."""
    return _EMAIL_RE.sub(lambda m: mask_email(m.group(0)), str(text))
