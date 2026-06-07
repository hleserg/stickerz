"""End-to-end orchestration tying the pipeline stages together (§3.1, §3.2).

Wires the building blocks into the two product actions:
- **new pack**  — canonical → sheet → slice → emojis → publish → persist;
- **add to pack** — reuse the saved character's canonical, generate, append.

The canonical is saved once on the Character and **reused** for every pack about
that person (§3.2 / §B.4), so packs stay stylistically consistent.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from sticker_service.db import Character, Database, Pack
from sticker_service.db.models import SubjectType
from sticker_service.services.canonical.engine import CanonicalEngine
from sticker_service.services.canonical.loader import StyleLoader
from sticker_service.services.canonical.schema import Style
from sticker_service.services.models.base import ImageModel
from sticker_service.services.postprocess import grid_for, process_sheet
from sticker_service.services.publish import Publisher
from sticker_service.services.publish.publisher import StickerInput
from sticker_service.services.stickers import (
    MAX_CAPTIONS,
    PER_PAGE,
    assign_emojis,
    build_caption_set,
    generate_sheet,
)

logger = logging.getLogger(__name__)

# Awaited with a short human stage label ("sheet", "slice", "emoji", "publish").
StageCallback = Callable[[str], Awaitable[None]]
StepCallback = Callable[[int, int], Awaitable[None]]


class OrchestratorError(RuntimeError):
    """A pipeline stage could not complete."""


@dataclass(frozen=True)
class PackResult:
    """Outcome of building a pack."""

    set_name: str
    link: str
    count: int


class Orchestrator:
    """Coordinates canonical generation, sheet build, slicing, and publishing."""

    def __init__(
        self,
        *,
        model: ImageModel,
        db: Database,
        publisher: Publisher,
        loader: StyleLoader,
        storage_dir: Path,
        engine: CanonicalEngine | None = None,
    ) -> None:
        self._model = model
        self._db = db
        self._publisher = publisher
        self._loader = loader
        self._storage = Path(storage_dir)
        self._engine = engine or CanonicalEngine(model, gate_threshold=0.5, max_step_retries=1)

    async def build_canonical(
        self,
        *,
        photo: bytes,
        style_id: str,
        subject_type: SubjectType,
        child_age: int | None,
        on_step: StepCallback | None = None,
    ) -> bytes:
        """Run the canonical pipeline for a style; returns canonical bytes."""
        style = self._require_style(style_id)
        return await self._engine.run(
            style, photo, subject_type=subject_type, child_age=child_age, on_step=on_step
        )

    async def save_character(
        self,
        *,
        owner_id: int,
        name: str,
        style_id: str,
        subject_type: SubjectType,
        child_age: int | None,
        canonical: bytes,
    ) -> Character:
        """Persist the confirmed canonical as a reusable Character (§3.2)."""
        path = self._write(self._storage / "canonical" / f"{owner_id}_{name}.png", canonical)
        return await self._db.add_character(
            owner_id=owner_id,
            name=name,
            style_id=style_id,
            subject_type=subject_type,
            child_age=child_age,
            canonical_path=str(path),
        )

    async def build_stickers(
        self,
        character: Character,
        *,
        captions: list[str] | None = None,
        on_stage: StageCallback | None = None,
    ) -> list[StickerInput]:
        """Generate the sliced stickers (sheet → slice → emoji) WITHOUT publishing.

        ``captions`` lets the caller pass an explicit set (selected standard +
        custom). Pages of >12 are generated as separate sheets. Used to show a
        preview before the user confirms publication.
        """
        stickers, emojis = await self._generate_stickers(character, captions, on_stage)
        return list(zip(stickers, emojis, strict=True))

    async def publish_new(
        self,
        *,
        owner_id: int,
        character: Character,
        stickers: list[StickerInput],
        title: str | None = None,
    ) -> PackResult:
        """Publish already-generated stickers as a new Telegram set + persist."""
        title = title or character.name
        logger.info("publish: creating set owner=%s title=%s", owner_id, title)
        set_name = await self._publisher.create_pack(
            user_id=owner_id, title=title, stickers=list(stickers)
        )
        pack = await self._db.add_pack(
            character_id=character.id, owner_id=owner_id, set_name=set_name, title=title
        )
        await self._persist_pairs(pack, stickers, start=0)
        logger.info("publish: done set=%s stickers=%d", set_name, len(stickers))
        return PackResult(set_name, self._publisher.link(set_name), len(stickers))

    async def publish_extend(
        self, *, owner_id: int, pack: Pack, stickers: list[StickerInput]
    ) -> PackResult:
        """Append already-generated stickers to an existing set + persist."""
        current = await self._db.count_stickers(pack.id)
        await self._publisher.add_to_pack(
            user_id=owner_id, set_name=pack.set_name, stickers=list(stickers), current_count=current
        )
        await self._persist_pairs(pack, stickers, start=current)
        return PackResult(pack.set_name, self._publisher.link(pack.set_name), len(stickers))

    async def create_pack(
        self,
        *,
        owner_id: int,
        character: Character,
        title: str | None = None,
        captions: list[str] | None = None,
        on_stage: StageCallback | None = None,
    ) -> PackResult:
        """Convenience: generate + publish a new pack in one shot."""
        stickers = await self.build_stickers(character, captions=captions, on_stage=on_stage)
        await self._stage(on_stage, "publish")
        return await self.publish_new(
            owner_id=owner_id, character=character, stickers=stickers, title=title
        )

    async def extend_pack(
        self,
        *,
        owner_id: int,
        pack: Pack,
        captions: list[str] | None = None,
        on_stage: StageCallback | None = None,
    ) -> PackResult:
        """Convenience: generate + append to an existing pack in one shot (§3.2)."""
        character = await self._db.get_character(pack.character_id)
        if character is None:  # pragma: no cover - referential integrity
            raise OrchestratorError(f"pack {pack.id} references missing character")
        stickers = await self.build_stickers(character, captions=captions, on_stage=on_stage)
        await self._stage(on_stage, "publish")
        return await self.publish_extend(owner_id=owner_id, pack=pack, stickers=stickers)

    # --- internals -----------------------------------------------------------

    async def _generate_stickers(
        self,
        character: Character,
        captions: list[str] | None,
        on_stage: StageCallback | None = None,
    ) -> tuple[list[bytes], list[str]]:
        style = self._require_style(character.style_id)
        canonical = Path(character.canonical_path).read_bytes()
        captions = (captions if captions is not None else build_caption_set())[:MAX_CAPTIONS]
        pages = [captions[i : i + PER_PAGE] for i in range(0, len(captions), PER_PAGE)]
        await self._stage(on_stage, "sheet")
        stickers: list[bytes] = []
        for page_no, page in enumerate(pages, start=1):
            logger.info("sheet: page %d/%d (%d captions)", page_no, len(pages), len(page))
            sheet = await generate_sheet(
                self._model,
                canonical,
                style,
                page,
                subject_type=character.subject_type,
                child_age=character.child_age,
            )
            stickers.extend(process_sheet(sheet, grid=grid_for(len(page))))
        if not stickers:  # pragma: no cover - defensive
            raise OrchestratorError("slicing produced no stickers")
        await self._stage(on_stage, "slice")
        logger.info("slice: produced %d stickers total", len(stickers))
        await self._stage(on_stage, "emoji")
        emojis = await assign_emojis(self._model, stickers)
        return stickers, emojis

    @staticmethod
    async def _stage(on_stage: StageCallback | None, label: str) -> None:
        logger.info("stage: %s", label)
        if on_stage is not None:
            await on_stage(label)

    async def _persist_pairs(self, pack: Pack, stickers: list[StickerInput], *, start: int) -> None:
        for offset, (image, emoji) in enumerate(stickers):
            position = start + offset
            path = self._write(
                self._storage / "stickers" / pack.set_name / f"{position:03d}.png", image
            )
            await self._db.add_sticker(
                pack_id=pack.id, file_path=str(path), emoji=emoji, position=position
            )

    def _require_style(self, style_id: str) -> Style:
        style = self._loader.get(style_id)
        if style is None:
            raise OrchestratorError(f"unknown or invalid style: {style_id!r}")
        return style

    @staticmethod
    def _write(path: Path, data: bytes) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path
