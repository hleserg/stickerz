#!/usr/bin/env python
"""Extract and validate PLAYBOOK markers across the repo.

A PLAYBOOK marker documents a generalizable engineering pattern inline, next to
the code that illustrates it. See docs/development/PLAYBOOK_MARKERS.md.

A real marker is a multi-line block that contains an ``id`` field:

    # PLAYBOOK-START
    # id: short-kebab-id
    # title: Human readable title
    # status: draft | refined
    # PLAYBOOK-END

Usage:
    python scripts/extract_playbook.py            # summary
    python scripts/extract_playbook.py --check     # validate, non-zero on error
    python scripts/extract_playbook.py --output out.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

START = "PLAYBOOK-START"  # split so this file isn't self-detected
END = "PLAYBOOK-END"
REQUIRED_FIELDS = ("id", "title", "status")
VALID_STATUS = {"draft", "refined"}
SCAN_SUFFIXES = {".py", ".md"}
SKIP_DIRS = {".venv", ".git", "node_modules", "__pycache__", ".ruff_cache", ".pytest_cache"}
# Files that teach/define the marker format — their examples are not real markers.
SELF_DOC = {"extract_playbook.py", "PLAYBOOK_MARKERS.md"}
FIELD_RE = re.compile(r"^[#<!\-\s]*([a-zA-Z_]+):\s*(.+?)\s*$")
FENCE_RE = re.compile(r"```.*?```", re.DOTALL)


@dataclass
class Marker:
    file: str
    line: int
    fields: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def _iter_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if path.is_dir() or path.name in SELF_DOC:
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix in SCAN_SUFFIXES:
            files.append(path)
    return files


def _strip_fences(text: str, suffix: str) -> str:
    return FENCE_RE.sub("", text) if suffix == ".md" else text


def _parse_block(lines: list[str], start_idx: int, file: str) -> Marker | None:
    marker = Marker(file=file, line=start_idx + 1)
    saw_end = False
    for raw in lines[start_idx + 1 :]:
        if END in raw:
            saw_end = True
            break
        m = FIELD_RE.match(raw)
        if m:
            key, value = m.group(1).lower(), m.group(2)
            if key in {"id", "title", "status", "category", "tags"}:
                marker.fields[key] = value
    # Inline prose mention (START and END on one line) or a block without an
    # id is documentation, not a marker — ignore it silently.
    if not saw_end or "id" not in marker.fields:
        return None
    for req in REQUIRED_FIELDS:
        if req not in marker.fields:
            marker.errors.append(f"missing required field: {req}")
    status = marker.fields.get("status")
    if status and status not in VALID_STATUS:
        marker.errors.append(f"invalid status '{status}' (expected {sorted(VALID_STATUS)})")
    return marker


def extract(root: Path) -> list[Marker]:
    markers: list[Marker] = []
    for path in _iter_files(root):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if START not in text:
            continue
        lines = _strip_fences(text, path.suffix).splitlines()
        for i, line in enumerate(lines):
            if START in line and END not in line:
                parsed = _parse_block(lines, i, str(path))
                if parsed is not None:
                    markers.append(parsed)
    return markers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Extract/validate PLAYBOOK markers.")
    parser.add_argument("--check", action="store_true", help="validate; exit non-zero on error")
    parser.add_argument("--output", type=Path, help="write markers as JSON to this path")
    parser.add_argument("--root", type=Path, default=Path("."), help="repo root to scan")
    args = parser.parse_args(argv)

    markers = extract(args.root)
    errored = [m for m in markers if m.errors]

    if args.output:
        payload = [{"file": m.file, "line": m.line, **m.fields} for m in markers]
        args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Found {len(markers)} PLAYBOOK marker(s).")
    for m in markers:
        flag = "  [x]" if m.errors else "  -"
        print(f"{flag} {m.fields.get('id', '<no-id>')} - {m.file}:{m.line}")
        for err in m.errors:
            print(f"       - {err}")

    if args.check and errored:
        print(f"\n{len(errored)} marker(s) failed validation.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
