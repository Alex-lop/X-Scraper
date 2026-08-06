# X-Scraper

<p align="center">
  <img src="images/X_cool.png" alt="X-Scraper logo" width="280" />
</p>

X-Scraper is a local-first, human-in-the-loop feed-to-context bridge. It turns a small,
user-approved view of an X Home feed into a durable SQLite snapshot that can be inspected,
exported, and shared with compatible local agents through read-only MCP.

The snapshot is the trust boundary. Collection contacts X; inspection, export, deterministic
analysis, and MCP reads use the saved database and do not make another X request.

> **Terms warning:** X's current Terms of Service say that scraping is prohibited without X's
> prior written consent. Noncommercial, research, or personal intent does not itself create
> permission. You are responsible for obtaining appropriate authorization and following X's
> terms and applicable law. See [X's current Terms of Service](https://x.com/en/tos).

## What this release does

- Opens a real headed Playwright Chromium window for normal, manual X sign-in. The application
  never asks for, receives, logs, transmits, or stores your password.
- Captures 1–25 Posts currently visible in the authenticated Home feed; the default is 5.
- Persists each visible batch immediately in a durable, reproducible SQLite snapshot, including
  source, provider, observation time, stop reason, warnings, and available Post metadata.
- Preserves partial results after cancellation, timeout, session expiry, challenge, DOM drift, or
  a later browser failure.
- Inspects, filters, sorts, summarizes, and exports snapshots without revisiting X. Remote media is
  not loaded automatically during inspection.
- Exposes completed snapshots through bounded, local, read-only MCP tools and resources. Stored
  Post text is untrusted external content, not instructions to an agent.
- Retains the official X API recent and full-archive provider, including exact query preview,
  resource bounds, rate-limit recovery, and explicit paid-read confirmation.

This is not an API clone, bulk social-listening platform, CAPTCHA bypasser, proxy system, account
farm, write-action bot, or hosted multi-user service.

## Install

Python 3.11 or newer is required.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,browser,mcp]"
playwright install chromium
```

Playwright and MCP are optional package extras so an official-API-only installation can remain
small. Install the `browser` extra before using Browser capture or `auth`; install `mcp` before
starting the MCP server.

## Authenticate and run

```bash
xworkbench auth
xworkbench doctor
xworkbench serve
```

`xworkbench auth` launches a fresh headed Chromium context at X's normal login flow. Enter your
credentials and complete any normal verification only in that browser. The session saves
automatically after the Home surface is detected. X-Scraper never uses your normal Chrome profile.

Playwright authentication state is stored under the local runtime directory's `auth/` folder and
is protected with owner-only permissions where the platform supports them. That file contains
sensitive session material: do not share, commit, attach, or paste it into logs or bug reports.
Delete it or run `xworkbench auth` again when you want to replace the session.

The dashboard opens on <http://127.0.0.1:5000> and refuses non-loopback hosts and clients. Use
`--no-open` to suppress opening the dashboard or `--port 0` to select a free port. A second worker
for the same database is refused.

`xworkbench doctor` is local-only. It checks Python, the runtime directory, database compatibility,
Playwright, the Chromium executable, saved browser-state presence and permissions, and a loopback
port. A missing official API token does not disable Browser capture. `--require-token` is available
for an official-API-only readiness check; it never validates credits or makes a paid read.

## Browser capture (default, experimental)

Choose **Browser capture**, confirm that the session status is ready, keep **Home feed** selected,
choose a target from 1 to 25, and press **Start browser capture**. Chromium is headed by default so
the user can see the navigation. X-Scraper parses the visible outer Post articles before each
bounded scroll, deduplicates by canonical numeric Post ID, and stops when the target is reached or
progress can no longer be made safely.

Unknown DOM values stay missing. In particular, missing timestamps, language, type classification,
media, or engagement metrics are not turned into zeroes. A challenge or CAPTCHA is never solved or
bypassed: the job stops truthfully as requiring manual action, and any already saved rows remain
inspectable.

Only Home capture is supported in this recovery slice. Playwright profile/search, Following-tab
automation, threads, replies, lists, and historical collection are deferred.

X can change its DOM, expire a session, rate-limit access, present a challenge, or block automation
at any time. A locally present session marked `ready` means the prerequisites exist; it is not a
promise that X will accept the session on the next capture.

## Optional official X API provider

If you have official API access, store a Bearer Token with a masked prompt:

```bash
xworkbench configure
xworkbench doctor --require-token
```

The token is stored under the runtime `auth/` directory with owner-only permissions.
`XWORKBENCH_X_BEARER_TOKEN` can provide an environment override.

Official API mode retains profile/search sources, 10–500 Post limits, rolling seven-day recent
search, full archive with required inclusive dates, exact five-minute compilation previews, and
`confirmPaidRead: true`. Cost figures are list-price planning aids, not invoices or hard billing
limits; configure the real spending limit in the X Developer Console.

## Durable snapshots and exports

SQLite uses WAL mode and restrictive file permissions. Post observations are immutable per job,
batches and checkpoints commit together, duplicate IDs are ignored within a snapshot, and jobs
survive cancellation, rate-limit waits, process restart, and retry. A forward migration from the
current v1 schema creates a protected SQLite backup before changing nullable Post fields; unrelated
older schemas are preserved and rejected rather than guessed.

Stage 3 reads SQLite only. It retains local text search, author/language/type filters, sorting,
daily volume, languages, top Posts, top authors, and JSON/CSV exports. CSV cells are protected from
spreadsheet formula injection. Provider-neutral provenance and partial status are included; API
cost/resource fields appear only on official API snapshots.

## Read-only MCP

Start the dashboard, then run:

```bash
xworkbench mcp --url http://127.0.0.1:5000
```

The stdio MCP server connects only to a loopback dashboard and exposes bounded reads:

- `list_x_snapshots(limit=...)`
- `get_x_snapshot(snapshot_id)`
- `get_x_posts(snapshot_id, offset=..., limit=...)`
- `search_x_snapshot(snapshot_id, query, limit=...)`
- `get_latest_feed_snapshot()`

MCP cannot authenticate, start collection, revisit X, accept an arbitrary URL, expose provider
checkpoints or credentials, or perform X write actions. Collection remains an explicit human action
in the dashboard. Snapshot resources are passive context; tools provide bounded lookup and
pagination.

## Offline demo

```bash
xworkbench demo
```

The demo uses a temporary database seeded with clearly labeled synthetic Posts. It has no token,
makes no provider request, supports the normal inspection/export flow, and removes the database on
exit.

## Explicit live smoke runbook

No live X request runs in CI. After you have appropriate authorization and have completed
`xworkbench auth`, the opt-in smoke gate is:

```bash
xworkbench live-smoke --confirm-live-x
```

It launches headed Chromium, requests no more than two currently visible Home-feed Posts, disables
retries, uses a temporary database, writes no committed fixture, and reports only sanitized status
and counts. Missing login state, changed DOM, X failure, or a challenge is reported as a real
failure; it is never converted into a fake pass.

## Local data and configuration

By default, generated state stays under `var/`, which is excluded from Git:

- SQLite database, WAL, and migration backup
- official API token file
- Playwright authentication state
- worker lock and temporary runtime files

The application does not store full pages, request headers, raw private network payloads, or your
password. Browser diagnostics and Playwright artifacts are also excluded from Git. Runtime paths
can be changed with `XWORKBENCH_RUNTIME_DIR`, `XWORKBENCH_DB_PATH`,
`XWORKBENCH_X_BEARER_TOKEN_PATH`, and `XWORKBENCH_STORAGE_STATE_PATH`.

## Future enrichment boundary

A later enrichment adapter may consume an immutable completed snapshot asynchronously and write a
separate result tagged with adapter name, version, configuration, and source snapshot ID. It must
be reproducible and optional: enrichment failure can never change a successful collection into a
failed one. No server-side LLM, sentiment ensemble, Kafka, Redis, scheduler, alerting, or distributed
worker is included here; the chatbot reading MCP is the initial analysis layer.

## Verify a source checkout

```bash
ruff check .
pytest
node --check xworkbench/static/app.js
node --test tests/test_analysis.mjs
python -m pip wheel . --no-deps --wheel-dir dist
```

All ordinary tests use synthetic data and must pass without X, an authenticated session, or an
external model connection.

## Responsible use

There is no stealth plugin, randomized fingerprinting, proxy support, account rotation, direct
cookie extraction, private GraphQL replay, automated challenge solving, or X write action. Do not
describe this project as compliant scraping, undetectable, ban-proof, or an unrestricted free API
replacement. Technical limits reduce scope; they do not grant legal or contractual permission.
