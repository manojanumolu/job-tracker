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
- A short feed is read once more, 30 seconds later, in the same scan. ADP's
  partial feed is intermittent.
- A smaller, non-empty size that holds within 10 % for 8 scans in a row (a
  day) is accepted as the new normal. This was 3: ADP's partial feed came
  back two scans in a row twice, so 3 would have accepted it.

**Slow pages.** A SuccessFactors results page (Deloitte) that times out is
asked for again, after 5 s and then 15 s. Before, one slow page out of ~75
failed the whole scan. Only timeouts are retried. An HTTP error, such as
JioHotstar's 403 from Workday, is never retried.

**JioHotstar.** Since 6 Nov 2026, JioStar's firewall has answered its
Workday search API (`/wday/cxs/…`) with 403 "Your request has been
blocked". JioHotstar is therefore read from what the site publishes for
crawlers: its `robots.txt` lists `/JioStar/siteMap.xml` and allows `/JioStar/`,
and each job page carries a schema.org `JobPosting`. The adapter is
`sources.workday_sitemap_listings`, and it never calls the blocked API. Jobs
keep the identity they had under the old API (`id:JR…`), so none is saved or
alerted twice. The sitemap listed 100 jobs on 9 Nov, against about 230 from
the old API, so coverage may be narrower.

**Failure streaks.** A failed scan records `failing_since` and `failed_scans`
in its scan record. From the second failure in a row, the status reason says
"failing since … (N scans in a row)". A failed scan keeps the company's
`last_job`.

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
date (or "Not stated by the site") next to when the tracker found it.

**Reposts (time-bound since Nov 2026).** A posting with the same company,
title and location as an earlier record but a new ATS ID is a repost and
is not emailed — but only while that title and location were *sighted*
within the last 45 days (`scraper.REPOST_WINDOW_DAYS`). A sighting is the
record's `first_seen`, or `repost_last_seen`: the UTC date a scan last found
the record's own posting still listed and still qualifying (stamped at most
once a day, and only moving forward when published). A posting left unread
for a scan (detail budget, a failing portal) simply isn't stamped that scan. A suppressed repost never stamps anything,
so the window always runs out. A key quiet for more than 45 days is a
vacancy that closed and came back: it is alerted as a new job and counted in
the run record's `reposts_window_realerted`. Records with no readable date
keep the old rule (suppressed).

What the window does *not* change: while a recorded posting is still
listed, a second requisition with the same title and location stays
suppressed. The run records from 6–11 Nov 2026 show `reposts_suppressed` at
a steady 36–39 every scan — the same open duplicate requisitions (for
example Cisco's several "Software Engineer" openings in Bengaluru) judged
again each scan, not 39 new reposts. They are not saved, so they don't
appear in the app either. Showing them in the app without emailing them is a
separate, data-changing decision.

## Scan diagnostics

Each company's `scan` record keeps `rejected`, `unknown` and `rejections`:
the most common kinds of reason a posting was not accepted, such as
"seniority in title", "explicit requirement" or "no fresher/entry-level
evidence". Use it to judge a company that never yields a job without reading
the run log. The run record keeps `reposts_suppressed` and
`reposts_window_realerted`.

## Measuring discovery quality

`tests/golden/` holds a labelled golden set (222 postings from production
records, production scan-log decisions and curated edge cases), checked on
every test run: precision, recall and the false-rejection rate are printed,
and any accepted reject, lost accept or category drift fails CI. See
`tests/golden/README.md` before changing the classifier.

## Discovery fixes (10 Nov 2026)

From the per-job decisions of the 09:02 UTC scan (14,257 rejections):

* **Microsoft.** Its full-time university hire ("Software Engineering",
  India, Multiple Locations) was rejected because "proven experience coding
  in languages including C, Java…" was read as a prior-employment
  requirement. Proven experience *coding / programming / writing code* is now
  a skill, not employment ("proven experience in sales" still rejects).
  Microsoft tags its university-hire positions with the "Microsoft Aspire"
  media block and states the employment type; a **full-time** position with
  that tag gets the line "Career site: early careers programme role". The
  marker is set only in Microsoft's config (`early_career_media`).
  Internships stay out: most are for current students (Summer 2027, PhD),
  not 2026 graduates. The other ~210 Microsoft India postings are level II
  or higher, or ask for years of experience.
* **S&P Global, Qualcomm, Accenture, Salesforce.** No wrong rejection was
  found. Their India postings ask for 1+ years, carry levels (II, III), or are
  senior. Accenture's 0-2 years bucket mostly says "1 to 3 years".
* **IBM.** IBM labels 1,855 of its 1,857 India postings "Professional" and
  none "Entry Level" (`field_keyword_18`), so 0 matches is correct. Its
  "Multiple Cities" postings come from the India-filtered search and are no
  longer rejected as "not in India".
* **Posting dates.** Eightfold sites (Microsoft, Qualcomm) now store the
  `postedTs` they state as `posted_at`.
* **India-wide locations.** A posting placed anywhere in India without a
  city ("Multiple Locations", "Multiple Cities", "PAN India") matches every
  city choice, so the Hyderabad-tailored shared email and personal
  tailoring no longer drop it. A posting that names a city keeps only its
  cities, and "Remote" is never implied.
* **Detail budget.** This is now 500 detail pages per company (it was 400).
  Hitachi needs ~450, and 46 of its postings were left for a later scan.
  A Workday posting repeated across result pages is read once.

Unchanged on purpose: years of experience (1+ still rejects), seniority and
level titles, "Associate", "Intern" or "Engineer I" alone, repost
suppression (same company, title and location), the cutoffs and recipients.

## Discovery fixes (11 Nov 2026)

* **Role-named "Role: …" titles.** A title shaped like "Two Words: …" was
  always read as a career story ("Priya Sharma: My first year"). Cisco and
  Qualcomm title real requisitions that way ("Software Engineer: Silicon
  One (C++)", "Sr Engineer: Embedded AI"), so all five such postings in the
  11 Nov 06:02 scan were discarded as stories. Each was senior or asked for
  years, so nothing was lost that day — but a graduate opening titled
  "Software Engineer: Graduate Hire" would have been. A heading that names a
  role or a grade is now judged as a job title by every other rule; a name
  ("Priya Sharma:") or any other heading ("Employee Spotlight:") is still a
  story. Measured on the golden set: precision unchanged at 1.000, recall
  0.983 -> 1.000.
* **Repost window.** Reposts are suppressed for 45 days after their last
  sighting instead of forever (see "Reposts" above).

Unchanged on purpose: every eligibility rule — years of experience,
seniority and level titles, "Associate", "Intern" or "Engineer I" alone —
and the cutoffs and recipients.
