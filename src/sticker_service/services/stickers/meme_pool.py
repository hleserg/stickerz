"""Meme-idea pool for default packs: curated Runet humour (§6.1).

The bundled ``meme_pool.json`` is the hand-curated baseline. A refreshed copy
may live in the DB ``config`` table under :data:`POOL_CONFIG_KEY` (written by
the weekly trend refresh) and takes precedence when it parses cleanly — so a
bad refresh can never break pack creation, it just falls back to the baseline.

Each idea is a scene description plus an optional exact caption;
``as_sheet_item`` renders it the way the sheet prompt understands («…» marks
an exact caption, «Без подписи.» forbids text on that tile).
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass
from importlib import resources
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sticker_service.db import Database

logger = logging.getLogger(__name__)

#: DB config keys: the active pool JSON and the ISO timestamp of its last refresh.
POOL_CONFIG_KEY = "meme_pool"
POOL_REFRESHED_KEY = "meme_pool_refreshed_at"

#: Validation bounds — generous, but tight enough to reject model garbage.
MIN_ITEMS = 40
_MAX_ITEMS = 200
_MAX_DESCRIPTION = 200
_MAX_CAPTION = 80
_MAX_CATEGORY = 60

#: Default mix: ALWAYS 13 pre-filled items — 6-8 standard reactions, the rest
#: (5-7) meme ideas. 13 keeps room for 2 user ideas under the 15-per-sheet cap.
DEFAULT_TOTAL = 13
_STD_RANGE = (6, 8)


@dataclass(frozen=True)
class MemeIdea:
    """One sticker idea: a scene description and an optional exact caption."""

    category: str
    description: str
    caption: str | None

    def as_sheet_item(self) -> str:
        """Render for the sheet prompt: quoted exact caption or an explicit no-text."""
        if self.caption:
            return f"{self.description} Подпись: «{self.caption}»"
        return f"{self.description} Без подписи."


def parse_pool(raw: str) -> list[MemeIdea]:
    """Parse and validate a pool JSON; raise ``ValueError`` on anything off-shape.

    Strict on purpose — this also guards the weekly model-written refresh, so a
    malformed or truncated reply must never replace a working pool. Duplicate
    descriptions are dropped quietly (models love repeating themselves).
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"meme pool: not valid JSON ({exc})") from exc
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list) or not MIN_ITEMS <= len(items) <= _MAX_ITEMS:
        raise ValueError(f"meme pool: items must be a list of {MIN_ITEMS}..{_MAX_ITEMS}")
    out: list[MemeIdea] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("meme pool: item is not an object")
        description = item.get("description")
        caption = item.get("caption")
        category = item.get("category") or ""
        if (
            not isinstance(description, str)
            or not description.strip()
            or len(description) > _MAX_DESCRIPTION
        ):
            raise ValueError("meme pool: bad description")
        if caption is not None and (
            not isinstance(caption, str) or not caption.strip() or len(caption) > _MAX_CAPTION
        ):
            raise ValueError("meme pool: bad caption")
        if not isinstance(category, str) or len(category) > _MAX_CATEGORY:
            raise ValueError("meme pool: bad category")
        key = description.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(
            MemeIdea(
                category=category.strip(),
                description=description.strip(),
                caption=caption.strip() if isinstance(caption, str) else None,
            )
        )
    if len(out) < MIN_ITEMS:
        raise ValueError("meme pool: too few unique items")
    return out


def bundled_pool() -> list[MemeIdea]:
    """Load the baseline pool shipped inside the package."""
    raw = (
        resources.files("sticker_service.services.stickers")
        .joinpath("meme_pool.json")
        .read_text("utf-8")
    )
    return parse_pool(raw)


async def active_pool(db: Database) -> list[MemeIdea]:
    """The refreshed pool from the DB when present and valid, else the baseline."""
    raw = await db.get_config(POOL_CONFIG_KEY)
    if raw:
        try:
            return parse_pool(raw)
        except ValueError:
            logger.warning("meme pool: stored copy is invalid, falling back to bundled")
    return bundled_pool()


def sample_default_mix(
    pool: list[MemeIdea], *, rng: random.Random | None = None
) -> tuple[list[int], list[str]]:
    """Random default pack: ``(standard indices, meme sheet-items)``.

    Always exactly :data:`DEFAULT_TOTAL` (13) pre-filled items: 6-8 random
    standard reactions topped up with 5-7 random meme ideas. ``sample`` draws
    without replacement, so nothing repeats within the set; only a pool shorter
    than the top-up can yield fewer than 13.
    """
    from sticker_service.services.stickers.sets import STANDARD_BLOCK

    rng = rng or random.Random()  # nosec B311 - variety, not crypto
    std = sorted(rng.sample(range(len(STANDARD_BLOCK)), k=rng.randint(*_STD_RANGE)))
    want = min(DEFAULT_TOTAL - len(std), len(pool))
    memes = [idea.as_sheet_item() for idea in rng.sample(pool, k=want)] if want > 0 else []
    return std, memes
