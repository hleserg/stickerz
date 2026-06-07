"""End-to-end orchestration tests with a mock model + fake Bot API."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from io import BytesIO
from pathlib import Path

import pytest
import pytest_asyncio
from PIL import Image, ImageDraw

from sticker_service.config import get_settings
from sticker_service.db import Database
from sticker_service.services.canonical import StyleLoader
from sticker_service.services.models.base import ImageModel
from sticker_service.services.orchestrator import Orchestrator, OrchestratorError
from sticker_service.services.publish import Publisher

MAGENTA = (255, 0, 255, 255)


def _sheet_bytes(n: int = 15) -> bytes:
    """A clean magenta sheet of ``n`` well-separated squares (chroma-sliceable)."""
    from sticker_service.services.postprocess import grid_for

    rows, cols = grid_for(n)
    cell = 120
    sheet = Image.new("RGBA", (cols * cell, rows * cell), MAGENTA)
    draw = ImageDraw.Draw(sheet)
    drawn = 0
    for r in range(rows):
        for c in range(cols):
            if drawn >= n:
                break
            x, y = c * cell + 25, r * cell + 25
            draw.rectangle([x, y, x + 70, y + 70], fill=(0, 120, 200, 255))
            drawn += 1
    buffer = BytesIO()
    sheet.save(buffer, format="PNG")
    return buffer.getvalue()


# build_caption_set() yields the standard block.
EXPECTED = 13  # 12 reactions + «Пока!»


class _SheetModel(ImageModel):
    """Returns a magenta sheet sized to the number of captions in the prompt."""

    name = "sheet"

    def __init__(self) -> None:
        self.generate_calls: list[str] = []

    async def generate(self, prompt: str, refs: Sequence[bytes] = ()) -> bytes:
        self.generate_calls.append(prompt)
        n = prompt.count('"') // 2 or 1  # captions are quoted in the sheet prompt
        return _sheet_bytes(n)

    async def judge_geometry(self, frame_a: bytes, frame_b: bytes) -> float:
        return 0.95

    async def pick_emoji(self, image: bytes) -> str:
        return "🙂"


class _FakeBot:
    def __init__(self) -> None:
        self.created: list[dict[str, object]] = []
        self.added: list[dict[str, object]] = []

    async def create_new_sticker_set(self, **kwargs: object) -> None:
        self.created.append(kwargs)

    async def add_sticker_to_set(self, **kwargs: object) -> None:
        self.added.append(kwargs)

    async def set_sticker_set_thumbnail(self, **kwargs: object) -> None:
        pass


@pytest_asyncio.fixture
async def db() -> AsyncIterator[Database]:
    database = await Database.connect(":memory:")
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture
def loader() -> StyleLoader:
    return StyleLoader(get_settings().styles_dir)


def _orchestrator(db: Database, loader: StyleLoader, bot: _FakeBot, tmp: Path) -> Orchestrator:
    return Orchestrator(
        model=_SheetModel(),
        db=db,
        publisher=Publisher(bot, "yourbot"),
        loader=loader,
        storage_dir=tmp,
    )


async def test_build_canonical_and_save_character(
    db: Database, loader: StyleLoader, tmp_path: Path
) -> None:
    orch = _orchestrator(db, loader, _FakeBot(), tmp_path)
    canonical = await orch.build_canonical(
        photo=b"PHOTO", style_id="watercolor", subject_type="child", child_age=6
    )
    assert canonical.startswith(b"\x89PNG")
    char = await orch.save_character(
        owner_id=1,
        name="Лёшик",
        style_id="watercolor",
        subject_type="child",
        child_age=6,
        canonical=canonical,
    )
    assert char.id > 0
    assert Path(char.canonical_path).exists()  # canonical persisted for reuse (§3.2)


async def test_create_pack_full_flow(db: Database, loader: StyleLoader, tmp_path: Path) -> None:
    bot = _FakeBot()
    orch = _orchestrator(db, loader, bot, tmp_path)
    char = await orch.save_character(
        owner_id=42,
        name="Лёшик",
        style_id="watercolor",
        subject_type="child",
        child_age=6,
        canonical=_sheet_bytes(3),
    )
    result = await orch.create_pack(owner_id=42, character=char)

    assert result.count == EXPECTED
    assert result.set_name.endswith("_by_yourbot")
    assert result.link == f"https://t.me/addstickers/{result.set_name}"
    assert bot.created[0]["user_id"] == 42  # owner = user (§B.4)
    packs = await db.list_packs(42)
    assert len(packs) == 1
    assert await db.count_stickers(packs[0].id) == EXPECTED


async def test_create_pack_reports_stages(
    db: Database, loader: StyleLoader, tmp_path: Path
) -> None:
    orch = _orchestrator(db, loader, _FakeBot(), tmp_path)
    char = await orch.save_character(
        owner_id=1,
        name="A",
        style_id="watercolor",
        subject_type="adult",
        child_age=None,
        canonical=_sheet_bytes(3),
    )
    stages: list[str] = []

    async def on_stage(label: str) -> None:
        stages.append(label)

    await orch.create_pack(owner_id=1, character=char, on_stage=on_stage)
    assert stages == ["sheet", "slice", "emoji", "publish"]


async def test_one_character_many_packs(db: Database, loader: StyleLoader, tmp_path: Path) -> None:
    orch = _orchestrator(db, loader, _FakeBot(), tmp_path)
    char = await orch.save_character(
        owner_id=1,
        name="A",
        style_id="watercolor",
        subject_type="adult",
        child_age=None,
        canonical=_sheet_bytes(3),
    )
    await orch.create_pack(owner_id=1, character=char, title="Pack 1")
    await orch.create_pack(owner_id=1, character=char, title="Pack 2")
    packs = await db.list_packs(1)
    assert len(packs) == 2
    assert all(p.character_id == char.id for p in packs)  # reuse (§3.2)


async def test_extend_pack_appends(db: Database, loader: StyleLoader, tmp_path: Path) -> None:
    bot = _FakeBot()
    orch = _orchestrator(db, loader, bot, tmp_path)
    char = await orch.save_character(
        owner_id=7,
        name="A",
        style_id="watercolor",
        subject_type="adult",
        child_age=None,
        canonical=_sheet_bytes(3),
    )
    await orch.create_pack(owner_id=7, character=char)
    pack = (await db.list_packs(7))[0]

    result = await orch.extend_pack(owner_id=7, pack=pack)
    assert result.set_name == pack.set_name
    assert len(bot.added) == EXPECTED  # appended via add_sticker_to_set
    assert await db.count_stickers(pack.id) == 2 * EXPECTED  # appended, positions continue


async def test_build_then_publish_split(db: Database, loader: StyleLoader, tmp_path: Path) -> None:
    bot = _FakeBot()
    orch = _orchestrator(db, loader, bot, tmp_path)
    char = await orch.save_character(
        owner_id=5,
        name="A",
        style_id="watercolor",
        subject_type="adult",
        child_age=None,
        canonical=_sheet_bytes(EXPECTED),
    )
    # 1) generate without publishing — nothing created yet.
    stickers = await orch.build_stickers(char)
    assert len(stickers) == EXPECTED
    assert bot.created == []
    assert await db.list_packs(5) == []
    # 2) publish the already-generated stickers.
    result = await orch.publish_new(owner_id=5, character=char, stickers=stickers)
    assert result.count == EXPECTED
    assert len(bot.created) == 1
    assert await db.count_stickers((await db.list_packs(5))[0].id) == EXPECTED


async def test_publish_extend_split(db: Database, loader: StyleLoader, tmp_path: Path) -> None:
    bot = _FakeBot()
    orch = _orchestrator(db, loader, bot, tmp_path)
    char = await orch.save_character(
        owner_id=9,
        name="A",
        style_id="watercolor",
        subject_type="adult",
        child_age=None,
        canonical=_sheet_bytes(EXPECTED),
    )
    await orch.create_pack(owner_id=9, character=char)
    pack = (await db.list_packs(9))[0]
    stickers = await orch.build_stickers(char)
    result = await orch.publish_extend(owner_id=9, pack=pack, stickers=stickers)
    assert result.set_name == pack.set_name
    assert len(bot.added) == EXPECTED
    assert await db.count_stickers(pack.id) == 2 * EXPECTED


async def test_unknown_style_raises(db: Database, loader: StyleLoader, tmp_path: Path) -> None:
    orch = _orchestrator(db, loader, _FakeBot(), tmp_path)
    with pytest.raises(OrchestratorError, match="unknown"):
        await orch.build_canonical(
            photo=b"P", style_id="nope", subject_type="adult", child_age=None
        )
