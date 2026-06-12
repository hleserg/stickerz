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
    # Contract with the UI: every emitted label has a live status line —
    # "sheet" is the animated one (_SHEET_FRAMES), the rest are static.
    from sticker_service.handlers.flow import _SHEET_FRAMES, _STAGE_TEXT

    assert _SHEET_FRAMES
    assert set(stages) <= set(_STAGE_TEXT) | {"sheet"}


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
    # The generated bytes are parked in a scratch dir (not in the FSM, not in
    # the target pack's rows) until the user confirms the publish.
    assert bundle.scratch_path is not None
    assert await orch.load_scratch(bundle.scratch_path) == bundle.stickers
    assert await db.count_stickers(pack.id) == EXPECTED  # target rows untouched


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

    def fake_process_sheet(_sheet: bytes, *, grid=None, expected=None):
        nonlocal running, max_seen
        import time

        with lock:
            running += 1
            max_seen = max(max_seen, running)
        time.sleep(0.12)  # long enough for all four tasks to pile up
        with lock:
            running -= 1
        from sticker_service.services.postprocess import SheetQuality

        return [sticker], SheetQuality(True, "ok", 1.0, "components")

    monkeypatch.setattr(
        "sticker_service.services.orchestrator.process_sheet_checked", fake_process_sheet
    )
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


async def test_gate_slot_is_held_until_a_cancelled_keying_thread_exits(
    db: Database, loader: StyleLoader, tmp_path: Path
) -> None:
    """Cancelling the awaiting coroutine (generation timeout) must not free the
    gate slot while the keying thread is still chewing RAM — otherwise parallel
    timeouts let untracked ~0.7 GB peaks stack outside the gate."""
    import asyncio
    import threading

    orch = _orchestrator(db, loader, _FakeBot(), tmp_path)
    gate = orch._postprocess_gate  # the real semaphore, capacity 2
    release_thread = threading.Event()
    started = threading.Event()

    def slow_keying() -> str:
        started.set()
        release_thread.wait(timeout=5)
        return "done"

    task = asyncio.create_task(orch._gated_to_thread(slow_keying))
    await asyncio.to_thread(started.wait, 5)  # the thread is genuinely running
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # The waiter was cancelled, but the thread still runs → slot stays taken.
    assert gate._value == 1
    release_thread.set()
    for _ in range(100):  # released soon after the thread actually exits
        if gate._value == 2:
            break
        await asyncio.sleep(0.01)
    assert gate._value == 2


# --- scratch storage (extend-mode review bytes live on disk, not in the FSM) --


async def test_scratch_round_trip_and_drop(
    db: Database, loader: StyleLoader, tmp_path: Path
) -> None:
    orch = _orchestrator(db, loader, _FakeBot(), tmp_path)
    stickers = [(b"png-one", "🙂"), (b"png-two", "👍")]
    path = await orch.save_scratch(owner_id=7, stickers=stickers)
    assert (tmp_path / "scratch") in Path(path).parents
    assert await orch.load_scratch(path) == stickers
    await orch.drop_scratch(path)
    assert not Path(path).exists()
    assert await orch.load_scratch(path) == []  # expired/gone → empty, no raise


async def test_scratch_refuses_paths_outside_its_root(
    db: Database, loader: StyleLoader, tmp_path: Path
) -> None:
    orch = _orchestrator(db, loader, _FakeBot(), tmp_path)
    victim = tmp_path / "canonical"
    victim.mkdir(parents=True)
    (victim / "keep.png").write_bytes(b"precious")
    # A forged FSM value pointing outside data/scratch must be ignored.
    await orch.drop_scratch(str(victim))
    assert (victim / "keep.png").exists()
    assert await orch.load_scratch(str(victim)) == []


# --- extend resume (flood mid-batch must not duplicate or drop stickers) ------


