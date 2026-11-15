"""Pure text formatting for the app's pages (moved out of streamlit_app.py,
Nov 2026 — Milestone 4): host names, "3h ago", India-time stamps, plurals,
icon spans. No Streamlit, no I/O; tests/test_ui_format.py covers them."""

from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse


def _host(url: str) -> str:
    try:
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return url


def _short_url(url: str, limit: int = 40) -> str:
    try:
        p = urlparse(url)
    except ValueError:
        return url
    s = p.netloc.replace("www.", "") + p.path.rstrip("/")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _parse_iso(iso: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(iso)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _ago(dt: datetime | None, now: datetime | None = None) -> str:
    if dt is None:
        return "never"
    diff = int(((now or datetime.now(timezone.utc)) - dt).total_seconds())
    if diff < 60:
        return "just now"
    if diff < 3600:
        return f"{diff // 60} min ago"
    if diff < 86400:
        return f"{diff // 3600}h ago"
    days = diff // 86400
    return "1 day ago" if days == 1 else f"{days} days ago"


def _found_at(date_str: str, now: datetime) -> datetime | None:
    """The scraper stamps jobs as "Jul 15, 13:45" (UTC, no year). Assume the
    most recent such date that isn't in the future."""
    try:
        dt = datetime.strptime(f"{now.year} {date_str}", "%Y %b %d, %H:%M").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return dt.replace(year=now.year - 1) if dt > now + timedelta(days=1) else dt


# The scan schedule is the admin's global scan interval (settings.json,
# scan_schedule.py): check_jobs.yml ticks hourly and scans only when a scan is
# due. Times are UTC on GitHub and shown in India time: Asia/Kolkata is a
# fixed +05:30 (India has no DST), so no tz database is needed.
IST = timezone(timedelta(hours=5, minutes=30), "IST")


def interval_label(hours: int) -> str:
    """'Every hour' / 'Every 3 hours'."""
    return "Every hour" if hours == 1 else f"Every {hours} hours"


def _ist(dt: datetime) -> str:
    """'11:30 PM IST'."""
    return dt.astimezone(IST).strftime("%I:%M %p").lstrip("0") + " IST"


def _ist_stamp(dt: datetime) -> str:
    """'Oct 05, 7:29 PM IST'."""
    return f"{dt.astimezone(IST):%b %d}, {_ist(dt)}"


def _ist_full(dt: datetime) -> str:
    """'13 Oct 2026, 11:08 AM IST'."""
    local = dt.astimezone(IST)
    return f"{local.day} {local:%b %Y}, {_ist(dt)}"


def _ms(name: str, size: str = "") -> str:
    return f'<span class="ms {size}" aria-hidden="true">{name}</span>'


def _plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"
