"""End-to-end orchestration tests with a mock model + fake Bot API."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Sequence
from io import BytesIO
from pathlib import Path

import pytest
import pytest_asyncio
from PIL import Image, ImageDraw

from sticker_service.config import get_settings
from sticker_service.db import Database, Pack
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

    async def generate(self, prompt: str, refs: Sequence[bytes] = (), **_: object) -> bytes:
        self.generate_calls.append(prompt)
        # One numbered "Ideas:" line per sticker (standard items are no longer quoted).
        n = len(re.findall(r"^\d+\. ", prompt, flags=re.MULTILINE)) or 1
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
    assert stages == ["sheet", "clean", "slice", "emoji", "publish"]
    # Contract with the UI: every emitted label has a live status line.
    from sticker_service.handlers.flow import _STAGE_TEXT

    assert set(stages) <= set(_STAGE_TEXT)


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


async def test_draft_lifecycle(db: Database, loader: StyleLoader, tmp_path: Path) -> None:
    bot = _FakeBot()
    orch = _orchestrator(db, loader, bot, tmp_path)
    char = await orch.save_character(
        owner_id=3,
        name="A",
        style_id="watercolor",
        subject_type="adult",
        child_age=None,
        canonical=_sheet_bytes(EXPECTED),
    )
    stickers = await orch.build_stickers(char)
    # Save as a draft — persisted but not published.
    pack = await orch.save_draft(owner_id=3, character=char, title="A", stickers=stickers)
    assert pack.published is False
    assert bot.created == []
    assert await db.count_stickers(pack.id) == EXPECTED

    # Re-load stickers from disk (e.g. next session).
    loaded = await orch.load_pack_stickers(pack.id)
    assert len(loaded) == EXPECTED

    # Publish the draft → becomes published with a real set name.
    result = await orch.publish_draft(owner_id=3, pack=pack, stickers=loaded)
    assert len(bot.created) == 1
    refreshed = await db.get_pack(pack.id)
    assert refreshed is not None and refreshed.published is True
    assert refreshed.set_name == result.set_name


async def test_build_for_review_fresh_persists_character_and_draft(
    db: Database, loader: StyleLoader, tmp_path: Path
) -> None:
    orch = _orchestrator(db, loader, _FakeBot(), tmp_path)
    bundle = await orch.build_for_review(
        mode="fresh",
        owner_id=11,
        captions=["Привет", "Пока"],
        photo=b"PHOTO",
        style_id="watercolor",
        subject_type="adult",
        name="Аня",
    )
    assert bundle.mode == "fresh"
    assert len(bundle.stickers) == 2
    assert (await db.list_characters(11))[0].name == "Аня"  # character saved for reuse
    pack = await db.get_pack(bundle.pack_id or 0)
    assert pack is not None and pack.published is False  # draft saved, not published


async def test_build_for_review_reuse_does_not_make_new_character(
    db: Database, loader: StyleLoader, tmp_path: Path
) -> None:
    orch = _orchestrator(db, loader, _FakeBot(), tmp_path)
    char = await orch.save_character(
        owner_id=12,
        name="A",
        style_id="watercolor",
        subject_type="adult",
        child_age=None,
        canonical=_sheet_bytes(EXPECTED),
    )
    bundle = await orch.build_for_review(
        mode="reuse", owner_id=12, captions=["Йо"], character_id=char.id
    )
    assert bundle.character.id == char.id
    assert len(await db.list_characters(12)) == 1  # reused, not duplicated (§3.2)


async def test_build_for_review_extend_skips_draft(
    db: Database, loader: StyleLoader, tmp_path: Path
) -> None:
    orch = _orchestrator(db, loader, _FakeBot(), tmp_path)
    char = await orch.save_character(
        owner_id=13,
        name="A",
        style_id="watercolor",
        subject_type="adult",
        child_age=None,
        canonical=_sheet_bytes(EXPECTED),
    )
    await orch.create_pack(owner_id=13, character=char)
    pack = (await db.list_packs(13))[0]
    bundle = await orch.build_for_review(
        mode="extend", owner_id=13, captions=["Эй"], pack_id=pack.id
    )
    assert bundle.pack_id == pack.id  # appends to the same pack, no new draft
    assert len(await db.list_packs(13)) == 1


async def test_build_for_review_extend_rejects_when_no_room(
    db: Database, loader: StyleLoader, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An extend that can't fit the 120-limit fails for free, before generating."""
    from sticker_service.services.publish import MAX_STICKERS_PER_SET, PackFullError

    bot = _FakeBot()
    orch = _orchestrator(db, loader, bot, tmp_path)
    char = await orch.save_character(
        owner_id=14,
        name="A",
        style_id="watercolor",
        subject_type="adult",
        child_age=None,
        canonical=_sheet_bytes(EXPECTED),
    )
    await orch.create_pack(owner_id=14, character=char)
    pack = (await db.list_packs(14))[0]

    async def _full(_pack_id: int) -> int:
        return MAX_STICKERS_PER_SET  # pretend the set is already at the limit

    monkeypatch.setattr(db, "count_stickers", _full)
    generate_calls_before = len(bot.added)
    with pytest.raises(PackFullError):
        await orch.build_for_review(mode="extend", owner_id=14, captions=["Эй"], pack_id=pack.id)
    assert len(bot.added) == generate_calls_before  # nothing was generated/appended


