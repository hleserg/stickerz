"""End-to-end orchestration tying the pipeline stages together (§3.1, §3.2).

Wires the building blocks into the two product actions:
- **new pack**  — canonical → sheet → slice → emojis → publish → persist;
- **add to pack** — reuse the saved character's canonical, generate, append.

The canonical is saved once on the Character and **reused** for every pack about
that person (§3.2 / §B.4), so packs stay stylistically consistent.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sticker_service.config import get_settings
from sticker_service.db import Character, Database, Pack, Sticker
from sticker_service.db.models import SubjectType
from sticker_service.services.canonical.engine import CanonicalEngine
from sticker_service.services.canonical.loader import StyleLoader
from sticker_service.services.canonical.schema import Style
from sticker_service.services.models.base import ImageModel
from sticker_service.services.postprocess import apply_watermark, grid_for, process_sheet
from sticker_service.services.publish import (
    MAX_STICKERS_PER_SET,
    Publisher,
    capacity_error,
)
from sticker_service.services.publish.naming import build_set_name
from sticker_service.services.publish.publisher import StickerInput
from sticker_service.services.stickers import (
    MAX_CAPTIONS,
    PER_PAGE,
    assign_emojis,
    build_caption_set,
    generate_sheet,
)

logger = logging.getLogger(__name__)

# Awaited with a short stage label as work really progresses ("sheet", "clean",
# "slice", "emoji", "publish") — the flow renders these as live status lines.
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


@dataclass(frozen=True)
class ReviewBundle:
    """Everything the review/preview step needs after generation, before publish."""

    character: Character
    stickers: list[StickerInput]
    title: str
    pack_id: int | None
    mode: str


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
        self._engine = engine or CanonicalEngine(model)

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

    async def validate_photo(self, image: bytes) -> str | None:
        """Vision foolproof check on upload; returns a problem code or None."""
        from sticker_service.services.photo_check import validate_photo

        return await validate_photo(self._model, image)

    async def save_character(
        self,
        *,
        owner_id: int,
        name: str,
        style_id: str,
        subject_type: SubjectType,
        child_age: int | None,
        canonical: bytes,
        photo: bytes | None = None,
    ) -> Character:
        """Persist the confirmed canonical as a reusable Character (§3.2).

        The source ``photo`` is kept too (alpha) so the canonical can be inspected
        and redrawn later.
        """
        path = self._write(self._storage / "canonical" / f"{owner_id}_{name}.png", canonical)
        photo_path = (
            str(self._write(self._storage / "photos" / f"{owner_id}_{name}.jpg", photo))
            if photo
            else None
        )
        return await self._db.add_character(
            owner_id=owner_id,
            name=name,
            style_id=style_id,
            subject_type=subject_type,
            child_age=child_age,
            canonical_path=str(path),
            photo_path=photo_path,
        )

    async def redraw_canonical(
        self,
        character: Character,
        photo: bytes,
        *,
        on_step: StepCallback | None = None,
    ) -> bytes:
        """Rebuild a character's canonical from a fresh photo, replacing the old one."""
        canonical = await self.build_canonical(
            photo=photo,
            style_id=character.style_id,
            subject_type=character.subject_type,
            child_age=character.child_age,
            on_step=on_step,
        )
        path = self._write(
            self._storage / "canonical" / f"{character.owner_id}_{character.name}.png", canonical
        )
        photo_path = self._write(
            self._storage / "photos" / f"{character.owner_id}_{character.name}.jpg", photo
        )
        await self._db.update_character_canonical(
            character.id, canonical_path=str(path), photo_path=str(photo_path)
        )
        return canonical

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

    async def build_for_review(
        self,
        *,
        mode: str,
        owner_id: int,
        captions: list[str],
        on_step: StepCallback | None = None,
        on_stage: StageCallback | None = None,
        photo: bytes | None = None,
        style_id: str | None = None,
        subject_type: SubjectType | None = None,
        child_age: int | None = None,
        name: str | None = None,
        character_id: int | None = None,
        pack_id: int | None = None,
    ) -> ReviewBundle:
        """Resolve the character (fresh/reuse/extend), generate stickers, save a draft.

        Centralises the three-mode branching that the conversational flow used to
        inline, so the generate→draft path is testable end-to-end (e.g. with the
        mock model). ``extend`` appends to an existing pack and does not save a new
        draft; ``fresh``/``reuse`` persist an unpublished draft for later publish.
        """
        if mode == "extend":
            if pack_id is None:
                raise OrchestratorError("extend mode needs a pack_id")
            pack = await self._db.get_pack(pack_id)
            if pack is None:
                raise OrchestratorError("pack not found")
            # Capacity pre-check BEFORE generating (and before the caller charges):
            # an extend that can't fit the 120-sticker limit must fail for free,
            # not after a paid generation that publish_extend would then reject.
            current = await self._db.count_stickers(pack.id)
            if len(captions) + current > MAX_STICKERS_PER_SET:
                raise capacity_error(pack.title, current)
            character = await self._db.get_character(pack.character_id)
            title: str = pack.title
        elif mode == "reuse":
            if character_id is None:
                raise OrchestratorError("reuse mode needs a character_id")
            character = await self._db.get_character(character_id)
            title = character.name if character else ""
            pack_id = None
        else:  # fresh — build the canonical now
            if photo is None or style_id is None or subject_type is None:
                raise OrchestratorError("fresh mode needs photo, style_id and subject_type")
            canonical = await self.build_canonical(
                photo=photo,
                style_id=style_id,
                subject_type=subject_type,
                child_age=child_age,
                on_step=on_step,
            )
            character = await self.save_character(
                owner_id=owner_id,
                name=name or "Мой пак",
                style_id=style_id,
                subject_type=subject_type,
                child_age=child_age,
                canonical=canonical,
                photo=photo,
            )
            title = character.name
            pack_id = None
        if character is None:
            raise OrchestratorError("character not found")
        stickers = await self.build_stickers(character, captions=captions, on_stage=on_stage)
        if mode != "extend":
            draft = await self.save_draft(
                owner_id=owner_id, character=character, title=title, stickers=stickers
            )
            pack_id = draft.id
        return ReviewBundle(
            character=character, stickers=stickers, title=title, pack_id=pack_id, mode=mode
        )

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
            character_id=character.id,
            owner_id=owner_id,
            set_name=set_name,
            title=title,
            published=True,
        )
        await self._persist_pairs(pack, stickers, start=0)
        logger.info("publish: done set=%s stickers=%d", set_name, len(stickers))
        return PackResult(set_name, self._publisher.link(set_name), len(stickers))

    async def save_draft(
        self,
        *,
        owner_id: int,
        character: Character,
        title: str,
        stickers: list[StickerInput],
    ) -> Pack:
        """Persist generated stickers as an UNPUBLISHED pack for later publish/download."""
        # Hidden machine name reserved now; replaced with the real one on publish.
        placeholder = build_set_name(title, self._publisher.bot_username)
        pack = await self._db.add_pack(
            character_id=character.id,
            owner_id=owner_id,
            set_name=placeholder,
            title=title,
            published=False,
        )
        await self._persist_pairs(pack, stickers, start=0)
        logger.info("draft saved: pack=%s stickers=%d", pack.id, len(stickers))
        return pack

    async def publish_draft(
        self, *, owner_id: int, pack: Pack, stickers: list[StickerInput]
    ) -> PackResult:
        """Publish a previously-saved draft pack to Telegram and mark it published."""
        set_name = await self._publisher.create_pack(
            user_id=owner_id, title=pack.title, stickers=list(stickers)
        )
        await self._db.update_pack(pack.id, set_name=set_name, published=True)
        logger.info("publish draft: pack=%s set=%s", pack.id, set_name)
        return PackResult(set_name, self._publisher.link(set_name), len(stickers))

    async def load_pack_stickers(self, pack_id: int) -> list[StickerInput]:
        """Read a pack's persisted sticker files + emoji from disk."""
        rows = await self._db.list_stickers(pack_id)
        return [(Path(s.file_path).read_bytes(), s.emoji) for s in rows]

    async def gc_stale_drafts(self, *, older_than_days: int) -> int:
        """Delete unpublished drafts older than the window + their PNGs.

        Drafts are ephemeral (created mid-flow, then published or abandoned), so
        an old one is safe to remove; this bounds disk/DB growth on the small
        VDS. Published packs are never touched. Returns the count removed; a
        non-positive window disables the sweep.
        """
        if older_than_days <= 0:
            return 0
        cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
        drafts = await self._db.list_stale_drafts(cutoff)
        for pack in drafts:
            stickers = await self._db.list_stickers(pack.id)
            await self._db.delete_pack(pack.id)
            self._remove_sticker_files(pack, stickers)
        if drafts:
            logger.info("gc: removed %d stale draft pack(s)", len(drafts))
        return len(drafts)

    def _remove_sticker_files(self, pack: Pack, stickers: list[Sticker]) -> None:
        """Best-effort delete a draft's PNGs and its per-set directory."""
        for sticker in stickers:
            with contextlib.suppress(OSError):
                Path(sticker.file_path).unlink(missing_ok=True)
        set_dir = self._storage / "stickers" / pack.set_name
        with contextlib.suppress(OSError):
            if set_dir.is_dir():
                shutil.rmtree(set_dir)

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
        stickers: list[bytes] = []
        # Stage labels are emitted right BEFORE the work they describe, so the
        # live status line always tells the truth about what is running now.
        for page_no, page in enumerate(pages, start=1):
            await self._stage(on_stage, "sheet")
            logger.info("sheet: page %d/%d (%d captions)", page_no, len(pages), len(page))
            sheet = await generate_sheet(
                self._model,
                canonical,
                style,
                page,
                subject_type=character.subject_type,
                child_age=character.child_age,
            )
            await self._stage(on_stage, "clean")
            sliced = process_sheet(sheet, grid=grid_for(len(page)), expected=len(page))
            await self._stage(on_stage, "slice")
            stickers.extend(sliced)
        if not stickers:  # pragma: no cover - defensive
            raise OrchestratorError("slicing produced no stickers")
        settings = get_settings()
        if settings.watermark_enabled:
            stickers = [apply_watermark(s, text=settings.watermark_text) for s in stickers]
        logger.info("slice: produced %d stickers total", len(stickers))
        await self._stage(on_stage, "emoji")
        emojis = await assign_emojis(self._model, stickers, captions)
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
