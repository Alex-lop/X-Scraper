# Terminal operations

The optional Textual interface covers setup, capture approval, and queue operations without
replacing the loopback web dashboard. Install it from a source checkout with:

```bash
python -m pip install -e '.[tui]'
```

Textual is not imported or installed by the base package. Both terminal commands fail before
creating runtime files or acquiring a worker lock when the extra is absent.

## Owner workbench

```bash
xworkbench tui --port 5000
```

`tui` initializes the normal runtime, binds Flask only to `127.0.0.1`, and owns the sole
`JobService`. Werkzeug runs in one background thread while Textual stays on the main thread. Port
`0` selects a free port; the resolved dashboard URL and exact monitor command appear on the Setup
tab. The program never spawns another terminal.

Keyboard bindings are `1` Setup, `2` Capture, `3` Queue, `w` web dashboard, and `q` orderly quit.
The web dashboard remains available and retains advanced analysis and export operations.

### Setup

Setup shows public settings and each provider's exact readiness. An optional official API Bearer
Token is accepted only in a masked field, written through the existing protected-file checks, and
cleared from the field. Headed Browser authentication opens the existing manual sign-in flow; the
terminal never asks for a username or password. Quitting during authentication requests orderly
Browser closure before the owner process exits. Low-level worker and resource settings are
read-only here; use the documented configuration surface outside the terminal.

### Capture

The terminal supports the same bounded server contracts as the dashboard:

- Browser Home, profile, or Latest search with 1-25 Posts.
- Official profile or search, recent or full archive, dates, reply/media filters, and 10-500 Posts.
- Saved-source creation and an atomic selection of 2-25 sources, priority 0-100, deadline
  60-3600 seconds, and fixed `capture_fresh` freshness.

Preview displays the exact server execution plan or batch manifest, destination, limits, expiry,
cost information, queue order, digest, and concurrency boundaries. Official confirmations are
explicitly labeled as paid reads. Changing a field or reaching expiry invalidates approval. If a
POST response is truncated, malformed, or lost after transmission, the outcome is shown as
unknown, approval is discarded, durable state is refreshed, and the terminal never resubmits
automatically.

### Queue

Queue shows durable jobs, bounded progress events, depth/capacity, workers, active source/auth
keys, wait p50/p95, throughput, persistence backlog, event gaps/drops, resource pauses, and cleanup
failures. Coordinator RSS is labeled separately; Chromium process-tree RSS remains `unsupported`,
never zero. The owner may explicitly cancel the selected job or the current-session batch.

Resume, delete, purge, search, export, Changes, advanced analysis, and MCP remain web/CLI-first in
this release. The terminal does not render stored Post content, response bodies, credentials,
cookies, auth state, filesystem secrets, or unsanitized exceptions.

## Read-only monitor

Start an owner server in one terminal, then attach from another:

```bash
xworkbench start
xworkbench monitor --url http://127.0.0.1:5000
```

`monitor` reuses the Queue view without mutation controls. It opens neither SQLite nor a worker and
accepts only a plain loopback root URL. Credentials, fragments, redirects, arbitrary paths,
environment proxies, oversized bodies, and malformed JSON are rejected.

Queue/progress polling runs every 1.5 seconds and connection readiness every 10 seconds with one
request in flight. Durable job rows take precedence after an event gap. On disconnection, stale
state remains visibly labeled and the client retries after three seconds.

## Shutdown boundary

`q` stops the terminal, loopback HTTP server, workers, and worker lock in order. Running work is
interrupted truthfully; queued and completed durable jobs are not silently cancelled or deleted.
The owner process reports a cleanup failure instead of claiming a clean exit while a server thread,
worker, or lock remains.

The Linux workflow is configured for Pilot/lifecycle tests and the base-wheel missing-extra command
paths; exact-head hosted CI is pending. The local Pilot gate was recorded on macOS. Windows terminal
use has not passed a clean platform gate. See [testing](testing.md) and [verification](verification.md)
for the exact evidence boundaries.
