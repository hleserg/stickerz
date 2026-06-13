"""Tests for the /upload ingest path: ZIP validation, sheet slicing, publish."""

from __future__ import annotations

import io
import zipfile
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from PIL import Image, ImageDraw

from sticker_service.config import get_settings
from sticker_service.db import Database
from sticker_service.handlers import upload as upload_handlers
from sticker_service.services import analytics
from sticker_service.services.canonical import StyleLoader
from sticker_service.services.models import MockImageModel
from sticker_service.services.orchestrator import UPLOADED_STYLE_ID, Orchestrator
from sticker_service.services.postprocess import slice_uploaded_sheet
from sticker_service.services.publish import Publisher
from sticker_service.services.stickers.upload import (
    MAX_ZIP_BYTES,
    MAX_ZIP_FILES,
    ZipRejectedError,
    extract_zip_stickers,
    looks_like_sticker_sheet,
)


def _png(size: tuple[int, int] = (200, 240), color: tuple[int, ...] = (10, 120, 200, 255)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def _zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


# --- ZIP validation -----------------------------------------------------------


def test_zip_extracts_images_in_order_as_512_png() -> None:
    stickers = extract_zip_stickers(_zip({"a.png": _png(), "b.png": _png((300, 100))}))
    assert len(stickers) == 2
    for data in stickers:
        image = Image.open(io.BytesIO(data))
        assert image.format == "PNG"
        assert max(image.size) == 512


def test_zip_rejects_garbage_and_empty() -> None:
    with pytest.raises(ZipRejectedError, match="обычный ZIP"):
        extract_zip_stickers(b"not a zip at all")
    with pytest.raises(ZipRejectedError, match="пустой"):
        extract_zip_stickers(_zip({}))
    with pytest.raises(ZipRejectedError, match="не похож на картинку"):
        extract_zip_stickers(_zip({"a.png": _png(), "readme.txt": b"hello"}))


def test_zip_rejects_too_many_files() -> None:
    tiny = _png((8, 8))
    too_many = {f"{i}.png": tiny for i in range(MAX_ZIP_FILES + 1)}
    with pytest.raises(ZipRejectedError, match="больше"):
        extract_zip_stickers(_zip(too_many))


def test_zip_rejects_declared_bomb() -> None:
    # 90 MB of zeros deflates to ~90 KB: the archive passes the size cap, but
    # the DECLARED unpacked size must reject it before a single entry is read.
    bomb = zipfile.ZipInfo("bomb.png")
    bomb.compress_type = zipfile.ZIP_DEFLATED
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(bomb, b"\x00" * (90 * 1024 * 1024))
    assert len(buffer.getvalue()) < MAX_ZIP_BYTES  # the bomb is small on the wire
    with pytest.raises(ZipRejectedError, match="слишком большим"):
        extract_zip_stickers(buffer.getvalue())


def test_zip_skips_macos_junk_dirs() -> None:
    stickers = extract_zip_stickers(_zip({"__MACOSX/._a.png": b"junk", "real.png": _png()}))
    assert len(stickers) == 1


# --- vision gate ----------------------------------------------------------------


async def test_looks_like_sticker_sheet_verdicts() -> None:
    assert await looks_like_sticker_sheet(MockImageModel(ask_answer="ДА"), b"img") is True
    assert await looks_like_sticker_sheet(MockImageModel(ask_answer="Нет."), b"img") is False
    assert await looks_like_sticker_sheet(MockImageModel(ask_answer=""), b"img") is None


async def test_looks_like_sticker_sheet_fails_open() -> None:
    class _Boom(MockImageModel):
        async def ask(self, image: bytes, question: str) -> str:
            raise RuntimeError("vision down")

    assert await looks_like_sticker_sheet(_Boom(), b"img") is None


# --- sheet slicing (any solid background / transparency) -----------------------


def _sheet(bg: tuple[int, int, int, int], n: int = 4) -> bytes:
    """2×2 grid of well-separated squares on the given background."""
    cell = 150
    sheet = Image.new("RGBA", (2 * cell, 2 * cell), bg)
    draw = ImageDraw.Draw(sheet)
    for i in range(n):
        x, y = (i % 2) * cell + 30, (i // 2) * cell + 30
        draw.rectangle([x, y, x + 90, y + 90], fill=(200, 30, 30, 255))
    buffer = io.BytesIO()
    sheet.save(buffer, format="PNG")
    return buffer.getvalue()


def test_slice_uploaded_sheet_solid_background() -> None:
    pieces = slice_uploaded_sheet(_sheet((240, 255, 240, 255)))
    assert len(pieces) == 4
    assert all(Image.open(io.BytesIO(p)).format == "PNG" for p in pieces)


def test_slice_uploaded_sheet_transparent_background() -> None:
    pieces = slice_uploaded_sheet(_sheet((0, 0, 0, 0)))
    assert len(pieces) == 4


def test_slice_uploaded_sheet_caps_piece_count() -> None:
    # A crafted confetti picture must be refused, not turned into thousands of
    # pieces that each cost a paid vision call later.
    assert slice_uploaded_sheet(_sheet((240, 255, 240, 255)), max_pieces=3) == []


def test_slice_uploaded_sheet_refuses_gradient() -> None:
    cell = 150
    sheet = Image.new("RGBA", (2 * cell, 2 * cell))
    for x in range(sheet.width):  # horizontal gradient: no honest single bg
        for y in range(sheet.height):
            sheet.putpixel((x, y), (x % 256, (x * 2) % 256, 255 - x % 256, 255))
    buffer = io.BytesIO()
    sheet.save(buffer, format="PNG")
    assert slice_uploaded_sheet(buffer.getvalue()) == []


# --- orchestrator: prepare + publish-new ---------------------------------------


class _FakeBot:
    def __init__(self) -> None:
        self.created: list[dict[str, object]] = []

    async def create_new_sticker_set(self, **kwargs: object) -> None:
        self.created.append(kwargs)

    async def add_sticker_to_set(self, **kwargs: object) -> None:
        pass

    async def set_sticker_set_thumbnail(self, **kwargs: object) -> None:
        pass


@pytest_asyncio.fixture
async def db() -> AsyncIterator[Database]:
    database = await Database.connect(":memory:")
    try:
        yield database
    finally:
        await database.close()


def _orchestrator(db: Database, tmp_path: object) -> Orchestrator:
    return Orchestrator(
        model=MockImageModel(emoji="🔥"),
        db=db,
        publisher=Publisher(_FakeBot(), "yourbot"),
        loader=StyleLoader(get_settings().styles_dir),
        storage_dir=tmp_path,  # type: ignore[arg-type]
    )


async def test_prepare_upload_parks_scratch_with_placeholder_emojis(
    db: Database, tmp_path: object
) -> None:
    # Vision emoji picking is deferred to publish: free ingest retries must
    # never burn paid per-sticker calls.
    from sticker_service.services.stickers import DEFAULT_EMOJI

    orch = _orchestrator(db, tmp_path)
    scratch, stickers = await orch.prepare_upload(owner_id=5, images=[_png(), _png()])
    assert len(stickers) == 2
    assert all(emoji == DEFAULT_EMOJI for _, emoji in stickers)
    assert await orch.load_scratch(scratch) == stickers  # survives until publish


async def test_publish_upload_new_creates_set_behind_synthetic_character(
    db: Database, tmp_path: object
) -> None:
    orch = _orchestrator(db, tmp_path)
    scratch, _ = await orch.prepare_upload(owner_id=5, images=[_png(), _png()])
    result = await orch.publish_upload_new(owner_id=5, title="Мои стики", scratch_path=scratch)
    assert result.count == 2
    packs = await db.list_packs(5)
    assert len(packs) == 1 and packs[0].published
    chars = await db.list_characters(5)
    assert len(chars) == 1 and chars[0].style_id == UPLOADED_STYLE_ID
    assert await orch.load_scratch(scratch) == []  # scratch dropped after publish
    rows = await db.list_stickers(packs[0].id)
    assert all(s.emoji == "🔥" for s in rows)  # vision emojis applied at publish


async def test_publish_upload_reuses_one_synthetic_character(
    db: Database, tmp_path: object
) -> None:
    # Repeat uploads (and failed-publish retries) must not pile up orphan rows.
    orch = _orchestrator(db, tmp_path)
    for title in ("Первый", "Второй"):
        scratch, _ = await orch.prepare_upload(owner_id=5, images=[_png()])
        await orch.publish_upload_new(owner_id=5, title=title, scratch_path=scratch)
    assert len(await db.list_packs(5)) == 2
    assert len(await db.list_characters(5)) == 1  # one synthetic backer, reused


async def test_publish_upload_chunks_big_batches_for_telegram(
    db: Database, tmp_path: object
) -> None:
    # createNewStickerSet takes at most 50 initial stickers; the rest must be
    # appended — otherwise every 51-120 ZIP dies at the very last step.
    class _CountingBot(_FakeBot):
        def __init__(self) -> None:
            super().__init__()
            self.added = 0

        async def add_sticker_to_set(self, **kwargs: object) -> None:
            self.added += 1

    bot = _CountingBot()
    orch = Orchestrator(
        model=MockImageModel(emoji="🔥"),
        db=db,
        publisher=Publisher(bot, "yourbot"),
        loader=StyleLoader(get_settings().styles_dir),
        storage_dir=tmp_path,  # type: ignore[arg-type]
    )
    tiny = _png((16, 16))
    scratch, _ = await orch.prepare_upload(owner_id=5, images=[tiny] * 60)
    result = await orch.publish_upload_new(owner_id=5, title="Большой", scratch_path=scratch)
    assert result.count == 60
    created_batch = bot.created[-1]["stickers"]
    assert len(created_batch) == 50  # type: ignore[arg-type]
    assert bot.added == 10  # the tail rides addStickerToSet


async def test_publish_upload_resumes_after_tail_failure_without_duplicate_set(
    db: Database, tmp_path: object
) -> None:
    # createNewStickerSet succeeds, the >50 tail add fails → retry must CONTINUE
    # the same set from its live count, not mint a duplicate (finding #3/#19).
    class _FlakyTailBot(_FakeBot):
        def __init__(self) -> None:
            super().__init__()
            self.added = 0
            self.live = 0
            self.fail_tail = True

        async def create_new_sticker_set(self, **kwargs: object) -> None:
            await super().create_new_sticker_set(**kwargs)
            self.live = len(kwargs["stickers"])  # type: ignore[arg-type]

        async def add_sticker_to_set(self, **kwargs: object) -> None:
            if self.fail_tail and self.added == 0:
                raise RuntimeError("Bad Request: tail add boom")
            self.added += 1
            self.live += 1

        async def get_sticker_set(self, *, name: str) -> object:
            return SimpleNamespace(stickers=[object()] * self.live, title="Большой")

    bot = _FlakyTailBot()
    orch = Orchestrator(
        model=MockImageModel(emoji="🔥"),
        db=db,
        publisher=Publisher(bot, "yourbot"),
        loader=StyleLoader(get_settings().styles_dir),
        storage_dir=tmp_path,  # type: ignore[arg-type]
    )
    scratch, _ = await orch.prepare_upload(owner_id=5, images=[_png((16, 16))] * 60)
    with pytest.raises(Exception, match="tail add boom"):
        await orch.publish_upload_new(owner_id=5, title="Большой", scratch_path=scratch)
    assert len(bot.created) == 1 and bot.live == 50  # set created, tail not yet in

    bot.fail_tail = False
    result = await orch.publish_upload_new(owner_id=5, title="Большой", scratch_path=scratch)
    assert len(bot.created) == 1  # NO duplicate createNewStickerSet
    assert result.count == 60 and bot.live == 60  # tail finished on the retry
    assert len(await db.list_packs(5)) == 1  # one pack, not two


# --- packs adopted by link (owner's spec, 13.06) --------------------------------


class _LinkedSetBot(_FakeBot):
    """Serves a published foreign-to-the-DB set with 7 stickers."""

    def __init__(self) -> None:
        super().__init__()
        self.downloads = 0

    async def get_sticker_set(self, *, name: str) -> object:
        stickers = [SimpleNamespace(file_id=f"f{i}") for i in range(7)]
        return SimpleNamespace(title="Старый пак", stickers=stickers)

    async def download(self, file_id: str) -> io.BytesIO:
        self.downloads += 1
        return io.BytesIO(_png((64, 64)))


def _link_orchestrator(db: Database, tmp_path: object, bot: _FakeBot) -> Orchestrator:
    return Orchestrator(
        model=MockImageModel(emoji="🔥"),
        db=db,
        publisher=Publisher(bot, "yourbot"),
        loader=StyleLoader(get_settings().styles_dir),
        storage_dir=tmp_path,  # type: ignore[arg-type]
    )


async def test_adopt_link_pack_builds_grid_canonical(db: Database, tmp_path: object) -> None:
    from sticker_service.services.orchestrator import OrchestratorError

    bot = _LinkedSetBot()
    orch = _link_orchestrator(db, tmp_path, bot)
    pack = await orch.adopt_pack_by_link(
        owner_id=5, text="https://t.me/addstickers/old_abc_by_yourbot"
    )
    assert pack.published and pack.title == "Старый пак"
    assert bot.downloads == 6  # only the first 6 stickers feed the 2×3 grid
    chars = await db.list_characters(5)
    assert len(chars) == 1 and chars[0].style_id == orch.LINK_PACK_STYLE
    grid = Image.open(chars[0].canonical_path)
    assert grid.size[0] < grid.size[1]  # 2 cols × 3 rows: portrait grid

    again = await orch.adopt_pack_by_link(owner_id=5, text="old_abc_by_yourbot")
    assert again.id == pack.id  # second adoption returns the same pack

    with pytest.raises(OrchestratorError, match="другому пользователю"):
        await orch.adopt_pack_by_link(owner_id=6, text="old_abc_by_yourbot")


async def test_adopt_link_pack_refuses_foreign_and_garbage(db: Database, tmp_path: object) -> None:
    from sticker_service.services.orchestrator import OrchestratorError

    orch = _link_orchestrator(db, tmp_path, _LinkedSetBot())
    with pytest.raises(OrchestratorError, match="созданный этим ботом"):
        await orch.adopt_pack_by_link(owner_id=5, text="t.me/addstickers/x_by_other_bot")
    with pytest.raises(OrchestratorError, match="созданный этим ботом"):
        await orch.adopt_pack_by_link(owner_id=5, text="просто текст")

    class _NoSuchSet(_FakeBot):
        async def get_sticker_set(self, *, name: str) -> object:
            raise RuntimeError("Bad Request: STICKERSET_INVALID")

    orch_missing = _link_orchestrator(db, tmp_path, _NoSuchSet())
    with pytest.raises(OrchestratorError, match="не нашёл"):
        await orch_missing.adopt_pack_by_link(owner_id=5, text="ghost_by_yourbot")


# --- the command gate -----------------------------------------------------------


async def test_cmd_upload_gated_until_first_generation(db: Database, tmp_path: object) -> None:
    orch = _orchestrator(db, tmp_path)
    msg = AsyncMock()
    msg.from_user = SimpleNamespace(id=7)
    state = AsyncMock()
    state.get_data.return_value = {}
    await upload_handlers.cmd_upload(msg, state, db, orch)
    assert "первого" in msg.answer.await_args.args[0]  # refused: no generation yet
    state.set_state.assert_not_awaited()

    await analytics.log(db, 7, analytics.GENERATION_DONE, mode="fresh")
    await upload_handlers.cmd_upload(msg, state, db, orch)
    state.set_state.assert_awaited_with(upload_handlers.Upload.media)


async def test_cmd_upload_reentry_drops_previous_scratch(db: Database, tmp_path: object) -> None:
    # /upload in the middle of an unfinished upload must not orphan the old
    # scratch dir until the 30-day GC.
    orch = _orchestrator(db, tmp_path)
    await analytics.log(db, 7, analytics.GENERATION_DONE, mode="fresh")
    scratch, _ = await orch.prepare_upload(owner_id=7, images=[_png()])
    msg = AsyncMock()
    msg.from_user = SimpleNamespace(id=7)
    state = AsyncMock()
    state.get_data.return_value = {"scratch_path": scratch}
    await upload_handlers.cmd_upload(msg, state, db, orch)
    assert await orch.load_scratch(scratch) == []  # dropped on re-entry


async def test_pack_live_count_prefers_telegram(db: Database, tmp_path: object) -> None:
    # Capacity decisions follow the live set (the owner prunes by hand);
    # the DB rows are only the fallback when Telegram can't answer.
    class _LiveBot(_FakeBot):
        async def get_sticker_set(self, *, name: str) -> object:
            return SimpleNamespace(stickers=[object()] * 84)

    orch_live = Orchestrator(
        model=MockImageModel(emoji="🔥"),
        db=db,
        publisher=Publisher(_LiveBot(), "yourbot"),
        loader=StyleLoader(get_settings().styles_dir),
        storage_dir=tmp_path,  # type: ignore[arg-type]
    )
    scratch, _ = await orch_live.prepare_upload(owner_id=3, images=[_png()])
    await orch_live.publish_upload_new(owner_id=3, title="Серг", scratch_path=scratch)
    pack = (await db.list_packs(3))[0]
    assert await orch_live.pack_live_count(pack) == 84  # live wins over 1 DB row

    orch_blind = _orchestrator(db, tmp_path)  # _FakeBot has no get_sticker_set
    assert await orch_blind.pack_live_count(pack) == 1  # falls back to rows


async def test_extend_wizard_refuses_uploaded_packs(db: Database, tmp_path: object) -> None:
    # /addto into an upload-backed pack would walk the whole caption wizard
    # and die at create time on the synthetic style — refuse at the door.
    from aiogram.types import Message as TgMessage

    from sticker_service.handlers import flow

    orch = _orchestrator(db, tmp_path)
    scratch, _ = await orch.prepare_upload(owner_id=9, images=[_png()])
    await orch.publish_upload_new(owner_id=9, title="Загруженный", scratch_path=scratch)
    pack = (await db.list_packs(9))[0]

    cb = AsyncMock()
    cb.from_user = SimpleNamespace(id=9)
    cb.data = f"extend:{pack.id}"
    cb.message = AsyncMock(spec=TgMessage)
    cb.message.answer = AsyncMock()
    await flow.on_pick_pack(cb, AsyncMock(), db, orch)
    answer_call = cb.message.answer.await_args
    assert answer_call is not None and "/upload" in answer_call.args[0]
