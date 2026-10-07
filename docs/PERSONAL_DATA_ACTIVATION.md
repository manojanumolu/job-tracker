# Personal data (Phase 3) — activation

Per-user data and personal alert emails are built and tested, but **off** until
the owner activates them. Until then the app and the scanner behave exactly as
before: the app shows no personal features, and the workflow's
"Send personal email alerts" step does nothing and exits 0.

## The one credential

| | |
|---|---|
| **Secret name** | `FIREBASE_SERVICE_ACCOUNT` |
| **What it is** | A Firebase **service-account key** for the Job Tracker Firebase project (the Admin SDK credential) |
| **Format** | The key file's JSON, unchanged: one object with `"type": "service_account"`, `project_id`, `private_key_id`, `private_key`, `client_email`, `client_id`, `auth_uri`, `token_uri`, … |
| **Where it comes from** | Firebase Console → Project settings → Service accounts → Firebase Admin SDK → Generate new private key |
| **Who reads it** | Only the server: the Streamlit app and the workflow's personal-alerts step. It is never sent to a browser and never printed. |

The same value is used in two places:

1. **GitHub** — repository → Settings → Secrets and variables → Actions → new
   repository secret `FIREBASE_SERVICE_ACCOUNT`, value = the whole JSON.
2. **Streamlit** — app → Settings → Secrets, a **top-level** key (above any
   `[section]` line, or TOML makes it part of that section):

   ```toml
   FIREBASE_SERVICE_ACCOUNT = '''
   { …the whole JSON key file, pasted unchanged… }
   '''
   ```

   (A `[firebase_service_account]` section with the same fields also works.)

The key's `project_id` must be the app's `FIREBASE_PROJECT_ID`; the app refuses
a key for another project. Nothing else changes: Google sign-in, the `[auth]`
section, `JT_ADMIN_EMAILS`, `ALERT_RECIPIENT` and the Gmail secrets stay as
they are (personal emails reuse `GMAIL_ADDRESS` / `GMAIL_APP_PASSWORD`).

## Activation order

1. **Firestore database** — Firebase Console → Firestore Database → Create
   database, in **production mode** (all direct client access denied). Skip if
   one exists. Optionally publish `firestore.rules` from this repository (deny all).
2. **Merge** the Phase 3 branch. Still inert: no secret yet.
3. **GitHub secret** `FIREBASE_SERVICE_ACCOUNT`. From the next scan the
   personal step runs; with no subscribers yet it sends nothing.
4. **Streamlit secret** `FIREBASE_SERVICE_ACCOUNT`. Personal features appear:
   own dismissals, Follow, My alerts, onboarding.

To switch off again: delete the Streamlit secret (personal features disappear)
and/or the GitHub secret (personal emails stop). Shared alerts are unaffected
throughout. Personal data stays in Firestore, keyed by Firebase UID.

## Account policy

* **Disable** a Firebase account to block someone: refused at sign-in, an open
  session ends within 5 minutes, and they get no personal emails.
* **Delete** = reset: if they sign in again, Firebase gives them a new UID and
  they return as a fresh Member with empty personal data.
