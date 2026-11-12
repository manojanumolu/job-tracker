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

None of this has been done. Each step changes production configuration and
needs the owner. **Follow the order below.** Each stage starts only after
the previous one is verified.

A rule throughout: **do not delete the repository secrets until stage C has
passed.** They are what keeps scans and emails working if anything in
stages A–C is wrong.

### Stage A: before merging this branch (no effect on running workflows)

1. *Settings → Environments → New environment*, name exactly
   `production`.
2. In it, *Deployment branches and tags → Selected branches and tags →
   Add deployment branch rule*: `main`.
3. Leave **Required reviewers**, **Wait timer** and other protection rules
   **off**. Scans and alert runs are unattended; a reviewer rule would make
   every run wait for approval.
4. In it, *Environment secrets → Add environment secret*, four times,
   with the **same names and values** as today's repository secrets:
   `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `ALERT_RECIPIENT`,
   `FIREBASE_SERVICE_ACCOUNT`. GitHub never shows a stored secret's value;
   paste each one again from where you keep it.
5. **Keep the repository secrets.**

Consequence: none yet. Today's workflows don't reference the environment.
Once this branch is merged, an environment secret overrides a repository
secret of the same name.

### Stage B: merge this branch (between runs)

* Merge when no *Check Jobs* run is queued or running.
* Avoid the 6-hourly personal-alert pass at 00:20, 06:20, 12:20 and
  18:20 UTC.
* **Merging starts a workflow.** This branch changes
  `scan_scheduler.yml`, whose push trigger restarts the scan scheduler. The
  scheduler dispatches *Check Jobs* in "plan" mode, which scans **only if
  one is due**, as every scheduled tick does.
* **Do not** use *Run workflow* to test: it starts a real scan, and sends
  real alert emails for any pending jobs. Wait for the scheduled runs.

### Stage C: verify the new workflows (read-only)

On the first scan after the merge (Actions → *Check Jobs* → the run):

1. *plan*: "Checkout the schedule code" and "Checkout schedule inputs"
   succeed, and "Decide what this run does" prints its plan.
2. *scrape*: "Checkout the data" succeeds, and the uploaded
   `scan-results` artifact holds exactly 2 files.
3. *publish*:
   * the run page shows a **production** deployment;
   * "Checkout the data branch" succeeds;
   * the log contains `publish: pushed (chore: update seen_jobs + companies [skip ci])`
     and a `delivery: this run …` line;
   * "Send personal email alerts" logs its counts.
4. The next 6-hourly *personal-alerts* job: a production deployment, and
   its counts.
5. The next daily *Application retention* run (03:05 IST): it succeeds in
   the production environment.

If any of these fails: revert the merge. While the data is still on `main`,
reverting is complete; nothing else needs undoing. If the publish job fails
before its first push, nothing is emailed for that run.

### Stage D: delete the repository secrets (closes the main risk)

Only after stage C has passed in full: *Settings → Secrets and variables →
Actions → Repository secrets* → delete `GMAIL_ADDRESS`,
`GMAIL_APP_PASSWORD`, `ALERT_RECIPIENT`, `FIREBASE_SERVICE_ACCOUNT`.

* **Consequence:** only jobs running on `main` in the `production`
  environment can read the secrets. A workflow started on any other branch
  gets none.
* **Not affected:** the Streamlit app's own secrets, and workflows without
  secrets (Tests, Company logos, Scan scheduler).
* **Verify:** the next scan and personal pass behave as in stage C.

### Stage E: move the data to its own branch (a production migration)

Do this in a quiet window, with the scans paused. Otherwise, for a few
minutes the app and the scans write different branches, and a job dismissed
in the app could still be emailed.

1. *Actions → Check Jobs → ⋯ → Disable workflow*, and the same for *Scan
   scheduler*. Check that no run is in progress.
2. Create branch `data` from the latest `main` (*Code → branches → New
   branch*).
3. *Settings → Secrets and variables → Actions → Variables → New repository
   variable*: `JT_DATA_BRANCH` = `data`.
4. In the Streamlit app's secrets (top level): `JT_DATA_BRANCH = "data"`.
   Save, then reboot the app.
5. Re-enable both workflows.
6. **Verify:**
   * the next scan's commit lands on `data`, not `main`;
   * the app shows that scan's jobs;
   * a dismissal made in the app appears on `data`.

After the move:

* `main`'s copy of `data/*.json` stops changing. The app uses it only if it
  can't reach GitHub.
* The logos workflow's push trigger (on `main`) no longer fires; its daily
  schedule still runs.
* **Rollback:**
  1. Disable the workflows.
  2. Copy `data/*.json` from `data` to `main`.
  3. Delete the variable and the Streamlit secret.
  4. Reboot the app and re-enable the workflows.

### Stage F: protect the branches (after stage E)

*Settings → Rules → Rulesets → New branch ruleset*:

* `protect-main`: target the default branch. Restrict deletions, block force
  pushes, require a pull request before merging, require status check
  `test`. **No bypass list.** Bypass is checked against the actor, and the
  app's token acts as you.
* `protect-data`: target `data`. Restrict deletions and block force pushes
  only. The scans and the app must still push to it.

Consequence: every later change to `main` goes through a pull request. Doing
this before stage E would block the scans' data commits to `main`.

### Stage G: narrow the app's token (after stage F)

1. Create a **fine-grained personal access token** (*Settings → Developer
   settings → Fine-grained tokens*):
   * repository access: only this repository;
   * **Contents: Read and write**;
   * **Actions: Read and write**, which the app's "Run check" button needs;
     drop it if you can do without the button;
   * nothing else, in particular **no Workflows** permission;
   * an expiry date.
2. Put it in the Streamlit secrets as `GITHUB_TOKEN` and reboot.
3. Check that a dismissal saves and that "Run check" works.
4. Then revoke the old token.

With stages D–G done, that token can write only data, on `data`. It can't
change `main` (pull requests only) or any workflow, and a workflow it starts
on another branch gets no secrets.

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
