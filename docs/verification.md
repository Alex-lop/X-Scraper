# Verification record

Checked: **2026-08-19**. Audited starting SHA:
`ab93ba7f02c9c82fff87d82be583090fa8a6c1f5`. The reproducible-lock milestone
inspected separately was `a3196ca050da9b4e6f6255058719bfdacfec408e`; Phase 0 evidence was reviewed
through local commit `df8fca5`.

No live X request was made for this record. A green offline or loopback test does not prove that
X will accept a session, that its current DOM matches a fixture, or that a live capture succeeds.

## Status meanings

| Status | Meaning |
| --- | --- |
| `proven offline` | Automated or recorded local evidence passed without contacting X. |
| `proven local Chromium` | Production browser code passed in real Chromium against sanitized local-only fixtures. It still says nothing about live X. |
| `owner live-verified` | An authorized owner completed the bounded live gate and recorded only sanitized results. |
| `not yet verified` | The required evidence is absent, incomplete, or the capability is not implemented. |

## Claim matrix

| Claim | Status | Evidence and limit |
| --- | --- | --- |
| Locked clean install, package import, CLI help, and wheel contents work on Python 3.13 | `proven offline` | A fresh archived `a3196ca` checkout passed the commands below. `pip check` passed; the wheel contained all 13 Python modules and four static assets and excluded tests. Commit `6c46702` then pinned `setuptools==84.0.0` and recorded uv 0.11.29; Playwright 1.62.0 and MCP 2.0.0 are pinned, but the lock still has no artifact hashes. |
| Ordinary Python, JavaScript, lint, and build gates pass | `proven offline` | At `df8fca5`: Ruff passed, `66 passed`, JavaScript syntax and `3/3` tests passed. The archived `a3196ca` clean install also passed both help commands and wheel build; baseline GitHub Actions run [31074707953](https://github.com/Alex-lop/X-Scraper/actions/runs/31074707953) was green. No post-`a3196ca` remote Actions result is claimed here. |
| SQLite jobs, immediate batch/checkpoint persistence, restart recovery, partial-result retention, protected v1-to-v2 backup, and incompatible-schema rejection | `proven offline` | `tests/test_storage.py`, `tests/test_jobs.py`, and `tests/test_adversarial_recovery.py`. This does not prove recovery during a real X session. |
| Provider-neutral API surfaces, exact preview confirmation, bounded Browser Home requests, tested loopback rejection/public-field filtering, and basic CSV formula protection | `proven offline` | `tests/test_api.py`, `tests/test_interfaces.py`, and `tests/test_models.py`. Browser behavior in these tests uses fakes; nested-object allowlisting and invisible/control CSV prefixes need more adversarial coverage. |
| Browser article normalization, deduplication, partial results, cancellation/timeout cleanup, and auth-state file permissions | `proven offline` | `tests/test_playwright_browser.py` uses Python-reconstructed projections plus fake pages/lifecycles; it does not execute `DOM_PROJECTION` in Chromium. |
| Production `DOM_PROJECTION` and scrolling against static and delayed/virtualized local pages in real Chromium | `proven local Chromium` | Commit `df8fca5`, `tests/test_playwright_integration.py`: three tests passed repeatedly in Chromium. Sanitized cards cover original, reply, repost, quote/nested quote, media-only, promoted-like, hidden, missing-field, compact-label, and deliberate-drift shapes. The dynamic fixture proves exact target, cross-scan deduplication, bounded stall, and page/context/browser cleanup with all navigation fulfilled locally. Compact labels reach the projection, but compact-number normalization, orphan-process accounting, and live X remain unverified. |
| Read-only MCP bounds, numeric-loopback REST access, tested top-level secret exclusion, and untrusted-content labeling | `proven offline` | `tests/test_mcp.py` unit coverage. The registration unit test uses a fake server; nested `media` allowlisting and `localhost` normalization remain gaps. |
| MCP 2.0 stdio initialize/discovery, tool/resource discovery, representative calls, JSON-RPC stdout, and clean client shutdown with the real SDK | `proven offline` | `test_real_mcp_v2_stdio_modern_and_legacy_round_trip` passed against real Flask/SQLite state: MCP 2.0.0 auto (`2026-07-28`) and legacy (`2025-11-25`) modes, five tools, one resource template, tool/resource reads, JSON-RPC-only stdout, empty stderr, and clean subprocess exits. It does not prove live X, external client configuration, or the later Phase 3 schema. |
| Official X API query compilation, response mapping, resource ceilings, and paid-read confirmation | `proven offline` | `tests/test_models.py` and `tests/test_provider.py` use synthetic responses. No paid or live API request was made. |
| Live headed Home capture on a fresh install and after restart | `not yet verified` | The owner gate below is pending. Do not describe Browser capture as live-proven. |
| Truthful local auth-state validation and rejection before preview/queue | `not yet verified` | The 2026-08-19 audit reproduced malformed nonempty state being reported `ready`; missing/corrupt status metadata also defaults to `ready`. |
| A saved browser state is currently accepted by X | `not yet verified` | File presence and local JSON validity cannot establish a live session. Login, expiry, challenge, rate-limit, and DOM drift remain live outcomes. |
| Live official X API collection | `not yet verified` | No authorized paid-read record exists. |
| Playwright profile/topic capture, cross-snapshot change analysis, bounded concurrent batches, realistic two-snapshot demo, and the isolated capability lab | `not yet verified` | These later-phase capabilities are not established by the current Phase 0 evidence; the capability lab remains blocked safely until Phases 0–5 and hard isolation pass. |
| Codex, Claude/Claude Code, or Cursor MCP registration | `not yet verified` | No client-specific configuration and end-to-end record exists yet. |

### Recorded clean-install commands

The following sequence exited 0 in a fresh Python 3.13.3 virtual environment from an archived
`a3196ca` checkout; Chromium 151.0.7922.34 also launched and rendered local `set_content` HTML:

```bash
python -m pip install -r requirements.lock
python -m pip install -e . --no-deps
python -m playwright install chromium
python -m pip check
ruff check .
pytest
node --check xworkbench/static/app.js
node --test tests/test_analysis.mjs
xworkbench --help
python -m xworkbench --help
python -m pip wheel . --no-deps --wheel-dir dist
```

## Pending owner live gate

There are **no `owner live-verified` claims yet**. An owner with appropriate authorization must:

1. Install from the lock in a clean environment, run visible `xworkbench auth`, then confirm the
   local readiness checks without sharing the storage-state file.
2. Run `xworkbench live-smoke --confirm-live-x` three times, each requesting at most two visible
   Home Posts. Stop on login, challenge, rate limit, or other manual-action state; do not bypass it.
3. Restart the process, then repeat the same bounded smoke three times.
4. Record only date, app commit, platform/browser version, status, stored count, completion reason,
   warnings/error codes, and cleanup result. Never record Post text, cookies, tokens, screenshots
   containing content, or the storage-state file.

Until all six runs and cleanup are recorded truthfully, the live rows above remain
`not yet verified`.

## Upstream architecture references

`git ls-remote` confirmed these exact branch heads on 2026-08-19. They are research references,
not dependencies; this verification pass copied no upstream code.

| Project and exact revision | Primary documentation checked | Safe takeaway; excluded behavior |
| --- | --- | --- |
| [Scrapling v0.4.14, `5d213a2d4764002bfc4fed33c32fe09fa8b0bf7f`](https://github.com/D4Vinci/Scrapling/tree/5d213a2d4764002bfc4fed33c32fe09fa8b0bf7f) | [Spider architecture](https://github.com/D4Vinci/Scrapling/blob/5d213a2d4764002bfc4fed33c32fe09fa8b0bf7f/docs/spiders/architecture.md), [advanced guide](https://github.com/D4Vinci/Scrapling/blob/5d213a2d4764002bfc4fed33c32fe09fa8b0bf7f/docs/spiders/advanced.md) | Useful neutral patterns: fingerprints, bounded scheduling, atomic checkpoints, streaming progress, replay, statistics, exports. Excluded: stealth fetching, impersonation, block retries, proxy/identity rotation. |
| [Scweet 5.3.1, `c42e1222c632dbfeb5ae91633f426a6bd44a677a`](https://github.com/Altimis/Scweet/tree/c42e1222c632dbfeb5ae91633f426a6bd44a677a) | [v5 documentation](https://github.com/Altimis/Scweet/blob/c42e1222c632dbfeb5ae91633f426a6bd44a677a/DOCUMENTATION.md) | Useful neutral patterns: explicit limits, structured inputs, sync/async entry points, parameter-hash resume, WAL/busy timeout, run history, stable schemas. Excluded: private GraphQL, cookie-token extraction, account pools/switching, per-account proxies. |
| [Scrapy 2.17.0, `685abd6dbb87ea09564ebaf32aea95879ca21d31`](https://github.com/scrapy/scrapy/tree/685abd6dbb87ea09564ebaf32aea95879ca21d31) | [architecture](https://github.com/scrapy/scrapy/blob/685abd6dbb87ea09564ebaf32aea95879ca21d31/docs/topics/architecture.rst), [scheduler](https://github.com/scrapy/scrapy/blob/685abd6dbb87ea09564ebaf32aea95879ca21d31/docs/topics/scheduler.rst), [jobs](https://github.com/scrapy/scrapy/blob/685abd6dbb87ea09564ebaf32aea95879ca21d31/docs/topics/jobs.rst), [stats](https://github.com/scrapy/scrapy/blob/685abd6dbb87ea09564ebaf32aea95879ca21d31/docs/topics/stats.rst) | Useful neutral patterns: global/per-domain caps, priority and deduplication, backpressure, classified retry statistics, graceful persistence. For X, 401/403/429, login, or challenge must stop/cool down rather than change identity or route. |
| [Crawlee Python 1.9.3, `c5bb9133fbb8c7c36d48e8348fa1ccbf9d8eae43`](https://github.com/apify/crawlee-python/tree/c5bb9133fbb8c7c36d48e8348fa1ccbf9d8eae43) | [storages](https://github.com/apify/crawlee-python/blob/c5bb9133fbb8c7c36d48e8348fa1ccbf9d8eae43/docs/guides/storages.mdx), [request routing](https://github.com/apify/crawlee-python/blob/c5bb9133fbb8c7c36d48e8348fa1ccbf9d8eae43/docs/guides/request_router.mdx), [scaling](https://github.com/apify/crawlee-python/blob/c5bb9133fbb8c7c36d48e8348fa1ccbf9d8eae43/docs/guides/scaling_crawlers.mdx), [quick start](https://github.com/apify/crawlee-python/blob/c5bb9133fbb8c7c36d48e8348fa1ccbf9d8eae43/docs/quick-start/index.mdx) | Useful neutral patterns: persistent queues, typed routing, structured datasets, resource-aware admission, browser lifecycle, guided setup. Excluded: fingerprint generation, proxy rotation, and block-driven session replacement. |

## Current policy sources

These sources were checked on 2026-08-19; this is a product boundary, not legal advice.

| Official source | What it establishes for this project |
| --- | --- |
| [X Terms of Service](https://x.com/en/tos) | X states that scraping in any form or for any purpose without prior written consent is prohibited, and prohibits working around technical limitations or security/authentication measures. Personal, research, or noncommercial intent is not permission. |
| [X Developer Policy](https://docs.x.com/developer-terms/policy) | Official API/X Content use remains subject to the approved use case, rate and distribution limits, privacy/content rules, credential protection, and a prohibition on circumventing limits. |
| [DSA Article 40](https://eur-lex.europa.eu/eli/reg/2022/2065/oj), [European Commission delegated act](https://digital-strategy.ec.europa.eu/en/library/delegated-act-data-access-under-digital-services-act-dsa), and [DSA Data Access Portal FAQ](https://data-access.dsa.ec.europa.eu/public/hns/faq) | Qualified researchers have a formal, application-based data-access route for systemic-risk research. Eligibility and a reasoned request are required; it is not a general permission for personal/noncommercial scraping or bypass. |
