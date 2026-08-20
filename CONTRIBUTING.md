# Contributing

Keep changes small, local-first, and proportional to a demonstrated product need. Read
[the architecture decision](docs/adr/0001-feed-to-context-providers.md),
[responsible use](docs/responsible-use.md), and [verification rules](docs/testing.md) first.

## Set up a checkout

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install -e . --no-deps
python -m playwright install chromium
python -m pip check
xworkbench setup
```

On Windows PowerShell use `.venv\Scripts\Activate.ps1`; the project does not yet claim a green
Windows clean-install gate. Do not update the lock incidentally. If dependencies change, regenerate
it with the exact uv 0.11.29 command in [Getting started](docs/getting-started.md) and explain the
change.

## Before submitting

```bash
ruff check .
pytest
node --check xworkbench/static/app.js
node --test tests/test_analysis.mjs
xworkbench --help
python -m xworkbench --help
python -m pip wheel . --no-deps --wheel-dir dist
```

Run the focused Chromium, terminal/local-client, MCP, storage/migration, CLI, and API-security gates
from [testing](docs/testing.md) when those surfaces change. Tests must be deterministic and offline
from X by default. Unexpected network access in a browser fixture should fail the test.

## Evidence and claim rules

Every user-facing claim must map to exactly scoped evidence:

- synthetic or fake tests support `proven offline` only;
- production code in real Chromium with local fixtures supports `proven local Chromium` only;
- a sanitized authorized run supports only its exact `owner live-verified` path;
- everything else is `not yet verified` or explicitly experimental.

Update [verification](docs/verification.md) when evidence changes. Include the commit, environment,
exact command, result, and limit of the proof. Never turn a failure, missing dependency, skipped
test, or simulated response into a green claim.

## Secrets, fixtures, and live checks

Never commit tokens, Playwright state, cookies, passwords, databases, WAL files, live Post content,
private screenshots, network traces, or browser profiles. Use fictional synthetic data and
`example.invalid` URLs. Logs and public errors must not contain raw exception secrets.

Live X and paid API checks are owner-only, opt-in, bounded, and excluded from CI. Stop at any login,
challenge, rate-limit, or manual-action state. Do not add stealth, fingerprint manipulation,
unrelated-profile cookie extraction, private GraphQL, automated challenge solving, identity/account
rotation, block-triggered proxy changes, or X write actions.

The synthetic [capability lab](docs/capability-lab.md) is not an exception for production code.
Lab changes must stay under `tests/capability_lab/`, use only private fixed fixtures and internally
allocated loopback handles, reject external inputs before file/network access, leave no production
import or activation surface, and remain excluded from wheels. Its enabled suite belongs only in an
environment with external networking disabled; the normal test run must keep it gated.

## Design expectations

Prefer the standard library and the existing provider/storage boundaries over new dependencies or
frameworks. Preserve loopback-only access, explicit preview and confirmation, immutable observations,
truthful partial states, bounded reads, nullable unknowns, and untrusted-content labels. Do not add
abstractions for hypothetical scale.

When changing a public request, response, CLI command, config key, schema, or MCP tool, update the
focused documentation in the same patch and test backward compatibility or fail-closed migration.
