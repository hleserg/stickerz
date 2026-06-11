"""Tests for /start-family copy and the alpha money surface (/balance, notes)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from sticker_service.db import Database
from sticker_service.handlers import apply as apply_handlers
from sticker_service.handlers import report as report_handlers
from sticker_service.handlers.start import (
    HELP,
    WELCOME,
    alpha_balance_note,
    cmd_balance,
    cmd_start,
)
from sticker_service.services import modes


@pytest_asyncio.fixture
async def db() -> AsyncIterator[Database]:
    database = await Database.connect(":memory:")
    try:
        yield database
    finally:
        await database.close()


def _state() -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))


# --- copy invariants ----------------------------------------------------------


def test_welcome_speaks_one_register() -> None:
    # ты-tone throughout: no formal «Нажмите» mixed into the informal greeting.
    assert "Нажмите" not in WELCOME and "Нажми" in WELCOME
    assert "ты автоматически соглашаешься" in WELCOME


def _buttons(markup: object) -> list[tuple[str, str | None]]:
    rows = getattr(markup, "inline_keyboard", None) or []
    return [(b.text, b.url) for row in rows for b in row]


async def test_start_in_alpha_offers_yuki_welcome_letter(db: Database) -> None:
    await modes.set_mode(db, modes.ALPHA)
    await db.allow(42)  # approved tester
    message = AsyncMock()
    message.from_user = SimpleNamespace(id=42)
    await cmd_start(message, db)
    buttons = _buttons(message.answer.await_args.kwargs.get("reply_markup"))
    welcome = [url for text, url in buttons if "Юки" in text]
    assert welcome and welcome[0] and "telegra.ph" in welcome[0]


async def test_start_outside_alpha_has_no_welcome_letter(db: Database) -> None:
    message = AsyncMock()
    message.from_user = SimpleNamespace(id=42)
    await cmd_start(message, db)  # default mode = debug
    buttons = _buttons(message.answer.await_args.kwargs.get("reply_markup"))
    assert all("Юки" not in text for text, _ in buttons)


def test_help_lists_balance_and_free_failures() -> None:
    assert "/balance" in HELP
    assert "ошибки — бесплатно" in HELP.lower() or "Списание только после" in HELP


def test_report_prompt_explains_what_and_why() -> None:
    # The user must understand what a bug report is and what it pays.
    p = report_handlers.REPORT_PROMPT
    assert "что делали" in p and "что ожидали" in p
    assert "+2 пака" in p and "/cancel" in p


# --- /balance & the balance note -----------------------------------------------


async def test_balance_note_only_for_alpha_non_admin(db: Database) -> None:
    assert await alpha_balance_note(db, 7) is None  # debug mode → no note
    await modes.set_mode(db, modes.ALPHA)
    note = await alpha_balance_note(db, 7)
    assert note is not None and "3" in note and "/balance" in note


async def test_cmd_balance_shows_packs_and_prices(db: Database) -> None:
    await modes.set_mode(db, modes.ALPHA)
    message = AsyncMock()
    message.from_user = SimpleNamespace(id=7)
    await cmd_balance(message, db)
    text = message.answer.await_args.args[0]
    assert "Баланс: 3 паков" in text
    assert "1 пак" in text and "0.5 пака" in text
    assert "после успешной генерации" in text  # failures are free — say so
    assert "+2 пака" in text  # the /report incentive


async def test_cmd_balance_outside_alpha_is_friendly(db: Database) -> None:
    message = AsyncMock()
    message.from_user = SimpleNamespace(id=7)
    await cmd_balance(message, db)
    assert "лимиты не действуют" in message.answer.await_args.args[0]


# --- report/apply text-state hygiene -------------------------------------------


async def test_report_empty_text_reprompts_instead_of_sending(db: Database) -> None:
    state = _state()
    await state.set_state(report_handlers.Report.text)
    message = AsyncMock()
    message.text = "   "
    message.caption = None
    message.from_user = SimpleNamespace(id=7)
    bot = AsyncMock()
    await report_handlers.on_report_text(message, state, db, bot)
    assert await state.get_state() == report_handlers.Report.text.state  # still waiting
    bot.send_message.assert_not_awaited()  # nothing forwarded to the admin


async def test_report_html_in_text_cannot_break_admin_message(db: Database, monkeypatch) -> None:
    from sticker_service.config import get_settings

    monkeypatch.setenv("APP_ADMIN_IDS", "111")
    get_settings.cache_clear()
    try:
        state = _state()
        await state.set_state(report_handlers.Report.text)
        message = AsyncMock()
        message.text = 'жму <b>кнопку</b> & <a href="x">ломаю</a> бота'
        message.caption = None
        message.from_user = SimpleNamespace(id=7, username="eve")
        bot = AsyncMock()
        await report_handlers.on_report_text(message, state, db, bot)
        sent = bot.send_message.await_args.args[1]
        assert "<b>кнопку</b>" not in sent  # user markup neutralized
        assert "&lt;b&gt;кнопку&lt;/b&gt;" in sent
    finally:
        monkeypatch.delenv("APP_ADMIN_IDS", raising=False)
        get_settings.cache_clear()


def test_report_and_apply_let_commands_fall_through() -> None:
    # The state handlers must not swallow /cancel etc. — the filter lets
    # commands reach their real handlers in the flow router.
    for router in (report_handlers.build_router(), apply_handlers.build_router()):
        state_handlers = [
            h for h in router.message.handlers if h.callback.__name__.startswith("on_")
        ]
        assert state_handlers, "state handler must be registered"
        assert all(len(h.filters or ()) >= 2 for h in state_handlers)  # state + command filter


async def test_apply_empty_source_reprompts(db: Database) -> None:
    state = _state()
    await state.set_state(apply_handlers.Apply.source)
    message = AsyncMock()
    message.text = ""
    message.from_user = SimpleNamespace(id=7, username=None)
    await apply_handlers.on_apply_source(message, state, db)
    assert await state.get_state() == apply_handlers.Apply.source.state
    assert await db.get_application(7) is None


# --- showcase page surfacing ----------------------------------------------------
# The default demo URL is empty while the promo is reworked, so these tests
# configure one explicitly; the last test pins the hidden-by-default behavior.


@pytest.fixture
def demo_url(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    from sticker_service.config import get_settings

    url = "https://telegra.ph/test-demo"
    monkeypatch.setenv("APP_DEMO_PAGE_URL", url)
    get_settings.cache_clear()
    yield url
    get_settings.cache_clear()


async def test_start_offers_demo_button_unobtrusively(db: Database, demo_url: str) -> None:
    # The Telegraph showcase is a url-button on /start — visible to every
    # newcomer, zero extra text (owner's rule: visible, never pushy).
    message = AsyncMock()
    message.from_user = SimpleNamespace(id=7)
    from sticker_service.handlers.start import cmd_start

    await cmd_start(message, db)
    markup = message.answer.await_args.kwargs.get("reply_markup")
    assert markup is not None
    buttons = [b for row in markup.inline_keyboard for b in row]
    assert any(b.url == demo_url and "Примеры" in b.text for b in buttons)
    # The greeting text itself did not grow a link paragraph.
    assert "telegra.ph" not in message.answer.await_args.args[0]


async def test_alpha_application_screen_offers_demo(db: Database, demo_url: str) -> None:
    # An applicant can't try the bot yet — the demo button shows what it makes.
    from sticker_service.handlers.start import cmd_start

    await modes.set_mode(db, modes.ALPHA)
    message = AsyncMock()
    message.from_user = SimpleNamespace(id=123456)
    await cmd_start(message, db)
    markup = message.answer.await_args.kwargs.get("reply_markup")
    buttons = [b for row in markup.inline_keyboard for b in row]
    assert any(b.callback_data == "apply:new" for b in buttons)
    assert any(b.url == demo_url for b in buttons)


async def test_help_links_demo_page(db: Database, demo_url: str) -> None:
    from sticker_service.handlers.start import cmd_help

    message = AsyncMock()
    message.from_user = SimpleNamespace(id=7)
    await cmd_help(message, db)
    assert demo_url in message.answer.await_args.args[0]


async def test_showcase_hidden_when_url_empty(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An empty APP_DEMO_PAGE_URL hides the button on /start and the /help line.
    from sticker_service.config import get_settings
    from sticker_service.handlers.start import cmd_help, cmd_start

    monkeypatch.setenv("APP_DEMO_PAGE_URL", "")
    get_settings.cache_clear()

    message = AsyncMock()
    message.from_user = SimpleNamespace(id=7)
    await cmd_start(message, db)
    markup = message.answer.await_args.kwargs.get("reply_markup")
    assert markup is None or all(
        not (b.url and "telegra.ph" in b.url) for row in markup.inline_keyboard for b in row
    )
    help_message = AsyncMock()
    help_message.from_user = SimpleNamespace(id=7)
    await cmd_help(help_message, db)
    assert "Примеры работ" not in help_message.answer.await_args.args[0]
    get_settings.cache_clear()
