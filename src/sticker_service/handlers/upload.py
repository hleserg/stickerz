"""``/upload`` — publish ready-made stickers: a sheet picture or a ZIP.

Owner's spec (13.06): free of charge but only for users who already finished
at least one generation; no watermark (the art is not ours); the owner gets a
preview DM of every upload — content published under the bot's mark deserves
an eye while the alpha runs on a whitelist.

Sticker bytes never live in the FSM: after slicing/unpacking they are parked
in the same scratch dirs the extend flow uses (``scratch_path`` in FSM data),
so /cancel and the maintenance GC clean uploads up for free.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from sticker_service.config import get_settings
from sticker_service.db import Database
from sticker_service.handlers.errors import _user_ref
from sticker_service.handlers.flow import _BUSY_TEXT, _begin_action, _end_action, _friendly_error
from sticker_service.observability import tag_component
from sticker_service.services import analytics
from sticker_service.services.moderation import caption_rejection_reason
from sticker_service.services.orchestrator import Orchestrator, OrchestratorError
from sticker_service.services.postprocess import compose_preview
from sticker_service.services.publish import PackFullError, next_part_title
from sticker_service.services.publish.publisher import StickerInput
from sticker_service.services.stickers.upload import (
    MAX_ZIP_BYTES,
    ZipRejectedError,
    extract_zip_stickers,
)

logger = logging.getLogger(__name__)


class Upload(StatesGroup):
    media = State()  # waiting for a sheet picture or a ZIP
    confirm = State()  # preview shown, awaiting «Дальше»/«Отмена»
    dest = State()  # «Куда его?» new pack vs extend
    title = State()  # naming the new pack
    pick = State()  # choosing which pack to extend
    link = State()  # awaiting a t.me/addstickers/... link to extend


_TXT_PROMPT = (
    "Пришли мне лист со стикерами одной картинкой — или ZIP с готовыми PNG. "
    "Нарежу и опубликую в Telegram."
)
_TXT_GATE = "Загрузка готовых стикеров откроется после первого сгенерированного пака — /new 🙂"
_TXT_NOT_SHEET = (
    "Хм, на лист со стикерами не похоже. Нужна картинка, где стикеры стоят "
    "сеткой на ровном фоне, — или ZIP с PNG."
)
_TXT_BAD_BG = (
    "Не смог аккуратно вырезать стикеры: фон слишком пёстрый. "
    "Попробуй вариант с ровным однотонным фоном."
)
_TXT_DEST = "Куда его?"
_TXT_TITLE = "Как назвать пак? (можно кириллицу и эмодзи)"
_TXT_NO_PACKS = "Опубликованных паков пока нет — выбери «🆕 Новый стикерпак»."
_TXT_CANCELLED = "Отменено. Загрузить ещё раз — /upload"


async def _has_generated(db: Database, user_id: int) -> bool:
    """The owner's anti-abuse gate: uploads open after the first generation."""
    return bool(await db.events_for(user_id, analytics.GENERATION_DONE, limit=1))


async def cmd_upload(
    message: Message, state: FSMContext, db: Database, orchestrator: Orchestrator
) -> None:
    """Start the upload flow (gated: at least one finished generation)."""
    tag_component("handlers.upload")
    user_id = message.from_user.id if message.from_user else 0
    if not await _has_generated(db, user_id):
        await message.answer(_TXT_GATE)
        return
    # Re-entry must not orphan a previous attempt's scratch dir for the GC.
    with contextlib.suppress(Exception):
        if scratch := (await state.get_data()).get("scratch_path"):
            await orchestrator.drop_scratch(str(scratch))
    await state.clear()
    await state.set_state(Upload.media)
    await message.answer(_TXT_PROMPT)


async def _show_preview(
    message: Message, state: FSMContext, scratch: str, stickers: list[StickerInput]
) -> None:
    """Send the sliced/unpacked result and ask for confirmation."""
    await state.update_data(scratch_path=scratch)
    sheets = await asyncio.to_thread(compose_preview, [img for img, _ in stickers])
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Дальше", callback_data="up:ok")
    kb.button(text="✖️ Отмена", callback_data="up:no")
    for sheet in sheets[:-1]:
        await message.answer_photo(BufferedInputFile(sheet, filename="preview.png"))
    await message.answer_photo(
        BufferedInputFile(sheets[-1], filename="preview.png"),
        caption=f"Вот что получилось — {len(stickers)} стикеров. Публикуем?",
        reply_markup=kb.as_markup(),
    )
    await state.set_state(Upload.confirm)


