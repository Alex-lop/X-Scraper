# ADR 0001: Recover the feed-to-context product

Status: accepted on 2026-08-05

## Context

The current application has durable SQLite snapshots, safe local serving, incremental
persistence, recovery, exports, and an official X API collector. The product must again make a
small, user-approved Home-feed capture in a real Playwright browser the default path, without
restoring the historical private GraphQL replay implementation.

## Decision

Keep the current job and snapshot infrastructure and put providers behind this frozen contract:

```python
class CollectionProvider(Protocol):
    provider_id: ProviderType
    provider_version: int

    def capabilities(self) -> dict: ...
    def connection_status(self) -> dict: ...
    def prepare(self, request, supplied_plan=None) -> dict: ...
    def collect(
        self, request, *, execution_plan, checkpoint, on_batch, should_cancel
    ) -> CollectionSummary: ...
```

`prepare(request)` creates a safe execution plan. `prepare(request, supplied_plan)` validates and
returns that exact plan for confirmation/resume; it never silently replaces it. The registry owns
provider selection. `JobService` owns state transitions only and imports no provider compiler,
pricing, endpoint, or resource-budget logic.

On every claim, storage rebuilds the checkpoint from durable state:

```json
{"providerState": null, "storedCount": 0, "metadata": {}}
```

`providerState` is opaque and private. `storedCount` always comes from SQLite. `on_batch(posts,
provider_state, metadata)` atomically persists posts, the next provider state, warnings, and
provider metadata before returning the number added.

Provider IDs are `playwright_browser` and `official_x_api`. Missing provider data and the legacy
`x_api_search` plan ID are interpreted as `official_x_api` without rewriting old JSON. New browser
requests are explicit, support only `home`, default to 5 Posts, and accept 1–25. Official API
profile/search requests retain their 10–500 limits, recent/full-archive compilation, preview
expiry, resource ceiling, and paid-read confirmation.

Keep the three-table schema and reuse `request_json`, `compiled_request_json`, and `cursor` as the
request, execution plan, and provider state. Migrate only an exact current v1 database to v2: make
Post text, classification flags, and media nullable, create a 0600 SQLite backup before the first
write, and preserve rows, positions, indexes, foreign keys, and WAL behavior. Unrelated historical
schemas remain rejected.

The Playwright provider uses headed Chromium, an app-owned storage-state file, normal X navigation,
visible article DOM projection, immediate per-scan persistence, bounded scrolling, and truthful
session/challenge/failure states. It never replays GraphQL, extracts cookies, intercepts private
payloads, uses stealth/proxies, solves challenges, or performs write actions. Home-feed resume is
best-effort and deduplicates against the durable snapshot.

Public API, UI, exports, and MCP use allowlisted provider-neutral provenance. API cost/resource/rate
fields appear only for `official_x_api`; browser scan/scroll/observation fields appear only for
`playwright_browser`. Snapshot inspection never auto-loads remote media. MCP is optional, local,
bounded, read-only, terminal-snapshot-only, and exposes no collection, credentials, provider state,
arbitrary URLs, or write operations. Post text is labeled untrusted external content.

## Implementation sequence

1. Generalize models, storage, registry, jobs, and the existing official provider.
2. Add manual auth and the Home DOM provider with synthetic offline fixtures.
3. Make API/CLI/dashboard/exports provider-aware and restore GET-only MCP.
4. Run an adversarial regression pass, then lint, Python/JavaScript tests, doctor, and wheel checks.

## Deferred

Playwright profile/search/following capture, threads, replies, lists, enrichment, scheduling,
distributed workers, hosted mode, and agent-triggered collection remain out of scope.
