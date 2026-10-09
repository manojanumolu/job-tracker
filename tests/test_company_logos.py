"""Company logos found on each company's own website (company_logos.py ->
logos.json) and shown in the app and in alert emails.

The rules under test:
  * the logo comes from the company's own site, never a job board's; the
    page must name the company; parked pages, robots.txt refusals, tiny or
    odd-shaped or oversized or non-image files are refused
  * a found logo is cached and never fetched again or replaced by a miss;
    misses are retried later; a new company is looked up on the next run
  * the app and the emails only READ logos.json — no network, and missing
    or broken data means initials, never an error or a broken image
  * an employer's logo never touches the viewer's profile avatar
The network is httpx.MockTransport and a fake DNS resolver; nothing is
fetched and no email is sent."""
import base64
import json
import struct
import time
from email import message_from_string
from pathlib import Path

import httpx
import pytest

import brand_logos
import company_logos as L
import notifier
import repo_sync
from test_firebase_auth import google_env, no_env  # noqa: F401  (fixtures)
from test_streamlit_app import app  # noqa: F401

REPO = Path(__file__).resolve().parent.parent
PUBLIC = lambda host, port: [(2, 1, 6, "", ("93.184.216.34", port))]       # noqa: E731


def png(w=180, h=180):
    return L._png(w, h, [b"\x10\x20\x30\xff" * w] * h)


