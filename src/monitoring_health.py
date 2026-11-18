"""Pure, account-scope preserving portal health plus safe Actions summaries."""
from __future__ import annotations

import os
from collections import Counter
from datetime import datetime, timezone

from workday_state import epoch


def company_health(company, now=None):
    now = now or datetime.now(timezone.utc)
    scan = company.get("scan") if isinstance(company.get("scan"), dict) else {}
    rec = scan.get("recovery") if isinstance(scan.get("recovery"), dict) else {}
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
