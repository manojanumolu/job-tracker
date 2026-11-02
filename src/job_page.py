"""Fetch Job Details: what a public job posting says about itself (Nov 2026).

An admin pastes a job link in the Application Tracker; this reads that one
page and fills in what it states — company, job title, location, job ID,
posting date, description — for the admin to check before saving. Missing
details stay empty: nothing is guessed or made up.

Only public pages, and only when the site allows it:

  * http(s) links on the standard ports, no credentials in the link
  * the server never connects to a private, loopback, link-local or other
    non-public address (each address is resolved and checked, then the
    connection is made to that very address — with the real name for TLS
    and the Host header — so a second DNS answer can't redirect it inside);
    every redirect is checked the same way, at most MAX_REDIRECTS of them
  * the site's robots.txt is honoured (RFC 9309) for this tool's name
  * a page behind a sign-in, a CAPTCHA, a bot check or a rate limit is
    reported as such and left alone — nothing tries to get past it
  * timeouts and a size cap; the tool says who it is in its User-Agent

Structured data comes first: most career sites publish a schema.org
JobPosting (JSON-LD) for search engines. Failing that, the page's own
title/site-name metadata and the job ID in a known ATS link are used.
"""

from __future__ import annotations

import html
import ipaddress
import json
import re
import socket
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib import robotparser
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

import identity

USER_AGENT = "Mozilla/5.0 (compatible; FresherJobTracker/1.0; +https://github.com/manojanumolu/job-tracker)"
ROBOTS_AGENT = "FresherJobTracker"
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
MAX_BYTES = 2_000_000
MAX_ROBOTS_BYTES = 500_000
MAX_REDIRECTS = 5
DESCRIPTION_LIMIT = 20000
REQUIRED = ("company", "title", "location")

INCOMPLETE = ("We couldn't automatically retrieve all job details from this website. "
              "Please complete the missing information manually.")
MANUAL = "Please complete the missing information manually."


class Refused(Exception):
    """This link won't be fetched; ``str(e)`` says why, safely."""


@dataclass
class Fetched:
    """What was read: ``fields`` holds only what the page stated;
    ``problem`` is "" when every required detail was found."""
    fields: dict = field(default_factory=dict)
    problem: str = ""
    source: str = ""                     # "structured data", "page metadata" or ""
    asks_password: bool = False          # the page has a password field (a sign-in form)

    @property
    def missing(self) -> list[str]:
        return [f for f in REQUIRED if not self.fields.get(f)]


# ---------------------------------------------------------------------------
# Where the server may connect
# ---------------------------------------------------------------------------

def check_url(url: object) -> str:
    """The link, tidied, if it may be fetched at all; otherwise Refused."""
    if not isinstance(url, str) or not url.strip():
        raise Refused("Paste a job link first.")
    url = url.strip()
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise Refused("That isn't a valid web address.") from None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise Refused("Enter a full job link starting with https://.")
    if parts.username or parts.password:
        raise Refused("Links with a user name or password in them can't be fetched.")
    if port not in (None, 80, 443):
        raise Refused("Only links on the standard web ports can be fetched.")
    return urlunsplit((parts.scheme.lower(), parts.netloc, parts.path or "/", parts.query, ""))


def _public(addr: str) -> bool:
    ip = ipaddress.ip_address(addr.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def _system_resolver(host: str, port: int) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]


def public_address(host: str, port: int, resolver=_system_resolver) -> str:
    """One address for ``host`` — only if EVERY address it has is public."""
    try:
        ipaddress.ip_address(host)
        addrs = [host]
    except ValueError:
        try:
            addrs = resolver(host, port)
        except (OSError, UnicodeError):
            raise Refused("We couldn't find this website. Check the link.") from None
    if not addrs:
        raise Refused("We couldn't find this website. Check the link.")
    if not all(_public(a) for a in addrs):
        raise Refused("That link points to a private or local network address, so it can't be fetched.")
    return addrs[0]


def _get(client: httpx.Client, url: str, resolver, limit: int, accept: str):
    """GET through checked redirects: (status, content type, body, final url)."""
    for _ in range(MAX_REDIRECTS + 1):
        url = check_url(url)
        parts = urlsplit(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        addr = public_address(parts.hostname, port, resolver)
        ip_host = f"[{addr}]" if ":" in addr else addr
        pinned = urlunsplit((parts.scheme, f"{ip_host}:{port}", parts.path, parts.query, ""))
        request = client.build_request("GET", pinned, headers={"Host": parts.netloc, "Accept": accept,
                                                                 "User-Agent": USER_AGENT},
                                       extensions={"sni_hostname": parts.hostname})
        response = client.send(request, stream=True)
        try:
            if response.status_code in (301, 302, 303, 307, 308) and response.headers.get("location"):
                url = urljoin(url, response.headers["location"])
                continue
            body = bytearray()
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > limit:
                    break
            return response.status_code, response.headers.get("content-type", ""), bytes(body[:limit]), url
        finally:
            response.close()
    raise Refused("This link redirects too many times.")


def _robots_allows(client, url: str, resolver) -> bool:
    """RFC 9309: a missing robots.txt (4xx) allows everything; an
    unreachable one (5xx, network error) means stay out."""
    parts = urlsplit(url)
    robots_url = urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))
    try:
        status, _ctype, body, _final = _get(client, robots_url, resolver, MAX_ROBOTS_BYTES, "text/plain")
    except Refused:
        raise
    except httpx.HTTPError:
        return False
    if 400 <= status < 500:
        return True
    if status != 200:
        return False
    rp = robotparser.RobotFileParser()
    rp.parse(body.decode("utf-8", "replace").splitlines())
    return rp.can_fetch(ROBOTS_AGENT, url)