class _LiveBot(_FakeBot):
    """Stateful fake: get_sticker_set reflects what was actually added."""

    def __init__(self, initial: int) -> None:
        super().__init__()
        self.initial = initial
        self.fail_at: int | None = None  # raise a long 429 when len(added) hits this

    async def add_sticker_to_set(self, **kwargs: object) -> None:
        if self.fail_at is not None and len(self.added) == self.fail_at:
            from aiogram.exceptions import TelegramRetryAfter
            from aiogram.methods import GetMe

            self.fail_at = None  # next attempt succeeds
            raise TelegramRetryAfter(method=GetMe(), message="flood", retry_after=3600)
        await super().add_sticker_to_set(**kwargs)

    async def get_sticker_set(self, *, name: str) -> object:
        from types import SimpleNamespace

        return SimpleNamespace(stickers=[object()] * (self.initial + len(self.added)))


async def _published_pack(orch: Orchestrator, db: Database, owner: int) -> tuple[Pack, list]:
    char = await orch.save_character(
        owner_id=owner,
        name="A",
        style_id="watercolor",
        subject_type="adult",
        child_age=None,
        canonical=_sheet_bytes(EXPECTED),
    )
    await orch.create_pack(owner_id=owner, character=char)
    pack = (await db.list_packs(owner))[0]
    stickers = await orch.build_stickers(char, captions=["Раз", "Два", "Три"])
    return pack, stickers


async def test_extend_retry_after_mid_batch_crash_adds_no_duplicates(
    db: Database, loader: StyleLoader, tmp_path: Path
) -> None:
    bot = _LiveBot(initial=0)
    orch = _orchestrator(db, loader, bot, tmp_path)
    pack, stickers = await _published_pack(orch, db, owner=21)
    bot.initial = await db.count_stickers(pack.id)  # published baseline

    bot.fail_at = len(bot.added) + 2  # die after 2 of 3 new stickers land
    with pytest.raises(Exception, match="flood"):
        await orch.publish_extend(owner_id=21, pack=pack, stickers=stickers)
    added_before_retry = len(bot.added)
    # DB tracked every landed sticker (incremental persistence).
    assert await db.count_stickers(pack.id) == bot.initial + added_before_retry

    # The user taps publish again with the SAME batch → only the tail is added.
    result = await orch.publish_extend(owner_id=21, pack=pack, stickers=stickers)
    assert result.count == len(stickers)
    assert len(bot.added) == len(stickers)  # 3 total adds across both attempts
    assert await db.count_stickers(pack.id) == bot.initial + len(stickers)


async def test_extend_ignores_unrelated_live_drift_without_marker(
    db: Database, loader: StyleLoader, tmp_path: Path
) -> None:
    """The owner adding stickers manually via @Stickers must NOT make the next
    extend silently skip paid stickers (review finding: count-based heuristics
    mis-attribute pre-existing drift)."""
    bot = _LiveBot(initial=0)
    orch = _orchestrator(db, loader, bot, tmp_path)
    pack, stickers = await _published_pack(orch, db, owner=22)
    bot.initial = await db.count_stickers(pack.id) + 2  # manual drift: live = DB + 2

    await orch.publish_extend(owner_id=22, pack=pack, stickers=stickers)
    assert len(bot.added) == len(stickers)  # nothing skipped — no marker, no resume


async def test_extend_stale_marker_for_other_batch_does_not_skip(
    db: Database, loader: StyleLoader, tmp_path: Path
) -> None:
    import json as jsonlib

    bot = _LiveBot(initial=0)
    orch = _orchestrator(db, loader, bot, tmp_path)
    pack, stickers = await _published_pack(orch, db, owner=23)
    bot.initial = await db.count_stickers(pack.id)
    # A marker from some OTHER batch (different digest) survived a crash.
    marker = tmp_path / "extend_pending" / f"{pack.id}.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(jsonlib.dumps({"digest": "stale", "live_before": 0}))

    await orch.publish_extend(owner_id=23, pack=pack, stickers=stickers)
    assert len(bot.added) == len(stickers)  # digest mismatch → full batch added
    assert not marker.exists()  # marker cleared on success


