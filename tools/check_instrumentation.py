#!/usr/bin/env python
"""Guard: modules in instrumented areas must tag their Sentry component.

Any Python file under the instrumented directories (handlers / adapters /
agents / engines) should call ``tag_component(...)`` so errors are attributable
in Sentry. Runs in pre-commit over staged files (passed as argv) or, with no
args, scans the whole tree.
"""

from __future__ import annotations

import sys
from pathlib import Path

INSTRUMENTED_PARTS = {"handlers", "adapters", "agents", "engines"}
NEEDLE = "tag_component"


def _is_instrumented(path: Path) -> bool:
    return bool(INSTRUMENTED_PARTS.intersection(path.parts)) and path.suffix == ".py"


def _candidates(argv: list[str]) -> list[Path]:
    if argv:
        return [Path(a) for a in argv]
    return list(Path("src").rglob("*.py")) if Path("src").exists() else []


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    offenders: list[str] = []
    for path in _candidates(args):
        if not path.exists() or not _is_instrumented(path):
            continue
        if path.name == "__init__.py":
            continue
        if NEEDLE not in path.read_text(encoding="utf-8", errors="ignore"):
            offenders.append(str(path))

    if offenders:
        print("Sentry instrumentation missing (expected a tag_component call):", file=sys.stderr)
        for o in offenders:
            print(f"  - {o}", file=sys.stderr)
        print('Add tag_component("<name>") or move logic out of the instrumented dir.')
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