async def on_upload_media(  # pragma: no cover - thin I/O over tested services
    message: Message, state: FSMContext, orchestrator: Orchestrator
) -> None:
    """Receive the sheet picture or the ZIP, turn it into scratch stickers.

    Single-flighted per user: an album (media group) arrives as several
    updates, and without the lock each would start its own paid vision check
    and slicing run, orphaning the losers' scratch dirs.
    """
    tag_component("handlers.upload")
    user_id = message.from_user.id if message.from_user else 0
    if not _begin_action(user_id):
        await message.answer(_BUSY_TEXT)
        return
    try:
        await _ingest_media(message, state, orchestrator, user_id)
    finally:
        _end_action(user_id)


async def _ingest_media(  # pragma: no cover - thin I/O over tested services
    message: Message, state: FSMContext, orchestrator: Orchestrator, user_id: int
) -> None:
    try:
        if message.photo:
            photo = message.photo[-1]
            if (photo.file_size or 0) > MAX_ZIP_BYTES:
                await message.answer("Файл больше 20 МБ — пришли поменьше.")
                return
            file = await message.bot.download(photo.file_id)  # type: ignore[union-attr]
            image = file.read() if file else b""
            pieces = await orchestrator.upload_sheet_stickers(image)
            if pieces is None:
                await message.answer(_TXT_NOT_SHEET)
                return
            if not pieces:
                await message.answer(_TXT_BAD_BG)
                return
        elif message.document is not None:
            doc = message.document
            if (doc.file_size or 0) > MAX_ZIP_BYTES:
                await message.answer("Файл больше 20 МБ — пришли поменьше.")
                return
            file = await message.bot.download(doc.file_id)  # type: ignore[union-attr]
            payload = file.read() if file else b""
            if (doc.mime_type or "").startswith("image/"):
                pieces = await orchestrator.upload_sheet_stickers(payload)
                if pieces is None:
                    await message.answer(_TXT_NOT_SHEET)
                    return
                if not pieces:
                    await message.answer(_TXT_BAD_BG)
                    return
            else:
                pieces = await asyncio.to_thread(extract_zip_stickers, payload)
        else:
            await message.answer(_TXT_PROMPT)
            return
    except ZipRejectedError as exc:
        await message.answer(str(exc))
        return
    except Exception as exc:
        logger.exception("upload ingest failed")
        await message.answer(_friendly_error(exc))
        return
    scratch, stickers = await orchestrator.prepare_upload(owner_id=user_id, images=pieces)
    await _show_preview(message, state, scratch, stickers)


async def _disarm(callback: CallbackQuery) -> None:
    """Strip the pressed card's keyboard so a stale button can't re-fire."""
    if isinstance(callback.message, Message):
        with contextlib.suppress(Exception):
            await callback.message.edit_reply_markup(reply_markup=None)


async def on_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    """«Дальше» on the preview → ask where the stickers go."""
    tag_component("handlers.upload")
    await callback.answer()
    await _disarm(callback)
    kb = InlineKeyboardBuilder()
    kb.button(text="🆕 Новый стикерпак", callback_data="updest:new")
    kb.button(text="➕ Добавить к существующему", callback_data="updest:add")
    kb.adjust(1)
    if isinstance(callback.message, Message):
        await callback.message.answer(_TXT_DEST, reply_markup=kb.as_markup())
    await state.set_state(Upload.dest)


async def on_cancel(callback: CallbackQuery, state: FSMContext, orchestrator: Orchestrator) -> None:
    """«Отмена» anywhere in the upload flow: drop scratch, clear state."""
    tag_component("handlers.upload")
    with contextlib.suppress(Exception):
        if scratch := (await state.get_data()).get("scratch_path"):
            await orchestrator.drop_scratch(str(scratch))
    await state.clear()
    await callback.answer("Отменено")
    if isinstance(callback.message, Message):
        with contextlib.suppress(TelegramBadRequest):
            await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(_TXT_CANCELLED)


