"""Monitoring-failure emails: one warning when a company has ended THREE
consecutive scheduled full scans without a healthy scan, one recovery note
when it next scans healthy. Nov 2026.

* The count is scraper._blocked_streak ("blocked_scans" in each company's
  "scan" record): one per scheduled full scan (scope "all"), never one per
  HTTP retry or per targeted Workday-only run. A valid empty result is a
  healthy scan and never counts.
* An incident is opened once and remembered in data/monitoring_incidents.json
  (published by the Check Jobs publish job, the only writer, serialized by the
  check-jobs concurrency group), so the same incident is never re-sent on
  every scan. The ledger is updated only after the email went out; a failed
  send is retried by the next scan.
* Recipient: the primary admin's ALERT_RECIPIENT secret, as for the shared job
  alerts, and only for the companies in the shared alerts. Companies another
  admin keeps outside them ("shared_alerts": false) are that admin's own
  monitoring and are never reported to anyone else.
"""

from __future__ import annotations

from datetime import datetime, timezone
from html import escape

THRESHOLD = 3
LEDGER = "data/monitoring_incidents.json"
FAILING = ("failing", "broken", "needs_config")


def _source(company: dict) -> str:
    return str(company.get("source") or "")


def category(company: dict) -> str:
    """What kind of failure: rate-limited, inaccessible, invalid data or parse."""
    scan = company.get("scan") if isinstance(company.get("scan"), dict) else {}
    rec = scan.get("recovery") if isinstance(scan.get("recovery"), dict) else {}
    reason = (company.get("status_reason") or "").lower()
    if (rec.get("failure_category") in ("rate_limit", "cooldown", "cluster_gate", "canary_gate")
            or rec.get("http_status") == 429 or "429" in reason or "rate limit" in reason):
        return "rate-limited"
    if any(w in reason for w in ("parse", "unreadable", "not a sitemap", "could not read the", "unexpected format")):
        return "failing to parse"
    if any(w in reason for w in ("invalid", "validat", "malformed", "schema")):
        return "returning invalid data"
    if company.get("status") == "needs_config":
        return "needs configuration"
    return "inaccessible"


def _incident(company: dict, now: str) -> dict:
    scan = company.get("scan") or {}
    return {
        "name": company.get("name") or company.get("id"),
        "source": _source(company),
        "category": category(company),
        "error": (company.get("status_reason") or "")[:300],
        # the real start of the failure streak (any attempt), and when the scheduled-scan count began
        "first_failure": scan.get("failing_since") or scan.get("blocked_since") or "",
        "counted_since": scan.get("blocked_since") or "",
        "latest_failure": scan.get("last_failed_at") or company.get("last_checked") or "",
        "last_success": company.get("last_success") or "",
        "scans": scan.get("blocked_scans", 0),
        "opened_at": now,
    }


def evaluate(companies: list, ledger: dict, now: datetime | None = None) -> tuple[dict, list[dict], list[dict]]:
    """(new ledger, incidents to warn about, incidents now recovered).
    Pure: the caller sends and then publishes the ledger."""
    from config_store import in_shared_alerts
    now_iso = (now or datetime.now(timezone.utc)).isoformat()
    open_ = dict((ledger or {}).get("open") or {})
    warn, recovered = [], []
    for c in companies or []:
        if not isinstance(c, dict) or not c.get("id") or not in_shared_alerts(c):
            continue
        cid = str(c["id"])
        from config_store import monitoring_state
        if monitoring_state(c):
            # moved to manual monitoring (not scanned): its incident closes without a "recovered" email
            open_.pop(cid, None)
            continue
        scan = c.get("scan") if isinstance(c.get("scan"), dict) else {}
        blocked = scan.get("blocked_scans") if isinstance(scan.get("blocked_scans"), int) else 0
        if c.get("status") == "active":
            if cid in open_:
                recovered.append({**open_.pop(cid), "id": cid, "source": _source(c) or "",   # the source that recovered it
                                  "recovered_at": c.get("last_success") or now_iso})
            continue
        if c.get("status") in FAILING and blocked >= THRESHOLD and cid not in open_:
            inc = _incident(c, now_iso)
            open_[cid] = inc
            warn.append({**inc, "id": cid})
    # an incident whose company left the shared alerts or the catalogue is closed silently
    ids = {str(c.get("id")) for c in companies or [] if isinstance(c, dict) and in_shared_alerts(c)}
    for cid in [k for k in open_ if k not in ids]:
        open_.pop(cid)
    return {"open": open_}, warn, recovered


def _when(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return "never" if not iso else str(iso)
    return f"{dt:%d %b %Y %H:%M} UTC"


def build_email(warn: list[dict], recovered: list[dict]) -> tuple[str, str, str]:
    from notifier import _footer, _lead_card, _page, FONT, MUTED, TEXT
    parts_t, cards = [], []
    if warn:
        subject = (f"Monitoring warning: {warn[0]['name']} has failed {THRESHOLD}+ scans"
                   if len(warn) == 1 else f"Monitoring warning: {len(warn)} companies have failed {THRESHOLD}+ scans")
    else:
        subject = (f"Monitoring recovered: {recovered[0]['name']}" if len(recovered) == 1
                   else f"Monitoring recovered: {len(recovered)} companies")
    for title, items, recov in (("Monitoring failing", warn, False), ("Monitoring recovered", recovered, True)):
        if not items:
            continue
        rows = []
        for i in items:
            fields = ([("Source", i["source"]), ("Recovered", _when(i.get("recovered_at", ""))),
                       ("Was failing since", _when(i["first_failure"]))] if recov else
                      [("Source", i["source"]), ("Problem", i["category"]), ("Error", i["error"] or "—"),
                       ("First failure", _when(i["first_failure"])), ("Latest failure", _when(i["latest_failure"])),
                       ("Last successful scan", _when(i["last_success"])),
                       ("Consecutive scheduled scans", f'{i["scans"]} (counted since {_when(i.get("counted_since", ""))})'
                        if i.get("counted_since") else str(i["scans"]))])
            parts_t.append(f"{title}: {i['name']}\n" + "\n".join(f"  {k}: {v}" for k, v in fields))
            body = "".join(f'<div style="font:400 13px/1.6 {FONT};color:{MUTED};"><b style="color:{TEXT};">'
                           f'{escape(k)}:</b> {escape(str(v))}</div>' for k, v in fields)
            rows.append(f'<div style="margin:14px 0 0;"><div style="font:700 16px/1.4 {FONT};color:{TEXT};">'
                        f'{escape(str(i["name"]))}</div>{body}</div>')
        cards.append(_lead_card(f'<div style="font:700 18px/1.3 {FONT};color:{TEXT};">{escape(title)}</div>'
                                + "".join(rows)))
    note = (f"Sent once per incident, after {THRESHOLD} consecutive scheduled scans without a healthy scan, "
            "and once when the company scans healthy again.")
    html = _page(subject, subject, "".join(cards) + _footer([escape(note)]), tag="Monitoring")
    return subject, html, "\n\n".join(parts_t) + "\n\n" + note + "\n"


def run(companies: list, ledger: dict, recipient: str, send, now: datetime | None = None) -> dict | None:
    """Send what is due and return the ledger to publish (None: nothing changed,
    or the send failed and the next scan retries)."""
    new, warn, recovered = evaluate(companies, ledger, now)
    if not warn and not recovered:
        return new if new != {"open": dict((ledger or {}).get("open") or {})} else None
    if not recipient:
        return None
    subject, html, text = build_email(warn, recovered)
    try:
        send(recipient, subject, html, text)
    except Exception:
        return None
    return new
