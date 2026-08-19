# Official X API provider

The optional official provider compiles profile or search requests for X API v2. Its compiler,
response mapping, bounds, preview validation, and persistence are tested with synthetic responses.
No authorized paid live request has been recorded for this repository.

## Configure access

Obtain access and manage billing in X's official developer surfaces, then save a token locally:

```bash
xworkbench configure
xworkbench doctor --require-token
xworkbench start
```

The masked prompt writes the token under `var/auth/` by default. An environment override exists,
but a protected file is less likely to leak through shell history or process configuration. Never
commit a token or paste it into diagnostics.

## Supported request contract

| Source | Search mode | Budget | Required input |
| --- | --- | --- | --- |
| Profile | recent or full archive | 10-500 Posts | handle or X profile URL |
| Search query | recent or full archive | 10-500 Posts | X query text |

Recent search uses a preview-safe rolling seven-day window. An explicitly older `startDate` is
rejected, except that the boundary calendar date is clamped to the valid instant. Full archive
requires inclusive start and end dates and uses the all-Posts endpoint. Automatic reply exclusion
and media-only filters are compiled after the user's query, with grouping retained around
user-authored OR expressions.

Every request first produces an exact five-minute preview. Job creation must return that unchanged
execution plan and set `confirmPaidRead: true`. A stale or modified plan is rejected. There is no
supported CLI shortcut that skips dashboard preview and confirmation.

The preview reports resource ceilings and list-price planning estimates. Those values are not an
invoice or billing guarantee: current prices, daily resource deduplication, account access, and the
hard spending limit come from X's Developer Console. Re-check them before approving a request.

## Results and stops

The mapper preserves available long-form text, identity, authors, timestamps, language,
conversation and reference relationships, public metrics, and media metadata. Missing or malformed
optional expansions become warnings or null values rather than fabricated data. Valid siblings can
survive a partial response.

Pages and resource counts are checkpointed with stored observations. A rate limit ends the current
job as partial or failed with its recorded reason; it is not retried or moved to another identity or
route. Credentials failure, schema drift, or an exhausted resource ceiling also stops truthfully.
Any later run is a new collection with a fresh preview and approval.

## Current official references

These primary sources were checked on 2026-08-19:

- [Search Posts introduction](https://docs.x.com/x-api/posts/search/introduction)
- [Recent search quickstart](https://docs.x.com/x-api/posts/search/quickstart/recent-search)
- [Full-archive search](https://docs.x.com/x-api/posts/search-all-posts)
- [Rate limits](https://docs.x.com/x-api/fundamentals/rate-limits)
- [Pay-per-use pricing](https://docs.x.com/x-api/getting-started/pricing)
- [Developer Console and app setup](https://docs.x.com/fundamentals/developer-portal)
- [X Developer Policy](https://docs.x.com/developer-terms/policy)

Official documentation and account entitlements can change. A green compiler test proves the local
request shape only; it does not establish current access, price, quota, policy compliance, or live
response compatibility. See [verification](verification.md) for the pending live row.
