# Testing and evidence

Default tests must not contact X, require an authenticated session, make a paid API request, or
depend on an external model. Claims are classified in [verification](verification.md) as `proven
offline`, `proven local Chromium`, `owner live-verified`, or `not yet verified`.

## Complete source gate

Install from `requirements.lock`, then run:

```bash
python -m pip check
python -m playwright install chromium
ruff check .
pytest
node --check xworkbench/static/app.js
node --test tests/test_analysis.mjs
xworkbench --help
python -m xworkbench --help
python -m pip wheel . --no-deps --wheel-dir dist
```

`pytest` includes the local Chromium integration file when Chromium is installed. The tests route
or load only sanitized local fixtures and abort unexpected HTTP requests.

## Focused gates

Real production projection/navigation in local Chromium:

```bash
pytest tests/test_playwright_integration.py
```

Browser unit, hardening, adversarial, and local-Chromium coverage together:

```bash
pytest tests/test_playwright_browser.py tests/test_adversarial_recovery.py \
  tests/test_playwright_hardening.py tests/test_playwright_integration.py
```

Real MCP SDK plus its contract tests:

```bash
pytest tests/test_mcp.py tests/test_read_service.py tests/test_mcp_integration.py \
  -o addopts=''
```

Storage migration, rollback, and job terminal-state behavior:

```bash
pytest tests/test_storage.py tests/test_jobs.py
```

Deterministic complete offline demo:

```bash
pytest tests/test_offline_demo.py
```

Rendered demo QA was also recorded in pinned local Chromium. The in-app Browser had no attached
session, so the check used the installed Playwright Chromium fallback: 15 loopback requests, no
external request, no console/page error, no desktop or 390 px mobile overflow, named controls,
working skip-link/focus and dynamic `aria-current`, the exact 10/15/10 comparison, and 25 evidence
cards. The two sanitized desktop captures are in [the demo record](demo.md). This was a recorded
render check, not a new automated test or a live-X claim.

Queue correctness and the ordinary 300-job lightweight stress gate:

```bash
pytest tests/test_jobs.py tests/test_queue_performance.py -k 'not production_playwright'
```

The deterministic resource-governor, public-metrics, and configuration contracts are covered by:

```bash
pytest tests/test_jobs.py tests/test_api.py tests/test_api_security.py \
  tests/test_cli_config.py
```

Injected signals prove pre-lease pause and five-second recovery without interrupting active work.
The default runtime probe supports only coordinator-process RSS/CPU; unsupported browser counts and
the non-applicable synchronous event-loop metric must remain explicit nulls, not green zeros.

The measured production-Playwright concurrency matrix is deliberately opt-in because it starts
real Chromium. It still intercepts the exact validated destination before egress, serves only a
numeric-loopback fixture, and aborts every other request:

```bash
XWORKBENCH_RUN_BROWSER_MATRIX=1 \
  pytest tests/test_queue_performance.py -k production_playwright
```

On the recorded 2026-08-19 arm64 macOS machine, the local four-job fixture was 2.415 times faster
with two workers than one, while peak process-tree RSS rose by about 385 MB. Correctness and cleanup
passed in both cases; this supports default 1 and opt-in maximum 2, not higher live-X concurrency.
See [ADR 0002](adr/0002-bounded-capture-queue.md) and the
[sanitized benchmark](benchmarks/queue-performance-2026-08-19.json) for exact measurements and
limits.

CLI/configuration and local API boundaries:

```bash
pytest tests/test_cli_config.py tests/test_interfaces.py tests/test_api.py \
  tests/test_api_security.py
```

The project CI runs locked ordinary tests, Ruff, JavaScript checks, both help entry points, wheel
build, and package-content inspection on Linux with Python 3.11-3.13. A dependent Linux/Python
3.13 job installs Chromium once and runs the offline integration file plus the bounded queue browser
matrix. CI intentionally has no X credentials or live target. An archived Python 3.13.3 clean
install passed locally on macOS; Windows has no clean-install record, and neither desktop platform
has a CI job.

## Hard-isolated capability lab

The six-mechanism capability lab is test-only and skipped by every ordinary `pytest` run. Its
dedicated Linux CI job checks out without persisted credentials, builds a fresh release wheel,
creates network, mount, and PID namespaces, remounts sysfs for the network namespace, and asserts
that only loopback and no default route are visible. It starts from an empty environment, drops to
the non-root runner identity with no effective capabilities or privilege escalation, runs
`tests/capability_lab/` three times, and rejects a surviving Chromium process between runs. Do not
enable the lab on a normal networked host merely to remove the skips.

The mount namespace is used to make the network namespace visible through remounted sysfs; it is
not a general filesystem sandbox.

At `c843690`, the hardened local pre-CI gate ran three times with 30 passed and one macOS skip per
run; the skipped item is the Linux-only namespace/privilege assertion. The ordinary full suite
reported 200 passed and 32 skipped: 31 gated lab items plus the opt-in browser matrix. These local
results were then extended by the Linux-only gate.

At final implementation revision `1bd21ea`,
[CI run 32230574720](https://github.com/Alex-lop/X-Scraper/actions/runs/32230574720) passed all
31 lab items inside the isolated Linux job three times (`11.83s`, `10.22s`, and `9.71s`). The
dependent browser job also passed; the complete pipeline was green on Python 3.11, 3.12, and 3.13.

The exact positive/negative tests, production boundaries, and `prohibited and unreachable`
disposition for every mechanism are in [the capability-lab record](capability-lab.md). Lab tests
cannot support a product or live-X claim.

## What the categories mean

- `proven offline`: deterministic unit/integration evidence with synthetic state and no X request.
- `proven local Chromium`: real Chromium ran production browser code against locally fulfilled
  sanitized pages; it is still offline from X.
- `owner live-verified`: an authorized owner completed the documented bounded gate and retained
  only sanitized metadata.
- `not yet verified`: implementation or the required scope of evidence is absent.

A fake page cannot support a Chromium claim. A local Chromium fixture cannot support a live X
claim. A successful live Home capture cannot support profile, search, official API, Windows, batch,
or MCP-client claims unless those exact paths were separately exercised.

## Owner-only live gates

No live X request belongs in routine tests or CI. The pending browser runbook is deliberately
manual and opt-in:

```bash
xworkbench auth
xworkbench live-smoke --confirm-live-x
```

It requests at most two visible Posts in headed Chromium and uses a temporary database. Run only
with appropriate authorization, stop on any challenge/manual-action state, and record only commit,
date, platform/browser, status, count, stop reason, warnings/error codes, and cleanup. Never retain
Post text, screenshots containing content, cookies, tokens, or storage state. The complete repeated
gate is in [verification](verification.md).

The official API live row likewise remains pending; do not spend or issue a live request merely to
make a test green.

## Packaging limits

The universal lock is complete but has no artifact hashes. The wheel is expected to contain the
Python package and four static assets, while excluding tests and any capability-lab code. The exact
archived clean-install record and current caveats live in [verification](verification.md), rather
than being inferred from a developer's existing virtual environment.
