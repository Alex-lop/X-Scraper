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

Use one worker by default. Expose two as the global mixed-provider opt-in maximum and enforce four
as the internal hard maximum. Keep per-source and per-auth-state concurrency at one. Public route
admission derives one auth key per provider, so Browser+Browser and official+official remain serial;
only Browser+official can occupy both workers. Auth identifiers are not public inputs. More than one
worker requires a factory that returns a distinct provider registry for every worker, preventing
shared thread-affine Playwright objects. Each browser capture owns its Playwright runtime, browser,
context, page, and app-owned auth-state file; cleanup happens in the provider's `finally` path.
Auth-state refreshes remain serialized and a stale capture cannot overwrite newer state.

Combine fixed queue, worker, source, auth-state, and serialized-persistence bounds with a small
pre-lease resource governor. At most every 250 ms it samples the coordinator process's RSS and CPU;
defaults are 1536 MiB and 300%. Crossing either threshold pauses new leases but never interrupts an
active capture. Resume requires both signals to remain supported and below threshold for five
seconds; probe failure while paused restarts that window. Metrics retain at most 10 samples and
expose pause reasons, failures, thresholds, and signal status.

The default probe supports process RSS/CPU on macOS and Linux. Chromium process/context/page counts
remain unsupported, and synchronous workers have no applicable event-loop-lag signal. The governor
therefore complements rather than replaces the process-tree benchmark and must not be described as
a whole-browser memory ceiling.

## Evidence

The production-reachable local matrix is recorded in
[`reachable-mixed-provider-2026-08-20.json`](../benchmarks/reachable-mixed-provider-2026-08-20.json).
Each case used real saved-source creation/listing and batch preview/confirm, SQLite admission and
leases, one production Playwright provider against a loopback fixture, and one production official
provider with an in-memory synthetic transport. Three paired repetitions ran in alternating
`AB/BA/AB` order. The middle pair reverses order, but with only three pairs residual order and
warm-cache bias remain. Each run recorded both absolute peak process-tree RSS and the increment
above a baseline sampled immediately before the case started. Wall timing excluded sampler startup
and shutdown. CPU combined the coordinator `RUSAGE_SELF` delta after subtracting sampler-thread
`thread_time` with the maximum cumulative CPU observed for each descendant PID in the same `ps`
snapshots. Observer `ps` PIDs were excluded; `RUSAGE_CHILDREN` was not used.

| Fixture | Workers | Jobs/run | Median wall | Median process-tree RSS | Median CPU | Median SQLite fraction | Cleanup |
|---|---:|---:|---:|---:|---:|---:|---:|
| Route-admitted Browser + synthetic official | 1 | 2 | 1.194s | 468.4MB | 0.462166s | 0.3214% | 0 failures |
| Route-admitted Browser + synthetic official | 2 | 2 | 0.591s | 499.6MB | 0.481773s | 0.7398% | 0 failures |

The two-worker median was 2.020× as fast. CPU grew by about 4.2%, below the 25% ceiling. Median
baseline-adjusted process-tree RSS was `397,606,912` bytes with one worker and `428,228,608` bytes
with two, so the two-worker increment was `30,621,696` bytes higher and remained within the 128MiB
ceiling. SQLite callback time remained below 20% of wall time, backlog stayed at one, the stable state
digest matched, and exact results, duplicate, lease, thread, Chromium-descendant, cleanup, and
zero-egress gates passed. This retains the global maximum of two. It does not permit two Browser or
two official jobs to run together and does not support a live-X speed claim.

The earlier [`queue-performance-2026-08-19.json`](../benchmarks/queue-performance-2026-08-19.json)
is preserved with its raw `3.111s`, `1.288s`, and 2.415x values. That direct-submit matrix injected
one synthetic auth key per Browser job and bypassed production admission. It is isolated
runtime/auth-key and cleanup evidence only; its former production interpretation was incorrect.

Three consecutive 100-job lightweight drains used the real queue, leases, checkpoints, Post
persistence, and terminal transitions. All 300 jobs succeeded exactly once in 1.667s. The maximum
persistence backlog was two, leases and workers returned to zero after every drain, and RSS grew
884,736 bytes after warmup against a 33,554,432-byte tolerance. A concurrent reader completed
1,176 `queue_counts`/`list_jobs` calls while workers ran. The test also enforces global, source, and
auth-state caps and checks lock, thread, and temporary-state cleanup.

The mixed-provider matrix is opt-in so ordinary Python jobs do not need installed Chromium. CI
asserts topology/correctness/cleanup only; timing and resource decision thresholds are a separate
local opt-in:

```sh
pytest tests/test_queue_performance.py -k repeated_hundred
XWORKBENCH_RUN_BROWSER_MATRIX=1 pytest tests/test_queue_performance.py -k production_playwright
XWORKBENCH_RUN_BROWSER_MATRIX=1 XWORKBENCH_ASSERT_SCALE_THRESHOLDS=1 \
  pytest tests/test_queue_performance.py -k production_playwright
```

All fixtures are synthetic and local. Browser navigation begins at the exact validated X plan
destination, is intercepted before egress, redirected to a numeric-loopback server, and aborts all
other requests. The official transport returns in-memory sanitized JSON. The test never contacts or
load-tests X.

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
model only if a production-reachable mixed workload misses the declared speed, CPU, RSS,
persistence, backlog, correctness, cleanup, or zero-egress gates. Same-provider scaling would need
a separately authorized identity model and is not implied by this decision.
