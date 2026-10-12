# External scan trigger (optional, recommended)

## Why

GitHub Actions `schedule` events are best-effort. Measured on this repository
(Oct 11–12, 2026): of ~17 hourly `0 * * * *` ticks GitHub created 5 runs, none
cancelled; no Check Jobs run at all from 06:55 to past 15:00 UTC on Oct 12;
the daily Company Logos tick (`40 2 * * *`) started 7 h late. GitHub's status
page also reported an Actions incident ("queueing new Actions runs") from
12:21 UTC that day. The scan code itself ran correctly every time it was
started. Moving the tick to `17,47 * * * *` helps, but nothing inside GitHub's
scheduler can guarantee a 3-hour cadence.

An outside cron that calls GitHub's **workflow-dispatch API** starts the
existing workflow on time. It is a backup, not a replacement: the GitHub
schedule stays on, and whichever trigger arrives first in a window scans.

## How it stays safe

- **No new endpoint.** The caller talks to `api.github.com` directly; GitHub
  authenticates the token and rejects any `mode` other than `scan` / `plan`.
- **No duplicate scans.** The trigger sends `mode=plan`. That run goes
  through the same gate as the plan tick (`scan_schedule.py`): it scans only
  if no scan has started since the latest interval boundary. Every run of
  the workflow shares one concurrency group, so a second trigger waits for
  the first and then sees the new scan time and does nothing.
- **Minimal token** (below): in this one repository it can start, cancel and
  re-run workflow runs, delete run logs, and enable/disable workflows. It
  cannot read Actions secrets, push code, change settings or reach Firestore.
  Whoever holds it could at worst start extra scans or switch the workflow
  off — so keep it only at the cron service and rotate it yearly.
- **Personal alerts unchanged.** The 6-hourly `20 */6 * * *` pass and the
  ≤ 1 email per person per 6 h rule are untouched; personal alerts also run
  after every scan, as before.

## Options compared

| Option | Trigger delay | Credential | Setup / cost | Verdict |
|---|---|---|---|---|
| GitHub `schedule` only | minutes to 7 h+, some ticks dropped | none | none | current; unreliable |
| **cron-job.org → workflow dispatch** | usually seconds | fine-grained PAT (Actions: write, this repo) stored at cron-job.org | free, ~5 min, failure e-mails built in | **recommended** |
| Cloudflare Worker cron → dispatch | seconds | same PAT, as a Worker secret | free tier, needs a Worker + code | good if you already use Cloudflare |
| The Streamlit app dispatching when it sees an overdue scan | only when someone opens the app | the app's existing token | none | rejected: scans must not depend on page views |

Any external service can itself be down; when it is, the GitHub schedule is
still there as a fallback.

## Setup (≈ 5 minutes)

### 1. Create the token (GitHub)

GitHub → Settings → Developer settings → **Fine-grained personal access tokens**
→ Generate new token:

- **Token name:** `job-tracker external scan trigger`
- **Expiration:** 1 year (set a calendar reminder to rotate it)
- **Repository access:** *Only select repositories* → `manojanumolu/job-tracker`
- **Repository permissions:** **Actions → Read and write**. Nothing else
  (Metadata: read-only is added automatically).

Copy the token once. Do not paste it into the repository, an issue, a chat or
a URL.

### 2. Create the cron job (cron-job.org)

Create a free account, then **Create cronjob**:

- **URL:** `https://api.github.com/repos/manojanumolu/job-tracker/actions/workflows/check_jobs.yml/dispatches`
- **Schedule:** every hour at minute **5** (custom: minutes `5`, every hour).
  Minute 5 keeps a scan it starts clear of the GitHub ticks at :17 / :20.
- **Advanced → Request method:** `POST`
- **Advanced → Headers:**
  - `Authorization: Bearer <the token>`
  - `Accept: application/vnd.github+json`
  - `X-GitHub-Api-Version: 2022-11-28`
  - `Content-Type: application/json`
- **Advanced → Request body:** `{"ref":"main","inputs":{"mode":"plan"}}`
- **Notifications:** on failure (GitHub answers **204** on success; 401/403
  means the token expired or lacks Actions: write; 422 means a wrong body).

Use **Test run** once: it is safe — with `mode=plan` it scans only if a scan
is due anyway.

### 3. Check it worked

GitHub → Actions → Check Jobs: a run with event `workflow_dispatch` appears
within a minute. Open it: the summary shows
`Scan plan: trigger workflow_dispatch mode=plan · … · scan=True|False`.
`scan=False` with a recent last scan is correct (not due). The app's
"Last scan" should then never pass ~3 h 10 min, and "Scan overdue" should
only appear when both GitHub and cron-job.org failed.

## Turning it off

Disable the cron job at cron-job.org and delete the token on GitHub. Nothing
in the repository needs to change.
