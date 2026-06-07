#!/usr/bin/env python
"""Initialize a fresh project from this template.

Renames the ``projectname`` package, fills in the description, resets the
version to 0.1.0, and clears the changelog. Run once right after creating a
repo from the template:

    python scripts/init_template.py --name my_package --description "What it does"

Use --dry-run to preview changes.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

OLD_PKG = "projectname"
TEXT_SUFFIXES = {".py", ".md", ".toml", ".yml", ".yaml", ".cfg", ".txt", ".json"}
SKIP_DIRS = {".venv", ".git", "node_modules", "__pycache__", ".ruff_cache", ".pytest_cache"}
NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _iter_text_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for p in root.rglob("*"):
        if p.is_dir() or any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.suffix in TEXT_SUFFIXES:
            out.append(p)
    return out


def run(name: str, description: str, root: Path, *, dry_run: bool) -> int:
    if not NAME_RE.match(name):
        msg = f"Invalid package name '{name}': use lowercase letters/digits/underscore."
        print(msg, file=sys.stderr)
        return 2

    src_old = root / "src" / OLD_PKG
    src_new = root / "src" / name
    if src_old.exists() and not dry_run:
        src_old.rename(src_new)
    print(f"package dir: {src_old} -> {src_new}")

    changed = 0
    for path in _iter_text_files(root):
        if path.name == "init_template.py":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        new = text.replace(OLD_PKG, name)
        if description:
            new = new.replace("TODO: one-line project description.", description)
            new = new.replace("TODO: однострочное описание проекта.", description)
        if new != text:
            changed += 1
            if not dry_run:
                path.write_text(new, encoding="utf-8")

    changelog = root / "CHANGELOG.md"
    if changelog.exists() and not dry_run:
        changelog.write_text(
            "# Changelog\n\nManaged by Commitizen (`cz bump`).\n\n## [Unreleased]\n",
            encoding="utf-8",
        )

    verb = "would change" if dry_run else "changed"
    print(f"{verb} {changed} file(s).")
    if dry_run:
        print("dry run — nothing written.")
    else:
        print("done. Next: `uv sync --all-extras` then `make check`.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Initialize a project from the template.")
    parser.add_argument("--name", required=True, help="new package name (snake_case)")
    parser.add_argument("--description", default="", help="one-line project description")
    parser.add_argument("--root", type=Path, default=Path("."), help="repo root")
    parser.add_argument("--dry-run", action="store_true", help="preview only")
    args = parser.parse_args(argv)
    return run(args.name, args.description, args.root, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
