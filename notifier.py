import colorsys
import hashlib
import json
import os
import re
import smtplib
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime, timedelta, timezone
from pathlib import Path
from html import escape
from urllib.parse import urlparse



def _smtp_creds() -> tuple[str, str]:
    addr = os.environ.get("GMAIL_ADDRESS", "")
    pwd = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not addr or not pwd:
        raise RuntimeError("GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be set in environment")
    return addr, pwd


def _message(subject: str, html: str, text: str, images: dict | None = None):
    """text + HTML; company logos (if any) ride along as inline images the
    HTML points at with cid: — no image is fetched when the mail is read."""
    body = MIMEMultipart("alternative")
    body.attach(MIMEText(text, "plain"))
    body.attach(MIMEText(html, "html"))
    if not images:
        msg = body
    else:
        msg = MIMEMultipart("related")
        msg.attach(body)
        for cid, (data, subtype) in images.items():
            part = MIMEImage(data, _subtype=subtype)
            part.add_header("Content-ID", f"<{cid}>")
            part.add_header("Content-Disposition", "inline", filename=f"{cid.split('@', 1)[0]}.{subtype}")
            msg.attach(part)
    msg["Subject"] = subject
    return msg


def _send(to: str, subject: str, html: str, text: str, images: dict | None = None) -> None:
    addr, pwd = _smtp_creds()
    msg = _message(subject, html, text, images)
    msg["From"] = f"Fresher Job Tracker <{addr}>"
    msg["To"] = to
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(addr, pwd)
        server.sendmail(addr, to, msg.as_string())


# ---------------------------------------------------------------------------
# Rendering (pure functions — no network, unit tested)
# ---------------------------------------------------------------------------

ACCENT = "#4f46e5"
TEXT = "#17171a"
MUTED = "#6c6c76"
FAINT = "#9a9aa4"
BORDER = "#eaeaec"
BG = "#f4f5f8"
PANEL = "#f7f8fb"             # the soft details panel inside a card
FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"

# category -> (label, text colour, background)
_CATEGORY_BADGES = {
    "FRESHER": ("Fresher", "#15803d", "#dcfce7"),
    "ENTRY_LEVEL": ("Entry level", "#1d4ed8", "#dbeafe"),
}
_DEFAULT_BADGE = ("Fresher / entry level", ACCENT, "#e0e7ff")

_MAX_TITLE = 140
_MAX_QUOTE = 60


def safe_url(url: str) -> str:
    """Only http(s) links are clickable; anything else (javascript:, empty,
    relative) yields "" so no button is rendered."""
    url = (url or "").strip()
    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    return url if parsed.scheme in ("http", "https") and parsed.netloc else ""


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def category_label(job: dict) -> str:
    return _CATEGORY_BADGES.get(job.get("category") or "", _DEFAULT_BADGE)[0]


# The classifier's reasons look like "fresher signal: 'Freshers welcome'",
# "experience starts at 0: '0-1 years'" or "entry-level signal: 'Graduate'".
# Turn them into short user-facing sentences; anything unrecognised falls
# back to a generic line for the category so no internal wording leaks out.
_REASON_RE = re.compile(r"^\s*(?P<kind>[a-z0-9 -]+?)\s*:\s*'(?P<quote>.*)'\s*$", re.IGNORECASE)
# "Experience: 0", "Exp: nil", "No experience required", "does not require prior experience"
_NO_EXPERIENCE_QUOTE_RE = re.compile(
    r"^(?:experience|exp)\b.*\b(?:0|zero|nil|none)\b|\bno\b.*\bexperience\b|\brequire\b.*\bexperience\b",
    re.IGNORECASE,
)
_RANGE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:-|to)\s*(\d+(?:\.\d+)?)", re.IGNORECASE)
# "Up to 2 years", "Maximum 6 months" — a 0..N range, not "no experience"
_UP_TO_QUOTE_RE = re.compile(
    r"^\s*(?:up\s*to|upto|maximum(?:\s+of)?|max\.?|not\s+more\s+than|less\s+than)\s*(\d+(?:\.\d+)?)\s*"
    r"(years?|yrs?|months?|mos?)\b", re.IGNORECASE)
_MONTHS_RE = re.compile(r"\bmo(?:nth)?s?\b", re.IGNORECASE)


