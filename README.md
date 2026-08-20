# X-Scraper

[![CI](https://github.com/Alex-lop/X-Scraper/actions/workflows/ci.yml/badge.svg)](https://github.com/Alex-lop/X-Scraper/actions/workflows/ci.yml)

<p align="center">
  <img src="images/X_cool.png" alt="X-Scraper logo" width="240" />
</p>

X-Scraper turns a small, human-approved X capture into a durable local snapshot for inspection,
export, and bounded read-only agent access.

`X-Scraper` is the product and repository, `x-collection-workbench` is the Python package, and
`xworkbench` is its command-line program. This recovery build is experimental: its production
parser is proven against local Chromium fixtures, but no live X capture is owner-verified yet.

> **Future demo GIF:** insert the sanitized recording described in [docs/demo.md](docs/demo.md)
> only after every storyboard dependency passes. No live footage or broken placeholder is shipped.

## What you can do

- Approve one bounded capture or an exact saved-source batch, preserving available Posts,
  provenance, partial results, and stop reasons in local SQLite.
- Inspect, search, and export a saved snapshot without making another X request.
- Let a local MCP client read terminal snapshots through bounded, read-only tools.

## Try safely in 60 seconds

From a clean checkout with Python 3.11-3.13, activate a virtual environment and run:

```bash
python -m pip install -e '.[browser,mcp,dev]'
xworkbench setup
xworkbench demo
# Press Ctrl+C after exploring, then:
xworkbench start
```

The demo opens a loopback dashboard with two compatible 25-Post snapshots about a clearly fictional
topic, uses a temporary database, and cannot collect from X. `start` opens the persistent workbench.

For reproducible lock-based installation and Windows PowerShell commands, see
[Getting started](docs/getting-started.md). This project is installed from source; no PyPI release
is claimed.

## Real capture quickstart

Only proceed when you are authorized to access the content and have obtained any permission X's
terms require.

```bash
python -m playwright install chromium
xworkbench auth
xworkbench doctor
xworkbench start
```

`auth` opens a fresh headed Chromium context for normal manual sign-in; the program never asks for
your password. In the loopback dashboard, preview a Browser capture, review the exact source and
1-25 Post budget, then confirm it. Stop on login, challenge, rate limit, or other manual action;
the application does not bypass those states. See [Browser capture](docs/browser-capture.md).

For several sources, save each source first, select 2-25 in **Capture several saved sources**, then
preview and confirm the unchanged server manifest. There is no capture or batch CLI shortcut.

The official X API provider is optional and separately requires a token, an exact preview, and
paid-read confirmation. Its compiler and response mapper are proven only with synthetic data; see
[Official X API](docs/official-x-api.md).

## Connect one local MCP client

Codex documents this stdio registration form:

```bash
codex mcp add xworkbench -- xworkbench mcp
codex mcp list
```

`xworkbench mcp` reads configured SQLite directly without starting the dashboard. The repository
proves the MCP 2.0 stdio server against the real SDK and local storage, but has not
yet recorded an end-to-end Codex client session. Tool bounds and that distinction are documented in
[MCP](docs/mcp.md).

## Capabilities and limits

| Surface | Current evidence | Important limit |
| --- | --- | --- |
| Offline demo | Proven offline | Two synthetic 25-Post snapshots, Changes/search/export, and a real local MCP read |
| SQLite evidence/Changes/export | Proven offline | Freshness is descriptive; reuse and retention are never automatic |
| Bounded saved-source batch | Proven offline | UI only; atomic admission, default one worker and opt-in maximum two |
| Browser capture | Proven local Chromium | Sanitized local fixtures only; live X remains owner-gated |
| Official X API | Proven offline | Synthetic compiler/mapper tests only; no paid live request |
| MCP | Proven offline | Direct read-only SQLite by default; no collection or X writes |
| Live X acceptance | Not yet verified | Session, current DOM, challenge, and rate-limit outcomes are unknown |

## Documentation

- [Getting started](docs/getting-started.md)
- [Browser capture](docs/browser-capture.md)
- [Official X API](docs/official-x-api.md)
- [Storage and cache semantics](docs/storage-and-cache.md)
- [MCP](docs/mcp.md)
- [Configuration](docs/configuration.md)
- [Testing and evidence](docs/testing.md)
- [Demo and recording storyboard](docs/demo.md)
- [Responsible use](docs/responsible-use.md)
- [Roadmap](docs/roadmap.md)
- [Contributing](CONTRIBUTING.md)
- [Verification record](docs/verification.md) and [architecture decision](docs/adr/0001-feed-to-context-providers.md)

Generated state stays under the ignored `var/` directory by default. Treat Bearer Tokens and
Playwright storage state as secrets: never commit, paste, attach, or include them in diagnostics.

## License, use boundary, & note from developer (Alex-lop)

The code is MIT licensed; MIT permits commercial use. The operational boundary below is a project
safety rule and a summary of current platform terms, not a change to that license or legal advice.

So while of course scrapping X/Twitter for commercial gain (like for example a business gaining commercial profit using X's data) is unethical and I personally completely agree with that notion. However, I strongly do believe that independent developers should be able to study publicly available posts for personal projects, research, and experimentation especially as AI changes how we think about access to public data. This software isn't meant to defraud it's really just to experiment and have fun with.

 [X's current Terms](https://x.com/en/tos) 
 For more info you can look at the link above. Final note -- please, do not use this project for evasion, fraud, abuse, private-data collection, or commercial scale harvesting as that could possibly be illegal (just run it with your personal agents as you would like).