async def on_dest_new(callback: CallbackQuery, state: FSMContext) -> None:
    tag_component("handlers.upload")
    await callback.answer()
    await _disarm(callback)
    if isinstance(callback.message, Message):
        await callback.message.answer(_TXT_TITLE)
    await state.set_state(Upload.title)


async def on_dest_add(callback: CallbackQuery, state: FSMContext, db: Database) -> None:
    """List the user's published packs to extend (same as /addto)."""
    tag_component("handlers.upload")
    user_id = callback.from_user.id if callback.from_user else 0
    packs = [p for p in await db.list_packs(user_id) if p.published]
    await callback.answer()
    await _disarm(callback)
    if not packs:
        if isinstance(callback.message, Message):
            await callback.message.answer(_TXT_NO_PACKS)
        return
    kb = InlineKeyboardBuilder()
    for pack in packs:
        kb.button(text=pack.title, callback_data=f"uppick:{pack.id}")
    kb.button(text="🔗 По ссылке", callback_data="updest:link")
    kb.adjust(1)
    if isinstance(callback.message, Message):
        await callback.message.answer("К какому паку добавить?", reply_markup=kb.as_markup())
    await state.set_state(Upload.pick)


async def on_dest_link(callback: CallbackQuery, state: FSMContext) -> None:  # pragma: no cover
    """«🔗 По ссылке» in the upload flow: ask for the set link."""
    tag_component("handlers.upload")
    await callback.answer()
    await _disarm(callback)
    await state.set_state(Upload.link)
    if isinstance(callback.message, Message):
        await callback.message.answer("Введите ссылку на опубликованный стикерпак:")


async def on_upload_link(  # pragma: no cover - thin I/O over tested orchestrator path
    message: Message, state: FSMContext, orchestrator: Orchestrator
) -> None:
    """Adopt the linked set and offer it as the upload's extend target."""
    tag_component("handlers.upload")
    user_id = message.from_user.id if message.from_user else 0
    status = await message.answer("📥 Изучаю пак по ссылке…")
    try:
        pack = await orchestrator.adopt_pack_by_link(owner_id=user_id, text=message.text or "")
    except OrchestratorError as exc:
        with contextlib.suppress(TelegramBadRequest):
            await status.edit_text(f"Не получилось: {exc}.")
        return
    except Exception:
        logger.exception("upload link adoption failed")
        with contextlib.suppress(TelegramBadRequest):
            await status.edit_text("Что-то пошло не так со ссылкой — попробуй ещё раз.")
        return
    kb = InlineKeyboardBuilder()
    kb.button(text=f"➕ Добавить в «{pack.title}»", callback_data=f"uppick:{pack.id}")
    await state.set_state(Upload.pick)
    with contextlib.suppress(TelegramBadRequest):
        await status.edit_text(
            f"Нашёл пак «{pack.title}» — добавляем туда?", reply_markup=kb.as_markup()
        )


async def _notify_owner(  # pragma: no cover - best-effort DM
    bot: Bot, user: object, count: int, link: str, preview: bytes | None
) -> None:
    """Owner's eye on uploads: content under the bot's mark, observed live.

    ``user`` is the UPLOADER (callback/message ``from_user``) — never the
    bot's own account from a keyboard message. The first sticker rides along
    so the owner sees the art, not just a link.
    """
    owner = get_settings().first_admin_id
    user_id = getattr(user, "id", None)
    if owner is None or user_id is None or user_id == owner:
        return
    text = f"📤 Загрузка: {_user_ref(user)} → {count} стикеров → {link}"  # type: ignore[arg-type]
    with contextlib.suppress(Exception):
        if preview is not None:
            await bot.send_photo(
                owner, BufferedInputFile(preview, filename="upload.png"), caption=text
            )
        else:
            await bot.send_message(owner, text)


# Telegram caps a set's title at 64 chars; stay a notch under for safety.
_TITLE_MAX = 60