def friendly_reason(job: dict) -> str:
    m = _REASON_RE.match(str(job.get("reason") or ""))
    if m:
        kind = m.group("kind").lower()
        quote = _clip(m.group("quote"), _MAX_QUOTE)
        if kind == "experience starts at 0":
            unit = "months" if _MONTHS_RE.search(quote) else "years"
            up_to = _UP_TO_QUOTE_RE.search(quote)
            if up_to:
                return f"Up to {up_to.group(1)} {unit} experience"
            rng = _RANGE_RE.search(quote)
            if rng:
                return f"{rng.group(1)}–{rng.group(2)} {unit} experience"
            return "No prior experience required"
        if kind == "fresher signal":
            if _NO_EXPERIENCE_QUOTE_RE.search(quote):
                return "No prior experience required"
            # a bare "Fresher(s)" can only come from the job title (description
            # signals always carry context like "Freshers welcome") — say
            # where it came from instead of echoing the same word
            if quote.strip().lower() in ("fresher", "freshers"):
                return "Open to freshers (stated in the job title)"
            return f"Open to freshers — “{quote}”"
        if kind == "entry-level signal":
            return f"Entry-level / graduate role — “{quote}”"
    if job.get("category") == "FRESHER":
        return "Open to freshers"
    return "Entry-level / graduate role"


def _subject(jobs: list[dict]) -> str:
    if len(jobs) == 1:
        j = jobs[0]
        company = _clip(j.get("company") or "", 40)
        title = _clip(j.get("title") or "New posting", 70)
        return f"🎯 New fresher job: {title}" + (f" at {company}" if company else "")
    companies = []
    for j in jobs:
        c = (j.get("company") or "").strip()
        if c and c not in companies:
            companies.append(c)
    who = ", ".join(companies[:3]) + (" and more" if len(companies) > 3 else "")
    return f"🎯 {len(jobs)} new fresher jobs" + (f" — {who}" if who else "")


def _field_row(label: str, value: str) -> str:
    return f"""
              <tr>
                <td style="padding:4px 12px 4px 0;width:86px;vertical-align:top;font:600 11px/1.7 {FONT};color:{FAINT};text-transform:uppercase;letter-spacing:.05em;">{escape(label)}</td>
                <td style="padding:4px 0;vertical-align:top;font:400 14px/1.5 {FONT};color:{TEXT};word-break:break-word;">{escape(value)}</td>
              </tr>"""


_IST = timezone(timedelta(hours=5, minutes=30))


def _ist_label(dt: datetime) -> str:
    """"14 Oct 2026, 3:10 PM IST" (no zero padding, on every platform)."""
    t = dt.astimezone(_IST)
    return f"{t.day} {t:%b %Y}, {t.hour % 12 or 12}:{t:%M} {'AM' if t.hour < 12 else 'PM'} IST"


def _found_label(job: dict) -> str:
    """When the tracker first found the job (its record's first_seen), in IST,
    or "" when the record doesn't say (older records): never a guess."""
    value = job.get("first_seen")
    if not isinstance(value, str) or not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return _ist_label(dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))


# ---------------------------------------------------------------------------
# Company identity: the logo found on the company's own site (logos.json),
# attached to the mail; otherwise its initials on the same tint as the app.
# Reading it can never stop a mail: any problem means "initials".
# ---------------------------------------------------------------------------

BASE = Path(__file__).parent


def email_logos(jobs: list[dict], base: Path | None = None) -> tuple[dict, list[str]]:
    """({company name (lower): (cid, bytes, subtype)} for these jobs'
    companies, the tracked companies' names in order). ({}, []) if the
    files are missing or unreadable."""
    base = base or BASE
    try:
        import company_logos
        companies = [c for c in json.loads((base / "companies.json").read_text("utf-8")) if isinstance(c, dict)]
        found = company_logos.entries(company_logos.load_local(base / company_logos.LOGOS_FILE))
        ids = {(c.get("name") or "").strip().lower(): c.get("id") for c in companies}
        out = {}
        for j in jobs:
            name = (j.get("company") or "").strip().lower()
            entry = found.get(ids.get(name)) if name and name not in out else None
            img = company_logos.email_image(entry) if entry else None
            if img:
                out[name] = (f"logo-{hashlib.sha1(name.encode('utf-8')).hexdigest()[:12]}@jobtracker", *img)
        return out, [c.get("name") or "" for c in companies]
    except Exception as e:                       # never let a logo stop an alert
        print(f"[notifier] company logos unavailable: {type(e).__name__}")
        return {}, []


