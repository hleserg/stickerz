# CLAUDE.md

Claude Code does not read `AGENTS.md` automatically, so this file imports it.
**The single source of truth is `AGENTS.md` — read it first.**

@AGENTS.md

## Claude-specific notes

- Treat `make check` as the Definition of Done. Run it before declaring a task
  complete; paste the failing output and fix, never skip.
- Prefer small, reviewable diffs. One logical change per PR.
- When you discover a reusable pattern, add a PLAYBOOK marker (see
  `docs/development/PLAYBOOK_MARKERS.md`) rather than only describing it in chat.
- Keep edits to this file and `AGENTS.md` minimal and high-signal; push detail
  into `docs/development/DEVELOPMENT_STANDARD.md`.
