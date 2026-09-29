# Fresher Job Tracker

Monitors career pages of target companies for entry-level / fresher postings and emails you when new ones appear.

## Setup

### 1. GitHub Actions secrets

In your repo → **Settings → Secrets → Actions**, add:

| Secret | Value |
|--------|-------|
| `GMAIL_ADDRESS` | Your Gmail address |
| `GMAIL_APP_PASSWORD` | Gmail App Password (not your account password) |

> `GITHUB_TOKEN` is provided automatically by Actions.

### 2. Streamlit Cloud secrets

In your app → **Settings → Secrets**, add:

```toml
GMAIL_ADDRESS = "your-email@gmail.com"
GMAIL_APP_PASSWORD = "xxxx-xxxx-xxxx-xxxx"
GITHUB_TOKEN = "ghp_xxxxxxxxxxxxxxxxxxxx"
```

### 3. Deploy to Streamlit Cloud

1. Push this repo to GitHub.
2. Go to [share.streamlit.io](https://share.streamlit.io) → **New app**.
3. Select your repo, branch `main`, file `streamlit_app.py`.
4. Add the secrets above and click **Deploy**.

### 4. Local development

```bash
cp .env.example .env
# fill in your credentials in .env
pip install -r requirements.txt
playwright install chromium
streamlit run streamlit_app.py
```

### 5. Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The Playwright end-to-end test is skipped when Chromium isn't installed (`playwright install chromium`); set `PLAYWRIGHT_CHROMIUM_EXECUTABLE` to use an existing Chromium binary.

## How it works

The core rule: **only genuine India fresher / entry-level job postings are emailed, and only after the job's own posting was read.** When evidence is missing or unreadable the job is `UNKNOWN` (not emailed, retried next scan) — a missed ambiguous job is preferred over an experienced or non-job alert.

- **sources.py** — each company's *real* job source (verified against the live sites, never guessed):

  | Company | Source |
  |---|---|
  | Sanofi | Workday API (`sanofi.wd3`), India country facet, paginated |
  | PwC | Workday API (`pwc.wd3`, Experienced + Campus sites), India city facets, paginated |
  | PokerStars | Flutter International's Workday API (`flutterbe.wd3`), India facets |
  | Accenture | Accenture job-search API, India, newest 300 in Accenture's own "0-2 years" bucket |
  | Avalara | Jibe/iCIMS jobs API, India postings |
  | MetLife | Avature job search (all pages) + each India posting's page |
  | NPCI | Zoho Recruit career site (job data embedded in the page) |

  Any `*.myworkdayjobs.com` URL added in the app uses the Workday API automatically; other URLs use the browser path below.
- **scraper.py** — runs each company's source; companies without one are read with Playwright (job cards are read from their card container, so cards whose link has no text still work; links must look like postings or carry `JobPosting` data). Every candidate's detail is verified: HTTP errors, rate limits, bot challenges, redirects, a page whose JSON-LD is another job, empty descriptions or an exhausted detail budget make it *unreadable* → never emailed. Experience/level fields outside the description and hidden text are used as reject-only evidence. A company with a job source is never scraped through its marketing page. Health per company: `active` (Healthy), `failing` (partial errors / many unreadable job pages), `broken` (job list unreadable: blocked, HTTP error, site changed), `needs_config` (URL shows no postings); the reason is shown in Monitoring.
- **job_classifier.py** — decides whether a posting is `FRESHER`, `ENTRY_LEVEL`, `EXPERIENCED`, `SENIOR`, `NOT_A_JOB` or `UNKNOWN`, with a human-readable reason (stored on alerted jobs). An explicit experience requirement always wins over positive wording (`Freshers welcome` + `2+ years` → `EXPERIENCED`), including number-less ones ("prior experience required", "proven experience", "Mid-Senior level"). "Good to have / Preferred" lines never hide a mandatory requirement. Education durations ("15 years of full-time education") and colleagues' experience ("mentored by engineers with 10+ years") are not requirements. Programme pages, stories, talent networks, country pickers and recruiter-for-graduates roles are `NOT_A_JOB`. Ambiguous words ("Associate", "Junior", "Intern", "Engineer I") are never proof on their own.
- **locations.py** — India only when India is explicitly one of the job's locations (cities, states, `IN`/`IN-KA` codes; "India or US" counts); "Hyderabad, Pakistan", "Delhi, Ontario", "Columbus, IN" don't.
- **identity.py** — deduplication by ATS job ID (Workday `R…`/`JR…`, Accenture `id=`, Zoho, Avature, Jibe) or canonical URL, so two jobs with the same title are both kept; the same title *and* location under a new ID is treated as a repost.
- **alerts.py / repo_sync.py** — publishing and email, at most once per job: alertable jobs are marked *claimed* and pushed before the email goes out, then *sent*. If the first push fails nothing is emailed; a failed push after sending can't cause a duplicate; a failed send releases the claim for the next run. Data is merged record-by-record with the latest copy on GitHub, so a dismissal made in the app during a run is never overwritten. Dismissed, rejected and `UNKNOWN` jobs are never emailed.
- **notifier.py** — sends the alert email via Gmail SMTP: one card per job (title, company, location, Fresher / Entry level badge, why it matched, **View job** button) plus a plain-text version. `test_mail()` sends a single test message.
- **streamlit_app.py** — dashboard. **⟳ Reload** fetches the latest data from GitHub (also refreshed automatically every 5 minutes); **▷ Run check** starts the scraper workflow (at most once per 5 minutes). Removing / clearing alerts *hides* them — the records stay in `seen_jobs.json`, which is the scraper's "already emailed" history, so hidden jobs are never re-sent. Saves are applied to the latest copy on GitHub, so they never overwrite data the scraper committed in the meantime.
- **config_store.py** — reads/writes local JSON and applies merge-safe updates to the copies on GitHub via PyGithub.
- **GitHub Actions** — runs every 3 hours: `scraper.py`, then `alerts.py` (publish + email).