def _hex(h: float, s: float, l: float) -> str:
    r, g, b = colorsys.hls_to_rgb(h / 360, l, s)
    return "#{:02x}{:02x}{:02x}".format(round(r * 255), round(g * 255), round(b * 255))


def _company_mark(company: str, logos: dict, tracked: list[str]) -> str:
    """A 40px logo cell: the attached logo (alt text = the company), or the
    company's initials on its tint — never a broken image."""
    found = logos.get(company.strip().lower())
    if found:
        return (f'<img src="cid:{found[0]}" width="40" height="40" alt="{escape(company, quote=True)}" '
                f'style="display:block;width:40px;height:40px;border:1px solid {BORDER};border-radius:10px;'
                f'background:#ffffff;font:600 10px/1.2 {FONT};color:{MUTED};">')
    from brand_logos import initials, tile_hue
    hue = tile_hue(company, tracked)
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>'
            f'<td width="40" height="40" align="center" valign="middle" bgcolor="{_hex(hue, .7, .94)}" '
            f'style="width:40px;height:40px;border-radius:10px;font:800 14px/40px {FONT};color:{_hex(hue, .7, .34)};'
            f'text-align:center;">{escape(initials(company) or "•")}</td></tr></table>')


def _job_card(job: dict, index: int, total: int, logos: dict | None = None, tracked: list[str] | None = None) -> str:
    """One job as a card: the employer (logo or initials, name) with the
    category badge, the title, a details panel (location, why it qualifies,
    when it was found) and the "View job" button with the site it opens."""
    title = _clip(job.get("title") or "Untitled posting", _MAX_TITLE)
    company = _clip(job.get("company") or "", 80)
    location = _clip(job.get("location") or "", 120)
    label, fg, bg = _CATEGORY_BADGES.get(job.get("category") or "", _DEFAULT_BADGE)
    url = safe_url(job.get("url", ""))
    found = _found_label(job)

    badge = (f'<span style="display:inline-block;padding:4px 10px;border-radius:999px;background:{bg};color:{fg};'
             f'font:700 11px/1.3 {FONT};letter-spacing:.04em;text-transform:uppercase;white-space:nowrap;">{escape(label)}</span>')
    # the employer, with the badge on the same line (or the badge alone)
    if company:
        head = f"""
                  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                    <tr>
                      <td width="40" style="width:40px;padding:0 12px 0 0;vertical-align:middle;">{_company_mark(company, logos or {}, tracked or [])}</td>
                      <td style="vertical-align:middle;font:700 15px/1.35 {FONT};color:{TEXT};word-break:break-word;">{escape(company)}</td>
                      <td align="right" style="vertical-align:middle;padding:0 0 0 10px;">{badge}</td>
                    </tr>
                  </table>"""
    else:
        head = f'<div>{badge}</div>'
    counter = (f'<p style="margin:16px 0 4px;font:600 11px/1.4 {FONT};color:{FAINT};letter-spacing:.06em;">'
               f'JOB {index} OF {total}</p>' if total > 1 else "")
    rows = _field_row("Location", location or "See posting")
    rows += _field_row("Eligibility", friendly_reason(job))
    if found:
        rows += _field_row("Found", found)
    # "bulletproof" button: a bgcolor'd table cell works in Outlook as well
    # as Gmail/Apple Mail, and the whole cell is the link target
    if url:
        host = (urlparse(url).hostname or "").removeprefix("www.")
        action = f"""
                  <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin-top:18px;">
                    <tr>
                      <td bgcolor="{ACCENT}" style="border-radius:8px;">
                        <a href="{escape(url, quote=True)}" target="_blank" rel="noopener"
                           style="display:inline-block;padding:11px 20px;font:600 14px/1 {FONT};color:#ffffff;text-decoration:none;border-radius:8px;">View job&nbsp;&rarr;</a>
                      </td>
                      <td style="padding:0 0 0 12px;font:400 12px/1.4 {FONT};color:{FAINT};word-break:break-all;">{escape(_clip(host, 60))}</td>
                    </tr>
                  </table>"""
    else:
        action = f'<p style="margin:18px 0 0;font:400 13px/1.5 {FONT};color:{MUTED};">Open the company’s career page to apply.</p>'
    return f"""
        <tr>
          <td style="padding:0 0 14px;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
                   style="background:#ffffff;border:1px solid {BORDER};border-radius:14px;">
              <tr>
                <td class="jt-pad" style="padding:20px 22px 22px;">{head}{counter}
                  <h2 style="margin:{'0' if counter else '14px'} 0 0;font:700 18px/1.4 {FONT};color:{TEXT};word-break:break-word;overflow-wrap:anywhere;">{escape(title)}</h2>
                  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
                         style="margin-top:14px;background:{PANEL};border-radius:10px;">
                    <tr><td style="padding:10px 14px;">
                      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{rows}
                      </table>
                    </td></tr>
                  </table>{action}
                </td>
              </tr>
            </table>
          </td>
        </tr>"""


