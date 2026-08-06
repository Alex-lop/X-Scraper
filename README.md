# X Collection Workbench

<p align="center">
  <img src="images/X_cool.png" alt="X Collection Workbench logo" width="280" />
</p>

A local, single-user workbench for collecting and inspecting public Posts through the official X API. It does not use browser scraping, unofficial endpoints, account pools, proxies, stealth features, or X write actions.

Each collection is a durable SQLite snapshot. Completed pages survive later failures, and the browser can filter and sort the local result set, summarize authors, languages, Post types, and daily volume, and surface top Posts and authors without another X request.

## Before installing

Python 3.11 or newer and an X developer project with official API access are required for live collection.

1. Create an app in the [X Developer Console](https://developer.x.com/en/portal/dashboard).
2. Generate its Bearer Token.
3. Review [X API pricing](https://docs.x.com/x-api/getting-started/pricing) and set a spending limit in the Developer Console. That limit—not this application—is the billing hard stop.

At the bundled August 2026 list prices, Posts cost $0.005 per resource, Users $0.010, and Media $0.005. The preview shows the maximum Post resources and Post list-price estimate plus the separate User and Media unit prices. Completed jobs show returned counts for all three resource types and a pre-dedup list-price calculation. These are planning aids, not an invoice: X controls final billing, daily resource deduplication, access, and pricing.

## Install and run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
xworkbench configure
xworkbench doctor --require-token
xworkbench serve
```

`configure` uses a masked prompt and saves the Bearer Token under `var/auth/` with owner-only permissions. `XWORKBENCH_X_BEARER_TOKEN` overrides that file. Runtime paths can be changed with `XWORKBENCH_RUNTIME_DIR`, `XWORKBENCH_DB_PATH`, and `XWORKBENCH_X_BEARER_TOKEN_PATH`.

`doctor` makes no X request; `--require-token` confirms only that a token was found, not that it is valid, funded, or authorized for full archive.

The dashboard opens at <http://127.0.0.1:5000>. Use `--no-open` to suppress the browser or `--port 0` to select a free port. The server refuses non-loopback hosts and a second worker for the same database.

## Collection modes

Every live collection requires a fresh preview followed by `confirmPaidRead: true`. Previews expire after five minutes so the displayed query and effective UTC window cannot silently drift. Collections are capped at 10–500 Posts.

- **Recent** uses `/2/tweets/search/recent` over X's rolling seven-day window. Dates are optional; a date on the rolling boundary is clamped to the exact supported cutoff. The final compiled query, including automatic filters, must fit the 512-character endpoint limit.
- **Full archive** uses `/2/tweets/search/all`. Both dates are required, the UI treats the end date as inclusive, and the final compiled query must fit the 1,024-character limit. Your X project must have access to this endpoint.

Profile input compiles to `from:<handle>`. Reply and media controls add `-is:reply` and `has:media`; search expressions are grouped before those application filters are added so operator precedence is preserved. General searches request author and media expansions, while profile searches avoid a redundant author expansion. The preview displays the exact endpoint, compiled query, effective dates, expiry, and cost basis before collection starts.

## Offline demo

```bash
xworkbench demo
```

The demo opens a temporary database preseeded with clearly labeled synthetic Posts. It has no token, performs no provider request, and removes the database when stopped. The same local inspection and JSON/CSV export surfaces remain available.

## API and exports

- `GET /api/connection`
- `POST /api/collections/preview`
- `POST /api/jobs` with the preview's `compiledRequest` and `confirmPaidRead: true`
- `GET /api/jobs` and `GET /api/jobs/:id`
- `GET /api/jobs/:id/posts?limit=100&offset=0`
- `POST /api/jobs/:id/cancel`
- `POST /api/jobs/:id/resume`
- `DELETE /api/jobs/:id` to permanently delete a terminal job and its observations
- `GET /api/jobs/:id/export?format=json|csv`

API mutations require `Content-Type: application/json`. Cancellation and deletion are deliberately separate: cancellation stops an active job; deletion is accepted only after a job is terminal.

HTTP 429 jobs persist their reset time, enter `waiting`, and requeue automatically, including after restart. Valid Posts from mixed X responses are retained with sanitized warnings. Post and long-form note text are stored exactly as returned, and missing author expansions preserve the author ID with a fallback URL.

JSON exports include the request, effective compiled provenance, status, warnings, returned resource counts, cost calculation, and raw normalized Posts. CSV exports contain the same Post rows and protect spreadsheet formula prefixes. No server-side sentiment, report cache, Markdown export, or AI analysis is performed.

## v1 database break

v1 intentionally starts a clean schema and does not migrate v0.2 databases. Before upgrading, run v0.2 and export any snapshots you need. Then move the old database aside or point `XWORKBENCH_DB_PATH` at a new file. The application rejects an older database without modifying it.

## Verify a source checkout

```bash
ruff check .
pytest
node --check xworkbench/static/app.js
node --test tests/test_analysis.mjs
python -m pip wheel . --no-deps --wheel-dir dist
```

CI runs those checks on Python 3.11–3.13 and inspects the wheel for the `xworkbench` entry point, new package namespace, and packaged dashboard assets.

Hosted deployment, OAuth user accounts, sentiment scoring, scheduling, alerts, recurring monitoring, unofficial scraping, and write actions are out of scope for v1.
