.DEFAULT_GOAL := help
.PHONY: help install check lint fmt fmt-check type security test test-fast playbook docs clean

PY := uv run

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Create venv, install deps, set up pre-commit
	uv sync --all-extras
	$(PY) pre-commit install

check: lint fmt-check type security test ## Definition-of-Done gate (run before every PR)
	@echo "all checks passed"

lint: ## Ruff lint
	$(PY) ruff check .

fmt: ## Ruff format (write)
	$(PY) ruff format .

fmt-check: ## Ruff format (check only)
	$(PY) ruff format --check .

type: ## Pyright type check
	$(PY) pyright

security: ## Bandit + pip-audit (OSV fallback when the default PyPI service is unreachable)
	$(PY) bandit -c pyproject.toml -r src/ -q
	$(PY) pip-audit || $(PY) pip-audit --vulnerability-service osv

test: ## Full test suite with coverage gate
	$(PY) pytest --cov --cov-fail-under=90

test-fast: ## Unit tests only, parallel, no coverage
	$(PY) pytest tests/unit -n auto -q

playbook: ## Extract PLAYBOOK markers
	$(PY) python scripts/extract_playbook.py

docs: ## Serve docs locally (requires mkdocs)
	$(PY) mkdocs serve

clean: ## Remove caches and build artifacts
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov dist build *.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} +
