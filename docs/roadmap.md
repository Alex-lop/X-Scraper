# Roadmap

This roadmap is evidence-gated and has no delivery dates. A feature moves to “proven” only when its
specific offline, local-browser, client, platform, or authorized live gate passes.

## Proven foundations

- Locked source installation on macOS Python 3.13.3, CLI/package/wheel checks, and Linux Python
  3.11-3.13 CI.
- Versioned SQLite snapshots, atomic Post/checkpoint persistence, protected migration, and local
  export/search boundaries.
- Production projection, bounded navigation/scrolling, deduplication, telemetry, and cleanup in
  real Chromium against sanitized local-only Home/profile/Latest-search routes.
- Real MCP 2.0 stdio discovery and representative reads in direct-SQLite and legacy loopback modes.
- Synthetic official X API request compilation and response mapping.
- A deterministic two-snapshot, 50-Post fictional demo proving Changes, search, export, and a real
  direct-SQLite MCP comparison without contacting X.
- Atomic 2-25-item saved-source batch approval, priority/FIFO/source-fair scheduling, bounded
  progress, durable recovery, isolated cancellation, and measured one-/two-worker cleanup against
  offline and local-Chromium fixtures. Two is a global mixed-provider ceiling; same-provider jobs
  remain serial.
- Deterministic pre-lease pause/recovery from coordinator-process RSS/CPU, with unsupported browser
  child-process and event-loop signals exposed truthfully rather than fabricated.
- Optional Textual owner operations for setup/capture/queue and an attachable read-only monitor,
  covered at normal, wide, and narrow terminal sizes. Advanced analysis remains web-first.
- Automated local-Chromium dashboard flow through preview, confirm, progress, cancellation,
  filtering, and JSON export, with every non-loopback request rejected.

None of those bullets is a live X, headed-authentication, Windows clean-install, or external-client
claim.

## Next gates

1. Complete the sanitized owner Home live gate, including restart repetition and cleanup record.
2. Separately verify profile and Latest-search live behavior if the owner chooses to enable those
   experimental surfaces; do not infer them from Home.
3. Add deliberate cache-reuse/refresh and an explicit retention UI around the proven storage
   primitives; never refresh or purge in the background.
4. Add Chromium child-process resource signals only if they can be sampled portably and tested
   without turning missing telemetry into permission to raise concurrency.
5. Verify current Codex MCP registration and calls; add other client snippets only after their own
   current primary-doc and end-to-end checks.
6. Record a clean Windows path and separately verify headed authentication on each supported
   desktop platform.
7. Perform an authorized official-API live gate only when the owner deliberately accepts access and
   billing implications.

## Deferred product work

Cross-snapshot enrichment, scheduling, alerts, Following/lists/thread expansion, write actions,
multi-account support, distributed workers, hosted multi-user access, and remote MCP are outside the
current recovery product. An optional enrichment adapter may later consume immutable snapshots, but
its failure must never change collection status.

## Completed isolated capability lab (not a product feature)

Six fixed synthetic mechanisms now pass locally inside `tests/capability_lab/`: ordinary
fingerprint observability, app-owned session replay, fixture GraphQL compatibility, fake-identity
lease state, a toy challenge, and actual non-forwarding loopback proxy transitions. Production
activation, external targets and inputs, imports, and wheel inclusion fail closed in the local
suite. The hardened Linux network/mount/PID-namespace job also passed all 31 items three times after
the dependent browser job.

This fixture proof creates no production support or authorization. Every analogous production
mechanism remains **prohibited and unreachable**, and the lab must remain absent from runtime entry
points and release wheels. See [the capability-lab disposition](capability-lab.md) for the exact
positive and negative tests.

The shipped queue decision and sanitized measurements are recorded in
[ADR 0002](adr/0002-bounded-capture-queue.md) and the
[production-reachable 2026-08-20 benchmark](benchmarks/reachable-mixed-provider-2026-08-20.json).
The [2026-08-19 artifact](benchmarks/queue-performance-2026-08-19.json) is retained as historical
isolated-runtime/auth-key evidence only.

See [verification](verification.md) for the claim matrix rather than treating this roadmap as a
statement that pending work exists.
