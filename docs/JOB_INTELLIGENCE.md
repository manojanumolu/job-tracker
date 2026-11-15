# Job intelligence (Milestone 5, Nov 2026)

`src/job_intel.py`. It is local and deterministic: no AI service, no network
and no paid API. It is optional, and it shows nothing until a person adds
their skills.

| Feature | Where | Kind |
|---|---|---|
| Skills a posting names, each with the line that names it | job page → Skills & match | **fact** (quoted) |
| Match % with the skills you have and lack | job page, once you saved skills | **estimate**, labelled |
| Freshness (posting date, or when the tracker first found it) | job page | **fact** with its source |
| Why it is listed (the safety checks it passed, the fresher quote) | job page | **fact** (stored evidence) |
| Skill gaps across the jobs you can see, with next steps | Settings → Skills & interview prep | counts are facts, next steps are guidance |
| Interview topics by skill, basic / intermediate | Settings → Skills & interview prep | curated list, not generated |

## Rules it follows

* **Never a gate.** Nothing here decides whether a job is shown, kept or
  emailed. The classifier, the safety gate and `job_filters` are unchanged,
  and none of them imports `job_intel`. The scraper records skills
  (`evidence.skills`) only after a job has passed every gate. A failure there
  leaves the field out; it never costs the job.
* **Facts are not estimates.** Every skill attributed to a posting is a
  quote. Stored quotes are checked again when read, not trusted. A match
  score needs at least two named skills, and otherwise reads "Not enough
  detail".
* **Résumés are never stored.** A `.txt` or `.docx` file, or pasted text, is
  read in memory to *suggest* skills. Only the skill names a person confirms
  are saved, to `users/{uid}.skills`: known names only, at most 60. Without
  sign-in they last for the browser session. PDF needs a library the app
  doesn't ship, so the page asks for pasted text instead.
* **Private per account.** Skills live in the person's own profile document.
  Deleting the account deletes them (`delete_personal_data` removes the
  profile).

## Known limits

* **Coverage of existing jobs is fixed.** The tracker keeps a posting's
  title and up to four requirement lines, not the whole description. Of the
  112 accepted jobs on 12 Nov 2026, those lines name a known skill for 34,
  and at least two skills (enough for a score) for 15.
* **Only newly discovered jobs get full-posting skills.** The scraper reads
  the full posting and stores `evidence.skills` only when it first records a
  job: it skips jobs that are already in the history
  (`scraper.merge_new_jobs`). So jobs recorded before this release never gain
  the field. Their skills, match scores and skill gaps stay limited to their
  title and requirement lines, permanently.
* **Full-posting coverage is not measured yet.** No stored data holds full
  postings. Measure it on the first weeks of newly discovered jobs before
  relying on match scores. Until then, skill gaps count mostly the few jobs
  whose stored lines name skills.
* The vocabulary is curated (about 70 skills with aliases) and English-only.
  A skill outside it is invisible here.