# ---------------------------------------------------------------------------
# Reading the page
# ---------------------------------------------------------------------------

class _Page(HTMLParser):
    """The parts of a page this needs: JSON-LD blocks, meta tags, the
    title, and whether it asks for a password."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ld, self.meta, self.title, self.password = [], {}, "", False
        self._in, self._buf = "", []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "script" and a.get("type", "").lower().strip() == "application/ld+json":
            self._in, self._buf = "ld", []
        elif tag == "title" and not self.title:
            self._in, self._buf = "title", []
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower().strip()
            if key and a.get("content") and key not in self.meta:
                self.meta[key] = a["content"]
        elif tag == "input" and a.get("type", "").lower() == "password":
            self.password = True

    def handle_data(self, data):
        if self._in:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if self._in == "ld" and tag == "script":
            self.ld.append("".join(self._buf))
            self._in = ""
        elif self._in == "title" and tag == "title":
            self.title = " ".join("".join(self._buf).split())
            self._in = ""


class _Text(HTMLParser):
    """HTML (a JobPosting description) as plain text with paragraph breaks."""
    BREAKS = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []

    def handle_starttag(self, tag, attrs):
        if tag in self.BREAKS:
            self.out.append("\n")
        if tag == "li":
            self.out.append("• ")

    def handle_endtag(self, tag):
        if tag in self.BREAKS:
            self.out.append("\n")

    def handle_data(self, data):
        self.out.append(data)


def html_to_text(value: str) -> str:
    parser = _Text()
    try:
        parser.feed(value)
        parser.close()
    except Exception:
        return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value)).split())
    lines = [" ".join(line.split()) for line in "".join(parser.out).split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


# values sites put in a field they have nothing for: never shown as details
_PLACEHOLDERS = {"unavailable", "not available", "n/a", "na", "none", "null", "undefined", "-", "--", "tbd", "0"}


def _plain(value: object, limit: int = 300) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str):
        return ""
    text = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value)).split())[:limit]
    return "" if text.casefold() in _PLACEHOLDERS else text


def _postings(node, out: list) -> None:
    """Every schema.org JobPosting in a JSON-LD value (lists, @graph)."""
    if isinstance(node, list):
        for item in node:
            _postings(item, out)
    elif isinstance(node, dict):
        kind = node.get("@type")
        kinds = kind if isinstance(kind, list) else [kind]
        if "JobPosting" in kinds:
            out.append(node)
        for key in ("@graph", "mainEntity", "itemListElement"):
            if key in node:
                _postings(node[key], out)


def _name(node) -> str:
    if isinstance(node, dict):
        return _plain(node.get("name"))
    if isinstance(node, list):
        return next((n for n in (_name(x) for x in node) if n), "")
    return _plain(node)


def _place(node) -> str:
    """"Hyderabad, Telangana, India" from a jobLocation Place."""
    if isinstance(node, dict):
        addr = node.get("address")
        if isinstance(addr, dict):
            parts = []
            for key in ("addressLocality", "addressRegion", "addressCountry"):
                part = _name(addr.get(key)) if isinstance(addr.get(key), dict) else _plain(addr.get(key))
                if part and part.casefold() not in (p.casefold() for p in parts):
                    parts.append(part)
            return ", ".join(parts)
        return _plain(addr) or _plain(node.get("name"))
    return _plain(node)


def _locations(posting: dict) -> str:
    raw = posting.get("jobLocation")
    places = [p for p in (_place(x) for x in (raw if isinstance(raw, list) else [raw])) if p]
    if str(posting.get("jobLocationType") or "").upper() == "TELECOMMUTE":
        places.append("Remote")
    unique = []
    for p in places:
        if p.casefold() not in (u.casefold() for u in unique):
            unique.append(p)
    return "; ".join(unique)[:160]


def _identifier(node) -> str:
    if isinstance(node, dict):
        return _plain(node.get("value")) or _plain(node.get("name"))
    if isinstance(node, list):
        return next((v for v in (_identifier(x) for x in node) if v), "")
    return _plain(node)


def _day(value: object) -> str:
    m = re.match(r"\s*(\d{4})-(\d{2})-(\d{2})", value) if isinstance(value, str) else None
    if not m:
        return ""
    from datetime import date
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError:
        return ""


def _url_job_id(url: str) -> str:
    """The ATS job ID in the link itself (identity.ats_job_id), without its
    "workday:" style prefix."""
    found = identity.ats_job_id(url)
    return found.split(":", 1)[1] if ":" in found else found


def extract(page_html: str, url: str) -> Fetched:
    """The details a page states (no network)."""
    page = _Page()
    try:
        page.feed(page_html)
        page.close()
    except Exception:
        pass
    postings = []
    for block in page.ld:
        try:
            _postings(json.loads(block, strict=False), postings)
        except ValueError:
            continue
    fields, source = {}, ""
    if postings:
        p, source = postings[0], "structured data"
        fields = {"title": _plain(p.get("title"), 160), "company": _name(p.get("hiringOrganization"))[:120],
                  "location": _locations(p), "job_id": _identifier(p.get("identifier"))[:120],
                  "posted_on": _day(p.get("datePosted")),
                  "description": html_to_text(p.get("description"))[:DESCRIPTION_LIMIT]
                  if isinstance(p.get("description"), str) else ""}
    if not fields.get("title"):
        title = _plain(page.meta.get("og:title") or page.meta.get("twitter:title") or page.title, 160)
        if title:
            fields["title"], source = title, source or "page metadata"
    if not fields.get("company") and _plain(page.meta.get("og:site_name")):
        fields["company"], source = _plain(page.meta.get("og:site_name"), 120), source or "page metadata"
    if not fields.get("description") and _plain(page.meta.get("og:description") or page.meta.get("description")):
        fields["description"] = _plain(page.meta.get("og:description") or page.meta.get("description"), 2000)
    if not fields.get("job_id"):
        fields["job_id"] = _url_job_id(url)
    fields["url"] = url
    return Fetched({k: v for k, v in fields.items() if v}, "", source, page.password)


_CHALLENGE = re.compile(r"cf-chl|challenge-platform|cf_chl_opt|just a moment\.\.\.|attention required! \| cloudflare|"
                        r"g-recaptcha|h-captcha|hcaptcha\.com|px-captcha|captcha-delivery|are you a robot|"
                        r"verify you are human|unusual traffic", re.I)
_NOT_A_PAGE = re.compile(r"\s*(image|audio|video|font)/|\s*application/(pdf|zip|octet-stream|msword|vnd\.)", re.I)
_LOGIN_PATH = re.compile(r"/(login|log-in|signin|sign-in|sign_in|authwall|auth|sso|account/login|uas/login)\b", re.I)


def fetch_job_details(url: str, client: httpx.Client | None = None, resolver=_system_resolver) -> Fetched:
    """Read one job posting. Never raises: every outcome is a Fetched,
    with ``problem`` set to a message for the person when something was
    missing or couldn't be read. ``client``/``resolver``: for tests."""
    try:
        url = check_url(url)
    except Refused as e:
        return Fetched({}, str(e))
    own = client is None
    client = client or httpx.Client(timeout=TIMEOUT, follow_redirects=False, trust_env=False,
                                    headers={"User-Agent": USER_AGENT})
    try:
        if not _robots_allows(client, url, resolver):
            return Fetched({"url": url}, "This website asks automated tools not to read its pages, so the details "
                                         f"weren't fetched. {MANUAL}")
        status, ctype, body, final = _get(client, url, resolver, MAX_BYTES,
                                          "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5")
    except Refused as e:
        return Fetched({"url": url}, f"{e} {MANUAL}" if not str(e).startswith(("Paste", "Enter")) else str(e))
    except httpx.TimeoutException:
        return Fetched({"url": url}, f"The website took too long to answer. {MANUAL}")
    except httpx.HTTPError:
        return Fetched({"url": url}, f"We couldn't reach this website. {MANUAL}")
    finally:
        if own:
            client.close()
    text = body.decode(_charset(ctype), "replace")
    if status in (401, 407) or (status == 200 and _LOGIN_PATH.search(urlsplit(final).path or "")):
        return Fetched({"url": url}, f"This page needs you to sign in, so its details can't be read automatically. {MANUAL}")
    if status in (403, 429, 503) or _CHALLENGE.search(text[:200_000]):
        return Fetched({"url": url}, "This website blocks automated access (for example with a sign-in, CAPTCHA or "
                                     f"bot check), so the details weren't fetched. {MANUAL}")
    if status == 404 or status == 410:
        return Fetched({"url": url}, f"That page wasn't found — the posting may have been taken down. {MANUAL}")
    if status != 200:
        return Fetched({"url": url}, f"The website answered with an error ({status}). {MANUAL}")
    if _NOT_A_PAGE.match(ctype or ""):
        return Fetched({"url": url}, f"That link isn't a web page, so there were no details to read. {MANUAL}")
    found = extract(text, url)
    if found.missing:
        # a sign-in form and nothing about a job: the posting is behind a login
        signed_out = found.asks_password and not found.source == "structured data" and not found.fields.get("company")
        found.problem = (f"This page needs you to sign in, so its details can't be read automatically. {MANUAL}"
                         if signed_out else INCOMPLETE)
    return found


def _charset(content_type: str) -> str:
    m = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    if m:
        try:
            "".encode(m.group(1))
            return m.group(1)
        except LookupError:
            pass
    return "utf-8"
