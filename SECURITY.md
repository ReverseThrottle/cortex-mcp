# Security Policy

cortex-mcp bridges an AI client to a Cortex tenant and holds PAPI credentials
in its process environment. If you find a vulnerability, please report it
privately rather than opening a public issue.

## Supported versions

This project ships a single rolling release from the `main` branch; there is
no parallel maintenance of older release lines. Security fixes land on
`main` and are published as the next tagged release / container image
(`ghcr.io/reversethrottle/cortex-mcp:latest`). Please upgrade to the latest
release before reporting, and reproduce against `main` if possible.

| Version | Supported |
| --- | --- |
| Latest release / `main` | :white_check_mark: |
| Older tags | :x: |

## Reporting a vulnerability

**Do not open a public GitHub issue or discussion for a security report.**

Use GitHub's [Private Vulnerability Reporting](https://github.com/ReverseThrottle/cortex-mcp/security/advisories/new)
for this repository (Security tab → "Report a vulnerability"). This opens a
private advisory visible only to the maintainers and lets us collaborate on
a fix before any public disclosure.

If you cannot use that flow, open a GitHub issue asking a maintainer to
contact you, without including any vulnerability details, and we will
follow up with a private channel.

Please include, where possible:

- A description of the issue and its potential impact (e.g. credential
  exposure, SSRF, path traversal, auth bypass).
- Steps to reproduce, or a minimal proof of concept.
- The affected version/commit and environment configuration.

## Response expectations

- **Acknowledgement:** within 5 business days of the report.
- **Triage / next steps:** within 10 business days, including whether the
  report is accepted, needs more information, or is declined.
- **Fix & disclosure:** timeline depends on severity and complexity; we aim
  to ship a fix and coordinate disclosure with the reporter once a patch is
  available. Critical issues (e.g. credential leakage, RCE) are prioritized
  ahead of other work.

We do not currently operate a bug bounty program.
