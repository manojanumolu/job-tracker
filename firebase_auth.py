"""Firebase Authentication — technical spike.

Proves the dashboard can sign a person in with the Job Tracker Firebase
project and get a reliable Firebase UID. It grants NO access: owner access
(access.py) still controls every change. Nothing is written to Firestore.

Both sign-in methods run on the server through Firebase's Identity Toolkit
REST API, so no Firebase JavaScript or service-account credentials are
needed:

  email + password  accounts:signInWithPassword
  Google            Streamlit's st.login (Google OIDC) yields a Google ID
                    token, exchanged with accounts:signInWithIdp

Every answer is checked before it is trusted: the Firebase ID token in the
response must be issued by this project (aud / iss = FIREBASE_PROJECT_ID)
for this user (sub = localId). That also catches a Web API key that belongs
to a different Firebase project.

Configuration comes from the environment (root-level Streamlit secrets are
exported as environment variables): FIREBASE_WEB_API_KEY and
FIREBASE_PROJECT_ID. Errors never carry the key, a token or a password:
the key travels in the X-Goog-Api-Key header (never in a URL that HTTP
logging could print), and transport errors are reduced to a fixed message
and logged by exception type only.

No Streamlit import here, so this is unit-testable on its own.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass

import httpx

log = logging.getLogger("firebase_auth")

API_KEY_ENV = "FIREBASE_WEB_API_KEY"
PROJECT_ID_ENV = "FIREBASE_PROJECT_ID"
SESSION_KEY = "_firebase_user"       # session-state key for the signed-in Firebase user
_ENDPOINT = "https://identitytoolkit.googleapis.com/v1/accounts:{method}"
_TIMEOUT_S = 10
_PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_UID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class FirebaseConfigError(Exception):
    """Firebase isn't (correctly) configured for this app."""


class FirebaseAuthError(Exception):
    """Sign-in failed. ``str(e)`` is a short message that is safe to show."""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class FirebaseConfig:
    api_key: str
    project_id: str

    def __repr__(self) -> str:   # never print the key, even by accident
        return f"FirebaseConfig(project_id={self.project_id!r}, api_key=<hidden>)"


@dataclass(frozen=True)
class FirebaseUser:
    """The minimum kept for a signed-in session — no tokens, no password."""
    uid: str
    email: str
    provider: str            # "password" or "google.com"
    email_verified: bool | None
    expires_at: float        # when Firebase's ID token (and so this sign-in) expires

    def as_session(self) -> dict:
        return asdict(self)


def load_config() -> FirebaseConfig:
    key = os.environ.get(API_KEY_ENV, "").strip()
    project = os.environ.get(PROJECT_ID_ENV, "").strip()
    if not key or not project:
        raise FirebaseConfigError("Firebase sign-in isn't configured for this app.")
    if not _PROJECT_ID_RE.match(project):
        raise FirebaseConfigError("The Firebase project ID setting isn't valid.")
    return FirebaseConfig(key, project)


def configured() -> bool:
    try:
        load_config()
        return True
    except FirebaseConfigError:
        return False


# ---------------------------------------------------------------------------
# REST calls
# ---------------------------------------------------------------------------

def _new_client() -> httpx.Client:
    return httpx.Client(timeout=_TIMEOUT_S)


# Firebase error codes -> what the person sees. Wrong email and wrong password
# read the same, so the form doesn't reveal which accounts exist.
_ERRORS = {
    "EMAIL_NOT_FOUND": "Email or password is incorrect.",
    "INVALID_PASSWORD": "Email or password is incorrect.",
    "INVALID_LOGIN_CREDENTIALS": "Email or password is incorrect.",
    "INVALID_EMAIL": "Email or password is incorrect.",
    "MISSING_PASSWORD": "Enter your password.",
    "USER_DISABLED": "This account has been disabled.",
    "TOO_MANY_ATTEMPTS_TRY_LATER": "Too many attempts — try again later.",
    "OPERATION_NOT_ALLOWED": "This sign-in method isn't enabled for the Firebase project.",
    "INVALID_IDP_RESPONSE": "Google sign-in couldn't be verified.",
    "API_KEY_INVALID": "Firebase sign-in isn't configured correctly.",
    "PROJECT_NOT_FOUND": "Firebase sign-in isn't configured correctly.",
}


