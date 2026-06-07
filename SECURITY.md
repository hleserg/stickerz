# Security Policy

## Reporting a vulnerability

Please **do not** open public issues for security problems. Email the
maintainer (see repository profile / commit history) with a description and
impact, steps to reproduce, and the affected version/commit. Expect an
acknowledgement within a few days.

## Handling sensitive data

- Sentry runs with `send_default_pii=False` and an extended `EventScrubber`.
  Do not weaken this. See `src/projectname/observability/sentry.py`.
- Never log raw user input, prompts, completions, or embeddings at INFO+.
- Secrets live only in `.env` (git-ignored). Rotate anything committed by
  accident immediately.
- Dependency and code scanning run in CI (`pip-audit`, `bandit`) and weekly.
