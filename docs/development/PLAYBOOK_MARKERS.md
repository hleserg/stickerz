# PLAYBOOK markers

PLAYBOOK markers capture **generalizable** engineering patterns *inline*, right
next to the code or doc that demonstrates them, so they can later be harvested
into a shared playbook (e.g. `hleserg/agent-playbook`).

## When to add one

Add a marker when a piece of code/doc embodies a pattern that passes the
**substitution test**: remove all project-specific nouns and it is still a
useful, reusable idea. When unsure, add it with `status: draft` — erring toward
more markers is preferred; refine or delete later.

Do **not** mark project-specific business logic that wouldn't transfer.

## Format

Place the block immediately above the code it describes.

Python:

```python
# PLAYBOOK-START
# id: retry-with-jitter
# title: Exponential backoff with full jitter
# status: refined
# category: resilience
# tags: [retry, networking]
# Wrap idempotent network calls; cap attempts; add jitter to avoid
# thundering herds. Generalizes to any flaky downstream dependency.
# PLAYBOOK-END
def call_with_retry(...): ...
```

## Fields

| Field | Required | Notes |
|-------|----------|-------|
| `id` | yes | short kebab-case, unique-ish |
| `title` | yes | human-readable |
| `status` | yes | `draft` or `refined` |
| `category` | no | grouping (e.g. observability, resilience) |
| `tags` | no | `[a, b]` list |
| notes | no | free-form lines until `PLAYBOOK-END` |

## Tooling

- `python scripts/extract_playbook.py` — list/summarize markers.
- `python scripts/extract_playbook.py --check` — validate (pre-commit + CI).
- `python scripts/extract_playbook.py --output playbook.json` — export (weekly
  `playbook-sync` workflow uploads this).
