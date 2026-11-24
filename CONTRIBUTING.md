# Contributing to cortex-mcp

Thanks for your interest in improving cortex-mcp. This project accepts
external pull requests; this document covers what to do before you open one.

## Local setup

```bash
# Install all dev dependencies
poetry install

# Lint, format-check, type-check, and test (mirrors CI)
./scripts/verify
```

See the [Development](README.md#development) section of the README for
more detail, including how to run the server locally and debug it with the
MCP Inspector.

## Before opening a pull request

Run `./scripts/verify` and make sure it passes locally — it runs the same
checks as CI (`ruff`, `black --check`, `isort --check`, `mypy src/`,
`pytest`), and a PR won't be merged until all of them are green.

```bash
./scripts/verify
```

## Branch and commit conventions

- Branch from `main`; there's no required naming scheme, but a short,
  descriptive branch name (e.g. `fix/client-timeout`,
  `feat/xql-pagination`) is preferred.
- Commit subjects generally follow `type(scope): short description`
  (e.g. `fix(client): return before the MCP client times out`), matching
  the existing history. This isn't enforced by tooling, but please follow
  it for consistency.
- Keep commits focused; squash-on-merge is fine for cleaning up WIP commits.

## Pull request expectations

- If the PR addresses an open issue, link it and use a closing keyword in
  the description, e.g. `Fixes #123`, so the issue auto-closes on merge.
- Describe what changed and why, and call out anything you deliberately
  left out of scope.
- Keep the diff to the smallest complete change that satisfies the issue —
  avoid bundling unrelated refactors.
- Add or update tests for behavior changes. Docs-only changes don't need
  tests.
- All CI checks (`lint`, `format-check`, `type-check`, `test`,
  `docker-build`) must pass before merge.

## Reporting bugs and security issues

- **Regular bugs:** open a [GitHub issue](../../issues).
- **Security issues:** this repo doesn't have a `SECURITY.md` yet — until
  one is merged, please report security concerns via a
  [GitHub issue](../../issues) marked clearly as security-sensitive, or
  contact a maintainer directly rather than filing a public issue if the
  report includes exploit details.
