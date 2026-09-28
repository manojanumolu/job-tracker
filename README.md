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

## How it works

- **scraper.py** — tries a JSON/API endpoint first; falls back to Playwright. For each India-located candidate it opens the job's detail page (Workday detail API, or the page's schema.org `JobPosting` JSON-LD / description block) before classifying it. Marks companies "broken" on failure without crashing.
- **job_classifier.py** — decides whether a posting is `FRESHER`, `ENTRY_LEVEL`, `EXPERIENCED`, `SENIOR`, `NOT_A_JOB` or `UNKNOWN`, with a human-readable reason (logged on every run and stored on alerted jobs). Only `FRESHER` / `ENTRY_LEVEL` are alerted. Seniority is judged from the title; explicit experience requirements (`2+ years`, `1–3 yrs`, `minimum 2 years`, `experience required: 2 years`, …) reject a role even when it also carries an entry-level keyword. Ambiguous words such as "Associate", "Junior" or "Intern" are never proof on their own — without other evidence the result is `UNKNOWN` and no alert is sent.
- **notifier.py** — sends HTML email via Gmail SMTP. `test_mail()` sends a single test message.
- **config_store.py** — reads/writes local JSON and commits changes back to GitHub via PyGithub.
- **GitHub Actions** — runs every 3 hours, scrapes, emails if new jobs, commits updated JSON.