def _job_text(job: dict, index: int, total: int) -> str:
    lines = []
    if total > 1:
        lines.append(f"JOB {index} OF {total}")
    lines.append(_clip(job.get("title") or "Untitled posting", _MAX_TITLE))
    lines.append(f"Category: {category_label(job)}")
    if job.get("company"):
        lines.append(f"Company:  {_clip(job['company'], 80)}")
    lines.append(f"Location: {_clip(job.get('location') or '', 120) or 'See posting'}")
    lines.append(f"Why:      {friendly_reason(job)}")
    found = _found_label(job)
    if found:
        lines.append(f"Found:    {found}")
    url = safe_url(job.get("url", ""))
    lines.append(f"View job: {url}" if url else "Open the company's career page to apply.")
    return "\n".join(lines)


def _page(title: str, preheader: str, body: str, tag: str = "") -> str:
    """The shared frame of every email: a light page, a 600 px column (fluid
    below that), the brand row on top (with the email's kind on the right).
    Inline styles throughout; the one <style> block only tightens padding on
    small screens."""
    tag_cell = (f'<td align="right" style="vertical-align:middle;font:700 10px/1.4 {FONT};color:{FAINT};'
                f'letter-spacing:.12em;text-transform:uppercase;white-space:nowrap;">{escape(tag)}</td>' if tag else "")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="x-apple-disable-message-reformatting">
<title>{escape(title)}</title>
<style>
@media only screen and (max-width: 480px) {{
  .jt-pad {{ padding: 18px 16px !important; }}
  .jt-outer {{ padding: 16px 8px !important; }}
  .jt-h1 {{ font-size: 21px !important; }}
}}
</style>
</head>
<body style="margin:0;padding:0;background:{BG};">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:{BG};">{escape(preheader)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{BG};">
  <tr>
    <td class="jt-outer" align="center" style="padding:28px 12px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;">
        <tr>
          <td style="padding:0 4px 16px;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
              <tr>
                <td width="30" height="30" align="center" valign="middle" bgcolor="{ACCENT}"
                    style="width:30px;height:30px;border-radius:9px;font:800 12px/30px {FONT};color:#ffffff;text-align:center;letter-spacing:.02em;">FJ</td>
                <td style="padding:0 0 0 10px;vertical-align:middle;font:700 15px/1.2 {FONT};color:{TEXT};">Fresher Job Tracker</td>
                {tag_cell}
              </tr>
            </table>
          </td>
        </tr>{body}
      </table>
    </td>
  </tr>
