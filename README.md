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

## How it works

- **scraper.py** — tries a JSON/API endpoint first; falls back to Playwright. Filters by fresher keywords. Marks companies "broken" on failure without crashing.
- **notifier.py** — sends HTML email via Gmail SMTP. `test_mail()` sends a single test message.
- **config_store.py** — reads/writes local JSON and commits changes back to GitHub via PyGithub.
- **GitHub Actions** — runs every 3 hours, scrapes, emails if new jobs, commits updated JSON.
