# Company monitoring and alert rules (Nov 2026)

## The catalogue lists only companies the scan reads automatically

Every company in `data/companies.json` must have a configured official
source (`scraper.COMPANY_SOURCES` / `scraper.sources_for`). That means an
employer's own job API, JSON or XML feed, or ATS endpoint that works with
no login, CAPTCHA, browser interaction, paid access or private credentials.
`tests/test_catalogue_watchlist.py::test_every_listed_company_is_monitored_automatically`
enforces this.

A company that can only be checked by hand is not listed. In Nov 2026 the
following were removed:

| Company | Why it was removed |
|---|---|
| Uber | Its jobs page has no readable job list, and no official feed was found. Its search endpoint is undocumented, so it isn't suitable for scheduled scans. |
| TCS | The careers site answers "Access Denied" to the scanner. Fresher hiring runs through a login-gated portal. |
| Zepto | The careers site serves a bot challenge, and no official feed exists. |

None of them was ever scanned or had a saved job, so no history went with
them. A person's stored watchlist is left as it is. The app counts and uses
only follows of companies still listed.

**ADP's feed-size check.** ADP's XML feed can be well-formed but empty or
partial. On 6 Nov 2026 it listed 1,482, then 0, then 1,493, then 788
postings. Each ADP scan is therefore compared with the feed's last good size,
kept in its `scan.feed` record:
- A reading below 75 % of that size, or an empty feed, is a failed scan
  ("feed size anomaly ..."). Nothing is read from it, the last good size and
  `last_success` are kept, and the next scan retries.
- The first scan with no recorded size needs at least 1,000 postings.
- A smaller, non-empty size that holds within 10 % for 3 scans in a row is
  accepted as the new normal.

ServiceNow's feed has no such check.

A temporary failure is not a reason to remove a company. HTTP 429 or 5xx
answers, timeouts and malformed answers mark the company "Monitoring blocked"
(never "0 jobs"), keep its last successful scan time, and are retried on
every scan. IBM's timeout on 2026-11-06 00:02 UTC recovered by itself at
03:02. Having no fresher vacancy today is not a reason to remove a company
either.

### Adding a company

Add a company only after checking all of the following:

1. **Hiring evidence.** The employer's own careers site shows India
   (ideally Hyderabad) graduate or entry-level roles. A third-party job
   board is not evidence.
2. **Source.** Identify the job data source: Workday (`*.myworkdayjobs.com`),
   Eightfold, Oracle Recruiting, Jibe/iCIMS, Avature, SuccessFactors, Zoho
   Recruit, an XML job feed, or a JSON API. `sources.read_source` lists the
   supported types.
3. **Automated access.** A plain HTTP request returns the job list, with no
   login, no challenge and no robots.txt disallow for that path.
4. **Fields.** Title, location, a description and an application URL can
   all be read.
5. **Fixture test.** A recorded answer is checked in `tests/` before the
   entry goes live. Then watch its first scheduled scan in Monitoring.

## The shared alert email

* It covers the companies in the shared alerts (Account 1), narrowed by
  `shared_alert_tailoring` in `data/settings.json` (Hyderabad; Software
  engineering, Data & analytics, Cloud & DevOps). Account 2's companies are
  outside it (`"shared_alerts": false`).
* **Backlog cutoff.** A job is emailed only if it was first found at or
  after `shared_alert_since`, and never if it was first found more than 7
  days ago (`config_store.shared_alert_cutoff`). **Whenever you widen the
  tailoring or the job families, set `shared_alert_since` to the current
  UTC time**, so that saved jobs the old rules held back aren't sent as new.
  The 7-day bound applies even if you forget. A retry after a failed send is
  exempt, so a mail outage never loses an alert.
* Personal alerts have their own cutoff: the time the person turned alerts
  on (`user_alerts.py`). They don't use the shared cutoff.
* Hardware, VLSI and embedded titles are not in the email's job families on
  purpose. The app shows them under "Explore beyond your preferences".

## Posting dates and "new"

`posted_at` (YYYY-MM-DD) is stored on a job only when its source states a
posting date. Today that covers Workday `startDate`, Jibe `posted_date`,
Amazon `posted_date`, and schema.org `datePosted` on Avature pages.
Relative text such as "Posted 30+ days ago" is never turned into a date.

The email says "New fresher job" only for jobs posted within 7 days of being
found. Otherwise it says "Fresher job found", and each job shows its posting
date (or "Not stated by the site") next to when the tracker found it. A
repost of a job already alerted (the same title and location under a new
ID) is never emailed.

## Scan diagnostics

Each company's `scan` record keeps `rejected`, `unknown` and `rejections`:
the most common kinds of reason a posting was not accepted, such as
"seniority in title", "explicit requirement" or "no fresher/entry-level
evidence". Use it to judge a company that never yields a job without reading
the run log. The run record keeps `reposts_suppressed`.