def _error_code(response: httpx.Response) -> str:
    try:
        msg = response.json()["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return ""
    msg = str(msg or "")
    if msg.startswith("API key not valid"):
        return "API_KEY_INVALID"
    # "TOO_MANY_ATTEMPTS_TRY_LATER : Access to this account ..." -> the code only
    return msg.split(":", 1)[0].strip().split(" ", 1)[0]


def _post(method: str, body: dict, config: FirebaseConfig, client: httpx.Client | None) -> dict:
    own = client is None
    client = client or _new_client()
    try:
        # the key goes in a header, never the URL: URLs end up in HTTP-library
        # logs and exception messages
        resp = client.post(_ENDPOINT.format(method=method), headers={"X-Goog-Api-Key": config.api_key}, json=body)
    except Exception as e:
        # logged by type only: the text could echo request details
        log.warning("Firebase %s request failed: %s", method, type(e).__name__)
        raise FirebaseAuthError("Couldn't reach Firebase — try again in a minute.", "NETWORK") from None
    finally:
        if own:
            client.close()
    if resp.status_code != 200:
        code = _error_code(resp)
        log.info("Firebase %s refused: %s (HTTP %s)", method, code or "unknown", resp.status_code)
        raise FirebaseAuthError(_ERRORS.get(code, "Sign-in failed — try again."), code or "HTTP")
    try:
        data = resp.json()
    except ValueError:
        raise FirebaseAuthError("Firebase sent an answer that couldn't be read.", "MALFORMED") from None
    if not isinstance(data, dict):
        raise FirebaseAuthError("Firebase sent an answer that couldn't be read.", "MALFORMED")
    return data


# ---------------------------------------------------------------------------
# Response checking
# ---------------------------------------------------------------------------

def _jwt_claims(token: object) -> dict:
    """The payload of a JWT, unverified. Only used on a token received
    directly from Google over TLS, as a cross-check of the answer."""
    if not isinstance(token, str) or token.count(".") != 2:
        raise ValueError("not a JWT")
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    if not isinstance(claims, dict):
        raise ValueError("JWT payload is not an object")
    return claims


def user_from_response(data: dict, config: FirebaseConfig, now: float | None = None) -> FirebaseUser:
    """Validate a signInWithPassword / signInWithIdp answer and keep only
    what the session needs."""
    bad = FirebaseAuthError("Firebase sent an incomplete sign-in answer.", "MALFORMED")
    now = time.time() if now is None else now
    uid = data.get("localId")
    if not isinstance(uid, str) or not _UID_RE.match(uid):
        raise bad
    try:
        claims = _jwt_claims(data.get("idToken"))
    except (ValueError, UnicodeError):
        raise bad from None
    # the token must be this project's, for this user, and still valid
    if (claims.get("aud") != config.project_id
            or claims.get("iss") != f"https://securetoken.google.com/{config.project_id}"
            or claims.get("sub") != uid or claims.get("user_id", uid) != uid):
        log.warning("Firebase answer is for another project or user — rejected")
        raise FirebaseAuthError("Firebase sign-in isn't configured correctly (project mismatch).", "PROJECT_MISMATCH")
    try:
        exp = float(claims.get("exp"))
    except (TypeError, ValueError):
        raise bad from None
    if exp <= now:
        raise bad
    email = data.get("email") or claims.get("email") or ""
    if not isinstance(email, str):
        raise bad
    firebase_claims = claims.get("firebase") if isinstance(claims.get("firebase"), dict) else {}
    provider = data.get("providerId") or firebase_claims.get("sign_in_provider") or ""
    verified = data.get("emailVerified", claims.get("email_verified"))
    return FirebaseUser(uid=uid, email=email.strip().lower(), provider=str(provider),
                        email_verified=verified if isinstance(verified, bool) else None,
                        expires_at=exp)


# ---------------------------------------------------------------------------
# Sign-in
# ---------------------------------------------------------------------------

def sign_in_with_password(email: str, password: str, *, config: FirebaseConfig | None = None,
                          client: httpx.Client | None = None, now: float | None = None) -> FirebaseUser:
    config = config or load_config()
    email = (email or "").strip()
    if not email or not password:
        raise FirebaseAuthError("Enter your email and password.", "MISSING_FIELDS")
    data = _post("signInWithPassword", {"email": email, "password": password, "returnSecureToken": True},
                 config, client)
    user = user_from_response(data, config, now)
    if user.provider not in ("", "password"):
        raise FirebaseAuthError("Firebase sent an incomplete sign-in answer.", "MALFORMED")
    return FirebaseUser(**{**user.as_session(), "provider": "password"})


def sign_in_with_google_id_token(id_token: str, request_uri: str, *, config: FirebaseConfig | None = None,
                                 client: httpx.Client | None = None, now: float | None = None) -> FirebaseUser:
    """Exchange the Google ID token from st.login for a Firebase user."""
    config = config or load_config()
    if not isinstance(id_token, str) or not id_token:
        raise FirebaseAuthError("Google didn't return a sign-in token.", "NO_GOOGLE_TOKEN")
    body = {"postBody": f"id_token={id_token}&providerId=google.com",
            "requestUri": request_uri or "http://localhost",
            "returnSecureToken": True, "returnIdpCredential": False}
    data = _post("signInWithIdp", body, config, client)
    user = user_from_response(data, config, now)
    if user.provider != "google.com":
        raise FirebaseAuthError("Firebase sent an incomplete sign-in answer.", "MALFORMED")
    return user


def session_user(session, now: float | None = None) -> dict | None:
    """The signed-in Firebase user of this session, or None (signed out,
    expired or malformed)."""
    try:
        user = session.get(SESSION_KEY)
    except AttributeError:
        return None
    if not isinstance(user, dict) or not isinstance(user.get("uid"), str) or not user["uid"]:
        return None
    try:
        if float(user.get("expires_at", 0)) <= (time.time() if now is None else now):
            return None
    except (TypeError, ValueError):
        return None
    return user
