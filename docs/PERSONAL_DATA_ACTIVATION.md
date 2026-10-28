# Personal data (Phase 3) — activation

Per-user data (own dismissals, Follow, My alerts, onboarding) and personal
alert emails are switched on by ONE credential. Without it the app and the
scanner behave exactly as before: no personal features, and the workflow's
"Send personal email alerts" step does nothing and exits 0.

## The one credential

| | |
|---|---|
| **Secret name** | `FIREBASE_SERVICE_ACCOUNT` |
| **What it is** | A Firebase **service-account key** for the Job Tracker Firebase project (the Admin SDK credential) |
| **Format** | The key file's JSON: one object with `"type": "service_account"`, `project_id`, `private_key_id`, `private_key`, `client_email`, `client_id`, `auth_uri`, `token_uri`, … |
| **Where it comes from** | Firebase Console → Project settings → Service accounts → Firebase Admin SDK → Generate new private key |
| **Who reads it** | Only the server: the Streamlit app and the workflow's personal-alerts step. It is never sent to a browser and never printed. |

The same key goes in two places, in two different wrappers:

1. **GitHub** — repository → Settings → Secrets and variables → Actions →
   repository secret `FIREBASE_SERVICE_ACCOUNT`. Value = **the raw JSON**, exactly
   as in the downloaded key file (no quotes or TOML around it).
2. **Streamlit** — app → Settings → Secrets. A **top-level** key, written
   **above the first `[section]` line** (TOML puts every line after `[auth]`
   inside `[auth]`, and a key there is not read), wrapped in a TOML
   multi-line literal string:

   ```toml
   FIREBASE_SERVICE_ACCOUNT = '''
   { …the whole JSON key file, pasted unchanged… }
   '''
   ```

   (A `[firebase_service_account]` section with the same fields also works.
   Not read: the key inside `[auth]` or any other section, an uppercase
   `[FIREBASE_SERVICE_ACCOUNT]` section, or any other key name.)

The key's `project_id` must equal the app's `FIREBASE_PROJECT_ID`; the app
refuses a key for another project. The workflow uses the key's own project.
Nothing else changes: Google sign-in, `[auth]`, `JT_ADMIN_EMAILS`,
`ALERT_RECIPIENT` and the Gmail secrets stay as they are (personal emails reuse
`GMAIL_ADDRESS` / `GMAIL_APP_PASSWORD`).

## When the app notices the secret

From the commit that fixed the service-account cache onwards, the app reads
the secret on every page load: adding, fixing or removing it takes effect at
the next page load, no reboot needed. (Only a working store is cached, per
credential.)

**Before that fix (main at 7567fda)** the app cached its first answer for
the life of the server process. If it had already decided "no service
account", adding the secret changed nothing until the app was rebooted
(Streamlit Cloud → Manage app → ⋮ → Reboot app) — and removing the secret did
not switch personal data off until a reboot either. Streamlit reloads
`st.secrets` when they change, but it does not clear `st.cache_resource`.

## Checking it (all read-only)

1. **Settings → Account → "Personal data"** (signed in as an admin):

   | Shown | Meaning |
   |---|---|
   | Saved to your own account | Key accepted, Firestore reachable, your `users/{uid}` profile exists |
   | Your personal data is unavailable right now. + a note | The note says why: a fixed configuration message (e.g. "belongs to a different project") or the error's category (e.g. `PermissionDenied`, `NotFound`) |
   | Not switched on — no Firebase service account is configured for this app | The app sees no secret (see placement above; at 7567fda also: reboot) |

   Members see only their own outcome ("Saved…" or the plain "unavailable"
   message) and nothing when personal data is off.
2. **The next scheduled Check Jobs run** — the "Send personal email alerts"
   step logs counts only: `sent: {}` (no profiles yet — the key and Firestore
   work), `sent: {"off": 1, "off_jobs": 0}` (one profile, alerts off), or on a
   Firestore problem a category line such as `listing users failed:
   PermissionDenied` followed by `personal alerts stopped: UserStoreError`.
   It never logs an address, UID or key, and it can't fail the job.

## Safe first activation

* Everyone starts with personal alerts **off**. A new profile has
  `notifications.enabled = false`, mode General, all tracked companies,
  onboarding pending.
* Turning "Email me new jobs" on records the moment it was turned on; only
  jobs **found after** that moment are ever sent — never the existing backlog.
* So the first scheduled runs send **nothing** until someone opts in, and then
  only new jobs, to that person's own verified sign-in email (read from
  Firebase by UID at send time — never `ALERT_RECIPIENT`, never a typed address).
* The shared alerts (`src/alerts.py`, `ALERT_RECIPIENT`) are independent and
  unchanged; the personal step runs after them and can't affect them.

Order: (1) Firestore database in production mode, (2) merge, (3) GitHub
secret, (4) Streamlit secret, (5) check Settings → Account as an admin.

To switch off: delete the Streamlit secret (personal features disappear at the
next page load — at 7567fda, after a reboot) and/or the GitHub secret
(personal emails stop). Shared alerts are unaffected. Personal data stays in
Firestore, keyed by Firebase UID.

## Firestore security rules

`config/firestore.rules` in this repository denies **all** direct client access —
including a signed-in user reading their own profile, and admins. Only the
server's Admin SDK (which rules don't apply to) reads and writes personal
data, and the app binds every read/write to the signed-in account's own UID.
This file is not deployed automatically: the rules actually in force are the
ones published in the Firebase Console (Firestore Database → Rules), which
should likewise deny client access. Publishing this file there is optional.

## Account policy

* **Disable** a Firebase account to block someone: refused at sign-in, an open
  session ends within 5 minutes, and they get no personal emails.
* **Delete** = reset: if they sign in again, Firebase gives them a new UID and
  they return as a fresh Member with empty personal data.

## A second admin (separate monitoring)

Admins are not equal (`src/access.py`). The **primary admin** owns the shared
monitoring: the `ALERT_RECIPIENT` email, the shared dismissals and alert
ledger in `data/seen_jobs.json`, which catalogue companies the shared alerts
cover, and the scan interval. **Every other admin** monitors through their
own account: the companies they follow, their own dismissals and delivery
ledger, and alerts to their own verified email (My alerts). They can add
companies — saved with `"shared_alerts": false`, so they never reach the
primary admin's email — and run checks, but they never see or change the
primary admin's recipient, dismissals, companies or interval. One scanner and
one job store serve both; each job's `company_id` keeps the scopes apart.

To add one, in the Streamlit secrets only (never a file in this repository):

1. First set `JT_PRIMARY_ADMIN_EMAIL` to the existing admin's address. Until
   it is set every admin is primary, as before the split — so never add a
   second admin without it. Set to an address that isn't in
   `JT_ADMIN_EMAILS`, nobody is primary (fail closed: the shared settings are
   locked for every admin, nothing is handed to another one); fix the value
   to unlock them.
2. Check the existing admin still sees the full alert recipient under
   Email & Notifications, then add the second address to `JT_ADMIN_EMAILS`
   (order doesn't matter).
3. The second admin signs in with that verified email, follows companies (or
   adds new ones) and turns on My alerts. Their emails go out at most every
   6 hours, like every personal alert.

No GitHub secret or workflow changes: the scanner already sends both kinds of
alert (`alerts.py` for the shared one, `user_alerts.py` for personal ones).
