# Development standard

The detail behind [AGENTS.md](../../AGENTS.md). AGENTS.md is intentionally lean
(per evidence that long, generic context files hurt agents); this file holds the
extended rules.

## Philosophy

1. **Explicit over implicit** — state expectations; don't rely on guessing.
2. **Machine-readable standards** — encode rules in tools (ruff, pyright,
   pytest, pre-commit) so they're enforced, not just documented.
3. **Strategic human oversight** — humans decide architecture and review; agents
   implement within a tight gate.
4. **Isolation enables parallelism** — clear module boundaries let multiple
   agents/PRs proceed without collisions.
5. **Context is expensive** — keep instruction files short and high-signal.

## Layout & boundaries

- `src/projectname/` — the package. Separate **core** logic (pure, testable)
  from **adapters** (I/O, external services).
- Environment access lives **only** in `config.py` via `get_settings()`.
- Instrumented dirs (`handlers`/`adapters`/`agents`/`engines`) must call
  `tag_component(...)` (enforced by `tools/check_instrumentation.py`).

## Typing

- All public functions typed; prefer precise types over `Any`.
- `from __future__ import annotations` at the top of modules.
- pyright runs in **standard** mode. (Re-evaluate Astral `ty` once it hits ~1.0
  and high spec conformance — far faster, currently beta.)

## Testing

- `tests/unit/` fast, no external services. `tests/integration/` marked
  `@pytest.mark.integration`.
- Coverage gate **90%**; CLI/entry shells excluded.

## Commits & releases

- **Conventional Commits**; breaking changes use `!` or a `BREAKING CHANGE:`
  footer. Versioning + changelog via **Commitizen** (`cz bump`). Stay in `0.x`
  during early development.

## Documentation

- English canonical; user-facing docs add an `-ru.md` pair.
- Architecture decisions recorded as ADRs in `docs/architecture/ADR/`.

## Definition of Done (full)

`make check` green — `ruff check`, `ruff format --check`, `pyright`, `bandit`,
`pip-audit`, `pytest --cov --cov-fail-under=90`, and PLAYBOOK validation — all
zero errors. Plus tests for new behavior, docs/`.env.example` updated, a
Conventional Commit, and on PRs all bots satisfied + checks green before merge.