async def test_build_for_review_validates_required_args(
    db: Database, loader: StyleLoader, tmp_path: Path
) -> None:
    orch = _orchestrator(db, loader, _FakeBot(), tmp_path)
    with pytest.raises(OrchestratorError, match="fresh mode needs"):
        await orch.build_for_review(mode="fresh", owner_id=1, captions=["x"])
    with pytest.raises(OrchestratorError, match="reuse mode needs"):
        await orch.build_for_review(mode="reuse", owner_id=1, captions=["x"])
    with pytest.raises(OrchestratorError, match="extend mode needs"):
        await orch.build_for_review(mode="extend", owner_id=1, captions=["x"])


async def test_save_character_stores_source_photo(
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
        photo=b"PHOTOBYTES",
    )
    assert char.photo_path is not None and Path(char.photo_path).exists()  # kept for redraw


async def test_redraw_canonical_replaces_canonical_and_photo(
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
        photo=b"OLD",
    )
    new = await orch.redraw_canonical(char, b"NEWPHOTO")
    assert new.startswith(b"\x89PNG")
    refreshed = await db.get_character(char.id)
    assert refreshed is not None
    assert Path(refreshed.canonical_path).read_bytes() == new  # canonical replaced
    assert refreshed.photo_path is not None and Path(refreshed.photo_path).exists()


async def test_unknown_style_raises(db: Database, loader: StyleLoader, tmp_path: Path) -> None:
    orch = _orchestrator(db, loader, _FakeBot(), tmp_path)
    with pytest.raises(OrchestratorError, match="unknown"):
        await orch.build_canonical(
            photo=b"P", style_id="nope", subject_type="adult", child_age=None
        )


# --- draft garbage collection ------------------------------------------------


async def _backdate_pack(db: Database, pack_id: int, days: int) -> None:
    from datetime import UTC, datetime, timedelta

    old = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    await db._conn.execute("UPDATE packs SET created_at = ? WHERE id = ?", (old, pack_id))
    await db._conn.commit()


async def _draft_with_files(orch: Orchestrator, db: Database, owner: int) -> Pack:
    char = await orch.save_character(
        owner_id=owner,
        name="A",
        style_id="watercolor",
        subject_type="adult",
        child_age=None,
        canonical=_sheet_bytes(3),
    )
    stickers = await orch.build_stickers(char)
    return await orch.save_draft(owner_id=owner, character=char, title="D", stickers=stickers)


