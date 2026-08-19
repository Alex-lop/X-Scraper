# Getting started

X-Scraper currently supports installation from a source checkout. It is not published on PyPI.
Python 3.11, 3.12, and 3.13 are exercised in Linux CI; the package metadata permits newer Python,
but newer versions are not part of that matrix. An archived locked checkout also passed a clean
Python 3.13.3 install and local Chromium launch on macOS; see [verification](verification.md).

## macOS and Linux

The short first-run path is:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[browser,mcp,dev]'
xworkbench setup
xworkbench demo
# Press Ctrl+C after exploring, then:
xworkbench start
```

`setup` creates app-owned paths, writes a non-secret configuration file if absent, initializes or
migrates SQLite, checks for the dependency lock, and runs local readiness checks. It never signs in
or contacts X. Missing Chromium is an actionable warning for this offline path; a missing browser
session or optional API token is reported with the exact next command.

The reproducible development path installs the complete universal lock before the editable package:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.lock
python -m pip install -e . --no-deps
python -m playwright install chromium
python -m pip check
xworkbench setup
```

The universal extras lock was generated with uv 0.11.29; `pyproject.toml` separately pins the
setuptools build input. The lock does not contain artifact hashes, so `pip --require-hashes` is not
supported.

## Windows PowerShell

From a source checkout:

```powershell
py -3.13 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e '.[browser,mcp,dev]'
xworkbench setup
xworkbench demo
# Press Ctrl+C after exploring, then:
xworkbench start
```

The lock includes Windows-specific dependencies and the code avoids applying POSIX mode checks on
Windows. However, this repository does not currently run Windows CI, and a clean Windows install or
headed authentication has not been recorded in [verification](verification.md). Treat this as the
supported command path awaiting platform verification, not a parity claim. `caffeinate` is a macOS
utility and is neither required nor wrapped by this project.

## Safe demo, persistent app, and authentication

`xworkbench demo` starts an isolated temporary dashboard with two compatible 25-Post snapshots for
the clearly fictional “Project Glasswing” topic. It verifies Changes, local search, JSON/CSV export,
and a direct MCP comparison before serving; collection remains disabled. It deletes the temporary
database at shutdown. Press Ctrl+C, then run the persistent dashboard with:

```bash
xworkbench start
```

The default URL is <http://127.0.0.1:5000>. `xworkbench serve` remains an alias. Both refuse a
non-loopback host, and `--no-open` suppresses opening a browser. On startup, the worker recovers
durable queued jobs from SQLite. Terminal snapshots remain inspectable and are not recollected
automatically.

Browser capture needs an app-owned session created by visible manual sign-in:

```bash
python -m playwright install chromium
xworkbench auth
xworkbench doctor
xworkbench start
```

The application does not read another browser profile or receive your password. Keep
`var/auth/playwright_state.json` private. A saved file is not proof that X currently accepts it;
only the live check performed during `auth` can produce `verified_live`, and the owner live capture
gate is still pending.

The official API token is optional:

```bash
xworkbench configure
xworkbench doctor --require-token
```

`configure` uses a masked prompt. See [configuration](configuration.md),
[browser capture](browser-capture.md), and [official X API](official-x-api.md) before a real request.

## Common failures

| Message | Action |
| --- | --- |
| Playwright package missing | Install the `browser` extra or the lock. |
| Chromium missing/cannot launch | Run `python -m playwright install chromium`. |
| Local auth missing, invalid, expired, or unverified | Run `xworkbench auth`; do not copy cookies from another profile. |
| Database incompatible or backup already exists | Stop and preserve both files; read [storage and cache](storage-and-cache.md). |
| Port unavailable | Use `xworkbench start --port 0` or choose a free loopback port. |
| Official token missing | Ignore for Browser capture, or run `xworkbench configure`. |

Do not post auth files, tokens, live Post content, or database copies in an issue. Sanitized command
output and error codes are sufficient for most reports.
