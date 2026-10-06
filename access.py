"""Interim owner access and alert-recipient privacy.

Until real sign-in exists, the dashboard is readable by anyone with the URL
(everything it shows is already public in the GitHub repository), but every
change — dismiss/restore, adding or removing a company, dismissing all, Run
check, test email — needs the owner password from the JT_OWNER_PASSWORD
secret. The check is made on the server where each operation happens, not
just by hiding buttons.

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