async def test_gc_sweeps_old_scratch_dirs(
    db: Database, loader: StyleLoader, tmp_path: Path
) -> None:
    import os
    import time

    orch = _orchestrator(db, loader, _FakeBot(), tmp_path)
    old = await orch.save_scratch(owner_id=1, stickers=[(b"x", "🙂")])
    fresh = await orch.save_scratch(owner_id=2, stickers=[(b"y", "🙂")])
    ancient = time.time() - 60 * 86400
    os.utime(old, (ancient, ancient))

    await orch.gc_stale_drafts(older_than_days=30)

    assert not Path(old).exists()  # abandoned extend cleaned up
    assert Path(fresh).exists()  # recent review untouched


class _FlakyModel(_SheetModel):
    """Generates normally until ``failing`` is set; then every call overloads."""

    def __init__(self) -> None:
        super().__init__()
        self.failing = False

    async def generate(self, prompt: str, refs: Sequence[bytes] = (), **kw: object) -> bytes:
        if self.failing:
            from sticker_service.services.models.base import ModelError

            raise ModelError("overloaded")
        return await super().generate(prompt, refs, **kw)


async def test_canonical_resumes_from_persisted_steps(
    db: Database, loader: StyleLoader, tmp_path: Path
) -> None:
    # A canonical run killed mid-pipeline (overload/redeploy) must RESUME from
    # the last persisted step on the next try, not regenerate finished work.
    from sticker_service.services.models.base import ModelError

    model = _FlakyModel()
    orch = Orchestrator(
        model=model,
        db=db,
        publisher=Publisher(_FakeBot(), "yourbot"),
        loader=loader,
        storage_dir=tmp_path,
    )
    args = {
        "photo": b"PHOTO",
        "style_id": "watercolor",
        "subject_type": "adult",
        "child_age": None,
        "owner_id": 7,
    }

    async def _fail_after_first(done: int, total: int) -> None:
        model.failing = True  # step 1 done → every later call overloads

    with pytest.raises(ModelError):
        await orch.build_canonical(**args, on_step=_fail_after_first)  # type: ignore[arg-type]
    pending = tmp_path / "canonical_pending" / "7"
    assert (pending / "step1.png").exists()  # progress persisted for resume
    step1_calls = len(model.generate_calls)

    model.failing = False
    canonical = await orch.build_canonical(**args)  # type: ignore[arg-type]
    assert canonical.startswith(b"\x89PNG")
    # Step 1 was loaded from disk, not regenerated: only later steps hit the model.
    assert len(model.generate_calls) == step1_calls + 2  # steps 2 and 3 only
    assert not pending.exists()  # cleaned up after success


async def test_canonical_pending_dropped_for_new_job(
    db: Database, loader: StyleLoader, tmp_path: Path
) -> None:
    # Leftover steps from a DIFFERENT photo/style must never leak into a new job.
    from sticker_service.services.orchestrator import _load_pending_steps, _save_pending_step

    pending = tmp_path / "canonical_pending" / "7"
    _save_pending_step(pending, "old-key", 1, b"OLD")
    assert _load_pending_steps(pending, "new-key") == {}
    assert not pending.exists()  # stale leftovers removed on mismatch


