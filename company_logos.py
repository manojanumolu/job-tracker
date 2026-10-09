"""Company logos, found automatically on each company's OWN website.

For every tracked company this looks for the icon the company itself
publishes (apple-touch-icon, <link rel="icon">, its schema.org logo or a
logo og:image) and keeps a small copy in logos.json:

  {"version": 1, "logos": {"<company id>": {
      "status": "ok", "domain": "sanofi.com", "page": "https://www.sanofi.com/en",
      "web":   {"mime": "image/png", "data": "<base64>", "w": 180, "h": 180, "src": "<icon url>"},
      "email": {...same shape, PNG/JPEG/GIF only...} | null,
      "hint": "...", "checked_at": "..."}
   | {"status": "none", "reason": "...", "hint": "...", "checked_at": "..."}}}

Where it looks, in order (at most three sites per company):
  1. the company's "website" field, when one is set;
  2. the careers site's own domain when it carries the company's name
     (careers.npci.org.in -> npci.org.in), never the careers sub-site;
  3. <name>.com (metlife.com), accepted only if the page names the company;
  4. the careers site's domain otherwise (metlifecareers.com), likewise.
Job-board / applicant-tracking hosts (Workday, Greenhouse, Zoho Recruit,
...) are never used: their icon is the job board's, not the employer's.

Rules that keep it honest and light:
  * a page must identify the company (title / site name), parked or
    for-sale pages are refused, and an icon must be a real image of a
    sensible size and shape — otherwise the app shows initials;
  * one visit per site: robots.txt is honoured, redirects are followed by
    hand (at most 4, each to a public address), every response is size-
    capped and every request times out quickly; no authentication, no
    anti-bot workarounds, no third-party logo service;
  * results are cached: a found logo is kept and never replaced by a later
    failure; a miss is retried after NEGATIVE_TTL_DAYS; a changed company
    URL triggers a new look (the old logo stays until a new one is found).

It runs in its own GitHub Actions workflow (logos.yml), never in the app's
request path, the job scan or the email step. The app and the emails only
read logos.json. No Streamlit import here.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import logging
import re
import socket
import struct
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

log = logging.getLogger("company_logos")

BASE = Path(__file__).parent
LOGOS_FILE = "logos.json"
USER_AGENT = "FresherJobTracker-LogoFinder/1.0 (personal job tracker; fetches a site's own icon once)"
TIMEOUT_S, CONNECT_TIMEOUT_S = 6.0, 4.0
MAX_HTML_BYTES = 512 * 1024
MAX_ICON_BYTES = 16 * 1024              # a logo kept in logos.json (and sent with pages) stays small
MAX_REDIRECTS = 4
MAX_SITES = 3                           # candidate websites per company
MAX_ICON_TRIES = 4                      # icon downloads per site
MIN_PX = 32
NEGATIVE_TTL_DAYS = 14
FILE_BUDGET = 900 * 1024                # GitHub's contents API refuses files over 1 MB
DEFAULT_LIMIT = 8                       # companies looked up per run

# applicant-tracking systems and job boards: their pages and icons are not
# the employer's
ATS_DOMAINS = {
    "myworkdayjobs.com", "myworkdaysite.com", "workday.com", "greenhouse.io", "lever.co", "icims.com",
    "smartrecruiters.com", "zohorecruit.com", "zohorecruit.in", "zoho.com", "zoho.in", "ashbyhq.com", "jobvite.com",
    "taleo.net", "oraclecloud.com", "successfactors.com", "successfactors.eu", "sapsf.com", "avature.net",
    "breezy.hr", "recruitee.com", "workable.com", "bamboohr.com", "eightfold.ai", "phenompeople.com",
    "ultipro.com", "adp.com", "darwinbox.in", "darwinbox.com", "keka.com", "freshteam.com", "linkedin.com",
    "indeed.com", "naukri.com", "glassdoor.com", "instahyre.com", "wellfound.com", "hirist.com", "jobs.lever.co",
    "teamtailor.com", "personio.de", "personio.com", "jazzhr.com", "applytojob.com", "csod.com", "brassring.com",
    "kenexa.com", "skillate.com", "mynexthire.com", "turbohire.co", "superset.com",
}
_MULTI_SUFFIXES = {"co.uk", "org.uk", "ac.uk", "gov.uk", "co.in", "org.in", "net.in", "gov.in", "ac.in", "firm.in",
                   "gen.in", "ind.in", "com.au", "net.au", "org.au", "co.jp", "com.br", "com.sg", "com.cn",
                   "co.nz", "co.za", "com.mx", "com.hk", "com.tw", "co.kr"}
_NAME_STOPWORDS = {"the", "inc", "ltd", "llc", "llp", "plc", "pvt", "private", "limited", "corp", "corporation",
                   "company", "co", "group", "india", "technologies", "technology", "solutions", "services"}
_PARKED = re.compile(r"domain (?:is )?for sale|buy this domain|this domain (?:may be|is) for sale|parked (?:free|domain)"
                     r"|domain parking|godaddy\.com/domainsearch|sedo\.com|hugedomains", re.I)


class LogoError(Exception):
    pass


def _now_iso(now: float | None = None) -> str:
    return datetime.fromtimestamp(now if now is not None else time.time(), timezone.utc).isoformat(timespec="seconds")


def _age_days(iso: str | None, now: float | None = None) -> float:
    try:
        then = datetime.fromisoformat(iso).timestamp()
    except (TypeError, ValueError):
        return float("inf")
    return ((now if now is not None else time.time()) - then) / 86400


# ---------------------------------------------------------------------------
# names and domains
# ---------------------------------------------------------------------------

def _norm(text: object) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower()) if isinstance(text, str) else ""


def name_slug(name: object) -> str:
    """"MetLife" -> "metlife", "Morgan Stanley Inc." -> "morganstanley"."""
    words = [w for w in re.findall(r"[a-z0-9]+", (name if isinstance(name, str) else "").lower().replace("&", " and "))]
    kept = [w for w in words if w not in _NAME_STOPWORDS] or words
    return "".join(kept)


def registrable(host: str) -> str:
    """"careers.npci.org.in" -> "npci.org.in", "www.sanofi.com" -> "sanofi.com"."""
    labels = [p for p in (host or "").lower().strip(".").split(".") if p]
    if len(labels) >= 3 and ".".join(labels[-2:]) in _MULTI_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _label(domain: str) -> str:
    return domain.split(".", 1)[0] if domain else ""


def is_ats(host: str) -> bool:
    host = (host or "").lower()
    return any(host == d or host.endswith("." + d) for d in ATS_DOMAINS)


def _host(url: object) -> str:
    try:
        return (urlparse(url).hostname or "").lower() if isinstance(url, str) else ""
    except ValueError:
        return ""


def candidate_sites(company: dict) -> list[tuple[str, str]]:
    """[(homepage url, trust)] to try, best first. trust: "website" (set by
    the owner), "own-domain" (careers domain carrying the name), "check"
    (must prove it is the company)."""
    slug = name_slug(company.get("name"))
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(domain: str, trust: str, url: str = ""):
        if domain and "." in domain and domain not in seen and not is_ats(domain):
            seen.add(domain)
            out.append((url or f"https://www.{domain}/", trust))

    web = company.get("website")
    if isinstance(web, str) and web.strip():
        w = web.strip()
        w = w if re.match(r"^https?://", w, re.I) else "https://" + w
        if _host(w):
            add(registrable(_host(w)), "website", w)
    careers = registrable(_host(company.get("url")))
    careers_ok = careers and not is_ats(_host(company.get("url")))
    if careers_ok and slug and _label(careers) == slug:           # careers.avalara.com -> avalara.com
        add(careers, "own-domain")
    if slug and len(slug) >= 3:
        add(f"{slug}.com", "check")
    if careers_ok:
        add(careers, "check")
    return out[:MAX_SITES]


# ---------------------------------------------------------------------------
# network (one small, polite client; every hop checked)
# ---------------------------------------------------------------------------

def _public(host: str, resolve=None) -> bool:
    """Only named, public hosts: no IP literals, no localhost, nothing that
    resolves to a private, loopback or link-local address."""
    if not host or "." not in host or host.endswith((".local", ".internal", ".localhost")):
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    try:
        infos = (resolve or socket.getaddrinfo)(host, 443)
    except OSError:
        return False
    addrs = {i[4][0] for i in infos}
    return bool(addrs) and all(ipaddress.ip_address(a.split("%", 1)[0]).is_global for a in addrs)


class Fetcher:
    """GETs with a byte cap, a timeout, manual redirects (each to a public
    host) and robots.txt honoured per site. Remembers robots per host."""

    def __init__(self, client=None, resolve=None, pause_s: float = 0.5):
        import httpx
        self._httpx = httpx
        self.client = client or httpx.Client(timeout=httpx.Timeout(TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
                                             follow_redirects=False,
                                             headers={"User-Agent": USER_AGENT, "Accept-Language": "en"})
        self.resolve, self.pause_s = resolve, pause_s
        self._robots: dict[str, RobotFileParser | None] = {}
        self.requests = 0

    def _allowed(self, url: str) -> bool:
        p = urlparse(url)
        key = f"{p.scheme}://{p.netloc}"
        if key not in self._robots:
            rp = RobotFileParser()
            try:
                status, body, _, _ = self._get(key + "/robots.txt", 64 * 1024, check_robots=False)
            except LogoError:
                status, body = 404, b""
            if status >= 500:
                rp.disallow_all = True
            elif status >= 400:
                rp.allow_all = True
            else:
                rp.parse(body.decode("utf-8", "replace").splitlines())
            self._robots[key] = rp
        return self._robots[key].can_fetch(USER_AGENT, url)

    def _get(self, url: str, cap: int, check_robots: bool = True, truncate: bool = False):
        for _ in range(MAX_REDIRECTS + 1):
            p = urlparse(url)
            if p.scheme not in ("http", "https") or not _public(p.hostname or "", self.resolve):
                raise LogoError("blocked address")
            if check_robots and not self._allowed(url):
                raise LogoError("robots.txt")
            if self.requests:
                time.sleep(self.pause_s)
            self.requests += 1
            try:
                with self.client.stream("GET", url, headers={"Accept": "*/*"}) as r:
                    if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                        url = urljoin(url, r.headers["location"])
                        continue
                    body = b""
                    for chunk in r.iter_bytes():
                        body += chunk
                        if len(body) > cap:
                            if truncate:                     # a page: its <head> is at the start
                                return r.status_code, body[:cap], url, r.headers.get("content-type", "")
                            raise LogoError("too large")
                    return r.status_code, body, url, r.headers.get("content-type", "")
            except self._httpx.HTTPError as e:
                raise LogoError(type(e).__name__) from None
        raise LogoError("too many redirects")

    def get(self, url: str, cap: int, truncate: bool = False):
        status, body, final, ctype = self._get(url, cap, truncate=truncate)
        if status != 200:
            raise LogoError(f"HTTP {status}")
        return body, final, ctype


# ---------------------------------------------------------------------------
# reading a homepage
# ---------------------------------------------------------------------------

class _Head(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.links, self.metas, self.ld, self.base = "", [], {}, [], ""
        self._in_title = self._in_ld = False

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "title":
            self._in_title = True
        elif tag == "link":
            self.links.append(a)
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").lower()
            if key and key not in self.metas:
                self.metas[key] = a.get("content", "")
        elif tag == "script" and "ld+json" in a.get("type", "").lower():
            self._in_ld = True
            self.ld.append("")
        elif tag == "base" and a.get("href") and not self.base:
            self.base = a["href"]

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "script":
            self._in_ld = False

    def handle_data(self, data):
        if self._in_title and len(self.title) < 300:
            self.title += data
        elif self._in_ld and len(self.ld[-1]) < 200_000:
            self.ld[-1] += data


def _ld_logos(blocks: list[str]) -> list[str]:
    out = []

    def walk(node):
        if isinstance(node, list):
            for n in node:
                walk(n)
        elif isinstance(node, dict):
            kind = node.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if any(k in ("Organization", "Corporation", "LocalBusiness", "WebSite") for k in kinds if isinstance(k, str)):
                logo = node.get("logo")
                if isinstance(logo, dict):
                    logo = logo.get("url") or logo.get("contentUrl")
                if isinstance(logo, str):
                    out.append(logo)
            for v in node.values():
                if isinstance(v, (dict, list)):
                    walk(v)
    for b in blocks:
        try:
            walk(json.loads(b))
        except ValueError:
            continue
    return out


def _sizes(value: str) -> int:
    best = 0
    for m in re.finditer(r"(\d+)\s*x\s*(\d+)", value or "", re.I):
        best = max(best, min(int(m.group(1)), int(m.group(2))))
    return best


def icon_candidates(head: _Head, page_url: str) -> list[tuple[int, str, str]]:
    """[(score, url, kind)] best first."""
    base = urljoin(page_url, head.base) if head.base else page_url
    found: dict[str, tuple[int, str]] = {}

    def add(href: str, score: int, kind: str):
        if not href or href.startswith("data:"):
            return
        url = urljoin(base, href.strip())
        if urlparse(url).scheme in ("http", "https") and (url not in found or found[url][0] < score):
            found[url] = (score, kind)

    for link in head.links:
        rels = set((link.get("rel") or "").lower().split())
        href, size, typ = link.get("href", ""), _sizes(link.get("sizes", "")), link.get("type", "").lower()
        svg = "svg" in typ or href.lower().split("?")[0].endswith(".svg")
        if rels & {"apple-touch-icon", "apple-touch-icon-precomposed"}:
            add(href, 100 if (size or 180) >= 120 else 55, "apple-touch-icon")
        elif "icon" in rels and "mask-icon" not in rels:
            if svg:
                add(href, 95, "icon-svg")
            elif size >= 96:
                add(href, 90, "icon")
            elif size >= 48:
                add(href, 60, "icon")
            elif size == 0:
                add(href, 42, "icon")
            elif size >= MIN_PX:
                add(href, 40, "icon")
    for logo in _ld_logos(head.ld):
        add(logo, 70, "schema-logo")
    og = head.metas.get("og:logo") or ""
    if og:
        add(og, 65, "og-logo")
    ogi = head.metas.get("og:image") or ""
    if "logo" in ogi.lower():
        add(ogi, 50, "og-image-logo")
    root = f"{urlparse(page_url).scheme}://{urlparse(page_url).netloc}"
    add(root + "/apple-touch-icon.png", 45, "apple-touch-icon")
    add(root + "/favicon.ico", 30, "favicon")
    return sorted(((s, u, k) for u, (s, k) in found.items()), key=lambda t: -t[0])


def _identifies(head: _Head, slug: str, final_domain: str = "") -> bool:
    """Does the page name the company (title, site name, app name)? A
    domain that merely spells the name proves nothing — anyone can own it."""
    texts = [head.title, head.metas.get("og:site_name", ""), head.metas.get("application-name", ""),
             head.metas.get("og:title", ""), head.metas.get("apple-mobile-web-app-title", "")]
    if len(slug) < 3:
        return any(re.search(rf"\b{re.escape(slug)}\b", t or "", re.I) for t in texts)
    return any(slug in _norm(t) for t in texts)


# ---------------------------------------------------------------------------
# images: sniff, measure, accept
# ---------------------------------------------------------------------------

def _png_size(b: bytes):
    return struct.unpack(">II", b[16:24]) if len(b) >= 24 and b[12:16] == b"IHDR" else None


def _gif_size(b: bytes):
    return struct.unpack("<HH", b[6:10]) if len(b) >= 10 else None


def _jpeg_size(b: bytes):
    i = 2
    while i + 9 < len(b):
        if b[i] != 0xFF:
            i += 1
            continue
        marker = b[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg = struct.unpack(">H", b[i + 2:i + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h, w = struct.unpack(">HH", b[i + 5:i + 9])
            return w, h
        i += 2 + seg
    return None


def _webp_size(b: bytes):
    if b[12:16] == b"VP8 " and len(b) >= 30:
        w, h = struct.unpack("<HH", b[26:30])
        return w & 0x3FFF, h & 0x3FFF
    if b[12:16] == b"VP8L" and len(b) >= 25:
        bits = int.from_bytes(b[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if b[12:16] == b"VP8X" and len(b) >= 30:
        return int.from_bytes(b[24:27], "little") + 1, int.from_bytes(b[27:30], "little") + 1
    return None


def _svg_size(text: str):
    m = re.search(r"viewBox\s*=\s*[\"']\s*[-\d.]+[\s,]+[-\d.]+[\s,]+([\d.]+)[\s,]+([\d.]+)", text)
    if m:
        return float(m.group(1)), float(m.group(2))
    w, h = re.search(r"\bwidth\s*=\s*[\"']([\d.]+)", text), re.search(r"\bheight\s*=\s*[\"']([\d.]+)", text)
    return (float(w.group(1)), float(h.group(1))) if w and h else None


def _png(width: int, height: int, rgba_rows: list[bytes]) -> bytes:
    import zlib

    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + row for row in rgba_rows)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _ico_bitmap_to_png(blob: bytes) -> bytes | None:
    """A 32-bit (BGRA) bitmap from an .ico as PNG, or None for other kinds."""
    if len(blob) < 40:
        return None
    size, w, h2, _, bpp, comp = struct.unpack("<IiiHHI", blob[:20])
    h = abs(h2) // 2
    if size < 40 or bpp != 32 or comp != 0 or not (0 < w <= 256 and 0 < h <= 256):
        return None
    start, stride = size, w * 4
    if len(blob) < start + stride * h:
        return None
    rows = []
    for y in range(h):
        line = blob[start + stride * y:start + stride * (y + 1)]
        rows.append(bytes(c for i in range(0, len(line), 4) for c in (line[i + 2], line[i + 1], line[i], line[i + 3])))
    if h2 > 0:                                   # bitmaps are stored bottom-up
        rows.reverse()
    return _png(w, h, rows)


def sniff(data: bytes) -> tuple[str, bytes, tuple | None]:
    """(mime, bytes to keep, (w, h)) — ICO files with a PNG inside become
    that PNG. Raises LogoError for anything that isn't an image."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", data, _png_size(data)
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", data, _jpeg_size(data)
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif", data, _gif_size(data)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", data, _webp_size(data)
    if data[:4] == b"\x00\x00\x01\x00" and len(data) >= 6:
        count = struct.unpack("<H", data[4:6])[0]
        best = None
        for k in range(min(count, 32)):
            e = data[6 + 16 * k:22 + 16 * k]
            if len(e) < 16:
                break
            w, h = e[0] or 256, e[1] or 256
            size, off = struct.unpack("<II", e[8:16])
            if best is None or w > best[0]:
                best = (w, h, data[off:off + size])
        if best is None:
            raise LogoError("empty icon")
        w, h, blob = best
        if blob.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png", blob, _png_size(blob) or (w, h)
        png = _ico_bitmap_to_png(blob)
        if png:
            return "image/png", png, (w, h)
        return "image/x-icon", data, (w, h)
    head = data[:2048].decode("utf-8", "ignore").lower()
    if "<svg" in head and "<html" not in head:
        text = data.decode("utf-8", "replace")
        if re.search(r"<script|\bon\w+\s*=|javascript:|<foreignobject", text, re.I):
            raise LogoError("svg with scripts")
        return "image/svg+xml", data, _svg_size(text)
    raise LogoError("not an image")


