# GitHub and workflow security (Milestone 3, Nov 2026)

## The privilege chain this closes

Before this change, one token could lead to every secret:

1. The Streamlit app holds a GitHub token (`GITHUB_TOKEN` in its Streamlit
   secrets) with **Contents: write** on the repository, because it saves
   `data/*.json` (dismissals, companies, settings) through the GitHub API.
2. The scan workflow commits `data/*.json` to `main` with its own token.
3. Both write to **`main`**, which is also where the code lives. Contents:
   write on a branch means *any file* on it, `src/*.py` included.
4. Every scheduled run executes `main`'s code with the Gmail app password,
   the alert recipient and the Firebase service-account key.

So a leaked app token, or a bug that lets someone steer the app's writes,
could change `src/alerts.py` on `main`, and the next scheduled scan would run
that code with every secret. Two more paths reached the secrets:

* **The publish job ran code it had just reset to.** `repo_sync.publish`
  hard-resets its checkout to the remote branch tip. The publish job then
  ran `python src/user_alerts.py` from that reset tree — code that might have
  been pushed seconds earlier, not the code the run started with.
* **`workflow_dispatch` on any ref.** Anyone who can dispatch workflows
  (Actions: write) can run Check Jobs on any branch. The job definition comes
  from that branch, and so do repository secrets. A branch someone could
  push to was therefore a way to the secrets.

## What changed in the repository (this branch)

| Change | Effect | Default behaviour |
|---|---|---|
| `src/data_location.py`: `JT_DATA_BRANCH`, `JT_DATA_ROOT` | the data can live on its own branch, and in its own checkout | unset: data on the default branch, in this checkout, exactly as before |
| App reads/writes `data/*.json` with `ref=`/`branch=` set to `JT_DATA_BRANCH` | the app's token never needs to write where code lives | unset: the default branch |
| Every workflow checks out **code** (the commit the run started from, no stored credentials) and **data** (`./jt-data`, the data branch tip) separately | data files are read as JSON and never imported or executed. A push happens only in `jt-data`, so the hard reset can't swap the running code | the data branch is the run's own branch when the variable is unset |
| `environment: production` on every job that reads a secret (`publish`, `personal-alerts` in Check Jobs; `purge` in Application retention) | the environment's branch rules decide which refs get secrets | the first run creates the environment with no rules: repository secrets still work |
| `repo_sync.publish` refuses a malformed branch name; a malformed `JT_DATA_BRANCH` stops the app's GitHub writes instead of falling back to `main` | a typo can't redirect writes to the code branch | — |
| `tests/test_github_privilege_boundaries.py` | locks the boundaries in CI | — |

Unchanged: the schedule, the scan, the scrape job's read-only token and lack
of secrets, the artifact hand-over (`scan_handoff.py`), the email and
Firestore steps, every permission block, and action pins.

## Manual GitHub configuration (needs the repository owner)

None of this has been done. Each step changes production configuration, so
each needs your approval. **Do them in this order**: steps 1–2 need no code
change, and step 3 is what makes the data branch take effect.

### 1. Restrict which branches can use secrets (recommended first)

*Settings → Environments → `production`* (it appears after the first
Check Jobs run of this code; or create it with **New environment**):

1. **Deployment branches and tags → Selected branches and tags → Add rule**:
   `main`.
2. Do **not** add required reviewers or a wait timer: scans run unattended
   and would stall.
3. **Environment secrets → Add secret**: `GMAIL_ADDRESS`,
   `GMAIL_APP_PASSWORD`, `ALERT_RECIPIENT`, `FIREBASE_SERVICE_ACCOUNT`, with
   the same values as the repository secrets. (GitHub never shows a secret's
   value; paste it again from your password manager or the Firebase console.)
4. Run **Check Jobs → Run workflow** (main) once, and check that the
   publish step logs `delivery: this run …` and the personal step logs its
   counts. (This sends real alerts for any pending jobs, as every scan does.)
5. Then delete the four **repository** secrets (*Settings → Secrets and
   variables → Actions → Repository secrets*). Until you do, a dispatch on
   another branch can still read them: jobs outside the environment get
   repository secrets.

### 2. Protect `main`

*Settings → Rules → Rulesets → New branch ruleset*:

* Name `protect-main`, **Enforcement: Active**, target **Default branch**.
* Rules: **Restrict deletions**, **Block force pushes**, **Require a pull
  request before merging** (0 approvals is fine for a single maintainer),
  **Require status checks to pass**: `test`.
* **Bypass list:** leave it empty if you can. Bypass is checked against the
  actor, and a personal access token acts as you. So if you bypass, so does
  any token of yours, the app's included.

While the data is still on `main` (step 3 not done), this ruleset **blocks
the scans' and the app's data commits**. Either do step 3 first, or add
**Repository admin** to the bypass list until step 3 is done. That bypass is
weaker, because the app's token acts as the admin; remove it after step 3.

### 3. Move the data to its own branch (a production migration)

1. Create the branch from the current `main` **between scans**: *Code →
   branch menu → View all branches → New branch* named `data`, source `main`.
2. *Settings → Secrets and variables → Actions → Variables → New repository
   variable*: `JT_DATA_BRANCH` = `data`.
3. In the Streamlit app's secrets (top level): `JT_DATA_BRANCH = "data"`,
   then reboot the app.
4. Watch the next scan. Publish should push to `data`, and the app should
   show the new jobs. `main` keeps a frozen copy of `data/*.json`, which the
   app uses only as an offline fallback.
5. Optional: a second ruleset on `data` with **Block force pushes** and
   **Restrict deletions** (no pull-request rule: the scans push to it).

Rollback: delete the variable and the Streamlit secret. Everything then
reads and writes `main` again. First copy `data/*.json` from `data` back to
`main` so no scan result is lost.

### 4. Narrow the app's token

Replace the app's token with a **fine-grained personal access token**
(*Settings → Developer settings → Fine-grained tokens*):

* Repository access: only `manojanumolu/job-tracker`.
* Permissions: **Contents: Read and write** (data saves), **Actions: Read and
  write** (the app's "Run check" button and run status). Nothing else. In
  particular, no **Workflows** permission, so it can't change
  `.github/workflows`.
* An expiry date, and a calendar reminder to rotate it.

With steps 2 and 3 done, this token can write data to `data`, but it can't
change `main` (pull requests only) or any workflow file. Dispatching Check
Jobs on another branch doesn't reach the secrets (step 1).

The app needs **Actions: write** only for "Run check". If you can do without
that button, drop the permission: then the token can't start workflows at
all.

## What remains

* **Repository admins** can still change anything, rulesets included. Keep
  the account behind two-factor authentication.
* **The Actions `GITHUB_TOKEN` in the publish job** can write any branch
  that isn't protected (Contents: write is repository-wide), but it runs only
  `main`'s code, which a pull request controls.
* **Data is still trusted input to the code.** `scan_handoff.py` checks the
  shape and size of the scrape job's files, and the app and the notifier
  treat every field as untrusted text (HTML-escaped, never evaluated). A
  hostile `data/*.json` can mislead the UI or an alert's content. It can't
  run code or read a secret.
* **The scan reads hostile web content** in the scrape job, which holds no
  secret and a read-only token (unchanged since JT-01).
