"""Redact email addresses from publicly persisted employer posting text."""
import re


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")


def _redact_emails(value):
    """Quoted posting text kept as evidence never carries an email address into
    the public data files (an employer's "report fraud to x@..." line, Nov 2026)."""
    if isinstance(value, str):
        return _EMAIL_RE.sub("[email removed]", value)
    if isinstance(value, list):
        return [_redact_emails(v) for v in value]
    if isinstance(value, dict):
        return {k: _redact_emails(v) for k, v in value.items()}
    return value