def accept(data: bytes) -> dict:
    mime, blob, dims = sniff(data)
    if len(blob) > MAX_ICON_BYTES:
        raise LogoError("too large")
    w, h = dims or (0, 0)
    if mime != "image/svg+xml" and (not w or not h or min(w, h) < MIN_PX):
        raise LogoError("too small")
    if w and h and not 0.4 <= w / h <= 2.5:
        raise LogoError("not logo-shaped")
    return {"mime": mime, "data": base64.b64encode(blob).decode("ascii"), "w": int(w), "h": int(h)}


EMAIL_MIMES = ("image/png", "image/jpeg", "image/gif")


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------

def discover(company: dict, fetcher: Fetcher, domain_cache: dict | None = None) -> dict:
    """Find this company's own logo. Returns an "ok" or "none" entry (no
    timestamps/hint; update() adds those). Never raises."""
    slug = name_slug(company.get("name"))
    domain_cache = domain_cache if domain_cache is not None else {}
    reasons = []
    for url, trust in candidate_sites(company):
        domain = registrable(_host(url))
        if domain in domain_cache:
            if domain_cache[domain].get("status") == "ok":
                return domain_cache[domain]
            reasons.append(f"{domain}: {domain_cache[domain].get('reason')}")
            continue
        result = _discover_site(url, trust, slug, fetcher)
        domain_cache[domain] = result
        if result["status"] == "ok":
            return result
        reasons.append(f"{domain}: {result['reason']}")
    return {"status": "none", "reason": "; ".join(reasons)[:300] or "no website to look at"}


