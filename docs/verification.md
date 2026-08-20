# Verification record

Checked: **2026-08-20**. Audit work began at
`ab93ba7f02c9c82fff87d82be583090fa8a6c1f5`. The archived reproducible-lock
milestone was `a3196ca050da9b4e6f6255058719bfdacfec408e`; current local evidence was
recorded through `c84369024a439961c3742f9e66a3ca9a2346166a`, and the final pre-documentation
implementation revision is `1bd21ea9d0c7766b9440b65891569260ed92e5f3`.

No live X request or paid API request was made for this record. Local fixtures cannot establish
that X accepts a session, that its current DOM matches a fixture, or that a live capture succeeds.

## Status meanings

| Status | Meaning |
| --- | --- |
| `proven offline` | Automated or recorded local evidence passed without contacting X. |
| `proven local Chromium` | Production or test-only code ran in real Chromium against sanitized local-only fixtures; live X is still unproven. |
| `owner live-verified` | An authorized owner completed the exact bounded live gate and retained only sanitized results. |
| `not yet verified` | The required evidence is absent, incomplete, platform-specific, or still pending. |

## Claim matrix

| Claim | Status | Evidence and boundary |
| --- | --- | --- |
| Locked clean install, import, help entry points, wheel, and local Chromium launch on macOS Python 3.13.3 | `proven offline` | A fresh archived `a3196ca` checkout passed the sequence below with Chromium 151.0.7922.34. Commit `6c46702` then pinned `setuptools==84.0.0`; uv 0.11.29 generated the 46-package universal lock, including Playwright 1.62.0 and MCP 2.0.0. The lock has no artifact hashes. |
| Current ordinary Python, JavaScript, lint, CLI, and wheel gates | `proven offline` | At the final implementation, the normal full suite reported `200 passed, 32 skipped`; 31 skips are the gated capability lab and one is the opt-in browser matrix. `pip check`, Ruff, both help commands, wheel build/inspection, and all `6/6` JavaScript tests passed. The fresh wheel excluded tests and the lab. |
| Current Linux Python 3.11-3.13 CI, local-Chromium job, and Linux no-egress capability-lab job | `proven offline` | [Run 32230574720](https://github.com/Alex-lop/X-Scraper/actions/runs/32230574720) passed at `1bd21ea`: all three locked Python jobs, the 43-second dependent browser job, and the 1m13s capability-lab job. The workflow uses current `actions/*@v7`; no X credentials or live target are supplied. Baseline run [31074707953](https://github.com/Alex-lop/X-Scraper/actions/runs/31074707953) was also green at the starting revision. |
| Guided setup, strict standalone doctor, configuration validation/redaction, and loopback start/demo behavior | `proven offline` | CLI/config/API tests cover idempotent setup, unknown/invalid settings, protected paths, fixed concurrency/route fields, and command help. Missing Chromium is a warning for `setup`/offline demo, while standalone `doctor` and real capture remain strict. |
| Schema-v4 SQLite snapshots, protected v1-v3 migrations, rollback/corruption rejection, FTS search, bounded Changes/export, explicit retention, and connection cleanup | `proven offline` | Storage, interface, API-security, read-service, and adversarial suites use real SQLite. Snapshot freshness is descriptive; collection reuse, refresh, and purge are never automatic. |
| Durable single jobs and atomic 2-25-source batches, priority/FIFO/source fairness, idempotency, restart recovery, progress gaps, cancellation, and per-source/auth cap 1 | `proven offline` | `tests/test_route_concurrency.py` drives real source creation/listing and batch preview/confirm with SQLite and two workers. Browser+Browser and official+official each peak at one collector with the second job queued/unleased; Browser+official peaks at two with distinct provider auth keys. Resume reuses the durable source/auth keys. Other queue/storage/API tests cover leases and terminal precedence. |
| Pre-lease RSS/CPU pause/recovery and bounded queue metrics | `proven offline` | Injected-signal tests prove new leases pause without interrupting active work and resume after both supported signals stay low for 5 seconds. The default probe samples only coordinator-process RSS/CPU; Chromium child counts are unsupported and synchronous event-loop lag is not applicable. |
| One-/two-worker production-reachable mixed-provider performance against local fixtures | `proven local Chromium` | The 2026-08-20 three-run artifact uses the real route, SQLite, Playwright Browser, and synthetic official transport. Medians were `1.081s` versus `0.603s` (`1.793x`); CPU, incremental RSS, SQLite fraction, backlog, correctness, cleanup, and zero-egress gates passed. This supports a global maximum two only; same-provider work remains serial and no live-X speed claim follows. |
| Historical isolated Browser-runtime matrix | `proven local Chromium` | The preserved 2026-08-19 artifact records the raw 2.415x result, but direct submission injected a unique synthetic auth key per Browser job and bypassed production admission. It proves isolated runtime cleanup only, not production Browser concurrency. |
| Production Browser projection, profile/Latest-search destination derivation, target/stall behavior, deduplication, drift telemetry, cancellation, and lifecycle cleanup | `proven local Chromium` | Five real-Chromium integration cases plus browser/hardening/adversarial tests use static, delayed, and virtualized sanitized fixtures. Expected navigation is fulfilled locally and unexpected requests abort. Page/context/browser closure is asserted; current X selectors, acceptance, ordering, and completeness are not. |
| Local browser-state validation, private permissions, digest-bound readiness, and refusal of missing/malformed/stale/unverified state | `proven offline` | Browser hardening tests cover fail-closed state/status transitions and provider-versus-persistence errors. They do not prove a headed X sign-in or that a saved state is currently accepted by X. |
| Official X API compilation, recent/full-window bounds, paid-read confirmation, mapping, pagination/resource ceilings, and terminal rate-limit handling | `proven offline` | Provider/model/API tests use synthetic responses. A rate limit ends the current job as partial or failed and nonretryable; any later attempt is a new approved capture, not checkpoint resume. No entitlement, price, quota, or live response is proven. |
| Direct read-only SQLite MCP plus legacy loopback REST over real MCP 2.0 stdio | `proven offline` | Modern `2026-07-28` direct mode and legacy `2025-11-25` mode discover 12 tools and one resource template, make representative bounded reads, emit JSON-RPC-only stdout, close cleanly, and expose untrusted evidence without secrets. Direct mode uses SQLite `mode=ro` and `query_only`. |
| Deterministic two-snapshot offline product demo | `proven offline` | Two fictional 25-Post snapshots prove 10 new, 15 reobserved, 10 not observed in the newer bounded sample, metric deltas, literal search, 25-row JSON/CSV exports, and a real direct-SQLite MCP comparison without X contact. |
| Offline dashboard rendering, responsive layout, and basic interaction/accessibility state | `proven local Chromium` | Because the in-app Browser had no attached session, recorded QA used pinned local Playwright Chromium: 15 loopback requests, no external request or console/page error, no desktop/390 px overflow, named controls, skip-link/focus and dynamic `aria-current`, exact 10/15/10 comparison, and 25 evidence cards. Two sanitized desktop captures are retained in [the demo record](demo.md); this is not a full accessibility audit. |
| Capability-lab fingerprint observability and app-owned session replay | `proven local Chromium` | Python 3.13.9/Chromium 151.0.7922.34 ran the six hardened browser/session items three times. The proof is limited to two ordinary context cases and fixture-created sentinel state/artifacts; it does not prove stealth, external session handling, or arbitrary-secret redaction. |
| Capability-lab GraphQL compatibility, fake-identity lease state, toy challenge, and loopback route transitions | `proven offline` | Fixed private fixtures prove only the requested synthetic mechanisms. The hardened complete lab ran three times locally with `30 passed, 1 skipped` each; the skip is the Linux-only namespace/privilege assertion on macOS. Every production analogue remains **prohibited and unreachable**; see [the disposition matrix](capability-lab.md). |
| Linux capability-lab execution with isolated network/mount/PID namespaces, only loopback, scrubbed environment, and dropped privilege | `proven offline` | Run 32230574720 passed all `31/31` items three times (`11.83s`, `10.22s`, `9.71s`) with no default route, sterile non-root/no-new-privileges environment, wheel exclusion, and between-run Chromium cleanup. The mount namespace is not a general filesystem sandbox. |
| Live headed Home capture on a fresh install and after restart | `not yet verified` | The six-run owner gate below has not been performed. There are no `owner live-verified` rows. |
| Live profile or Latest-search Browser capture | `not yet verified` | Local Chromium proves derived destinations and fixture behavior only. Each live surface needs its own authorized gate. |
| Live official X API collection | `not yet verified` | No authorized paid-read record exists. |
| Codex, Claude/Claude Code, or Cursor MCP registration and a complete client tool call | `not yet verified` | The current official Codex command form was checked and is documented, but no external client session was recorded. No unverified Claude or Cursor snippet is published. |
| Windows clean install or headed authentication | `not yet verified` | A PowerShell path is documented, but there is no Windows CI or clean-platform record. POSIX permission evidence does not transfer to NTFS. |

## Recorded clean-install commands

This exact sequence exited 0 in a fresh Python 3.13.3 virtual environment from archived
`a3196ca`; Chromium 151.0.7922.34 also launched and rendered local HTML:

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

The archived wheel had 23 members: all then-current 13 Python modules, four static assets, license,
metadata, and entry point, with no tests or lab. An installed-wheel smoke outside the source tree
also passed. At `c843690`, the current wheel had 24 members: 14 Python modules, the same four static
assets, license/metadata/entry point files, and no tests or lab. The convenience extras install in
README does not consume the lock, so use the sequence above when reproducibility matters.

## Pending owner live gate

There are **no `owner live-verified` claims**. An owner with appropriate authorization must:

1. Install from the lock in a clean environment, run visible `xworkbench auth`, and confirm local
   readiness without sharing the storage-state file.
2. Run `xworkbench live-smoke --confirm-live-x` three times, each requesting at most two currently
   visible Home Posts. Stop on login, challenge, rate limit, or any manual-action state.
3. Restart the process and repeat the same bounded smoke three times.
4. Record only date, app commit, platform/browser version, status, stored count, completion reason,
   warning/error codes, and cleanup result. Never retain Post text, cookies, tokens, auth state, or
   screenshots containing content.

Until all six runs and cleanup are recorded truthfully, the live rows remain `not yet verified`.

## Upstream architecture references

`git ls-remote` confirmed these exact branch heads on 2026-08-19. They are research references,
not dependencies; no upstream code was copied.

| Project and exact revision | Primary documentation checked | Safe takeaway; excluded behavior |
| --- | --- | --- |
| [Scrapling v0.4.14, `5d213a2d4764002bfc4fed33c32fe09fa8b0bf7f`](https://github.com/D4Vinci/Scrapling/tree/5d213a2d4764002bfc4fed33c32fe09fa8b0bf7f) | [Spider architecture](https://github.com/D4Vinci/Scrapling/blob/5d213a2d4764002bfc4fed33c32fe09fa8b0bf7f/docs/spiders/architecture.md), [advanced guide](https://github.com/D4Vinci/Scrapling/blob/5d213a2d4764002bfc4fed33c32fe09fa8b0bf7f/docs/spiders/advanced.md) | Neutral patterns: fingerprints, bounded scheduling, atomic checkpoints, progress, replay, statistics, exports. Excluded: stealth fetching, impersonation, block retries, proxy/identity rotation. |
| [Scweet 5.3.1, `c42e1222c632dbfeb5ae91633f426a6bd44a677a`](https://github.com/Altimis/Scweet/tree/c42e1222c632dbfeb5ae91633f426a6bd44a677a) | [v5 documentation](https://github.com/Altimis/Scweet/blob/c42e1222c632dbfeb5ae91633f426a6bd44a677a/DOCUMENTATION.md) | Neutral patterns: explicit limits, structured inputs, parameter-hash resume, WAL/busy-timeout discipline, run history, stable schemas. Excluded: private GraphQL, cookie-token extraction, account pools/switching, per-account proxies. |
| [Scrapy 2.17.0, `685abd6dbb87ea09564ebaf32aea95879ca21d31`](https://github.com/scrapy/scrapy/tree/685abd6dbb87ea09564ebaf32aea95879ca21d31) | [architecture](https://github.com/scrapy/scrapy/blob/685abd6dbb87ea09564ebaf32aea95879ca21d31/docs/topics/architecture.rst), [scheduler](https://github.com/scrapy/scrapy/blob/685abd6dbb87ea09564ebaf32aea95879ca21d31/docs/topics/scheduler.rst), [jobs](https://github.com/scrapy/scrapy/blob/685abd6dbb87ea09564ebaf32aea95879ca21d31/docs/topics/jobs.rst), [stats](https://github.com/scrapy/scrapy/blob/685abd6dbb87ea09564ebaf32aea95879ca21d31/docs/topics/stats.rst) | Neutral patterns: caps, priority/deduplication, backpressure, classified statistics, graceful persistence. For X, `401`/`403`/`429`, login, and challenge stop rather than change identity or route. |
| [Crawlee Python 1.9.3, `c5bb9133fbb8c7c36d48e8348fa1ccbf9d8eae43`](https://github.com/apify/crawlee-python/tree/c5bb9133fbb8c7c36d48e8348fa1ccbf9d8eae43) | [storages](https://github.com/apify/crawlee-python/blob/c5bb9133fbb8c7c36d48e8348fa1ccbf9d8eae43/docs/guides/storages.mdx), [request routing](https://github.com/apify/crawlee-python/blob/c5bb9133fbb8c7c36d48e8348fa1ccbf9d8eae43/docs/guides/request_router.mdx), [scaling](https://github.com/apify/crawlee-python/blob/c5bb9133fbb8c7c36d48e8348fa1ccbf9d8eae43/docs/guides/scaling_crawlers.mdx), [quick start](https://github.com/apify/crawlee-python/blob/c5bb9133fbb8c7c36d48e8348fa1ccbf9d8eae43/docs/quick-start/index.mdx) | Neutral patterns: persistent queues, typed routing, datasets, resource-aware admission, browser lifecycle, guided setup. Excluded: fingerprint generation, proxy rotation, and block-driven session replacement. |

## Current policy sources

These official sources were checked on 2026-08-19. This boundary is not legal advice.

| Official source | Project implication |
| --- | --- |
| [X Terms of Service](https://x.com/en/tos) | X states that scraping without prior written consent is prohibited and bars working around technical limitations or authentication/security measures. Personal, research, or noncommercial intent is not permission. |
| [X Developer Policy](https://docs.x.com/developer-terms/policy) | Official API/X Content use remains subject to the approved use case, access and distribution limits, privacy/content rules, credential safeguards, and no circumvention. |
| [DSA Article 40](https://eur-lex.europa.eu/eli/reg/2022/2065/oj), [European Commission delegated act](https://digital-strategy.ec.europa.eu/en/library/delegated-act-data-access-under-digital-services-act-dsa), and [DSA Data Access Portal](https://data-access.dsa.ec.europa.eu/) | Qualified systemic-risk researchers have a formal application-based route with eligibility and reasoned-request requirements. It is not general permission for personal/noncommercial scraping or bypass. |