def ico_bmp(w=48, h=48):
    """A one-image .ico holding a 32-bit bitmap (what metlife.com serves)."""
    header = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    pixels = bytes([0x30, 0x20, 0x10, 0xFF]) * (w * h) + b"\x00" * (((w + 31) // 32) * 4 * h)
    blob = header + pixels
    return struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, len(blob), 22) + blob


def page(title="Acme Corp — Home", head=""):
    return f"<html><head><title>{title}</title>{head}</head><body>hi</body></html>".encode()


class Site:
    """A tiny web: {url: (status, body, content type) or an exception}."""
    def __init__(self, routes):
        self.routes, self.seen = routes, []

    def handler(self, request):
        url = str(request.url)
        self.seen.append(url)
        r = self.routes.get(url)
        if r is None:
            return httpx.Response(404, content=b"")
        if isinstance(r, Exception):
            raise r
        status, body, ctype = r
        headers = {"content-type": ctype}
        if status in (301, 302):
            headers["location"] = body.decode()
            body = b""
        return httpx.Response(status, content=body, headers=headers)

    def fetcher(self, resolve=PUBLIC):
        return L.Fetcher(client=httpx.Client(transport=httpx.MockTransport(self.handler)), resolve=resolve, pause_s=0)


ACME = {"id": "acme", "name": "Acme", "url": "https://acme.wd3.myworkdayjobs.com/External"}
ICON = '<link rel="apple-touch-icon" sizes="180x180" href="/icons/a.png">'


def acme_site(**over):
    routes = {"https://www.acme.com/": (200, page(head=ICON), "text/html"),
              "https://www.acme.com/icons/a.png": (200, png(), "image/png")}
    routes.update(over)
    return Site(routes)


# ── discovery ────────────────────────────────────────────────────────────────

def test_finds_the_official_icon_on_the_companys_own_site():
    site = acme_site()
    result = L.discover(ACME, site.fetcher())
    assert result["status"] == "ok" and result["domain"] == "acme.com"
    assert result["web"]["mime"] == "image/png" and (result["web"]["w"], result["web"]["h"]) == (180, 180)
    assert result["web"]["kind"] == "apple-touch-icon" and result["email"] == "web"
    assert base64.b64decode(result["web"]["data"]) == png()
    assert not [u for u in site.seen if "myworkdayjobs" in u]               # never the job board


@pytest.mark.parametrize("company, first", [
    ({"name": "Sanofi", "url": "https://sanofi.wd3.myworkdayjobs.com/SanofiCareers"}, "https://www.sanofi.com/"),
    ({"name": "NPCI", "url": "https://careers.npci.org.in/jobs/Careers"}, "https://www.npci.org.in/"),
    ({"name": "Avalara", "url": "https://careers.avalara.com/careers-home/jobs"}, "https://www.avalara.com/"),
    ({"name": "MetLife", "url": "https://www.metlifecareers.com/en_US/ml/SearchJobs"}, "https://www.metlife.com/"),
    ({"name": "Acme", "url": "https://x.greenhouse.io/acme", "website": "acme.example.org"}, "https://acme.example.org"),
])
def test_where_it_looks(company, first):
    sites = L.candidate_sites(company)
    assert sites[0][0] == first and len(sites) <= L.MAX_SITES
    assert not [u for u, _ in sites if L.is_ats(L._host(u))]


@pytest.mark.parametrize("title", ["Welcome to Example Industries", ""])
def test_a_page_that_does_not_name_the_company_is_not_trusted(title):
    site = acme_site(**{"https://www.acme.com/": (200, page(title=title, head=ICON), "text/html")})
    result = L.discover(ACME, site.fetcher())
    assert result["status"] == "none" and "doesn't name the company" in result["reason"]
    assert "https://www.acme.com/icons/a.png" not in site.seen


def test_parked_domains_are_refused():
    site = acme_site(**{"https://www.acme.com/": (200, page(title="acme.com is for sale | Buy this domain"), "text/html")})
    assert L.discover(ACME, site.fetcher())["reason"].endswith("parked domain")


def test_robots_txt_is_honoured():
    site = acme_site(**{"https://www.acme.com/robots.txt": (200, b"User-agent: *\nDisallow: /", "text/plain")})
    result = L.discover(ACME, site.fetcher())
    assert result["status"] == "none" and "robots.txt" in result["reason"]
    assert site.seen == ["https://www.acme.com/robots.txt"]


def test_a_refused_request_is_respected_not_retried():
    site = acme_site(**{"https://www.acme.com/": (403, b"denied", "text/html")})
    result = L.discover(ACME, site.fetcher())
    assert result["status"] == "none" and "HTTP 403" in result["reason"]
    assert site.seen.count("https://www.acme.com/") == 1


@pytest.mark.parametrize("bad, why", [
    ((200, b"<html>not an image</html>", "image/png"), "not an image"),
    ((200, png(16, 16), "image/png"), "tiny"),
    ((200, png(400, 60), "image/png"), "a wide banner"),
    ((200, b"\x89PNG\r\n\x1a\n" + b"\x00" * (L.MAX_ICON_BYTES * 3), "image/png"), "too big"),
    ((200, b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><script>alert(1)</script></svg>',
      "image/svg+xml"), "svg with script"),
])
def test_unusable_icons_are_skipped_for_the_next_candidate(bad, why):
    site = acme_site(**{"https://www.acme.com/icons/a.png": bad,
                        "https://www.acme.com/favicon.ico": (200, ico_bmp(64, 64), "image/x-icon")})
    result = L.discover(ACME, site.fetcher())
    assert result["status"] == "ok" and result["web"]["kind"] == "favicon", why
    assert result["web"]["mime"] == "image/png" and result["web"]["w"] == 64      # the bitmap became a PNG


def test_ico_bitmaps_become_png():
    mime, data, dims = L.sniff(ico_bmp(48, 48))
    assert mime == "image/png" and dims == (48, 48) and L._png_size(data) == (48, 48)
    assert L.accept(ico_bmp(48, 48))["mime"] == "image/png"


def test_schema_and_og_logo_metadata_are_used_but_random_images_are_not():
    head = ('<meta property="og:image" content="https://www.acme.com/img/summer-campaign.jpg">'
            '<script type="application/ld+json">{"@type": "Organization", "logo": "https://www.acme.com/brand/logo.png"}'
            '</script>')
    cands = L.icon_candidates(_head(page(head=head)), "https://www.acme.com/")
    urls = [u for _, u, _ in cands]
    assert "https://www.acme.com/brand/logo.png" in urls and "https://www.acme.com/img/summer-campaign.jpg" not in urls


def _head(body):
    h = L._Head()
    h.feed(body.decode())
    return h


@pytest.mark.parametrize("error", [httpx.ConnectTimeout("slow"), httpx.ReadTimeout("slow"), httpx.ConnectError("down")])
def test_slow_or_failing_sites_end_quickly_in_a_miss(error):
    site = acme_site(**{"https://www.acme.com/robots.txt": error, "https://www.acme.com/": error})
    start = time.monotonic()
    doc, changed = L.update([ACME], {}, site.fetcher())
    assert doc["logos"]["acme"]["status"] == "none" and changed == ["acme"]
    assert time.monotonic() - start < 2


@pytest.mark.parametrize("addr", ["10.0.0.5", "127.0.0.1", "169.254.169.254", "192.168.1.1", "::1"])
def test_private_addresses_are_never_fetched(addr):
    site = acme_site()
    result = L.discover(ACME, site.fetcher(resolve=lambda h, p: [(2, 1, 6, "", (addr, p))]))
    assert result["status"] == "none" and site.seen == []


def test_a_redirect_to_a_private_host_is_refused():
    site = acme_site(**{"https://www.acme.com/": (302, b"http://intranet.acme.com/", "text/html")})

    def resolve(host, port):
        return [(2, 1, 6, "", ("10.1.2.3" if host.startswith("intranet") else "93.184.216.34", port))]
    assert L.discover(ACME, site.fetcher(resolve=resolve))["status"] == "none"
    assert "http://intranet.acme.com/" not in site.seen


# ── caching and updates ──────────────────────────────────────────────────────

def test_a_found_logo_is_never_fetched_again():
    site = acme_site()
    doc, _ = L.update([ACME], {}, site.fetcher())
    again = site.fetcher()
    doc2, changed = L.update([ACME], doc, again)
    assert again.requests == 0 and changed == [] and doc2 == doc


def test_a_miss_is_retried_only_after_its_waiting_time():
    site = acme_site(**{"https://www.acme.com/": (403, b"", "text/html")})
    now = time.time()
    doc, _ = L.update([ACME], {}, site.fetcher(), now=now)
    soon = site.fetcher()
    L.update([ACME], doc, soon, now=now + 86400)
    assert soon.requests == 0
    later = acme_site().fetcher()
    doc3, changed = L.update([ACME], doc, later, now=now + (L.NEGATIVE_TTL_DAYS + 1) * 86400)
    assert later.requests > 0 and doc3["logos"]["acme"]["status"] == "ok" and changed == ["acme"]


def test_a_found_logo_is_kept_when_a_later_look_fails():
    doc, _ = L.update([ACME], {}, acme_site().fetcher())
    moved = {**ACME, "url": "https://careers.acme.com/new"}                 # its URL changed: look again
    down = acme_site(**{"https://www.acme.com/": httpx.ConnectError("down")}).fetcher()
    doc2, changed = L.update([moved], doc, down)
    assert down.requests > 0 and changed == [] and doc2["logos"]["acme"] == doc["logos"]["acme"]


def test_a_new_company_is_looked_up_on_the_next_run():
    doc, _ = L.update([ACME], {}, acme_site().fetcher())
    newco = {"id": "c99", "name": "Globex", "url": "https://globex.com/careers"}
    assert [c["id"] for c in L.pending([ACME, newco], doc["logos"])] == ["c99"]
    site = Site({"https://www.globex.com/": (200, page("Globex Corporation", '<link rel="icon" type="image/png" '
                                                                         'sizes="96x96" href="/f.png">'), "text/html"),
                 "https://www.globex.com/f.png": (200, png(96, 96), "image/png")})
    doc2, changed = L.update([ACME, newco], doc, site.fetcher())
    assert changed == ["c99"] and doc2["logos"]["c99"]["status"] == "ok"
    assert doc2["logos"]["acme"] == doc["logos"]["acme"]


def test_removed_companies_are_dropped_and_the_file_stays_small():
    doc, _ = L.update([ACME], {}, acme_site().fetcher())
    doc2, changed = L.update([], doc, acme_site().fetcher())
    assert doc2["logos"] == {} and changed == ["acme"]


def test_merging_with_the_published_copy_never_loses_a_logo():
    ok = {"status": "ok", "web": {"mime": "image/png", "data": "x"}}
    miss = {"status": "none", "reason": "HTTP 403"}
    remote = {"version": 1, "logos": {"a": ok, "b": miss, "gone": ok}}
    local = {"version": 1, "logos": {"a": miss, "b": ok, "c": miss}}
    assert repo_sync.merge_logos(remote, local)["logos"] == {"a": ok, "b": ok, "c": miss}
    assert repo_sync.merge_logos(None, local)["logos"] == local["logos"]


def test_the_workflow_is_separate_from_the_job_scan():
    wf = (REPO / ".github" / "workflows" / "logos.yml").read_text("utf-8")
    assert "paths: [companies.json]" in wf and "workflow_dispatch:" in wf and "schedule:" in wf
    assert "python company_logos.py --publish" in wf and "group: company-logos" in wf
    assert "secrets." not in wf                                         # needs no secret
    scan = (REPO / ".github" / "workflows" / "check_jobs.yml").read_text("utf-8")
    assert "company_logos" not in scan and "logos" not in scan            # scans and emails never wait for it


def test_the_published_logos_file_is_valid():
    doc = json.loads((REPO / "logos.json").read_text("utf-8"))
    companies = {c["id"]: c for c in json.loads((REPO / "companies.json").read_text("utf-8"))}
    found = L.entries(doc)
    assert set(doc["logos"]) <= set(companies) and found
    assert len(json.dumps(doc)) < L.FILE_BUDGET
    for cid, e in found.items():
        raw = base64.b64decode(e["web"]["data"])
        assert len(raw) <= L.MAX_ICON_BYTES and L.sniff(raw)[0] == e["web"]["mime"]
        assert not L.is_ats(L._host(e["page"])) and not L.is_ats(L._host(e["web"]["src"]))
        assert L.name_slug(companies[cid]["name"]) in L._norm(e["domain"])   # the company's own domain
        assert L.email_image(e) is not None


# ── reading: the app ─────────────────────────────────────────────────────────

def _logo_doc(**by_id):
    return {"version": 1, "logos": {cid: {"status": "ok", "domain": f"{cid}.com", "page": f"https://www.{cid}.com/",
                                          "web": {"mime": "image/png", "data": base64.b64encode(png(64 + i, 64 + i)).decode(),
                                                  "w": 64, "h": 64, "src": "x", "kind": "icon"}, "email": "web"}
                                    for i, cid in enumerate(by_id)}}


@pytest.fixture
def with_logos(tmp_path):
    def put(*ids):
        doc = _logo_doc(**{i: True for i in ids})
        (tmp_path / "logos.json").write_text(json.dumps(doc))
        return doc
    return put


def _co(html):
    import re
    return re.findall(r'<div class="logo(?: sm| lg)? co-mark (co-l-[0-9a-f]+)" style="--tile:#ffffff"', html)


def test_found_logos_show_on_companies_and_monitoring(app, with_logos):
    from test_streamlit_app import _html, _nav
    doc = with_logos("pwc", "sanofi")
    at = app([])
    for page in ("companies", "monitoring"):
        _nav(at, page)
        html = _html(at)
        assert len(set(_co(html))) == 2                                    # PwC and Sanofi
        for e in doc["logos"].values():
            assert html.count(L.web_uri(e)) == 1                           # each image sent once per page
        assert f'<img src="{brand_logos.brand_uri("accenture")}" alt="">' in html     # the drawn mark still wins
        assert '" aria-hidden="true">NP</div>' in html                     # no logo: initials


def test_job_cards_and_details_use_the_found_logo(app, with_logos):
    from test_streamlit_app import NEW_RECORD, _html, _nav
    doc = with_logos("pwc")
    jobs = [{**NEW_RECORD, "company": "PwC"}, {**NEW_RECORD, "company": "PwC", "url": NEW_RECORD["url"] + "2", "id": "x2"}]
    at = app(jobs)
    _nav(at, "jobs")
    html = _html(at)
    assert len(_co(html)) == 2 and len(set(_co(html))) == 1 and html.count(L.web_uri(doc["logos"]["pwc"])) == 1


def test_broken_or_missing_logo_data_means_initials(app, tmp_path):
    from test_streamlit_app import _html, _nav
    (tmp_path / "logos.json").write_text('{"logos": {"pwc": {"status": "ok", "web": {"mime": "text/html", "data": 1}}}')
    at = app([])
    _nav(at, "companies")
    assert not at.exception and not _co(_html(at)) and '" aria-hidden="true">Pw</div>' in _html(at)
    (tmp_path / "logos.json").write_text("not json")
    at = app([])
    _nav(at, "companies")
    assert not at.exception and not _co(_html(at))


def test_the_app_never_fetches_logos(app, with_logos, monkeypatch):
    """Pages are drawn from logos.json only: discovery can't even start."""
    def boom(*a, **k):
        raise AssertionError("the app tried to look up a logo")
    monkeypatch.setattr(L.Fetcher, "__init__", boom)
    monkeypatch.setattr(L, "discover", boom)
    with_logos("pwc")
    from test_streamlit_app import _nav
    at = app([])
    for page in ("home", "jobs", "companies", "monitoring", "settings"):
        _nav(at, page)
        assert not at.exception


def test_an_employers_logo_never_replaces_the_viewers_avatar(app, google_env, with_logos, monkeypatch):
    from test_account_onboarding import CountingBackend, _ready
    from test_phase3_user_data import A_MAIL, A_UID, AccountIdentity, _sign_in, _use_store
    from test_streamlit_app import NEW_RECORD, _html, _nav
    from user_store import UserStore
    store = UserStore(CountingBackend(), AccountIdentity())
    _use_store(monkeypatch, store)
    _ready(store, A_UID, A_MAIL).set_avatar("google")
    with_logos("pwc")
    at = _sign_in(app, google_env, A_UID, A_MAIL, seen=({**NEW_RECORD, "company": "PwC"},))
    _nav(at, "jobs")
    html = _html(at)
    styles = "".join(e.proto.body for e in at.get("html") if e.proto.body.startswith("<style>"))
    assert _co(html)                                                     # PwC's card: PwC's logo
    assert f'background: url("{brand_logos.brand_uri("google")}")' in styles   # the header: the viewer's pick
    assert '<div class="acct-head"><span class="acct-av av-mark" style="background:#ffffff" aria-hidden="true">' \
           f'<img src="{brand_logos.brand_uri("google")}"' in html
    assert store.backend.get(f"users/{A_UID}")["avatar"] == "google"


# ── reading: the emails ──────────────────────────────────────────────────────

JOB = {"title": "Graduate Analyst", "company": "PwC", "location": "Pune, India", "url": "https://example.com/j/1",
       "category": "FRESHER"}


def _base(tmp_path, doc=None, text=None):
    (tmp_path / "companies.json").write_text((REPO / "companies.json").read_text("utf-8"))
    (tmp_path / "logos.json").write_text(text if text is not None else json.dumps(doc or {}))
    return tmp_path


def test_the_email_shows_the_employers_logo_beside_its_name(tmp_path):
    logos, tracked = notifier.email_logos([JOB, {**JOB, "company": "NPCI"}], _base(tmp_path, _logo_doc(pwc=True)))
    assert set(logos) == {"pwc"} and tracked[:2] == ["MetLife", "Avalara"]
    _, html, text = notifier.build_alert_email([JOB, {**JOB, "company": "NPCI"}], logos, tracked)
    cid = logos["pwc"][0]
    assert f'<img src="cid:{cid}" width="40" height="40" alt="PwC"' in html
    assert ">PwC</td>" in html and ">NPCI</td>" in html                    # the name is always there as text
    assert ">NP</td>" in html                                              # no logo: initials, not an image
    assert html.count("<img") == 1 and "data:" not in html and "http" not in html.split("cid:")[1][:40]
    assert "Company:  PwC" in text and "https://example.com/j/1" in html and "Graduate Analyst" in html


def test_an_email_without_logos_is_complete_and_has_no_images():
    _, html, _ = notifier.build_alert_email([JOB])
    assert "<img" not in html and ">PwC</td>" in html and ">Pw</td>" in html


def test_the_initials_colour_matches_the_app():
    tracked = [c["name"] for c in json.loads((REPO / "companies.json").read_text("utf-8"))]
    hue = brand_logos.tile_hue("PwC", tracked)
    assert notifier._hex(hue, .7, .94) in notifier._company_mark("PwC", {}, tracked)


def test_logos_are_attached_inline_to_the_message(tmp_path):
    logos, tracked = notifier.email_logos([JOB], _base(tmp_path, _logo_doc(pwc=True)))
    subject, html, text = notifier.build_alert_email([JOB], logos, tracked)
    cid, data, subtype = logos["pwc"]
    msg = message_from_string(notifier._message(subject, html, text, {cid: (data, subtype)}).as_string())
    assert msg.get_content_type() == "multipart/related"
    body, image = msg.get_payload()
    assert body.get_content_type() == "multipart/alternative"
    assert [p.get_content_type() for p in body.get_payload()] == ["text/plain", "text/html"]
    assert image.get_content_type() == "image/png" and image["Content-ID"] == f"<{cid}>"
    assert image.get_payload(decode=True) == data and "inline" in image["Content-Disposition"]


@pytest.mark.parametrize("text", ["not json", '{"logos": 5}', '{"logos": {"pwc": {"status": "ok", "web": 3}}}', None])
def test_bad_or_missing_logo_data_never_stops_an_alert(tmp_path, monkeypatch, text):
    base = _base(tmp_path, text=text or "")
    if text is None:
        (base / "logos.json").unlink()
        (base / "companies.json").unlink()
    monkeypatch.setattr(notifier, "BASE", base)
    sent = []
    monkeypatch.setattr(notifier, "_send", lambda to, s, h, t, images=None: sent.append((to, images, h)))
    notifier.send_alerts([JOB], "me@example.com")
    assert len(sent) == 1 and not sent[0][1] and ">Pw</td>" in sent[0][2]


def test_alerts_carry_the_logo_when_one_is_found(tmp_path, monkeypatch):
    monkeypatch.setattr(notifier, "BASE", _base(tmp_path, _logo_doc(pwc=True)))
    sent = []
    monkeypatch.setattr(notifier, "_send", lambda to, s, h, t, images=None: sent.append(images))
    notifier.send_alerts([JOB, {**JOB, "company": "Sanofi"}], "me@example.com")
    assert len(sent) == 1 and len(sent[0]) == 1 and list(sent[0].values())[0][1] == "png"