def _discover_site(url: str, trust: str, slug: str, fetcher: Fetcher) -> dict:
    try:
        body, final, ctype = fetcher.get(url, MAX_HTML_BYTES, truncate=True)
    except LogoError as e:
        return {"status": "none", "reason": str(e)}
    if "html" not in ctype.lower() and b"<html" not in body[:4096].lower():
        return {"status": "none", "reason": "not a web page"}
    final_domain = registrable(_host(final))
    if is_ats(_host(final)):
        return {"status": "none", "reason": "redirects to a job board"}
    head = _Head()
    try:
        head.feed(body.decode("utf-8", "replace"))
    except Exception:                                        # malformed markup: use what was read
        pass
    text = body[:200_000].decode("utf-8", "replace")
    if _PARKED.search(head.title) or _PARKED.search(text[:20_000]):
        return {"status": "none", "reason": "parked domain"}
    moved = final_domain != registrable(_host(url)) and _label(final_domain) != slug
    if (trust == "check" or moved) and not _identifies(head, slug, final_domain):
        return {"status": "none", "reason": "page doesn't name the company"}
    web = email = None
    tries = 0
    for _, icon_url, kind in icon_candidates(head, final):
        if (web and email) or tries >= MAX_ICON_TRIES:
            break
        tries += 1
        try:
            data, src, _ = fetcher.get(icon_url, MAX_ICON_BYTES * 2)
            img = accept(data)
        except LogoError:
            continue
        img.update(src=src, kind=kind)
        if web is None:
            web = img
        if email is None and img["mime"] in EMAIL_MIMES:
            email = img
    if web is None:
        return {"status": "none", "reason": "no usable icon"}
    return {"status": "ok", "domain": final_domain, "page": final, "web": web,
            "email": None if email is None else ("web" if email is web else email)}


