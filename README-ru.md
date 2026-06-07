# projectname

> TODO: однострочное описание проекта.

[English version](README.md)

Python-стартеркит, заточенный под работу с кодинг-агентами (Claude Code, Cursor):
`uv` + `ruff` + `pyright` + `pytest`, лаконичный машиночитаемый `AGENTS.md`,
единые ворота качества `make check`, Sentry с приоритетом приватности,
PLAYBOOK-маркеры и CI из коробки.

---

## Как пользоваться шаблоном

1. Нажми **«Use this template» -> Create a new repository** на GitHub.
2. Клонируй и запусти init-скрипт (переименует пакет, вычистит плейсхолдеры):
   ```bash
   python scripts/init_template.py --name your_package --description "Что делает"
   ```
3. Установка и проверка зелёного базлайна:
   ```bash
   uv sync --all-extras
   make check
   ```
4. Прочитай **[AGENTS.md](AGENTS.md)** — это контракт для всех агентов и людей.

## Быстрый старт

```bash
uv sync --all-extras
cp .env.example .env
uv run projectname --version
make check
```

## Инструменты

uv (окружение/зависимости), ruff (линт+формат), pyright (типы),
pytest (тесты, >=90%), bandit/pip-audit (безопасность), pre-commit,
commitizen (conventional commits -> версия + changelog), Sentry.

## Лицензия

MIT — см. [LICENSE](LICENSE). Если форкаешь copyleft-код (например AGPL) — поменяй.
