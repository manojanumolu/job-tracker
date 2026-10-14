"""Which email addresses may hold a Job Tracker account (Oct 2026).

Disposable / temporary addresses (mailinator, yopmail, 10minutemail, ...)
are refused. The list is the community-maintained disposable-email-domains
project, vendored in disposable_domains.txt (CC0) so the check needs no
network, service or key at sign-in. A subdomain of a listed domain is
refused too ("x.mailinator.com"). ``python email_policy.py --update``
refreshes the file from upstream; review the diff and run the tests before
committing it.

Every place that lets someone in applies the same rule, on the server:

  * Google sign-in (st.login): before the Firebase account is linked or
    created, and again on every page load, so a session restored from
    Streamlit's 30-day cookie is refused too
  * email + password sign-in: before Firebase is asked, and again with the
    address Firebase answered with; restored sessions are re-checked
  * per-user alert emails (user_alerts.py) are never sent to such an address

The app has no sign-up form of its own: a new account is made by the first
Google sign-in. Email/password accounts are made outside the app (Firebase
console, or Firebase's own sign-up endpoint), so the email/password route
also requires a VERIFIED address before it grants anything.

Fails closed: a missing, unreadable, truncated (fewer entries than its
"# entries:" header says) or malformed list is never read as an empty one.
PolicyUnavailable is raised, sign-in shows UNAVAILABLE_MESSAGE and no
personal alert is sent, until the file is fixed (picked up without a
restart). Only answers that don't depend on the list still go through:
the admin list, and NEVER_BLOCKED providers (never disposable anyway).

Limits, said plainly: a blocklist only knows the domains it lists. A brand
new throwaway domain, a catch-all private domain or a disposable inbox at a
regular provider gets through until the list catches up — verified
ownership is the stronger control, and it is required everywhere. Gmail,
Outlook, Yahoo and the other big providers can never be blocked by a list
update (NEVER_BLOCKED). Addresses on JT_ADMIN_EMAILS are trusted
configuration and are never refused by this list.

Standard library only (user_alerts.py imports it in GitHub Actions).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent          # the repository root (this file is in src/)
BLOCKLIST_FILE = BASE / "config" / "disposable_domains.txt"
UPSTREAM_URL = ("https://raw.githubusercontent.com/disposable-email-domains/disposable-email-domains/"
                "main/disposable_email_blocklist.conf")

# Big personal-mail providers: never treated as disposable, whatever a list
# refresh says (a wrong upstream entry must not lock out ordinary people).
NEVER_BLOCKED = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com", "outlook.in",
    "yahoo.com", "yahoo.co.in", "yahoo.in", "ymail.com", "rocketmail.com", "icloud.com", "me.com", "mac.com",
    "aol.com", "proton.me", "protonmail.com", "pm.me", "zoho.com", "zohomail.in", "rediffmail.com",
    "gmx.com", "gmx.net", "mail.com", "yandex.com", "fastmail.com", "tutanota.com", "hey.com",
})

DISPOSABLE_MESSAGE = ("Disposable or temporary email addresses can't be used for Job Tracker. "
                      "Please sign in with your regular email address.")
UNVERIFIED_MESSAGE = "This email address isn't verified yet."
INVALID_MESSAGE = "This sign-in has no usable email address."
# shown when the blocklist itself can't be trusted (no file, damaged file):
# nothing about why, and the same text for everyone
UNAVAILABLE_MESSAGE = ("Sign-in is temporarily unavailable while we check email addresses. "
                       "Please try again later.")

_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}$")
_COUNT_RE = re.compile(r"^#\s*entries:\s*(\d+)\s*$")
MIN_ENTRIES = 1000            # far fewer than that means a truncated or wrong file


class PolicyUnavailable(Exception):
    """The disposable-address list is missing, unreadable or damaged, so no
    address can be judged against it. Never treated as an empty list: every
    caller refuses (fails closed) instead. ``str(e)`` is for the server log
    only (a fixed reason, never file contents)."""


# the list as last read successfully, keyed by the file's (size, mtime): a
# failure is never cached, so a fixed file is picked up without a restart
_cache: tuple[tuple, frozenset[str]] | None = None


def _read_blocklist() -> frozenset[str]:
    try:
        raw = BLOCKLIST_FILE.read_bytes()
    except OSError as e:
        raise PolicyUnavailable(f"the list can't be read ({type(e).__name__})") from None
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        raise PolicyUnavailable("the list isn't valid UTF-8") from None
    declared, domains = None, []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            m = _COUNT_RE.match(line)
            if m:
                declared = int(m.group(1))
            continue
        if not _DOMAIN_RE.match(line):
            # one bad line means the file was damaged or hand-edited: trust none of it
            raise PolicyUnavailable("the list has a malformed entry")
        domains.append(line)
    if len(domains) < MIN_ENTRIES:
        raise PolicyUnavailable(f"the list has only {len(domains)} entries")
    if declared is None or declared != len(domains):
        raise PolicyUnavailable("the list's entry count doesn't match its header")
    return frozenset(domains)


def disposable_domains() -> frozenset[str]:
    """The vendored blocklist, validated (cached while the file is
    unchanged). Raises PolicyUnavailable — never returns an empty set."""
    global _cache
    try:
        st = BLOCKLIST_FILE.stat()
        key = (str(BLOCKLIST_FILE), st.st_size, st.st_mtime_ns)
    except OSError:
        key = None
    if key is not None and _cache is not None and _cache[0] == key:
        return _cache[1]
    try:
        domains = _read_blocklist()
    except PolicyUnavailable as e:
        print(f"[email_policy] disposable-address list unavailable: {e} — refusing what it would decide",
              file=sys.stderr, flush=True)
        raise
    _cache = (key, domains) if key is not None else None
    return domains


def email_domain(email: object) -> str:
    """The normalised domain of an address ("" if it has none): lower case,
    no trailing dot, internationalised names in their ASCII (punycode) form."""
    if not isinstance(email, str):
        return ""
    local, at, domain = email.strip().rpartition("@")
    if not at or not local:
        return ""
    domain = domain.strip().rstrip(".").lower()
    try:
        domain = domain.encode("idna").decode("ascii")
    except UnicodeError:
        return ""
    return domain if _DOMAIN_RE.match(domain) else ""


def is_disposable(email: object) -> bool:
    """Is this a disposable / temporary address (its domain or any parent
    domain is on the list)? Raises PolicyUnavailable when the answer
    depends on a list that can't be read. An address with no usable domain,
    and the big providers in NEVER_BLOCKED, are answered without the list
    (the answer is "no" whatever the list says)."""
    domain = email_domain(email)
    if not domain or domain in NEVER_BLOCKED:
        return False
    listed = disposable_domains()
    labels = domain.split(".")
    return any(".".join(labels[i:]) in listed for i in range(len(labels) - 1))


def _admin_listed(email: object) -> bool:
    from access import admin_emails           # the same trusted list access.py uses
    return isinstance(email, str) and email.strip().lower() in admin_emails()


def address_problem(email: object) -> str:
    """DISPOSABLE_MESSAGE for a disposable address, UNAVAILABLE_MESSAGE when
    that can't be decided (the list is broken), else "". Addresses on the
    admin list are trusted configuration and need no list. The address
    alone — usable before anyone is asked."""
    if _admin_listed(email):
        return ""
    try:
        return DISPOSABLE_MESSAGE if is_disposable(email) else ""
    except PolicyUnavailable:
        return UNAVAILABLE_MESSAGE


def sign_in_problem(email: object, email_verified: object) -> str:
    """Why this identity may NOT use the app ("" = it may). Asked on the
    server for every sign-in route and every restored session; fails closed
    (UNAVAILABLE_MESSAGE) when the list can't be evaluated."""
    if not isinstance(email, str) or not email_domain(email):
        return INVALID_MESSAGE
    problem = address_problem(email)
    if problem:
        return problem
    if email_verified is not True:
        return UNVERIFIED_MESSAGE
    return ""


