"""Pure, account-scope preserving portal health plus safe Actions summaries."""
from __future__ import annotations

import os
from collections import Counter
from datetime import datetime, timezone

from workday_state import epoch


def current_recovery(company):
    """The company's M7 projection, or {} when it predates the company's latest
    real result. Every scan stamps last_checked before writing the projection,
    and a deferral writes a projection without touching last_checked, so a
    last_checked newer than the projection means a newer scan superseded it: a
    stale "failing" projection must never hide a real recovery."""
    scan = company.get("scan") if isinstance(company.get("scan"), dict) else {}
    rec = scan.get("recovery") if isinstance(scan.get("recovery"), dict) else {}
    if rec and epoch(company.get("last_checked")) > epoch(rec.get("observed_at")):
        return {}
    # run_all sets status before it writes a projection, and status changes only
    # on a real scan; so an unresolved failure on a company that is now active is
    # a leftover. needs_config is the one state that may sit on an active company.
    if (rec.get("unresolved_failure") and rec.get("outcome") != "needs_config"
            and company.get("status") not in ("failing", "broken", "needs_config")):
        return {}
    return rec


def company_health(company, now=None):
    now = now or datetime.now(timezone.utc)
    from config_store import monitoring_state
    if monitoring_state(company):
        # on manual monitoring (not scanned): never a failure, never an overdue scan
        return {"outcome": "manual", "failed": False, "overdue": False, "unavailable": False}
    rec = current_recovery(company)
    failed = company.get("status") in ("failing", "broken", "needs_config") or bool(rec.get("unresolved_failure"))
    last_success = rec.get("last_success") or company.get("last_success")
    success = epoch(last_success)
    outcome = rec.get("outcome")
    if not outcome:
        outcome = ("rate_limited" if failed and "HTTP 429" in company.get("status_reason", "") else
                   "failed" if failed else "healthy" if company.get("status") == "active" and company.get("last_checked") else "not_checked")
    overdue = not success or now.timestamp() - success >= 24 * 3600
    return {"outcome": outcome, "failed": failed or outcome in ("failed", "rate_limited", "needs_config"),
            "overdue": overdue, "unavailable": failed or overdue or outcome in ("not_checked", "needs_config") or bool(rec.get("pending"))}


def health(companies, now=None):
    rows = [company_health(c, now) for c in companies]
    n, failures = len(rows), sum(r["failed"] for r in rows)
    unavailable = sum(r["unavailable"] for r in rows)
    def severity(count):
        if not n:
            return "unknown"
        if count * 2 >= n:
            return "critical"
        if count * 4 >= n or count >= 5:
            return "degraded"
        return "warning" if count else "healthy"
    return {"total": n, "failures": failures, "unavailable": unavailable,
            "severity": severity(failures), "coverage": severity(unavailable),
            "counts": dict(Counter(r["outcome"] for r in rows)),
            "complete": bool(n) and not unavailable and all(r["outcome"] == "healthy" for r in rows)}


def report_actions(companies):
    result = health(companies)
    # Counts/enums only: no untrusted names, raw errors or response headers.
    line = (f"Monitoring: {result['severity']}; {result['failures']}/{result['total']} failing; "
            f"coverage {result['coverage']}; {result['unavailable']} unavailable/overdue. "
            f"Complete current coverage: {'yes' if result['complete'] else 'no'}. "
            "Scan finished; publication is a separate step.")
    print(line)
    if result["severity"] != "healthy" or result["coverage"] != "healthy":
        print("::warning title=Monitoring health::" + line)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as out:
            out.write("\n### Monitoring health\n\n" + line + "\n\n| Outcome | Companies |\n|---|---:|\n")
            for key, count in sorted(result["counts"].items()):
                out.write(f"| {key} | {count} |\n")


def report_workday(mode, requests, selected, not_due, throttled_hosts, cooling_hosts, clusters=""):
    """One safe line per run: what the Workday API rotation actually did (counts and
    data-centre names only)."""
    line = (f"Workday API: mode {mode}; {requests} request(s) sent; {selected} company(ies) selected, "
            f"{not_due} checked within the revisit interval; {throttled_hosts} host(s) throttled this run; "
            f"{cooling_hosts} host(s) cooling down." + (f" Data centres: {clusters}." if clusters else ""))
    print(line)
    if throttled_hosts:
        print("::warning title=Workday throttling::" + line)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as out:
            out.write("\n### Workday API\n\n" + line + "\n")