async def test_unusable_sheet_fails_generation_with_retryable_error(
    db: Database, loader: StyleLoader, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Garbage never ships (owner's rule): an un-sliceable sheet raises a
    # retryable error and logs the fallback analytics event.
    import sticker_service.services.orchestrator as orch_mod
    from sticker_service.services import analytics
    from sticker_service.services.models.errors import TransientPipelineError, is_retryable
    from sticker_service.services.postprocess import SheetQuality

    orch = _orchestrator(db, loader, _FakeBot(), tmp_path)
    char = await orch.save_character(
        owner_id=5,
        name="Тест",
        style_id="watercolor",
        subject_type="adult",
        child_age=None,
        canonical=_sheet_bytes(1),
    )

    def _bad(*a: object, **k: object) -> tuple[list[bytes], SheetQuality]:
        return [b"x"], SheetQuality(False, "scrap piece", 0.1, "split")

    monkeypatch.setattr(orch_mod, "process_sheet_checked", _bad)
    with pytest.raises(TransientPipelineError) as err:
        await orch.build_stickers(char, captions=["Привет!"])
    assert is_retryable(err.value)
    assert await db.count_events(analytics.SLICING_FALLBACK) == 1
    rejected = err.value.rejected_path
    assert rejected is not None and rejected.exists()  # evidence for the owner alert


async def test_caption_gate_rejects_and_dumps_evidence(
    db: Database, loader: StyleLoader, tmp_path: Path
) -> None:
    # Vision sees «Привет!» twice and no «Пока!» → the page fails into the
    # free-retry path, the analytics row is logged and the rejected sheet is
    # dumped so the owner's alert can attach the evidence.
    from sticker_service.services import analytics
    from sticker_service.services.models.errors import TransientPipelineError, is_retryable

    class _WrongTexts(_SheetModel):
        async def ask(self, image: bytes, question: str) -> str:
            return "Привет!\nПривет!"

    orch = Orchestrator(
        model=_WrongTexts(),
        db=db,
        publisher=Publisher(_FakeBot(), "yourbot"),
        loader=loader,
        storage_dir=tmp_path,
    )
    char = await orch.save_character(
        owner_id=5,
        name="Тест",
        style_id="watercolor",
        subject_type="adult",
        child_age=None,
        canonical=_sheet_bytes(1),
    )
    with pytest.raises(TransientPipelineError) as err:
        await orch.build_stickers(char, captions=["Привет!", "Пока!"])
    assert "caption check failed" in str(err.value)
    assert is_retryable(err.value)
    rejected = err.value.rejected_path
    assert rejected is not None and rejected.exists()
    assert rejected.parent == tmp_path / "rejected"
    assert await db.count_events(analytics.CAPTION_GATE) == 1


async def test_caption_gate_fails_open_without_vision_answer(
    db: Database, loader: StyleLoader, tmp_path: Path
) -> None:
    # «НЕТ» while texts ARE expected smells like vision misreading the style —
    # pass the sheet rather than burn a paid retry on a flaky check.
    class _NoTexts(_SheetModel):
        async def ask(self, image: bytes, question: str) -> str:
            return "НЕТ"

    orch = Orchestrator(
        model=_NoTexts(),
        db=db,
        publisher=Publisher(_FakeBot(), "yourbot"),
        loader=loader,
        storage_dir=tmp_path,
    )
    char = await orch.save_character(
        owner_id=5,
        name="Тест",
        style_id="watercolor",
        subject_type="adult",
        child_age=None,
        canonical=_sheet_bytes(1),
    )
    assert await orch.build_stickers(char, captions=["Привет!", "Пока!"])


async def test_scene_observer_reports_without_rejecting(
    db: Database, loader: StyleLoader, tmp_path: Path
) -> None:
    # The observer has no rejection power (the check is subjective): a
    # suspicious sheet still ships, but the owner gets the evidence DM with
    # the dumped sheet attached, and the analytics row lands.
    from sticker_service.services import analytics

    class _SuspectScenes(_SheetModel):
        async def ask(self, image: bytes, question: str) -> str:
            if question.startswith("Перечисли"):
                return "Привет!"  # caption gate: faithful texts
            return "1. сердце уехало на соседний стикер"

    alerts: list[tuple[str, Path | None]] = []

    async def _notify(text: str, attachment: Path | None) -> None:
        alerts.append((text, attachment))

    orch = Orchestrator(
        model=_SuspectScenes(),
        db=db,
        publisher=Publisher(_FakeBot(), "yourbot"),
        loader=loader,
        storage_dir=tmp_path,
        owner_notify=_notify,
    )
    char = await orch.save_character(
        owner_id=5,
        name="Тест",
        style_id="watercolor",
        subject_type="adult",
        child_age=None,
        canonical=_sheet_bytes(1),
    )
    assert await orch.build_stickers(char, captions=["Привет!"])  # ships anyway
    assert len(alerts) == 1
    text, attachment = alerts[0]
    assert "сердце" in text
    assert attachment is not None and attachment.exists()
    assert await db.count_events(analytics.SCENE_OBSERVER) == 1
