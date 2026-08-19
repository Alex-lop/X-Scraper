# ADR 0002: Keep the capture queue local, durable, and small

Status: accepted on 2026-08-19

## Context

An approved batch needs durability, fair ordering, bounded concurrency, restart recovery, and
truthful cleanup. It does not need distributed scheduling. The existing SQLite job and snapshot
model already owns the lifecycle and evidence boundary, while the browser provider uses the
thread-affine synchronous Playwright API.

## Decision

Keep one SQLite-backed queue inside the modular monolith and reuse `jobs` instead of adding a
second task model. Admission is transactional. A row carries its immutable source/request
fingerprints, batch and idempotency identifiers, priority and enqueue sequence, exact approval and
limits, deadline, attempt, and lease fields. A bounded in-process scheduler wakes through a
`Condition`, selects priority first, preserves FIFO within a source, and rotates sources at equal
priority. SQLite remains authoritative across restarts.

Queue capacity applies backpressure at admission. Persistence callbacks are serialized and
synchronous, so a slow transaction blocks producers instead of accumulating Post objects. Leases
and heartbeats are transactional; an expired lease becomes a new attempt and capture segment.
Cancellation and terminal state precedence stay in the existing job lifecycle.
Progress uses a bounded, monotonically sequenced in-memory buffer; reconnecting readers recover
the final truth from SQLite even if an intermediate animation was coalesced or evicted.

Use one worker by default. Expose two as the normal opt-in maximum and enforce four as the internal
hard maximum. Keep per-source and per-auth-state concurrency at one. More than one worker requires
a factory that returns a distinct provider registry for every worker, preventing shared
thread-affine Playwright objects. Each browser capture owns its Playwright runtime, browser,
context, page, and app-owned auth-state file; cleanup happens in the provider's `finally` path.
Auth-state refreshes remain serialized and a stale capture cannot overwrite newer state.

Start with fixed bounds. The current governor stops at queue, worker, source, auth-state, and
serialized-persistence limits. It does not yet pause admission from sampled RSS, CPU, or event-loop
thresholds, and therefore must not report `resource_paused`. Add that state only with deterministic
threshold and recovery-window tests. Existing queue metrics expose depth, waits, workers, states,
throughput, persistence backlog, and cleanup; browser and operating-system measurements stay in
the local benchmark proof until their sampling lifecycle is production-safe.

## Evidence

The sanitized local benchmark is recorded in
[`queue-performance-2026-08-19.json`](../benchmarks/queue-performance-2026-08-19.json).

| Fixture | Workers | Jobs | Wall | Peak process-tree RSS | CPU | SQLite callback time | Cleanup |
|---|---:|---:|---:|---:|---:|---:|---:|
| Production Playwright, dynamic numeric-loopback page | 1 | 4 | 3.111s | 487.2MB | 3.218s | 0.009s | 0 failures |
| Production Playwright, dynamic numeric-loopback page | 2 | 4 | 1.288s | 871.7MB | 2.754s | 0.014s | 0 failures |

Two workers were 2.415 times faster on this fixture and produced the same stable Post IDs, zero
duplicate observations within a snapshot, zero external requests, zero remaining leases, and no
live pages, contexts, browsers, or worker threads. Peak process-tree RSS increased by 385MB. This
supports an explicit two-worker option on the measured machine but not a higher default. Static
local HTML is not evidence that live X should receive more concurrency.

Three consecutive 100-job lightweight drains used the real queue, leases, checkpoints, Post
persistence, and terminal transitions. All 300 jobs succeeded exactly once in 1.667s. The maximum
persistence backlog was two, leases and workers returned to zero after every drain, and RSS grew
884,736 bytes after warmup against a 33,554,432-byte tolerance. A concurrent reader completed
1,176 `queue_counts`/`list_jobs` calls while workers ran. The test also enforces global, source, and
auth-state caps and checks lock, thread, and temporary-state cleanup.

The browser matrix is opt-in so ordinary Python jobs do not need an installed Chromium:

```sh
pytest tests/test_queue_performance.py -k repeated_hundred
XWORKBENCH_RUN_BROWSER_MATRIX=1 pytest tests/test_queue_performance.py -k production_playwright
```

Both fixtures are synthetic and local. Browser navigation begins at the exact validated X plan
destination, is intercepted before egress, redirected to a numeric-loopback server, and aborts all
other requests. The test never contacts or load-tests X.

## Borrowed concepts, not dependencies

Scrapling informed fingerprinted requests, checkpoints, progress, and separation of scheduling
from collection. Scrapy informed bounded global/per-source concurrency, backpressure, scheduler
fairness, and statistics. Crawlee informed durable queue admission, isolated browser-worker
lifecycle, and recoverable state. Their crawler frameworks are not imported: the existing job,
provider, and SQLite boundaries already supply the required behavior. Stealth, proxy rotation,
fingerprint impersonation, block retries, session replacement, and multi-identity operation remain
out of scope.

## Consequences

The queue remains inspectable, recoverable, and operational without Redis, Celery, Kafka, or a
second crawler engine. SQLite serialization and per-capture browser startup cap throughput, but
those costs are acceptable for small, explicitly approved local batches. Reconsider the ownership
model only if production measurements show that two isolated runtimes cannot meet the bounded
batch goal without exceeding the documented memory ceiling.
