# X Collection Workbench

A local analyst dashboard for collecting structured public posts from X profiles and search queries. It uses an authenticated Playwright browser session, captures the structured timeline responses loaded by X, persists resumable jobs in SQLite, and exports JSON or CSV.

## MVP capabilities

- Profile handles and `x.com`/`twitter.com` profile URLs
- Search queries with inclusive start/end dates
- Up to 500 posts per job
- Reply and media-only filters
- Real like, reply, repost, quote, and bookmark counts
- Persistent progress, partial results, cancellation, restart recovery, and cursor resume
- Optional VADER sentiment enrichment
- Local job history and matching server-generated JSON/CSV exports

The MVP intentionally does not implement posting, followers/following, proxies, multi-account rotation, stealth behavior, or hosted multi-user access.

## Install

Python 3.11 or newer is required.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium
```

For the exact dependency set used by CI, install `requirements.lock` before the editable package.

## Authenticate once

Use a dedicated X account rather than a personal account:

```bash
python -m xscraper auth
```

A headed Chromium window opens. Log in normally, wait for the home timeline, then return to the terminal and press Enter. Playwright state is saved under `var/auth/`, which is excluded from Git. Passwords and cookies are never submitted through the dashboard.

If `/api/session` reports `expired`, run the authentication command again.

## Run the dashboard

```bash
python -m xscraper serve
```

Open <http://127.0.0.1:5000>. The Flask server serves both the dashboard and API, so a second static-file server and permissive CORS are not needed.

You can also run one collection from the terminal:

```bash
python -m xscraper collect profile OpenAI --max-tweets 50
python -m xscraper collect search "AI agents lang:en" --start-date 2026-07-01 --max-tweets 100 --sentiment
```

`python server.py` and `python main.py` remain compatibility entry points.

## API

- `GET /api/session` — validate saved browser state
- `POST /api/jobs` — enqueue a profile or search collection
- `GET /api/jobs` and `GET /api/jobs/:id` — history and progress
- `GET /api/jobs/:id/tweets` — paginated persisted results
- `DELETE /api/jobs/:id` — request cancellation
- `POST /api/jobs/:id/resume` — resume an interrupted, cancelled, or failed job
- `GET /api/jobs/:id/export?format=json|csv` — download persisted results

Example request:

```json
{
  "sourceType": "search",
  "sourceValue": "open source lang:en",
  "maxTweets": 100,
  "startDate": "2026-07-01",
  "endDate": "2026-07-31",
  "includeReplies": false,
  "mediaOnly": false,
  "analyzeSentiment": false
}
```

## Runtime and diagnostics

The default runtime directory is `var/`:

- `var/twitter_scraper.db` — versioned SQLite data
- `var/auth/storage_state.json` — ignored Playwright session state
- `var/artifacts/` — screenshots produced on collection failures

Override these locations with `XSCRAPER_RUNTIME_DIR`, `XSCRAPER_DB_PATH`, `XSCRAPER_STORAGE_STATE`, and `XSCRAPER_ARTIFACTS_DIR`. Set `XSCRAPER_HEADLESS=0` to observe a collection browser. A job stops after ten minutes by default; override this with `XSCRAPER_JOB_TIMEOUT`.

Errors distinguish missing/expired sessions, unavailable profiles, rate limits, timeouts, cancellation, and timeline-schema drift. Partial rows and the last cursor remain available after recoverable failures.

## Tests

```bash
ruff check .
pytest
```

Parser tests use sanitized timeline fixtures and do not contact X. Live X smoke testing is intentionally opt-in and is not run in CI.

## Limitations and responsible use

This project relies on X's web application and undocumented timeline operations. X can change them, expire sessions, or rate-limit collection without notice. Only publicly visible content available to the authenticated account can be collected; protected accounts are unsupported. Respect applicable law, privacy expectations, X's terms, and reasonable collection volumes. For supported production access, evaluate the official X API.
