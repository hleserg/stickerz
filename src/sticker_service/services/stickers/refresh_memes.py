"""Weekly meme-pool refresh: the text model rewrites the pool against trends.

Runs inside the bot process (wired in ``bot.py``): a background loop wakes a
few times a day and, once the stored pool is older than ``meme_refresh_days``,
asks the text model (search-grounded Gemini) for an updated pool — dropping
stale memes, adding current ones, keeping the evergreens, with the configured
Runet/~60-40-female slant. The reply is validated by the same strict
``parse_pool`` used for the bundled file and additionally run through caption
moderation; on ANY doubt the old pool stays. The refreshed pool lives in the
DB ``config`` table, so deploys and restarts keep it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sticker_service.services.models.base import ImageModel, ModelError
from sticker_service.services.moderation import is_clean
from sticker_service.services.stickers.meme_pool import (
    MIN_ITEMS,
    POOL_CONFIG_KEY,
    POOL_REFRESHED_KEY,
    MemeIdea,
    active_pool,
    parse_pool,
)

if TYPE_CHECKING:
    from sticker_service.db import Database

logger = logging.getLogger(__name__)

#: Staleness is checked a few times a day; the refresh itself runs weekly.
_CHECK_EVERY_S = 6 * 3600.0

_PROMPT_HEADER = """\
Ты — редактор пула идей для Telegram-стикеров с рисованным персонажем.
Пул используется как случайная подборка «мемной бытовухи» для стикерпаков.
Аудитория: рунет, русскоязычная молодёжь; крен примерно 60/40 в сторону девушек.

Задача: обнови пул под АКТУАЛЬНЫЕ тренды рунета прямо сейчас (поищи свежие мемы,
сленг и форматы реакций последнего месяца). Правила:
- выкинь устаревшее и отгоревшее, добавь свежее; вечную классику оставь;
- ровно 100 пунктов; каждый — сцена/поза/эмоция персонажа, смешная и живая;
- подпись добавляй только там, где она усиливает; короткая, разговорная, русская;
- без политики, брендов, знаменитостей, оскорблений, мата и 18+;
- description ≤ 200 символов, caption ≤ 80, category ≤ 60.

Ответь ТОЛЬКО валидным JSON без пояснений и без markdown-ограждений, в формате:
{"items": [{"category": "...", "description": "...", "caption": "..." | null}, ...]}

Текущий пул:
"""


def _pool_payload(items: list[MemeIdea]) -> str:
    doc = {
        "items": [
            {"category": i.category, "description": i.description, "caption": i.caption}
            for i in items
        ]
    }
    return json.dumps(doc, ensure_ascii=False)


def build_refresh_prompt(current: list[MemeIdea]) -> str:
    """The full instruction plus the current pool for the model to rewrite."""
    return _PROMPT_HEADER + _pool_payload(current)


def strip_code_fences(text: str) -> str:
    """Drop a ``` / ```json wrapper if the model added one despite instructions."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else ""
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
    return cleaned.strip()


def _moderated(items: list[MemeIdea]) -> list[MemeIdea]:
    """Drop any model-written idea that trips the caption moderation."""
    return [
        i for i in items if is_clean(i.description) and (i.caption is None or is_clean(i.caption))
    ]


async def refresh_meme_pool(
    model: ImageModel, db: Database, *, now: datetime | None = None
) -> bool:
    """One refresh attempt; ``True`` when a new pool was stored.

    Defensive by design: a model error, malformed JSON, or a pool that shrinks
    below :data:`MIN_ITEMS` after moderation leaves the previous pool in place.
    """
    try:
        prompt = build_refresh_prompt(await active_pool(db))
        reply = await model.generate_text(prompt)
        items = _moderated(parse_pool(strip_code_fences(reply)))
    except (ModelError, ValueError) as exc:
        logger.warning("meme refresh skipped: %s", str(exc)[:200])
        return False
    if len(items) < MIN_ITEMS:
        logger.warning("meme refresh skipped: only %d clean items in the reply", len(items))
        return False
    await db.set_config(POOL_CONFIG_KEY, _pool_payload(items))
    stamp = (now or datetime.now(UTC)).isoformat()
    await db.set_config(POOL_REFRESHED_KEY, stamp)
    logger.info("meme pool refreshed: %d ideas (%s)", len(items), stamp)
    return True


async def pool_is_stale(db: Database, *, days: int, now: datetime | None = None) -> bool:
    """True when the stored pool was never refreshed or is older than ``days``."""
    raw = await db.get_config(POOL_REFRESHED_KEY)
    if not raw:
        return True
    try:
        refreshed = datetime.fromisoformat(raw)
    except ValueError:
        return True
    return (now or datetime.now(UTC)) - refreshed >= timedelta(days=days)


async def meme_refresh_loop(
    model: ImageModel,
    db: Database,
    *,
    days: int,
    check_every_s: float = _CHECK_EVERY_S,
) -> None:
    """Background loop: refresh the pool whenever it goes stale.

    ``days <= 0`` disables the feature. Each iteration is fully guarded — the
    loop survives anything except cancellation (bot shutdown).
    """
    if days <= 0:
        return
    while True:
        try:
            if await pool_is_stale(db, days=days):
                await refresh_meme_pool(model, db)
        except Exception:  # the loop must outlive any single failure
            logger.exception("meme refresh iteration failed")
        await asyncio.sleep(check_every_s)
