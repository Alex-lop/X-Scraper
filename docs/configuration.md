# Configuration

The single configuration surface is an owner-private JSON file. By default it is
`var/config.json`; set `XWORKBENCH_RUNTIME_DIR` to move the whole runtime or
`XWORKBENCH_CONFIG_PATH` to select a different file.

Precedence is documented environment variable, then JSON value, then default. Unknown keys,
invalid types, and out-of-range values fail with a configuration error. `config show` prints only
paths and non-secret values; it never prints a Bearer Token or browser-state contents.

```bash
xworkbench config show
xworkbench config validate
```

## JSON keys

| Key | Default | Allowed value | Environment override |
| --- | --- | --- | --- |
| `db_path` | `var/x_collection_workbench.db` | filesystem path | `XWORKBENCH_DB_PATH` |
| `x_bearer_token_path` | `var/auth/x_bearer_token` | filesystem path | `XWORKBENCH_X_BEARER_TOKEN_PATH` |
| `storage_state_path` | `var/auth/playwright_state.json` | filesystem path | `XWORKBENCH_STORAGE_STATE_PATH` |
| `browser_headless` | `false` | JSON boolean; env `0`/`1` | `XWORKBENCH_BROWSER_HEADLESS` |
| `job_timeout_seconds` | `120` | integer 1-3600 | `XWORKBENCH_JOB_TIMEOUT_SECONDS` |
| `page_timeout_ms` | `30000` | integer 100-300000 | `XWORKBENCH_PAGE_TIMEOUT_MS` |
| `no_progress_limit` | `3` | integer 1-100 | `XWORKBENCH_NO_PROGRESS_LIMIT` |
| `max_workers` | `1` | integer 1-2 | `XWORKBENCH_MAX_WORKERS` |
| `queue_capacity` | `100` | integer 1-10000 | `XWORKBENCH_QUEUE_CAPACITY` |
| `resource_max_rss_mb` | `1536` | integer 128-131072 | `XWORKBENCH_RESOURCE_MAX_RSS_MB` |
| `resource_max_cpu_percent` | `300` | integer 1-1000 | `XWORKBENCH_RESOURCE_MAX_CPU_PERCENT` |
| `resource_recovery_seconds` | `5` | integer 1-300 | `XWORKBENCH_RESOURCE_RECOVERY_SECONDS` |
| `retention_keep_per_source` | `10` | integer 1-100 | `XWORKBENCH_RETENTION_KEEP_PER_SOURCE` |
| `snapshot_stale_after_seconds` | `86400` | integer 0-315360000 | `XWORKBENCH_SNAPSHOT_STALE_AFTER_SECONDS` |

`XWORKBENCH_X_BEARER_TOKEN` is the only direct secret override. Prefer the protected token file for
interactive use, and never place the token in a committed shell script or issue report.

An equivalent minimal file is:

```json
{
  "browser_headless": false,
  "job_timeout_seconds": 120,
  "page_timeout_ms": 30000,
  "no_progress_limit": 3,
  "max_workers": 1,
  "queue_capacity": 100,
  "resource_max_rss_mb": 1536,
  "resource_max_cpu_percent": 300,
  "resource_recovery_seconds": 5,
  "retention_keep_per_source": 10,
  "snapshot_stale_after_seconds": 86400
}
```

`xworkbench setup` creates that file only when it is absent; it does not overwrite an existing
configuration. With two workers, each worker receives its own provider registry. Scheduling still
caps one active job per logical source and per auth state. The durable queue accepts either one
approved capture or an atomically admitted UI batch of 2-25 saved sources; there is no public
capture or batch CLI.

`config show` also reports fixed read-only boundaries: `per_source_concurrency: 1`,
`per_auth_state_concurrency: 1`, internal `hard_worker_maximum: 4`, and `route_mode: direct`. The
user-facing worker setting remains capped at 2. `route_mode` is not a proxy control.

Before a new durable lease starts, the coordinator samples its own process RSS and CPU at most once
per 250 ms. Crossing either configured high threshold pauses only new starts; active captures keep
their existing deadline and queued rows do not acquire a new attempt. Starts resume after both
signals remain supported and below threshold continuously for the configured recovery window. A
probe failure while paused resets that window.

The default macOS/Linux probe supports coordinator-process RSS and CPU only. It does not measure
Chromium child-process memory, browser/context/page counts, or an event loop; those metrics are
truthfully `unsupported` or `not_applicable`. Queue metrics expose `resourcePaused`, reasons,
limits, failures, and at most 10 recent samples. The separate browser benchmark measures the whole
process tree; do not treat the coordinator RSS limit as a total browser-memory ceiling. On other
platforms the process signals remain unsupported and the fixed queue/concurrency caps still apply.

## File safety

On POSIX systems, app-owned directories must be mode `0700` and protected files mode `0600`.
Symlinks, non-regular files, wrong ownership, and permissive modes are rejected. Existing unsafe
parents are not silently re-permissioned. Browser storage state must remain inside the `auth/`
directory next to the selected configuration file.

Windows does not implement POSIX modes, so those numeric guarantees do not apply. Paths and regular
file checks remain relevant, but the Windows command path has not passed this project's clean
platform gate. Apply suitable NTFS access controls yourself and keep the runtime local to your
account.

## What is not configurable

The dashboard and MCP bridge remain loopback-only. There is no proxy rotation, stealth or
fingerprint control, challenge solver, unrelated-profile import, account pool, remote bind,
scheduled retention, or scheduled collection setting. Retention runs only after an explicit
confirmed local purge request. A configuration file cannot expand provider limits or bypass the
preview and confirmation step.