async def on_title(  # pragma: no cover - thin I/O over tested orchestrator path
    message: Message, state: FSMContext, orchestrator: Orchestrator, db: Database, bot: Bot
) -> None:
    """Receive the new pack's title and publish the upload as a new set."""
    tag_component("handlers.upload")
    user_id = message.from_user.id if message.from_user else 0
    title = (message.text or "").strip()
    if not title:
        await message.answer(_TXT_TITLE)
        return
    if len(title) > _TITLE_MAX:
        await message.answer(f"Название длинновато — уложись в {_TITLE_MAX} символов 🙂")
        return
    if reason := caption_rejection_reason(title):
        await message.answer(f"Так нельзя ({reason}). Придумай другое название.")
        return
    if not _begin_action(user_id):
        await message.answer(_BUSY_TEXT)
        return
    try:
        scratch = str((await state.get_data()).get("scratch_path") or "")
        first = await orchestrator.load_scratch(scratch)
        preview = first[0][0] if first else None
        status = await message.answer("🎭 Подбираю каждому стикеру эмодзи…")
        try:
            result = await orchestrator.publish_upload_new(
                owner_id=user_id, title=title, scratch_path=scratch
            )
        except OrchestratorError as exc:  # dead scratch: tell the truth, free the user
            await state.clear()
            with contextlib.suppress(TelegramBadRequest):
                await status.edit_text(f"{exc} — /upload")
            return
        except Exception as exc:
            logger.exception("upload publish (new) failed")
            with contextlib.suppress(TelegramBadRequest):
                await status.edit_text(_friendly_error(exc))
            return
        await state.clear()
        await analytics.log(db, user_id, analytics.UPLOAD_PUBLISHED, mode="new", count=result.count)
        with contextlib.suppress(TelegramBadRequest):
            await status.edit_text(f"✅ Готово! Пак: {result.link}")
        await _notify_owner(bot, message.from_user, result.count, result.link, preview)
    finally:
        _end_action(user_id)


async def on_pick(  # pragma: no cover - thin I/O over tested orchestrator path
    callback: CallbackQuery, state: FSMContext, orchestrator: Orchestrator, db: Database, bot: Bot
) -> None:
    """Extend the chosen pack with the uploaded stickers."""
    tag_component("handlers.upload")
    user_id = callback.from_user.id if callback.from_user else 0
    if not _begin_action(user_id):
        await callback.answer(_BUSY_TEXT, show_alert=True)
        return
    try:
        await callback.answer()
        msg = callback.message if isinstance(callback.message, Message) else None
        if msg is None:
            return
        pack_id = int((callback.data or "uppick:0").split(":", 1)[-1])
        pack = await db.get_pack(pack_id)
        if pack is None or pack.owner_id != user_id or not pack.published:
            await msg.answer(_TXT_NO_PACKS)
            return
        scratch = str((await state.get_data()).get("scratch_path") or "")
        await _disarm(callback)
        status = await msg.answer("🎭 Подбираю каждому стикеру эмодзи…")
        try:
            # emojify_scratch picks emojis once and persists them back, so a
            # retry reuses the SAME (bytes, emoji) batch — the publish_extend
            # resume digest stays stable and nothing re-adds on a retry.
            stickers = await orchestrator.emojify_scratch(scratch)
            if not stickers:
                await state.clear()
                with contextlib.suppress(TelegramBadRequest):
                    await status.edit_text(
                        "Загруженные стикеры устарели — пришли их ещё раз: /upload"
                    )
                return
            result = await orchestrator.publish_extend(
                owner_id=user_id, pack=pack, stickers=stickers
            )
        except PackFullError:
            # A full pack is not a dead end (owner's rule, 13.06): offer a
            # continuation pack named «{title} часть N» for the same upload.
            sequel = next_part_title(pack.title)
            await state.update_data(cont_title=sequel)
            kb = InlineKeyboardBuilder()
            kb.button(text=f"✅ Создать «{sequel}»", callback_data="upcont")
            kb.button(text="✖️ Не надо", callback_data="up:no")
            kb.adjust(1)
            with contextlib.suppress(TelegramBadRequest):
                await status.edit_text(
                    f"Пак «{pack.title}» заполнен — 120 стикеров, это лимит Telegram. "
                    f"Создать продолжение «{sequel}» из загруженных стикеров?",
                    reply_markup=kb.as_markup(),
                )
            return
        except Exception as exc:
            logger.exception("upload publish (extend) failed")
            with contextlib.suppress(TelegramBadRequest):
                await status.edit_text(_friendly_error(exc))
            return
        with contextlib.suppress(Exception):
            await orchestrator.drop_scratch(scratch)
        await state.clear()
        await analytics.log(
            db, user_id, analytics.UPLOAD_PUBLISHED, mode="extend", count=result.count
        )
        with contextlib.suppress(TelegramBadRequest):
            await status.edit_text(f"✅ Готово! Пак: {result.link}")
        await _notify_owner(bot, callback.from_user, result.count, result.link, stickers[0][0])
    finally:
        _end_action(user_id)


