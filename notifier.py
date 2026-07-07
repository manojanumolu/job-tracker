import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime


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


def test_mail(recipient: str = "") -> None:
    recipient = recipient.strip()
    if not recipient:
        from config_store import load_settings
        recipient = load_settings().get("recipient_email", "").strip()
    if not recipient:
        raise RuntimeError("No recipient email configured")
    subject = "Test email from Fresher Job Tracker"
    text = "This is a test email from your Job Tracker. If you received this, email delivery is working correctly."
    html = """<!DOCTYPE html>
<html><body style="font-family:sans-serif;max-width:520px;margin:0 auto;padding:24px">
<h2 style="color:#4f46e5">Fresher Job Tracker</h2>
<p>This is a test email from your Job Tracker.</p>
<p style="color:#6c6c76">If you received this, email delivery is working correctly.</p>
</body></html>"""
    _send(recipient, subject, html, text)
    print(f"[notifier] Test mail sent to {recipient}")


def send_alerts(jobs: list[dict], recipient: str) -> None:
    if not jobs or not recipient:
        return
    subject = f"[Job Alert] {len(jobs)} new fresher posting{'s' if len(jobs) > 1 else ''}"
    rows = ""
    for job in jobs:
        rows += f"""
        <tr>
          <td style="padding:12px 0;border-bottom:1px solid #eaeaec">
            <div style="font-weight:600;font-size:14px">{job.get('title','')}</div>
            <div style="color:#6c6c76;font-size:12px;margin-top:3px">{job.get('company','')} &middot; {job.get('date','')}</div>
          </td>
          <td style="padding:12px 0 12px 16px;border-bottom:1px solid #eaeaec;text-align:right;white-space:nowrap">
            <a href="{job.get('url','#')}" style="display:inline-block;padding:7px 14px;background:#4f46e5;color:#fff;border-radius:7px;text-decoration:none;font-size:13px;font-weight:600">Apply</a>
          </td>
        </tr>"""
    html = f"""<!DOCTYPE html>
<html><body style="font-family:sans-serif;max-width:560px;margin:0 auto;padding:24px;color:#17171a">
<h2 style="color:#4f46e5;margin-bottom:4px">Fresher Job Tracker</h2>
<p style="color:#6c6c76;margin-top:0">{len(jobs)} new entry-level posting{'s' if len(jobs)>1 else ''} found</p>
<table style="width:100%;border-collapse:collapse">{rows}</table>
<p style="margin-top:24px;font-size:12px;color:#9a9aa4">Fresher Job Tracker &middot; automated alert</p>
</body></html>"""
    plain = "\n".join(
        f"{j.get('title','')} @ {j.get('company','')} — {j.get('url','')}" for j in jobs
    )
    _send(recipient, subject, html, plain)
    print(f"[notifier] Alert sent to {recipient} with {len(jobs)} job(s)")


if __name__ == "__main__":
    test_mail()
