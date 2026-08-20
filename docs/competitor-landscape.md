# Competitor landscape

This is a dated repository-evidence snapshot, not a live-X bake-off. It was accessed on
**2026-08-20**. No competitor was installed, given credentials, or run against X. GitHub stars are
included only as volatile context on that date, never as a quality score.

X's [developer guidelines](https://docs.x.com/developer-guidelines) direct developers to the
official API and reject scraping or browser automation without permission. Official API access is
[credit-based pay-per-use](https://docs.x.com/x-api/getting-started/pricing), with endpoint prices
shown in the Developer Console. Accordingly, unofficial and Browser transports below are
repository research only. X-Scraper's Browser tests use loopback fixtures; a live gate requires
specific written authorization from X and the content owner.

## Claim grades

- `REPO FACT`: repository metadata or documentation visible at the pinned revision.
- `SOURCE PRESENT`: implementation is present at the pinned revision; this does not prove it works
  against X now.
- `PROJECT CI`: the upstream project's recorded automation result; scope is limited to the steps it
  actually runs.
- `UPSTREAM-DOCUMENTED`: a maintainer's explicit statement, including limitations or support state.
- `CONTRADICTED`: a positive claim has material contrary evidence in upstream source, CI, or open
  reports.
- `LIVE NOT RUN`: this review made no live request, login, paid API call, or acceptance claim.

“Documentation language” describes the reviewed files, not a maintainer's nationality or location.
A pinned default-branch SHA proves only what was present at that revision. A release tag, recent
commit, passing workflow, or star count does not by itself prove current X compatibility.

## X-Scraper

- **Snapshot — `REPO FACT`:** local implementation checkpoint
  `71d42c58690e83058f21813d5ea781b76f62b9d9`; no published package release;
  MIT; repository star count intentionally omitted because this is the subject, not a candidate.
- **Documentation language:** English.
- **Transport class:** official X API plus a separately approved Playwright Browser provider.
- **Claimed and source-present surfaces:** Browser Home/profile/Latest search and official
  profile/search, all behind exact preview and confirmation.
- **Durability:** SQLite jobs, atomic batch admission, leases, checkpoints, cancellation, restart
  recovery, snapshots, local search, comparison, and JSON/CSV export.
- **UI:** loopback web dashboard, CLI, read-only MCP, and optional terminal operations UI.
- **CI evidence — `PROJECT CI`:** the earlier baseline passed Python, JavaScript, fixture Chromium,
  packaging, and isolated capability-lab workflow jobs. At this checkpoint, local gates cover the
  dashboard workflow, terminal Pilot sizes, and production-route topology, but an exact-head hosted
  run is pending. The route regression proves the global two-worker ceiling and same-provider auth
  serialization.
- **Contradictory evidence:** the historical 2.415× Browser matrix injected distinct synthetic auth
  keys and bypassed route admission. Live X, Windows, external MCP-client, and browser-process-tree
  governance are not established.
- **Prohibited inference:** local fixture success does not prove current X selectors, ordering,
  completeness, login acceptance, or permission to scrape.
- **Cheapest authorized falsification:** run the offline route topology and local-Chromium fixture
  gates; after written authorization, run one bounded sanitized `live-smoke` gate and retain its
  full evidence envelope.
- **Recheck trigger:** any provider/parser change, X policy or DOM change, authorized live-smoke
  result, or release decision.
- **Verdict:** `LIMITED`; useful local evidence controls and dashboard/terminal coverage, with live
  acceptance deliberately unclaimed. Production Browser concurrency is one.

## Tweepy

- **Snapshot — `REPO FACT`:** `master` at
  [`c1978d643ecce491929084e4290b35f57e4921ad`](https://github.com/tweepy/tweepy/tree/c1978d643ecce491929084e4290b35f57e4921ad),
  [v4.17.0](https://github.com/tweepy/tweepy/releases/tag/v4.17.0), MIT, 11,175 stars.
- **Documentation language:** English.
- **Transport class:** Python client for documented official API transports.
- **Claimed and source-present surfaces:** v1.1/v2 REST clients, pagination, streaming, and
  authentication documented in the pinned
  [README](https://github.com/tweepy/tweepy/blob/c1978d643ecce491929084e4290b35f57e4921ad/README.md).
- **Durability:** request/pagination helpers; queue, checkpoints, and evidence storage remain caller
  responsibilities.
- **UI:** Python library.
- **CI evidence — `PROJECT CI`:** [exact-head run](https://github.com/tweepy/tweepy/actions/runs/28615117027)
  passed; the pinned
  [workflow](https://github.com/tweepy/tweepy/blob/c1978d643ecce491929084e4290b35f57e4921ad/.github/workflows/test.yml)
  replays committed VCR cassettes.
- **Contradictory evidence:** cassette-backed tests are not current live endpoint or entitlement
  evidence.
- **Prohibited inference:** a passing upstream run does not prove this workbench needs another
  transport layer or that every official endpoint is enabled for an account.
- **Cheapest authorized falsification:** first compare a missing behavior against the existing
  official transport using synthetic responses; only then make one owner-approved paid call.
- **Recheck trigger:** measured limitation in the existing transport, a major Tweepy release, or an
  official API contract change.
- **Verdict:** `OFFICIAL REFERENCE`; do not add it without a measured gap.

## node-twitter-api-v2

- **Snapshot — `REPO FACT`:** `master` at
  [`f185e7ee8060a394e5ea1d7b62b52e9600156dcf`](https://github.com/PLhery/node-twitter-api-v2/tree/f185e7ee8060a394e5ea1d7b62b52e9600156dcf),
  [1.29.1](https://github.com/PLhery/node-twitter-api-v2/releases/tag/1.29.1), Apache-2.0,
  1,561 stars.
- **Documentation language:** English.
- **Transport class:** TypeScript/Node client for official API v1.1, v2, labs, ads, and streams.
- **Claimed and source-present surfaces:** authentication, REST, streaming, pagination, and plugins
  in the pinned
  [README](https://github.com/PLhery/node-twitter-api-v2/blob/f185e7ee8060a394e5ea1d7b62b52e9600156dcf/README.md).
- **Durability:** paginator/stream helpers; no durable evidence queue or snapshot store.
- **UI:** TypeScript library.
- **CI evidence — `PROJECT CI`:** [exact-head run](https://github.com/plhery/node-twitter-api-v2/actions/runs/30945361430)
  passed with repository-managed X secrets.
- **Contradictory evidence:** the recorded run cannot establish every endpoint, product tier, or
  caller entitlement.
- **Prohibited inference:** successful upstream Node tests do not justify adding a second runtime to
  this Python product.
- **Cheapest authorized falsification:** use its typed pagination/error design as a review reference;
  test any suspected Python-transport gap with the workbench's local response fixtures.
- **Recheck trigger:** a measured official-transport defect or official schema change.
- **Verdict:** `OFFICIAL REFERENCE`; useful design evidence, wrong runtime here.

## twarc

- **Snapshot — `REPO FACT`:** `main` at
  [`12104e080f48f849726b9662b32cb970c34e1689`](https://github.com/DocNow/twarc/tree/12104e080f48f849726b9662b32cb970c34e1689),
  [v2.14.1](https://github.com/DocNow/twarc/releases/tag/v2.14.1), MIT, 1,394 stars.
- **Documentation language:** English.
- **Transport class:** Python official API v2 archive/search CLI and library.
- **Claimed and source-present surfaces:** search, counts, hydration/dehydration, conversations,
  timelines, lists, and JSON Lines workflows in the pinned
  [README](https://github.com/DocNow/twarc/blob/12104e080f48f849726b9662b32cb970c34e1689/README.md).
- **Durability:** append-friendly JSON Lines and plugin pipelines, but not this product's SQLite job
  and evidence model.
- **UI:** CLI/library.
- **CI evidence:** the head commit removes Actions; the last recorded predecessor
  [run failed](https://github.com/DocNow/twarc/actions/runs/18980893156).
- **Contradictory evidence — `UPSTREAM-DOCUMENTED`:** the README states active support ended after
  API quota changes made the tool unusable; the head commit is
  [“bye bye github actions”](https://github.com/DocNow/twarc/commit/12104e080f48f849726b9662b32cb970c34e1689).
- **Prohibited inference:** release recency does not override the upstream support notice.
- **Cheapest authorized falsification:** none is needed for adoption; re-read upstream status after
  an explicit support restart.
- **Recheck trigger:** maintainer reactivation plus a current official-API CI gate.
- **Verdict:** `HISTORICAL ONLY`.

## Twikit

- **Snapshot — `REPO FACT`:** `main` at
  [`c3b7220866f8582009fe2d1155b6fe92192a2711`](https://github.com/d60/twikit/tree/c3b7220866f8582009fe2d1155b6fe92192a2711),
  [version2.3.1](https://github.com/d60/twikit/releases/tag/version2.3.1), MIT, 4,624 stars.
- **Documentation language:** English, Japanese, and Chinese are present.
- **Transport class:** unofficial authenticated web/internal GraphQL client; pinned
  [GraphQL source](https://github.com/d60/twikit/blob/c3b7220866f8582009fe2d1155b6fe92192a2711/twikit/client/gql.py).
- **Claimed and source-present surfaces:** user/tweet lookup, search, timelines, trends, lists,
  communities, media, and interactions in the pinned
  [README](https://github.com/d60/twikit/blob/c3b7220866f8582009fe2d1155b6fe92192a2711/README.md).
- **Durability:** client-side cursors/models; no equivalent durable approval/evidence queue.
- **UI:** synchronous/asynchronous Python library.
- **CI evidence:** no project test workflow was found; the current scheduled Action is
  GitHub-managed CodeQL, not acceptance.
- **Contradictory evidence — `CONTRADICTED`:** current reports include
  [429 responses](https://github.com/d60/twikit/issues/433),
  [login/liveness lock](https://github.com/d60/twikit/issues/430),
  [parsing failure](https://github.com/d60/twikit/issues/425), and
  [automated-post denial](https://github.com/d60/twikit/issues/413).
- **Prohibited inference:** broad source-present methods do not prove current live acceptance or
  authorize private-interface use.
- **Cheapest authorized falsification:** review pinned types/cursor handling only; a live check waits
  for written X authorization and an isolated bounded acceptance protocol.
- **Recheck trigger:** a release with project acceptance CI or resolution of the cited failures.
- **Verdict:** `RESEARCH ONLY`; `LIVE NOT RUN`.

## twscrape

- **Snapshot — `REPO FACT`:** `main` at
  [`9745b021d8a7405bed8bc56a725813367b3f07dd`](https://github.com/vladkens/twscrape/tree/9745b021d8a7405bed8bc56a725813367b3f07dd),
  [v0.20.0](https://github.com/vladkens/twscrape/releases/tag/v0.20.0), MIT, 2,694 stars.
- **Documentation language:** English.
- **Transport class:** unofficial async GraphQL collector with account/session rotation.
- **Claimed and source-present surfaces:** search, users, timelines, lists, trends, tweets, and
  related objects; the pinned
  [README](https://github.com/vladkens/twscrape/blob/9745b021d8a7405bed8bc56a725813367b3f07dd/readme.md)
  explicitly describes an account pool, proxies, SQLite sessions/locks, and JSONL CLI output.
- **Durability:** durable account/session pool and locks; not a single-identity evidence store.
- **UI:** Python library and CLI.
- **CI evidence — `PROJECT CI`:** [exact-head run](https://github.com/vladkens/twscrape/actions/runs/31134172889)
  passed.
- **Contradictory evidence — `CONTRADICTED`:** recent interface-break reports
  [#322](https://github.com/vladkens/twscrape/issues/322) and
  [#320](https://github.com/vladkens/twscrape/issues/320) limit a generic compatibility claim.
- **Prohibited inference:** CI does not make account rotation, proxies, or private GraphQL suitable
  for this project's single-identity boundary.
- **Cheapest authorized falsification:** none for adoption; inspect only bounded pagination,
  checkpoint, and backpressure patterns.
- **Recheck trigger:** a product-boundary change explicitly requested by the owner, not ordinary
  upstream churn.
- **Verdict:** `DO NOT ADOPT`.

## Nitter

- **Snapshot — `REPO FACT`:** `master` at
  [`0bc9702854b6dc6e952012352375410277ca2368`](https://github.com/zedeus/nitter/tree/0bc9702854b6dc6e952012352375410277ca2368),
  no release/tag found, AGPL-3.0, 13,460 stars.
- **Documentation language:** English.
- **Transport class:** self-hosted alternative web frontend using unofficial access and real
  accounts, per the pinned
  [README](https://github.com/zedeus/nitter/blob/0bc9702854b6dc6e952012352375410277ca2368/README.md).
- **Claimed and source-present surfaces:** profile/timeline/search/thread presentation and RSS.
- **Durability:** Redis/Valkey service cache and account state; archiving remains roadmap work, not a
  durable evidence-ingestion queue.
- **UI:** web service and feeds.
- **CI evidence — `PROJECT CI`:** exact-head
  [Docker/integration run](https://github.com/zedeus/nitter/actions/runs/32200660037) passed.
- **Contradictory evidence — `CONTRADICTED`:** current failures remain reported in
  [#1439](https://github.com/zedeus/nitter/issues/1439) and
  [#1436](https://github.com/zedeus/nitter/issues/1436).
- **Prohibited inference:** a built service image or public instance says nothing about durable
  snapshot correctness, authorization, or independent current acceptance.
- **Cheapest authorized falsification:** inspect its service/cache separation; do not probe public
  instances or attach an account without written authorization.
- **Recheck trigger:** an archival release, explicit acceptance CI, or material transport rewrite.
- **Verdict:** `DESIGN REFERENCE`, not an evidence SDK; `LIVE NOT RUN`.

## snscrape

- **Snapshot — `REPO FACT`:** `master` at
  [`614d4c2029a62d348ca56598f87c425966aaec66`](https://github.com/JustAnotherArchivist/snscrape/tree/614d4c2029a62d348ca56598f87c425966aaec66),
  tag `v0.7.0.20230622`, GPL-3.0-or-later in the pinned project metadata, 5,442 stars.
- **Documentation language:** English.
- **Transport class:** Python scraping CLI/library using guest/private GraphQL in the pinned
  [Twitter module](https://github.com/JustAnotherArchivist/snscrape/blob/614d4c2029a62d348ca56598f87c425966aaec66/snscrape/modules/twitter.py).
- **Claimed and source-present surfaces:** user/search/list/hashtag-era collectors in the pinned
  [README](https://github.com/JustAnotherArchivist/snscrape/blob/614d4c2029a62d348ca56598f87c425966aaec66/README.md).
- **Durability:** iterators and JSONL output; storage/restart semantics remain caller-owned.
- **UI:** CLI/library.
- **CI evidence:** no GitHub Actions workflow was present at the pinned head.
- **Contradictory evidence — `CONTRADICTED`:** maintainer issue
  [#996](https://github.com/JustAnotherArchivist/snscrape/issues/996) says Twitter scraping is
  blocked with no known workaround; it remained open with 2026 activity.
- **Prohibited inference:** historical popularity and source presence cannot outweigh an explicit
  continuing failure report.
- **Cheapest authorized falsification:** watch the maintainer issue and repository; do not run a
  private-interface probe.
- **Recheck trigger:** a new release and an upstream acceptance gate that resolves #996.
- **Verdict:** `REJECT`; `LIVE NOT RUN`.

## twitter-scraper-selenium

- **Snapshot — `REPO FACT`:** `main` at
  [`62580cfb0359db4f6000e35afc796140a9445950`](https://github.com/shaikhsajid1111/twitter-scraper-selenium/tree/62580cfb0359db4f6000e35afc796140a9445950),
  latest GitHub release [v2.0.0](https://github.com/shaikhsajid1111/twitter-scraper-selenium/releases/tag/v2.0.0)
  while pinned source declares 6.2.2, MIT, 345 stars.
- **Documentation language:** English.
- **Transport class:** Selenium/browser application plus source-present internal/third-party helper
  access.
- **Claimed and source-present surfaces:** search/profile-oriented collection and CSV/JSON-style
  output in the pinned
  [README](https://github.com/shaikhsajid1111/twitter-scraper-selenium/blob/62580cfb0359db4f6000e35afc796140a9445950/README.md).
- **Durability:** output/checkpoint behavior is application-specific; no comparable durable
  approval, lease, and evidence model is established.
- **UI:** script/CLI-oriented application.
- **CI evidence:** no Actions test workflow or current acceptance fixture was found.
- **Contradictory evidence:** 2026 activity mixes a helper change with sponsor-banner maintenance;
  the old latest release does not prove current compatibility.
- **Prohibited inference:** recent commits or a version bump are not live-X acceptance evidence.
- **Cheapest authorized falsification:** inspect its browser lifecycle and output shape only; a live
  browser run requires written X authorization.
- **Recheck trigger:** a tagged release with deterministic local browser fixtures and current
  acceptance evidence.
- **Verdict:** `RESEARCH ONLY`; `LIVE NOT RUN`.

## RSSHub

- **Snapshot — `REPO FACT`:** `master` at
  [`db04d6a8ed7e37a4794d9b846fb892fd4fce99a4`](https://github.com/DIYgod/RSSHub/tree/db04d6a8ed7e37a4794d9b846fb892fd4fce99a4),
  no release/tag found, AGPL-3.0, 45,813 stars.
- **Documentation language:** multilingual project documentation.
- **Transport class:** TypeScript feed-generation service; the pinned X
  [namespace](https://github.com/DIYgod/RSSHub/blob/db04d6a8ed7e37a4794d9b846fb892fd4fce99a4/lib/routes/twitter/namespace.ts)
  declares authentication and route organization.
- **Claimed and source-present surfaces — `SOURCE PRESENT`:** user, search, home/latest, list,
  likes, media, tweet, and trends routes under the pinned `lib/routes/twitter` tree.
- **Durability:** feed delivery and Redis-style cache behavior, not immutable evidence snapshots or
  a durable resume queue.
- **UI:** HTTP feed service/routes.
- **CI evidence — `PROJECT CI`:** exact-head lint/format passed; the pinned
  [full-route workflow](https://github.com/DIYgod/RSSHub/blob/db04d6a8ed7e37a4794d9b846fb892fd4fce99a4/.github/workflows/test-full-routes.yml)
  marks route testing `continue-on-error`.
- **Contradictory evidence:** workflow success therefore cannot prove a working X route.
- **Prohibited inference:** a source-present route or delivered feed is not durable evidence,
  completeness, or current acceptance.
- **Cheapest authorized falsification:** reuse route organization/cache-provenance ideas; wait for
  explicit route acceptance evidence before any authorized comparison.
- **Recheck trigger:** X route rewrite, failure-hard CI, or a documented durable output feature.
- **Verdict:** `DESIGN REFERENCE`; `LIVE NOT RUN`.

## MediaCrawler

- **Snapshot — `REPO FACT`:** `main` at
  [`d6f7c5bb906b6dac40ddf343ef9e26438a3de092`](https://github.com/NanmiCoder/MediaCrawler/tree/d6f7c5bb906b6dac40ddf343ef9e26438a3de092),
  no release/tag found, GitHub license `NOASSERTION`, 63,128 stars.
- **License — `REPO FACT`:** custom
  [Non-Commercial Learning License 1.1](https://github.com/NanmiCoder/MediaCrawler/blob/d6f7c5bb906b6dac40ddf343ef9e26438a3de092/LICENSE),
  not a permissive reuse grant.
- **Documentation language:** Chinese primary README with English and Spanish variants.
- **Transport class:** browser/platform-specific research crawler.
- **Claimed and source-present surfaces:** the pinned
  [README](https://github.com/NanmiCoder/MediaCrawler/blob/d6f7c5bb906b6dac40ddf343ef9e26438a3de092/README.md)
  lists XHS, Douyin, Kuaishou, Bilibili, Weibo, Tieba, and Zhihu—no X adapter.
- **Durability:** adapter-dependent local file/database outputs.
- **UI:** CLI/web-oriented tooling.
- **CI evidence:** exact-head Action deploys Pages; it is not product or collector CI.
- **Contradictory evidence — `CONTRADICTED`:** the project name and popularity do not supply an X
  surface, and the custom license limits reuse.
- **Prohibited inference:** architecture similarity is not X support or license compatibility.
- **Cheapest authorized falsification:** inspect generic adapter/output organization only; do not
  copy code or run unrelated platform collectors.
- **Recheck trigger:** an explicit X adapter or license replacement, each reviewed independently.
- **Verdict:** `NO X / LICENSE-LIMITED`.

## x-crawler

- **Snapshot — `REPO FACT`:** `main` at
  [`0c62d5df24830c139cfe10834c65bff842c45361`](https://github.com/wjzdw007/x-crawler/tree/0c62d5df24830c139cfe10834c65bff842c45361),
  no release/tag found, MIT, 21 stars.
- **Documentation language:** Chinese.
- **Transport class:** cookie/private-GraphQL script; Home recommended/following access is present
  in pinned [`crawler.py`](https://github.com/wjzdw007/x-crawler/blob/0c62d5df24830c139cfe10834c65bff842c45361/crawler.py).
- **Claimed and source-present surfaces:** Home feed collection and repository data updates in the
  pinned [README](https://github.com/wjzdw007/x-crawler/blob/0c62d5df24830c139cfe10834c65bff842c45361/README.md).
- **Durability:** repository data files/commits, not an operator evidence queue.
- **UI:** scripts and repository workflow.
- **CI evidence — `PROJECT CI`:** the pinned
  [hourly workflow](https://github.com/wjzdw007/x-crawler/blob/0c62d5df24830c139cfe10834c65bff842c45361/.github/workflows/hourly-crawler.yml)
  allows the crawl step to fail before committing data.
- **Contradictory evidence:** current head is an automated data commit; the last `crawler.py` code
  change was [`8c84a68`](https://github.com/wjzdw007/x-crawler/commit/8c84a6819b7a237288db0ef5297a73e53764bea8).
- **Prohibited inference:** workflow success or data churn is not core maintenance or live
  acceptance.
- **Cheapest authorized falsification:** inspect only repository history and workflow semantics;
  no cookie/private-interface execution.
- **Recheck trigger:** a core-code change paired with failure-hard acceptance evidence.
- **Verdict:** `WATCH ONLY`; `LIVE NOT RUN`.

## TwitterAccountMediaDownload

- **Snapshot — `REPO FACT`:** `main` at
  [`62571796a954afa522678ba3b19b47bb659e55a3`](https://github.com/JDDKCN/TwitterAccountMediaDownload/tree/62571796a954afa522678ba3b19b47bb659e55a3),
  [v1.1.0](https://github.com/JDDKCN/TwitterAccountMediaDownload/releases/tag/v1.1.0),
  AGPL-3.0, 81 stars.
- **Documentation language:** Simplified Chinese, Traditional Chinese, English, and Japanese.
- **Transport class:** .NET private-GraphQL media downloader; endpoint constants are
  [source-present](https://github.com/JDDKCN/TwitterAccountMediaDownload/blob/62571796a954afa522678ba3b19b47bb659e55a3/Src/TAMDownload.Core/Constants/TwitterApiConstants.cs).
- **Claimed and source-present surfaces:** account media, likes, bookmarks, user media, and tweets
  in the pinned
  [README](https://github.com/JDDKCN/TwitterAccountMediaDownload/blob/62571796a954afa522678ba3b19b47bb659e55a3/README.md).
- **Durability:** local media/metadata plus source-present cursor/resume
  [metadata](https://github.com/JDDKCN/TwitterAccountMediaDownload/blob/62571796a954afa522678ba3b19b47bb659e55a3/Src/TAMDownload.Core/Models/MetadataContainer.cs).
- **UI:** cross-platform core CLI plus Windows GUI.
- **CI evidence:** no project CI workflow was found.
- **Contradictory evidence — `CONTRADICTED`:** current parse/cookie complaints remain in
  [#4](https://github.com/JDDKCN/TwitterAccountMediaDownload/issues/4) and
  [#5](https://github.com/JDDKCN/TwitterAccountMediaDownload/issues/5).
- **Prohibited inference:** source-present resume UX does not prove current private-interface
  compatibility or broader platform support.
- **Cheapest authorized falsification:** reuse only the operator concept of resumable destination
  progress; any live check requires written X authorization.
- **Recheck trigger:** a release with current parser fixtures, CI, and resolved compatibility issues.
- **Verdict:** `NICHE REFERENCE`; `LIVE NOT RUN`.

## Safe takeaways and exclusions

Safe design takeaways are limited to typed adapters, bounded pagination, checkpointing, cache
provenance, backpressure, output adapters, and clearer CLI/Web organization. No competitor code is
installed or copied. In particular, this work does not adopt or test evasion, CAPTCHA handling,
fingerprint manipulation, private endpoints, proxies, cookie transfer, account pools, or multiple
identities. Cloudflare and other anti-bot bypass work is out of scope.
