# projectname

> TODO: one-line project description.

[![CI](https://github.com/hleserg/projectname/actions/workflows/ci.yml/badge.svg)](https://github.com/hleserg/projectname/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/)

[Русская версия](README-ru.md)

A Python starter kit wired for working with coding agents (Claude Code, Cursor):
`uv` + `ruff` + `pyright` + `pytest`, a lean machine-readable `AGENTS.md`, a single
`make check` quality gate, privacy-first Sentry, PLAYBOOK markers, and CI out of the box.

---

## Using this template

1. Click **"Use this template" -> Create a new repository** on GitHub.
2. Clone it and run the init script to rename the package and clear placeholders:
   ```bash
   python scripts/init_template.py --name your_package --description "What it does"
   ```
3. Install and verify the green baseline:
   ```bash
   uv sync --all-extras
   make check        # lint + format + types + security + tests, all green
   ```
4. Read **[AGENTS.md](AGENTS.md)** — it is the contract every agent (and human) follows.

## Quickstart

```bash
uv sync --all-extras        # create .venv and install everything
cp .env.example .env        # fill in secrets locally (never commit)
uv run projectname --version
make check                  # the Definition-of-Done gate
```

## Project layout

| Path | Purpose |
|------|---------|
| `src/projectname/` | the package (src-layout, fully typed, ships `py.typed`) |
| `src/projectname/config.py` | typed settings via `pydantic-settings` |
| `src/projectname/observability/` | Sentry init (`send_default_pii=False`) + component tags |
| `tests/` | `unit/` + `integration/`, pytest with >=90% coverage gate |
| `docs/` | architecture (ADRs), development standard, PLAYBOOK marker spec |
| `scripts/` | `init_template.py`, `extract_playbook.py` |
| `tools/` | `check_instrumentation.py` (pre-commit guard) |
| `AGENTS.md` | canonical agent instructions (the open standard) |
| `CLAUDE.md` | imports `AGENTS.md` + Claude-specific notes |

## Make targets

```
make install     # uv sync --all-extras + pre-commit install
make check       # lint + fmt-check + type + security + tests  (DoD gate)
make test        # full test suite with coverage
make test-fast   # unit tests only, parallel, no coverage
make lint        # ruff check
make fmt         # ruff format
make type        # pyright
make playbook    # extract PLAYBOOK markers
```

## Tooling

- **uv** — environment & dependency management (lockfile committed)
- **ruff** — lint + format
- **pyright** — static type checking (standard mode)
- **pytest** — tests, >=90% coverage
- **bandit / pip-audit** — security
- **pre-commit** — local gate
- **commitizen** — conventional commits -> version bump + changelog
- **Sentry** — error/perf monitoring, privacy-first

## License

MIT — see [LICENSE](LICENSE). Note: if you fork copyleft code (e.g. AGPL), change this.
