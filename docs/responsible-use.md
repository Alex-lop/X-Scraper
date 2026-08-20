# Responsible use

This document states the project's operating boundary; it does not promise that any use is lawful
or contractually permitted. Platform terms and applicable law are different sources of obligations.
Obtain appropriate advice for your jurisdiction, account, authorization, research protocol, and use
case.

## Platform terms

[X's current Terms](https://x.com/en/tos) state that scraping in any form and for any purpose without
prior written consent is prohibited and prohibit working around technical limitations or
authentication/security measures. Personal, research, or noncommercial intent does not itself
create permission.

Use of the official API is separately governed by the
[X Developer Policy](https://docs.x.com/developer-terms/policy), the approved use case, account
entitlements, content/privacy rules, rate and distribution limits, and credential safeguards.
Technical availability is not authorization.

All official sources on this page were checked on 2026-08-19. Re-check the current primary text
before any real use.

## Project boundary

Use this workbench only for bounded content you are authorized to access. Keep a human in the
approval loop, minimize collection, retain only necessary fields, protect local snapshots, and stop
on login, challenge, rate limit, account-access, or other manual-action states.

Do not use or extend it for stealth/fingerprint manipulation, cookie extraction from unrelated
profiles, private GraphQL replay, automated challenge solving, evasion, credential abuse, account
or identity rotation, block-triggered proxy changes, X write actions, private-data collection, or
commercial-scale harvesting. Do not republish content or personal data merely because it was
visible to an authenticated account.

The local-only dashboard and terminal surfaces, small budgets, explicit previews, stop behavior,
and read-only MCP reduce technical scope. They do not make a use compliant, licensed, fair,
permitted, or safe by themselves.

## Capability lab boundary

The repository's [hard-isolated capability lab](capability-lab.md) uses only disposable synthetic
identities, secrets, challenges, operations, and routes on fixture-owned loopback listeners. It is
a test of fixed mechanisms and fail-closed boundaries, not a production feature or an evasion
toolkit. Every analogous production mechanism remains prohibited and unreachable.

Local synthetic proof does not authorize an external test. Any external security test would need
explicit written authorization from the service owner covering the exact target, accounts,
controls, volume, and test window. This project and its MIT license provide none.

## Law and the EU research process

The EU [Digital Services Act, Regulation (EU) 2022/2065](https://eur-lex.europa.eu/eli/reg/2022/2065/oj)
contains an Article 40 data-access framework. Qualified systemic-risk researchers have a formal
[DSA Data Access Portal](https://data-access.dsa.ec.europa.eu/) process with eligibility, affiliation,
independence, security, proportionality, and reasoned-request requirements. The European Commission
also publishes the [delegated act on DSA data access](https://digital-strategy.ec.europa.eu/en/library/delegated-act-data-access-under-digital-services-act-dsa).

That application-based route is not general permission for personal or noncommercial scraping and
does not authorize bypass. Researchers should use the formal process and obtain institutional and
legal guidance rather than infer permission from this software.

## License

The repository's MIT license permits commercial use of the software. A safety preference in this
documentation does not convert MIT into a noncommercial license. Conversely, the software license
does not grant rights to access X, use an account, collect content, process personal data, or
redistribute third-party material.

When in doubt, do not make the request. Use the synthetic demo and local fixtures while you resolve
authorization, terms, privacy, and retention questions.