ALERT_OK, ALERT_BLOCKED, ALERT_UNAVAILABLE = "ok", "blocked", "unavailable"


def alert_address_check(email: object) -> str:
    """user_alerts.py: ALERT_OK, ALERT_BLOCKED (no usable address, or a
    disposable one — accounts made before this policy keep their data but
    get no alerts) or ALERT_UNAVAILABLE (the list is broken: nothing is
    sent to anyone it would decide until it is fixed)."""
    if not isinstance(email, str) or not email_domain(email):
        return ALERT_BLOCKED
    try:
        return ALERT_BLOCKED if is_disposable(email) else ALERT_OK
    except PolicyUnavailable:
        return ALERT_UNAVAILABLE


def may_receive_alerts(email: object) -> bool:
    """True only when the address is known to be fine (fails closed)."""
    return alert_address_check(email) == ALERT_OK


def update(url: str = UPSTREAM_URL) -> int:
    """Refresh disposable_domains.txt from upstream (a maintainer's command,
    never run by the app or a workflow). Keeps the header comment."""
    import urllib.request
    with urllib.request.urlopen(url, timeout=30) as resp:          # noqa: S310 (fixed https URL)
        body = resp.read().decode("utf-8")
    domains = sorted({d.strip().lower() for d in body.splitlines() if d.strip() and not d.startswith("#")})
    bad = [d for d in domains if not _DOMAIN_RE.match(d)]
    if bad or len(domains) < 1000:
        print(f"refusing the update: {len(bad)} malformed entries, {len(domains)} domains", file=sys.stderr)
        return 1
    clash = sorted(NEVER_BLOCKED & set(domains))
    header = [line for line in BLOCKLIST_FILE.read_text("utf-8").splitlines()
              if line.startswith("#") and not _COUNT_RE.match(line)]
    header.append(f"# entries: {len(domains)}")       # checked on every load: a truncated file is refused
    BLOCKLIST_FILE.write_text("\n".join(header + domains) + "\n", encoding="utf-8")
    print(f"{len(domains)} domains written" + (f"; ignored for safety: {', '.join(clash)}" if clash else ""))
    print("Update the commit noted in the header, review the diff, then run the tests.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--update"]:
        sys.exit(update())
    print("usage: python email_policy.py --update", file=sys.stderr)
    sys.exit(2)
