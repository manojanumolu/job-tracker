import os
import re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape
from urllib.parse import urlparse

from access import mask_email


def _smtp_creds() -> tuple[str, str]:
    addr = os.environ.get("GMAIL_ADDRESS", "")
    pwd = os.environ.get("GMAIL_APP_PASSWORD", "")
    if not addr or not pwd:
        raise RuntimeError("GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be set in environment")
    return addr, pwd


def _send(to: str, subject: str, html: str, text: str) -> None:
    addr, pwd = _smtp_creds()
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"Fresher Job Tracker <{addr}>"
    msg["To"] = to
    msg.attach(MIMEText(text, "plain"))
    msg.attach(MIMEText(html, "html"))
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
BG = "#f6f6f7"
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
                <td style="padding:3px 0;width:92px;vertical-align:top;font:600 12px/1.5 {FONT};color:{FAINT};text-transform:uppercase;letter-spacing:.04em;">{escape(label)}</td>
                <td style="padding:3px 0;vertical-align:top;font:400 14px/1.5 {FONT};color:{TEXT};">{escape(value)}</td>
              </tr>"""


def _job_card(job: dict, index: int, total: int) -> str:
    title = _clip(job.get("title") or "Untitled posting", _MAX_TITLE)
    company = _clip(job.get("company") or "", 80)
    location = _clip(job.get("location") or "", 120)
    label, fg, bg = _CATEGORY_BADGES.get(job.get("category") or "", _DEFAULT_BADGE)
    url = safe_url(job.get("url", ""))

    rows = ""
    if company:
        rows += _field_row("Company", company)
    rows += _field_row("Location", location or "See posting")
    rows += _field_row("Why", friendly_reason(job))

    counter = (
        f'<span style="font:600 11px/1 {FONT};color:{FAINT};letter-spacing:.04em;">'
        f"JOB {index} OF {total}</span>&nbsp;&nbsp;"
        if total > 1 else ""
    )
    # "bulletproof" button: a bgcolor'd table cell works in Outlook as well
    # as Gmail/Apple Mail, and the whole cell is the link target
    button = (
        f"""
            <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin-top:16px;">
              <tr>
                <td bgcolor="{ACCENT}" style="border-radius:8px;">
                  <a href="{escape(url, quote=True)}" target="_blank" rel="noopener"
                     style="display:inline-block;padding:11px 22px;font:600 14px/1 {FONT};color:#ffffff;text-decoration:none;border-radius:8px;">View job&nbsp;&rarr;</a>
                </td>
              </tr>
            </table>"""
        if url else
        f'<p style="margin:16px 0 0;font:400 13px/1.5 {FONT};color:{MUTED};">Open the company’s career page to apply.</p>'
    )
    return f"""
        <tr>
          <td style="padding:0 0 16px;">
            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
                   style="background:#ffffff;border:1px solid {BORDER};border-radius:12px;">
              <tr>
                <td style="padding:20px 22px;">
                  <div style="margin:0 0 8px;">{counter}<span style="display:inline-block;padding:3px 9px;border-radius:999px;background:{bg};color:{fg};font:700 11px/1.4 {FONT};letter-spacing:.03em;text-transform:uppercase;">{escape(label)}</span></div>
                  <h2 style="margin:0 0 12px;font:700 18px/1.35 {FONT};color:{TEXT};word-break:break-word;">{escape(title)}</h2>
                  <table role="presentation" cellpadding="0" cellspacing="0" border="0" style="width:100%;">{rows}
                  </table>{button}
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
    url = safe_url(job.get("url", ""))
    lines.append(f"View job: {url}" if url else "Open the company's career page to apply.")
    return "\n".join(lines)


def build_alert_email(jobs: list[dict]) -> tuple[str, str, str]:
    """Return (subject, html, plain_text) for a job-alert email."""
    total = len(jobs)
    heading = "New fresher job" if total == 1 else f"{total} new fresher jobs"
    preheader = "; ".join(
        f"{_clip(j.get('title') or '', 60)}" + (f" at {j['company']}" if j.get("company") else "")
        for j in jobs[:3]
    )
    cards = "".join(_job_card(j, i, total) for i, j in enumerate(jobs, 1))
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="x-apple-disable-message-reformatting">
<title>{escape(heading)}</title>
</head>
<body style="margin:0;padding:0;background:{BG};">
<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:{BG};">{escape(preheader)}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{BG};">
  <tr>
    <td align="center" style="padding:24px 12px;">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;">
        <tr>
          <td style="padding:0 4px 18px;">
            <p style="margin:0;font:700 12px/1.4 {FONT};color:{ACCENT};letter-spacing:.06em;text-transform:uppercase;">🎯 Fresher Job Tracker</p>
            <h1 style="margin:4px 0 0;font:700 22px/1.3 {FONT};color:{TEXT};">{escape(heading)}</h1>
          </td>
        </tr>{cards}
        <tr>
          <td style="padding:4px 4px 0;font:400 12px/1.5 {FONT};color:{FAINT};">
            Fresher Job Tracker &middot; Automated alert. You get this because your address is set as the alert recipient.
          </td>
        </tr>
      </table>
    </td>
  </tr>
</table>
</body>
</html>"""
    divider = "\n\n" + "-" * 40 + "\n\n"
    text = (
        f"{heading.upper()}\n\n"
        + divider.join(_job_text(j, i, total) for i, j in enumerate(jobs, 1))
        + "\n\n--\nFresher Job Tracker · Automated alert\n"
    )
    return _subject(jobs), html, text


def build_test_email() -> tuple[str, str, str]:
    subject = "Test email from Fresher Job Tracker"
    text = (
        "This is a test email from your Job Tracker.\n"
        "If you received this, email delivery is working correctly.\n\n"
        "--\nFresher Job Tracker · Automated alert\n"
    )
    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:{BG};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:{BG};">
  <tr><td align="center" style="padding:24px 12px;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;background:#ffffff;border:1px solid {BORDER};border-radius:12px;">
      <tr><td style="padding:24px 22px;">
        <p style="margin:0;font:700 12px/1.4 {FONT};color:{ACCENT};letter-spacing:.06em;text-transform:uppercase;">🎯 Fresher Job Tracker</p>
        <h1 style="margin:6px 0 10px;font:700 20px/1.3 {FONT};color:{TEXT};">Test email</h1>
        <p style="margin:0;font:400 14px/1.6 {FONT};color:{MUTED};">This is a test email from your Job Tracker. If you received this, email delivery is working correctly.</p>
      </td></tr>
    </table>
  </td></tr>
</table>
</body></html>"""
    return subject, html, text


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
    print(f"[notifier] Test mail sent to {mask_email(recipient)}")


# keep pytest from collecting the sender above as a test when it imports this module
test_mail.__test__ = False


def send_alerts(jobs: list[dict], recipient: str) -> None:
    if not jobs or not recipient:
        return
    subject, html, text = build_alert_email(jobs)
    _send(recipient, subject, html, text)
    print(f"[notifier] Alert sent to {mask_email(recipient)} with {len(jobs)} job(s)")


if __name__ == "__main__":
    test_mail()