async def test_gc_removes_stale_draft_and_files(
    db: Database, loader: StyleLoader, tmp_path: Path
) -> None:
    orch = _orchestrator(db, loader, _FakeBot(), tmp_path)
    draft = await _draft_with_files(orch, db, owner=1)
    set_dir = tmp_path / "stickers" / draft.set_name
    assert set_dir.is_dir() and any(set_dir.iterdir())  # PNGs persisted
    await _backdate_pack(db, draft.id, days=60)

    removed = await orch.gc_stale_drafts(older_than_days=30)

    assert removed == 1
    assert await db.get_pack(draft.id) is None
    assert await db.count_stickers(draft.id) == 0
    assert not set_dir.exists()  # files swept too


async def test_gc_keeps_published_and_recent(
    db: Database, loader: StyleLoader, tmp_path: Path
) -> None:
    orch = _orchestrator(db, loader, _FakeBot(), tmp_path)
    char = await orch.save_character(
        owner_id=2,
        name="A",
        style_id="watercolor",
        subject_type="adult",
        child_age=None,
        canonical=_sheet_bytes(3),
    )
    await orch.create_pack(owner_id=2, character=char)  # published
    published = (await db.list_packs(2))[0]
    await _backdate_pack(db, published.id, days=60)  # old, but published → keep
    recent_draft = await _draft_with_files(orch, db, owner=2)  # within window → keep

    removed = await orch.gc_stale_drafts(older_than_days=30)

    assert removed == 0
    assert await db.get_pack(published.id) is not None
    assert await db.get_pack(recent_draft.id) is not None


async def test_gc_disabled_with_nonpositive_window(
    db: Database, loader: StyleLoader, tmp_path: Path
) -> None:
    orch = _orchestrator(db, loader, _FakeBot(), tmp_path)
    draft = await _draft_with_files(orch, db, owner=3)
    await _backdate_pack(db, draft.id, days=999)
    assert await orch.gc_stale_drafts(older_than_days=0) == 0
    assert await db.get_pack(draft.id) is not None  # untouched when disabled


async def test_postprocess_gate_bounds_concurrent_sheet_keying(
    db: Database, loader: StyleLoader, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Heavy postprocess from many users queues at the global gate (max 2).

    One 4K key+slice holds ~0.7 GB; without the gate four simultaneous users
    would stack peaks until the VDS OOMs (see docs/operations/CAPACITY.md).
    """
    import asyncio
    import threading

    lock = threading.Lock()
    running = 0
    max_seen = 0

    def tiny_png() -> bytes:
        buf = BytesIO()
        Image.new("RGBA", (64, 64), (0, 120, 200, 255)).save(buf, format="PNG")
        return buf.getvalue()

    sticker = tiny_png()

    def fake_process_sheet(_sheet: bytes, *, grid=None, expected=None) -> list[bytes]:
        nonlocal running, max_seen
        import time

        with lock:
            running += 1
            max_seen = max(max_seen, running)
        time.sleep(0.12)  # long enough for all four tasks to pile up
        with lock:
            running -= 1
        return [sticker]

    monkeypatch.setattr("sticker_service.services.orchestrator.process_sheet", fake_process_sheet)
    orch = _orchestrator(db, loader, _FakeBot(), tmp_path)
    chars = []
    for i in range(4):
        path = tmp_path / f"canon{i}.png"
        path.write_bytes(sticker)
        chars.append(
            await db.add_character(
                owner_id=100 + i,
                name=f"C{i}",
                style_id="watercolor",
                subject_type="adult",
                canonical_path=str(path),
            )
        )
    results = await asyncio.gather(*(orch.build_stickers(c, captions=["Привет!"]) for c in chars))
    assert all(len(r) == 1 for r in results)
    assert max_seen <= 2  # the gate held: never more than 2 keyings at once
    assert max_seen >= 2  # and it actually ran in parallel, not serialized