def _hint(company: dict) -> str:
    raw = json.dumps([company.get("name"), company.get("url"), company.get("website")], sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def pending(companies: list[dict], logos: dict, now: float | None = None) -> list[dict]:
    """Companies that need a look: never looked up, their name/URL/website
    changed, or a miss older than NEGATIVE_TTL_DAYS. A found logo whose
    company is unchanged is never fetched again."""
    out = []
    for c in companies:
        if not isinstance(c, dict) or not c.get("id"):
            continue
        e = logos.get(c["id"])
        if not isinstance(e, dict) or e.get("hint") != _hint(c):
            out.append(c)
        elif e.get("status") != "ok" and _age_days(e.get("checked_at"), now) >= NEGATIVE_TTL_DAYS:
            out.append(c)
    return out


def update(companies: list[dict], doc: dict, fetcher: Fetcher, now: float | None = None,
           limit: int = DEFAULT_LIMIT) -> tuple[dict, list[str]]:
    """Look up the companies that need it (at most ``limit``) and return
    (new logos.json document, ids changed). A found logo is never replaced
    by a miss; entries of removed companies are dropped."""
    logos = dict((doc or {}).get("logos") or {}) if isinstance(doc, dict) else {}
    ids = {c.get("id") for c in companies if isinstance(c, dict)}
    changed = [k for k in logos if k not in ids]
    for k in changed:
        logos.pop(k)
    domain_cache: dict = {}
    for c in pending(companies, logos, now)[:max(0, limit)]:
        old = logos.get(c["id"])
        result = discover(c, fetcher, domain_cache)
        stamp = {"hint": _hint(c), "checked_at": _now_iso(now)}
        if result["status"] == "ok":
            entry = {**result, **stamp}
            if _size(logos) + len(json.dumps(entry)) > FILE_BUDGET:
                entry = {"status": "none", "reason": "logo file budget reached", **stamp}
        elif isinstance(old, dict) and old.get("status") == "ok":
            continue                                    # keep the logo we have; look again later
        else:
            entry = {**result, **stamp}
        logos[c["id"]] = entry
        changed.append(c["id"])
        log.info("logo %s: %s", c["id"], entry["status"] if entry["status"] == "ok" else entry.get("reason"))
    return {"version": 1, "logos": logos}, changed


def _size(logos: dict) -> int:
    return len(json.dumps(logos))


# ---------------------------------------------------------------------------
# reading (the app and the emails) — never touches the network
# ---------------------------------------------------------------------------

def entries(doc: object) -> dict:
    """{company id: ok entry} from a logos.json document; anything malformed
    is ignored."""
    logos = doc.get("logos") if isinstance(doc, dict) else None
    out = {}
    for k, e in (logos or {}).items() if isinstance(logos, dict) else ():
        web = e.get("web") if isinstance(e, dict) and e.get("status") == "ok" else None
        if isinstance(web, dict) and isinstance(web.get("data"), str) and web.get("mime") in (
                "image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml", "image/x-icon"):
            out[k] = e
    return out


def web_uri(entry: dict) -> str:
    return f"data:{entry['web']['mime']};base64,{entry['web']['data']}"


def email_image(entry: dict) -> tuple[bytes, str] | None:
    """(bytes, subtype) of the email-safe copy (PNG/JPEG/GIF), or None."""
    e = entry.get("email")
    e = entry.get("web") if e == "web" else e
    if not isinstance(e, dict) or e.get("mime") not in EMAIL_MIMES:
        return None
    try:
        return base64.b64decode(e["data"], validate=True), e["mime"].split("/", 1)[1]
    except (ValueError, KeyError, TypeError):
        return None


def load_local(path: Path | None = None) -> dict:
    """logos.json from the checkout; {} if missing or unreadable."""
    try:
        return json.loads((path or BASE / LOGOS_FILE).read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Find tracked companies' own logos (logos.json).")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    ap.add_argument("--publish", action="store_true", help="merge and push logos.json (the workflow)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        companies = json.loads((BASE / "companies.json").read_text("utf-8"))
    except (OSError, ValueError):
        log.warning("companies.json unreadable; nothing to do")
        return 0
    doc, changed = update([c for c in companies if isinstance(c, dict)], load_local(), Fetcher(), limit=args.limit)
    if not changed:
        log.info("logos: nothing to update")
        return 0
    if args.publish:
        from repo_sync import publish
        ok = publish("chore: update company logos [skip ci]", {LOGOS_FILE: doc})
        log.info("logos: %s %d entr%s", "published" if ok else "could not publish", len(changed),
                 "y" if len(changed) == 1 else "ies")
        return 0 if ok else 1
    (BASE / LOGOS_FILE).write_text(json.dumps(doc, indent=1) + "\n", encoding="utf-8")
    log.info("logos: wrote %s (%d changed)", LOGOS_FILE, len(changed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
