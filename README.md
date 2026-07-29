# AI Morning Brief — Cloud

A public, read-only Streamlit dashboard for a daily RSS-based AI news briefing.
It does not call an LLM or any paid AI API.

## Cloud workflow

1. GitHub Actions runs every day at 08:00 in `Asia/Shanghai`.
2. `summarizer.py` collects news from the configured RSS feeds.
3. New articles are merged into `output/articles.json`.
4. Duplicate URLs are not added again.
5. Streamlit Community Cloud reloads the repository update.
6. Visitors can browse the current 08:00-to-08:00 briefing or all history.

The cloud dashboard is intentionally read-only. Visitors cannot start the RSS
collector from the webpage.

## Files

- `app.py` — Streamlit dashboard.
- `news_collector.py` — downloads and normalizes RSS entries.
- `summarizer.py` — filters important articles and updates permanent JSON.
- `database.py` — builds a temporary SQLite index from the JSON history.
- `config.yaml` — RSS sources and rule-based importance keywords.
- `output/articles.json` — permanent, deduplicated article history.
- `.github/workflows/daily-brief.yml` — daily cloud automation.
- `requirements.txt` — Python dependencies.

## Run locally

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m streamlit run app.py
```

Then open `http://localhost:8501`.

## Deploy

1. Push this folder to a GitHub repository.
2. Sign in to [Streamlit Community Cloud](https://share.streamlit.io/).
3. Choose **Create app**.
4. Select the repository, its default branch, and `app.py`.
5. Deploy and share the generated `streamlit.app` URL.

No secrets or API keys are required.