</table>
</body>
</html>"""


def _lead_card(inner: str) -> str:
    """The first card of an email: white, a slim indigo-to-teal line on top
    (a solid indigo where gradients don't render), then its content."""
    return f"""
        <tr>
          <td style="padding:0 0 16px;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
                   style="background:#ffffff;border:1px solid {BORDER};border-radius:16px;overflow:hidden;">
              <tr><td height="4" bgcolor="{ACCENT}" style="height:4px;line-height:4px;font-size:0;background:{ACCENT};background-image:linear-gradient(90deg,{ACCENT},#0d9488);">&nbsp;</td></tr>
              <tr>
                <td class="jt-pad" style="padding:24px 26px 24px;">{inner}
                </td>
              </tr>
            </table>
          </td>
        </tr>"""


def _footer(lines: list[str]) -> str:
    rows = "".join(f'<span style="display:block;margin-top:4px;">{line}</span>' for line in lines[1:])
    return f"""
        <tr>
          <td style="padding:6px 4px 0;font:400 12px/1.6 {FONT};color:{FAINT};">
            {lines[0]}{rows}
          </td>
        </tr>"""


def build_alert_email(jobs: list[dict], logos: dict | None = None,
                      tracked: list[str] | None = None) -> tuple[str, str, str]:
    """Return (subject, html, plain_text) for a job-alert email. ``logos``:
    {company name (lower): (cid, bytes, subtype)} from email_logos(); the
    caller attaches those images. Without it every company shows initials."""
    total = len(jobs)
    heading = "New fresher job" if total == 1 else f"{total} new fresher jobs"
    preheader = "; ".join(
        f"{_clip(j.get('title') or '', 60)}" + (f" at {j['company']}" if j.get("company") else "")
        for j in jobs[:3]
    )
    companies = []
    for j in jobs:
        c = _clip(j.get("company") or "", 40)
        if c and c not in companies:
            companies.append(c)
    shown = companies[:4] + ([f"{len(companies) - 4} more"] if len(companies) > 4 else [])
    source = (("From " + (", ".join(shown[:-1]) + " and " + shown[-1] if len(shown) > 1 else shown[0]) + ". ")
              if shown else "")
    # what kinds of role these are, from the jobs' own categories
    kinds = {}
    for j in jobs:
        label, fg, bg = _CATEGORY_BADGES.get(j.get("category") or "", _DEFAULT_BADGE)
        n, _, _ = kinds.get(label, (0, fg, bg))
        kinds[label] = (n + 1, fg, bg)
    chips = "".join(
        f'<span style="display:inline-block;margin:0 6px 6px 0;padding:4px 10px;border-radius:999px;background:{bg};color:{fg};'
        f'font:700 11px/1.4 {FONT};letter-spacing:.04em;text-transform:uppercase;white-space:nowrap;">{n} {escape(label)}</span>'
        for label, (n, fg, bg) in kinds.items())
    lead = _lead_card(f"""
                  <p style="margin:0;font:700 11px/1.4 {FONT};color:{ACCENT};letter-spacing:.1em;text-transform:uppercase;">New job alert</p>
                  <h1 class="jt-h1" style="margin:6px 0 8px;font:800 26px/1.25 {FONT};color:{TEXT};letter-spacing:-.01em;">{escape(heading)}</h1>
                  <p style="margin:0 0 14px;font:400 14px/1.6 {FONT};color:{MUTED};">{escape(source)}Fresher and entry-level roles in India, each checked against its own posting before it reached you.</p>
                  <div style="font-size:0;line-height:0;">{chips}</div>""")
    cards = "".join(_job_card(j, i, total, logos, tracked) for i, j in enumerate(jobs, 1))
    footer = _footer(["You’re receiving this because job alerts are turned on for this address. Each job is emailed once, "
                      "and dismissed jobs are never sent.", "Fresher Job Tracker &middot; automated alert"])
    divider = "\n\n" + "-" * 40 + "\n\n"
    text = (
        f"{heading.upper()}\n"
        + (f"{source.strip()}\n" if source else "")
        + "\n"
        + divider.join(_job_text(j, i, total) for i, j in enumerate(jobs, 1))
        + "\n\n--\nYou're receiving this because job alerts are turned on for this address.\n"
        + "Each job is emailed once, and dismissed jobs are never sent.\nFresher Job Tracker · automated alert\n"
    )
    return _subject(jobs), _page(heading, preheader, lead + cards + footer, "Job alert"), text


# what a delivery test can truthfully promise: how real alerts behave
_TEST_PROMISES = (
    ("Fresher and entry-level roles in India", "Each one read from the company’s own posting before it is sent."),
    ("One email per batch of new jobs", "Every job reaches you once — jobs you dismiss are never sent."),
    ("Straight to the posting", "Every alert links to the job on the company’s own careers site."),
)


def build_test_email(now: datetime | None = None) -> tuple[str, str, str]:
    """The test email: a delivery check, nothing more — no jobs, no scan, no
    links. It confirms delivery and says what real alerts will look like."""
    subject = "Test email from Fresher Job Tracker"
    sent = _ist_label(now or datetime.now(timezone.utc))
    steps = "".join(f"""
                    <tr>
                      <td width="30" valign="top" style="width:30px;padding:2px 12px 14px 0;">
                        <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
                          <td width="26" height="26" align="center" valign="middle" bgcolor="#eef0ff"
                              style="width:26px;height:26px;border-radius:999px;font:700 12px/26px {FONT};color:{ACCENT};text-align:center;">{i}</td>
                        </tr></table>
                      </td>
                      <td valign="top" style="padding:0 0 14px;">
                        <p style="margin:0;font:700 14px/1.45 {FONT};color:{TEXT};">{escape(title)}</p>
                        <p style="margin:2px 0 0;font:400 13px/1.55 {FONT};color:{MUTED};">{escape(note)}</p>
                      </td>
                    </tr>""" for i, (title, note) in enumerate(_TEST_PROMISES, 1))
    lead = _lead_card(f"""
                  <table role="presentation" cellpadding="0" cellspacing="0" border="0"><tr>
                    <td style="padding:5px 12px 5px 10px;border-radius:999px;background:#ecfdf3;border:1px solid #bbf7d0;font:700 11px/1.3 {FONT};color:#15803d;letter-spacing:.06em;text-transform:uppercase;white-space:nowrap;">
                      <span style="display:inline-block;width:7px;height:7px;border-radius:999px;background:#16a34a;margin-right:7px;vertical-align:1px;"></span>Delivery test passed</td>
                  </tr></table>
                  <h1 class="jt-h1" style="margin:16px 0 8px;font:800 26px/1.25 {FONT};color:{TEXT};letter-spacing:-.01em;">Your job alerts can reach you</h1>
                  <p style="margin:0;font:400 15px/1.65 {FONT};color:{MUTED};">This is a test email from your Job Tracker. If you received this, email delivery is working correctly — no jobs were scanned or sent.</p>
                  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin:22px 0 0;border-top:1px solid {BORDER};">
                    <tr><td style="padding:18px 0 6px;font:700 11px/1.4 {FONT};color:{FAINT};letter-spacing:.1em;text-transform:uppercase;">What real alerts bring you</td></tr>
                  </table>
                  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{steps}
                  </table>""")
    footer = _footer([f"Test sent {escape(sent)} from the Email &amp; Notifications page.",
                      "Only a test: no jobs were scanned and no other email was sent.", "Fresher Job Tracker"])
    text = (
        "DELIVERY TEST PASSED\n\n"
        "Your job alerts can reach you.\n"
        "This is a test email from your Job Tracker. If you received this, email delivery is working correctly\n"
        "— no jobs were scanned or sent.\n\n"
        "What real alerts bring you:\n"
        + "".join(f"{i}. {t} — {n}\n" for i, (t, n) in enumerate(_TEST_PROMISES, 1))
        + f"\n--\nTest sent {sent} from the Email & Notifications page.\n"
        "Only a test: no jobs were scanned and no other email was sent.\nFresher Job Tracker\n"
    )
    return subject, _page("Delivery test passed", "Email delivery is working — your job alerts can reach you.", lead + footer,
                          "Delivery test"), text


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def test_mail(recipient: str = "") -> None:
    recipient = recipient.strip()
    if not recipient:
        from access import alert_recipient
        from config_store import load_settings
        recipient, _ = alert_recipient(load_settings())
    if not recipient:
        raise RuntimeError("No recipient email configured")
    subject, html, text = build_test_email()
    _send(recipient, subject, html, text)
    print("[notifier] Test mail sent to the configured recipient")


# keep pytest from collecting the sender above as a test when it imports this module
test_mail.__test__ = False


def send_alerts(jobs: list[dict], recipient: str) -> None:
    if not jobs or not recipient:
        return
    logos, tracked = email_logos(jobs)
    subject, html, text = build_alert_email(jobs, logos, tracked)
    images = {cid: (data, subtype) for cid, data, subtype in logos.values() if f"cid:{cid}" in html}
    _send(recipient, subject, html, text, images)
    print(f"[notifier] Alert sent to the configured recipient with {len(jobs)} job(s)"
          + (f", {len(images)} company logo(s)" if images else ""))


if __name__ == "__main__":
    test_mail()
