# X Collection Workbench

A local analyst dashboard for collecting structured public posts from X profiles and search queries. It uses an authenticated Playwright browser session, captures the structured timeline responses loaded by X, persists resumable jobs in SQLite, and exports JSON or CSV.

## MVP capabilities

- Profile handles and `x.com`/`twitter.com` profile URLs
- Search queries with inclusive start/end dates
- Up to 500 posts per job
- Reply and media-only filters
- Point-in-time like, reply, repost, quote, and bookmark counts
- Immutable per-job snapshots, persistent progress, partial results, cancellation, and restart recovery
- Cursor resume guarded by the original request and GraphQL operation context
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

A headed Chromium window opens. Log in normally, wait for the home timeline, then return to the terminal and press Enter. Playwright state is saved atomically with owner-only permissions under `var/auth/`, which is excluded from Git. Passwords and cookies are never submitted through the dashboard.

If `/api/session` reports `expired`, run the authentication command again.

## Live GraphQL smoke gate

Before adding caching or expanding search syntax, validate the current X web operations with a
dedicated collector account and a stable public fixture profile:

```bash
python -m xscraper smoke graphql --profile fixture_handle --confirm-live-x
```

The fixture profile must have at least 30 posts and, within the last 14 UTC days, an original,
reply, photo, and quote post. The opt-in run validates the session plus exactly five timeline
payloads, disables sentiment and retries, and uses a temporary database. It writes only sanitized
reports and review-only fixture candidates under `var/smoke-reports/`; it never updates committed
fixtures or analyst history.

Exit codes are `2` for preconditions, `3` for an expired/invalid session, `4` for schema or semantic
failure, and `5` for rate limiting. A 429 aborts immediately. Do not run this command in CI.

## Run the dashboard

```bash
python -m xscraper serve
```

Open <http://127.0.0.1:5000>. The Flask server serves both the dashboard and API, so a second static-file server and permissive CORS are not needed.

The MVP deliberately refuses non-loopback hosts and a second worker process for the same database.

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
- `POST /api/jobs/:id/resume` — resume an interrupted, cancelled, partial, or retryable failed job
- `GET /api/jobs/:id/export?format=json|csv` — download persisted results

JSON exports use a versioned envelope containing job/completion metadata and tweets. CSV exports contain the same tweet fields, use spreadsheet-safe text cells, and carry job status and snapshot metadata in response headers.

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

Failure screenshots and sanitized JSON summaries may contain visible account or post information and should be retained carefully. Full Playwright trace ZIPs are disabled by default; opt in with `XSCRAPER_ENABLE_TRACING=1` only for short troubleshooting sessions.

Database migrations create a one-time `*.bak` snapshot before changing an existing schema. For routine backups while the server is running, use SQLite's backup API rather than copying only the main database file while WAL mode is active.

Errors distinguish missing/expired sessions, unavailable profiles, rate limits, timeouts, cancellation, incompatible resume state, and timeline-schema drift. Partial rows and the last compatible cursor remain available after recoverable failures. Jobs expose completion reasons such as `target_reached`, `timeline_exhausted`, `date_boundary_reached`, and `no_progress`.

## Tests

```bash
ruff check .
pytest
```

Parser tests use sanitized timeline fixtures and do not contact X. Live X smoke testing is intentionally opt-in, requires a dedicated authorized account, and is not run in CI.

## Limitations and responsible use

This project relies on X's web application and undocumented timeline operations. X can change them, expire sessions, or rate-limit collection without notice. Only publicly visible content available to the authenticated account can be collected; protected accounts are unsupported. Respect applicable law, privacy expectations, X's terms, and reasonable collection volumes. For supported production access, evaluate the official X API.

VADER sentiment is optional, English-oriented lexical polarity rather than confidence, and should be treated as a lightweight annotation rather than a factual classification.
