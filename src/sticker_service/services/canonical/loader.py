"""Load, validate, and cache style plugins from the ``styles/`` folder (§5.1.1).

The folder is scanned **once** (on start or explicit reload), each YAML is
validated against the pydantic schema, and the result is cached in memory — the
bot menu is built from the cache, never from disk per click. A broken file is
logged with its reason and skipped; the bot keeps running on valid styles.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml
from pydantic import ValidationError

from sticker_service.services.canonical.schema import Style

logger = logging.getLogger(__name__)


# PLAYBOOK-START
# id: data-driven-plugin-loader
# title: Validate-and-cache plugin folder, skip-not-crash on bad files
# status: draft
# category: architecture
# tags: [plugins, pydantic, yaml, data-driven]
# Scan a folder of declarative plugin files once, validate each against a
# schema, cache the valid ones in memory, and log+skip the broken ones so one
# malformed file never takes the service down. Substitution test passes:
# applies to any data-driven plugin system, not just styles.
# PLAYBOOK-END
class StyleLoader:
    """Owns the in-memory cache of valid styles for a ``styles/`` directory."""

    def __init__(self, styles_dir: str | Path) -> None:
        self._dir = Path(styles_dir)
        self._styles: dict[str, Style] = {}
        self._loaded = False

    def load(self) -> dict[str, Style]:
        """Scan + validate the folder, refresh the cache, return valid styles."""
        styles: dict[str, Style] = {}
        if not self._dir.is_dir():
            logger.warning("styles dir %s does not exist; no styles loaded", self._dir)
            self._styles, self._loaded = styles, True
            return styles

        for path in sorted(self._dir.glob("*.yaml")):
            style = self._load_one(path)
            if style is None:
                continue
            if style.style_id in styles:
                logger.error("duplicate style_id '%s' in %s; skipping", style.style_id, path.name)
                continue
            styles[style.style_id] = style

        self._styles, self._loaded = styles, True
        logger.info("loaded %d style(s): %s", len(styles), ", ".join(sorted(styles)))
        return styles

    def _load_one(self, path: Path) -> Style | None:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            logger.error("cannot read/parse style %s: %s", path.name, exc)
            return None
        try:
            style = Style.model_validate(raw)
        except ValidationError as exc:
            logger.error("invalid style %s: %s", path.name, exc)
            return None
        if style.style_id != path.stem:
            logger.error(
                "style_id '%s' must match filename '%s'; skipping", style.style_id, path.stem
            )
            return None
        return style

    @property
    def styles(self) -> dict[str, Style]:
        """The cached valid styles (loads once lazily if needed)."""
        if not self._loaded:
            self.load()
        return self._styles

    def get(self, style_id: str) -> Style | None:
        """Return a cached style by id, or ``None`` if absent/invalid."""
        return self.styles.get(style_id)

    def menu(self) -> list[tuple[str, str]]:
        """``(style_id, display_name)`` for enabled styles, for the bot menu."""
        return [(s.style_id, s.display_name) for s in self.styles.values() if s.enabled]

    def reload(self) -> dict[str, Style]:
        """Re-scan the folder (admin ``/reload_styles``)."""
        return self.load()
