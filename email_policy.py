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
from functools import lru_cache
from pathlib import Path

BASE = Path(__file__).parent
BLOCKLIST_FILE = BASE / "disposable_domains.txt"
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

_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}$")


@lru_cache(maxsize=1)
def disposable_domains() -> frozenset[str]:
    """The vendored blocklist (read once per process)."""
    try:
        lines = BLOCKLIST_FILE.read_text("utf-8").splitlines()
    except OSError:
        # fail loudly in the log, never silently open: an empty list would
        # let every throwaway address in
        print("[email_policy] disposable_domains.txt is missing — the disposable-address check is off",
              file=sys.stderr, flush=True)
        return frozenset()
    return frozenset(d for d in (line.strip().lower() for line in lines) if d and not d.startswith("#"))


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
    domain is on the list)?"""
    domain = email_domain(email)
    if not domain or domain in NEVER_BLOCKED:
        return False
    listed = disposable_domains()
    labels = domain.split(".")
    return any(".".join(labels[i:]) in listed for i in range(len(labels) - 1))


def _admin_listed(email: str) -> bool:
    from access import admin_emails           # the same trusted list access.py uses
    return email.strip().lower() in admin_emails()


def address_problem(email: object) -> str:
    """DISPOSABLE_MESSAGE for a disposable address (unless it is on the admin
    list), else "". The address alone — usable before anyone is asked."""
    return DISPOSABLE_MESSAGE if is_disposable(email) and not _admin_listed(email) else ""


def sign_in_problem(email: object, email_verified: object) -> str:
    """Why this identity may NOT use the app ("" = it may). Asked on the
    server for every sign-in route and every restored session."""
    if not isinstance(email, str) or not email_domain(email):
        return INVALID_MESSAGE
    problem = address_problem(email)
    if problem:
        return problem
    if email_verified is not True:
        return UNVERIFIED_MESSAGE
    return ""


def may_receive_alerts(email: object) -> bool:
    """user_alerts.py: never email a disposable address (accounts made
    before this policy existed keep their data, but get no alerts)."""
    return isinstance(email, str) and bool(email_domain(email)) and not is_disposable(email)


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
    header = [line for line in BLOCKLIST_FILE.read_text("utf-8").splitlines() if line.startswith("#")]
    BLOCKLIST_FILE.write_text("\n".join(header + domains) + "\n", encoding="utf-8")
    print(f"{len(domains)} domains written" + (f"; ignored for safety: {', '.join(clash)}" if clash else ""))
    print("Update the commit noted in the header, review the diff, then run the tests.")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--update"]:
        sys.exit(update())
    print("usage: python email_policy.py --update", file=sys.stderr)
    sys.exit(2)