async def on_continue_upload(  # pragma: no cover - thin I/O over tested orchestrator path
    callback: CallbackQuery, state: FSMContext, orchestrator: Orchestrator, db: Database, bot: Bot
) -> None:
    """Publish the upload as the continuation pack offered after PackFullError."""
    tag_component("handlers.upload")
    user_id = callback.from_user.id if callback.from_user else 0
    title = str((await state.get_data()).get("cont_title") or "")
    if not title:
        await on_stale_button(callback)
        return
    if not _begin_action(user_id):
        await callback.answer(_BUSY_TEXT, show_alert=True)
        return
    try:
        await callback.answer()
        await _disarm(callback)
        msg = callback.message if isinstance(callback.message, Message) else None
        if msg is None:
            return
        scratch = str((await state.get_data()).get("scratch_path") or "")
        first = await orchestrator.load_scratch(scratch)
        preview = first[0][0] if first else None
        status = await msg.answer("🎭 Подбираю каждому стикеру эмодзи…")
        try:
            result = await orchestrator.publish_upload_new(
                owner_id=user_id, title=title, scratch_path=scratch
            )
        except OrchestratorError as exc:
            await state.clear()
            with contextlib.suppress(TelegramBadRequest):
                await status.edit_text(f"{exc} — /upload")
            return
        except Exception as exc:
            logger.exception("upload publish (continuation) failed")
            with contextlib.suppress(TelegramBadRequest):
                await status.edit_text(_friendly_error(exc))
            return
        await state.clear()
        await analytics.log(db, user_id, analytics.UPLOAD_PUBLISHED, mode="new", count=result.count)
        with contextlib.suppress(TelegramBadRequest):
            await status.edit_text(f"✅ Готово! Пак: {result.link}")
        await _notify_owner(bot, callback.from_user, result.count, result.link, preview)
    finally:
        _end_action(user_id)


async def on_stale_button(callback: CallbackQuery) -> None:
    """An upload button pressed outside the upload flow: dead, say so kindly.

    Without a state filter the old «✖️ Отмена» could fire inside ANOTHER
    active flow and delete ITS scratch from disk — so every live handler is
    state-filtered, and everything else lands here.
    """
    tag_component("handlers.upload")
    await callback.answer("Эта кнопка уже устарела. Загрузить ещё раз — /upload", show_alert=True)
    await _disarm(callback)


def build_router() -> Router:
    """Build a fresh upload router."""
    router = Router(name="upload")
    router.message.register(cmd_upload, Command("upload"))
    # Commands fall through (so /cancel cancels instead of being parsed as media).
    router.message.register(on_upload_media, Upload.media, ~F.text.startswith("/"))
    router.message.register(on_title, Upload.title, ~F.text.startswith("/"))
    router.callback_query.register(on_confirm, F.data == "up:ok", Upload.confirm)
    # «Отмена» is honored only INSIDE the upload flow; a stale press elsewhere
    # must never wipe another flow's state/scratch (catch-all below).
    router.callback_query.register(on_cancel, F.data == "up:no", StateFilter(Upload))
    router.callback_query.register(on_dest_new, F.data == "updest:new", Upload.dest)
    router.callback_query.register(on_dest_add, F.data == "updest:add", Upload.dest)
    router.callback_query.register(on_dest_link, F.data == "updest:link", Upload.pick)
    router.message.register(on_upload_link, Upload.link, ~F.text.startswith("/"))
    router.callback_query.register(on_pick, F.data.startswith("uppick:"), Upload.pick)
    router.callback_query.register(on_continue_upload, F.data == "upcont", Upload.pick)

    # Catch-all for any upload button outside its state: registered last.
    def _is_upload_button(data: str | None) -> bool:
        return bool(data) and (
            data in ("up:ok", "up:no", "upcont") or data.startswith(("updest:", "uppick:"))  # type: ignore[union-attr]
        )

    router.callback_query.register(on_stale_button, F.data.func(_is_upload_button))
    return router
