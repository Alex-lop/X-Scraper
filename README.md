# X API Analyst

<p align="center">
  <img src="images/logo.png" alt="X API Analyst logo" width="280" />
</p>

A local, bring-your-own-token workbench for collecting public Posts through the official X API recent-search endpoint. It keeps immutable SQLite snapshots, resumable single-worker jobs, exact 15-minute cache reuse, JSON/CSV exports, and an optional MCP bridge.

## Install and connect

Python 3.11 or newer is required.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
xscraper configure
```

`configure` uses a masked prompt and saves the Bearer Token under `var/auth/` with owner-only permissions. `XSCRAPER_X_BEARER_TOKEN` overrides the file.

## Run

```bash
xscraper serve
```

The dashboard opens automatically at <http://127.0.0.1:5000>; use `--no-open` to suppress that. The server refuses non-loopback hosts and a second worker for the same database.

Every new collection requires a preview and explicit paid-read confirmation. Profile input compiles to `from:<handle>`; search input is passed through after edge trimming and NFC normalization. Reply and media filters compile to `-is:reply` and `has:media`. Dates use the API `start_time` and `end_time` parameters and must overlap the recent-search seven-day window.

X currently requires 10–100 results per recent-search request, so jobs allow 10–500 Posts. A final page can read up to nine more Posts than the requested collection limit. The UI estimates that ceiling at $0.005/Post, marked pricing as of August 2026; actual billing and X deduplication may differ. See [X pricing](https://docs.x.com/x-api/getting-started/pricing).

## API

- `GET /api/connection`
- `POST /api/collections/preview`
- `POST /api/jobs` with `compiledRequest` from preview and `confirmPaidRead: true`
- `GET /api/jobs` and `GET /api/jobs/:id`
- `GET /api/jobs/:id/posts?limit=50&offset=0`
- `DELETE /api/jobs/:id`
- `POST /api/jobs/:id/resume`
- `GET /api/jobs/:id/export?format=json|csv`

HTTP 429 jobs enter `waiting` and requeue automatically after the persisted rate-limit reset, including after restart. Completed pages remain available after any later failure. Legacy browser jobs remain readable but cannot resume.

## MCP and paid smoke

```bash
pip install -e ".[mcp]"
xscraper serve --no-open
xscraper mcp
```

The stdio MCP server proxies only the loopback REST API and exposes preview, start, status, Post pagination, and history tools. It does not read the token or offer posting, engagement, arbitrary URLs, or upstream payloads.

An opt-in live check reads at most 10 Posts (maximum documented estimate $0.05):

```bash
xscraper smoke api --profile OpenAI --confirm-paid-x
```

It is excluded from CI.

## Verification and scope

```bash
ruff check .
pytest
node --check xscraper/static/app.js
```

Deferred: full-archive search, deep timelines, OAuth user accounts, hosted SaaS, recurring monitoring, alerts, query ASTs, desktop bundles, proxy/stealth/CAPTCHA features, and write actions.

<p align="center">
  <img src="images/logo.png" alt="X API Analyst logo" width="180" />
</p>
