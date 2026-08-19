# Browser capture

Browser capture is an experimental, human-in-the-loop provider. Production projection, parsing,
scrolling, deduplication, bounded stalls, and cleanup run in real Chromium against sanitized local
fixtures. No test in this repository establishes that X's current live DOM matches those fixtures
or that X will accept a saved session.

## Supported request contract

| Surface | Destination construction | Budget | Evidence |
| --- | --- | --- | --- |
| Home | fixed `https://x.com/home` | 1-25 Posts; default 5 | Real local Chromium fixture |
| Profile | normalized handle, then fixed X profile URL | 1-25 Posts; default 5 | Derived destination in local Chromium; no live proof |
| Search | normalized text, then X's Latest-search URL | 1-25 Posts; default 5 | Derived destination in local Chromium; no live proof |

Profile inputs accept a handle or an exact X profile URL and reject reserved/non-profile paths.
Search accepts text, not a URL, normalizes whitespace and Unicode, and is capped at 512 characters.
The provider derives the destination itself; callers cannot substitute an arbitrary URL or alter a
previewed execution plan.

These surfaces remain experimental until an authorized owner records the bounded live gate in
[verification](verification.md). In particular, profile/search destination proof does not prove
that current X navigation, timelines, or result ordering work live.

## Create and verify an app-owned session

```bash
python -m playwright install chromium
xworkbench auth
xworkbench doctor
```

`auth` launches fresh headed Chromium at X's normal sign-in flow. Enter credentials only in that
browser. X-Scraper never requests your password, reads an existing Chrome profile, imports cookies,
or automates a challenge. It saves filtered storage state only after the expected private X surface
is observed.

The state file and its SHA-bound status marker are owner-private local files. Readiness requires
`verified_live`; file presence alone is `present_unverified`, while malformed, stale, expired,
manual-action, and unavailable states are not ready. A capture refuses state that changed after its
preview. The digest compare-and-swap protects one local process flow, not a future multi-process
coordination protocol.

## Run a bounded capture

```bash
xworkbench start
```

In the loopback dashboard:

1. Select Browser capture and a supported source.
2. Choose 1-25 Posts.
3. Preview the exact normalized source, derived destination, provider version, and budget.
4. Confirm the Browser capture, then keep headed Chromium visible.
5. Inspect the terminal or partial snapshot rather than requesting the source again.

Concrete single-source example: select **Browser capture → Home**, set **Maximum Posts** to `5`,
and preview. Confirm only if **Source** is `https://x.com/home` and **Target** is `5 visible Posts
maximum`; otherwise edit or stop.

There is no standalone `xworkbench capture` or batch command; do not copy examples that imply one
exists. The dashboard preview-and-confirm path is the maintained interface.

For a bounded batch, first save each Browser or official-API source through the single-capture
form. Then, in **Capture several saved sources**:

1. Select 2-25 distinct saved sources and enter the provider-specific budget, a 60-3600-second
   deadline, and priority 0-100.
2. Keep the only supported freshness choice, `capture_fresh`; batch reuse is not implicit.
3. Preview the server-canonical source fingerprints, destinations, budgets, route, concurrency,
   and expiry without collecting or adding queue work.
4. Confirm the identical manifest. All jobs are admitted atomically or none are admitted.
5. Follow bounded progress; durable job state remains authoritative if intermediate events were
   coalesced. Cancelling the remaining batch does not alter completed snapshots.

The approval digest uses a process-local secret and expires quickly, so a restart requires a new
preview. Scheduling favors higher priority; at equal priority it preserves FIFO and rotates
sources. It permits only one active capture per logical source and auth state. The configured
default is one worker; two isolated workers are the normal opt-in maximum. These are offline queue
and local-fixture claims, not permission or evidence that concurrent live X capture succeeds.

Concrete batch example: after saving two distinct Browser sources you are authorized to capture,
select exactly those two, set **Browser Posts per source** to `5`, **Deadline** to `600`, **Priority**
to `0`, and keep `capture_fresh`. Preview, compare both rows to those values and destinations, then
confirm only the unchanged manifest. Because there is no capture CLI, these exact UI sequences
replace fictional copy-paste commands.

## Collection behavior

Each scan runs the production DOM projection over visible outer Post articles. It prefers the outer
status identity, normalizes supported compact metric labels, preserves quote/repost/reply/media
flags where evidence exists, and leaves unknown fields null. It never turns a missing metric into
zero. Duplicate canonical Post IDs are not inserted twice.

The provider persists each accepted batch and its checkpoint together. Telemetry includes scan and
scroll counts, capture segment, timing, visible/parsed/skipped/duplicate counts, field coverage,
stop reason, and a bounded sanitized selector-drift report. A snapshot may stop at the exact target,
no progress, cancellation, timeout, session expiry, manual action, rate limit, DOM failure, or an
unexpected browser failure. Already committed rows remain available with truthful partial state.

The progress wait polls for cancellation at 100 ms intervals. Launch and navigation have bounded
timeouts and checkpoints, but synchronous Playwright native calls cannot be interrupted in the
middle of one call. Cleanup tests prove page, context, browser, and manager closure; they are not an
all-platform guarantee. The separate opt-in queue benchmark also recorded process-tree cleanup on
one macOS machine; see [testing](testing.md).

## What the local Chromium tests prove

Sanitized fixtures cover original, reply, repost, quote and nested quote, media-only,
promoted-like, hidden, missing-field, compact-metric, deliberate selector-drift, delayed insertion,
virtualized replacement, and duplicate-card shapes. Route interception fulfills only the expected
local test navigation and aborts unexpected network requests.

They do not prove live login, session longevity, current X selectors, current profile/search
behavior, result completeness, anti-automation outcomes, or permission. The owner must stop at any
login, challenge, rate-limit, or account-access screen; no bypass behavior belongs in this project.

For the exact test record and the pending sanitized owner gate, see
[verification](verification.md). For operational restrictions, read
[responsible use](responsible-use.md).
