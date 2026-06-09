#!/bin/bash
# SessionStart hook: make tests/linters runnable in Claude Code on the web.
# Installs the full dev environment from the committed uv.lock (idempotent,
# cache-friendly — the container state is cached after the hook completes).
set -euo pipefail

# Web-only: local sessions manage their own environment.
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-$(pwd)}"

# uv drives everything in this repo (see AGENTS.md). Bootstrap if missing.
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
  if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
    echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$CLAUDE_ENV_FILE"
  fi
fi

# THE install command (AGENTS.md): everything (extras + dev tools) into .venv.
uv sync --all-extras

echo "session-start: dependencies ready (uv sync --all-extras)"
