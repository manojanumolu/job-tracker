# Application Tracker

Admins can record the jobs they apply to and follow each application through to an offer. The tracker is a page in the existing app, listed under **Jobs** in the sidebar.

## Who can use it

| Viewer | What they get |
|---|---|
| Admin (verified email in `JT_ADMIN_EMAILS`) | Their **own** applications, and nobody else's |
| Member (any other account) | No sidebar item and no "I've applied" button. Opening `?page=applications` lands on Home |
| No sign-in configured (legacy owner-password mode) | No tracker: records are kept by Firebase UID |

Two checks run on the server before **every** operation, whether it reads, writes, deletes, restores, purges or exports.

1. **Who.** `ApplicationBook` calls its `authorize` hook first. The app's hook (`_tracker_allowed`) re-checks admin rights against the sign-in source (`is_owner()`). It also checks that the session is still signed in as the UID the book is bound to. A refusal, or an error inside the hook, raises `NotAuthorized`. Every public method carries the check, and `tests/test_application_tracker.py` fails if one doesn't.
2. **Whose.** A book is bound to one UID when it is created. The app creates it only through `UserData.application_book`, from the account the sign-in gate resolved on the server. Every path it touches starts with `users/{that uid}/applications`.
   - No method takes a UID.
   - Application IDs are random hex values, checked against a strict pattern.

   So no value from the URL, a form or the browser can name another admin's data. Two admins never see each other's records.

Exports are built on the server from a fresh, authorized read. The file is kept only in that admin's own session.

## Data (Firestore, by Firebase UID)

```
users/{uid}/applications/{id}     one document per application
```

The timeline, the interview rounds and the follow-up reminder are stored inside the application's document. As a result:

- an application and its history change together in one compare-and-set write (`rev`);
- the list loads in one read.

Sizes are capped (300 timeline events, 30 interview rounds, field limits), far below Firestore's 1 MB limit. The full field list is in `src/applications.py`.

- **Migration:** none needed. This is a new subcollection, and existing documents are untouched.
- **Account deletion:** deleting a person's data (`delete_personal_data`) now also deletes their applications.
- **Security rules:** `config/firestore.rules` still denies all client access, which already covers the new path. There is nothing to deploy.

## Features

- **Two ways to add an application.** Both use the same checks (`clean_fields`) and create the same record.
  - **Paste job URL → Fetch Job Details** (`src/job_page.py`). This reads public pages only:
    - it refuses private, loopback and link-local addresses, and checks every redirect the same way;
    - the connection is pinned to the checked IP address;
    - it honours robots.txt;
    - it has a 15 s timeout and a 2 MB cap;
    - it reports sign-in walls, CAPTCHAs and bot checks instead of getting past them;
    - it fills in only what the page states (schema.org JobPosting first, then the page's own metadata), and anything missing stays empty.
  - **Enter manually.**
- **"I've applied" button** on job cards and on the job page (admins only). It opens the form filled in from that job.
- **Application progress and email response** are separate fields. A change to either is recorded as its own timeline event, with the date it happened and an optional note. Changing one never changes the other.
- **Timeline:** append-only history, oldest first.
- **Interview rounds:** as many as needed per application, each with date, time, round, type, place or link, interviewer, preparation notes, feedback and outcome.
- **Follow-up reminders:** shown in the app as Overdue, Due today, Upcoming and Completed. Only the Done button marks one done. Nothing is emailed.
- **Resume name/version:** a text field only. No file is uploaded.
- **Duplicate warning** before saving. A possible duplicate is one with:
  - the same job card;
  - the same link (ignoring tracking parameters and trailing slashes);
  - the same job ID at the same company; or
  - the same company and title, unless the job IDs or the locations differ.

  The matching record is shown for review. You can go back and edit, or save anyway. Nothing is merged, deleted or blocked.
- **Recently deleted:** applications are kept there for 30 days, then permanently deleted. Permanent deletion is also available earlier, after a confirmation. Deleted applications appear in no list, analytics view or export.
- **Analytics** (all time by default, with date ranges). The definitions are shown on the page. Each application is counted once, and an application with no response is never counted as rejected.
- **Exports** to Excel (.xlsx) and PDF. You can export everything, the current filtered view, selected applications, or a date range. Exports contain your own live applications only.

## Retention (30 days)

Expiry is decided from `deleted_at`, which is set by the server's clock. An expired record is never shown or restorable. It is permanently deleted:

- when its owner next opens the tracker;
- by the daily **Application Tracker retention** workflow (`.github/workflows/application_retention.yml`, `python src/applications.py --purge-expired`). This covers admins who don't come back.

The workflow has a read-only token, reads no website, pushes nothing and logs only a count.

## Deployment steps

1. Merge the branch. Streamlit Cloud redeploys automatically. No new Python dependencies and no new secrets are needed.
2. The retention workflow uses the existing `FIREBASE_SERVICE_ACCOUNT` GitHub secret. It starts running on schedule once the workflow file is on `main`. Run it once by hand (Actions → *Application Tracker retention* → Run workflow) to confirm it says `permanently deleted 0 expired application(s)`.
3. Nothing changes in Firebase: no rules, indexes or settings.

## Known limitations

- Some job sites can't be read automatically, and the tracker does not try to work around them. In these cases the job ID comes from the link where possible, and you fill in the rest:
  - Workday pages are built in the browser, and Workday's job API refuses anonymous requests;
  - sites behind a bot check (for example Avature);
  - LinkedIn, which disallows automated reading in its robots.txt and needs a sign-in.
- PDF exports use the standard PDF fonts (Windows-1252). Text in other scripts (for example Devanagari) prints as `?`. The Excel export keeps every character.
- The server checks every address it connects to, but connects only to addresses that resolve publicly. A site that is reachable only through a proxy can't be fetched.
- "Average response time" uses responses recorded as status changes (they carry the date they happened). A response already set when the application was saved has no known date, so it isn't included.
