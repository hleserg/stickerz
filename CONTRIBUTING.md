# Contributing

Thanks for contributing. This repo is built to be worked on by humans **and**
coding agents — the contract is in [AGENTS.md](AGENTS.md). Read it first.

## Setup

```bash
uv sync --all-extras
uv run pre-commit install
make check        # confirm a green baseline
```

## Workflow

1. Pick/open an issue; set it **In Progress** in Linear before coding.
2. Branch: `feat/<slug>`, `fix/<slug>`, `docs/<slug>`.
3. Make a small, focused change. Add tests.
4. `make check` must be green (it is the Definition of Done).
5. Commit using **Conventional Commits** (`feat:`, `fix:`, `feat!:` …).
6. Open a PR using the template; address every bot comment and red check.

## Conventions

- Python >= 3.12, fully typed, `src/` layout.
- Env access only through `projectname.config.get_settings()`.
- Secrets in `.env`; document keys in `.env.example`.
- Docs English-canonical; add `-ru.md` for user-facing docs.
- Reusable patterns get a PLAYBOOK marker (`docs/development/PLAYBOOK_MARKERS.md`).

## License

By contributing you agree your contributions are licensed under the MIT License.
