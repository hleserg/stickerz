"""Refund flow: the 🐞-card button returns a verified charge, never a gift on top."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiogram.types import Message

from sticker_service.config import get_settings
from sticker_service.db import DEFAULT_CREDITS, Database
from sticker_service.handlers import admin, report
from sticker_service.services import analytics

TESTER = 4242


@pytest_asyncio.fixture
async def db() -> AsyncIterator[Database]:
    database = await Database.connect(":memory:")
    try:
        yield database
    finally:
        await database.close()


@pytest.fixture(autouse=True)
def _admin_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("APP_ADMIN_IDS", "999")  # the acting admin
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _admin_cb(data: str) -> AsyncMock:
    cb = AsyncMock()
    cb.from_user = SimpleNamespace(id=999)
    cb.data = data
    cb.message = AsyncMock(spec=Message)  # passes the isinstance(message, Message) gate
    # spec turns methods into sync mocks; these two are awaited by the handlers.
    cb.message.answer = AsyncMock()
    cb.message.edit_reply_markup = AsyncMock()
    return cb


def _buttons(markup: object) -> list[tuple[str, str | None]]:
    rows = getattr(markup, "inline_keyboard", None) or []
    return [(b.text, b.callback_data) for row in rows for b in row]


async def _charge(db: Database, credits: int = 2, mode: str = "fresh") -> None:
    await db.consume_credits(TESTER, credits)
    await analytics.log(db, TESTER, analytics.CREDITS_CHARGED, mode=mode, credits=credits)


async def test_report_card_carries_the_refund_button(db: Database) -> None:
    msg = AsyncMock()
    msg.from_user = SimpleNamespace(id=TESTER)
    msg.text = "пак пришёл с кривыми подписями"
    msg.caption = None
    state, bot = AsyncMock(), AsyncMock()
    await report.on_report_text(msg, state, db, bot)
    markup = bot.send_message.await_args.kwargs["reply_markup"]
    datas = [d for _, d in _buttons(markup)]
    assert f"bug:{TESTER}" in datas
    assert f"refund:{TESTER}" in datas


async def test_refund_request_without_charge_refuses(db: Database) -> None:
    cb = _admin_cb(f"refund:{TESTER}")
    await admin.on_refund_request(cb, db)
    assert "нечего" in cb.answer.await_args.args[0]
    cb.message.answer.assert_not_awaited()  # no confirm card
    assert await db.credits_left(TESTER) == DEFAULT_CREDITS  # nothing granted


async def test_refund_request_shows_the_latest_charge(db: Database) -> None:
    await _charge(db, credits=2, mode="fresh")
    cb = _admin_cb(f"refund:{TESTER}")
    await admin.on_refund_request(cb, db)
    text = cb.message.answer.await_args.args[0]
    assert "fresh" in text
    datas = [d for _, d in _buttons(cb.message.answer.await_args.kwargs["reply_markup"])]
    assert f"refundok:{TESTER}" in datas  # amount re-verified at confirm, not carried
    assert "refundno" in datas
    assert await db.credits_left(TESTER) == DEFAULT_CREDITS - 2  # not refunded yet


async def test_refund_request_skips_malformed_charge_detail(db: Database) -> None:
    # A hand-edited event row (credits="2") must hit the refusal branch, not
    # crash or build a refund card for a garbage amount.
    await db.consume_credits(TESTER, 2)
    await analytics.log(db, TESTER, analytics.CREDITS_CHARGED, mode="fresh", credits="2")
    cb = _admin_cb(f"refund:{TESTER}")
    await admin.on_refund_request(cb, db)
    assert "нечего" in cb.answer.await_args.args[0]
    cb.message.answer.assert_not_awaited()
    assert await db.credits_left(TESTER) == DEFAULT_CREDITS - 2  # nothing granted


async def test_refund_confirm_returns_charge_and_apologizes(db: Database) -> None:
    await _charge(db, credits=2)
    cb, bot = _admin_cb(f"refundok:{TESTER}"), AsyncMock()
    await admin.on_refund_confirm(cb, db, bot)
    assert await db.credits_left(TESTER) == DEFAULT_CREDITS  # charge returned
    sent_to, apology = bot.send_message.await_args.args
    assert sent_to == TESTER
    assert "Прости" in apology and "/new" in apology
    cb.message.edit_reply_markup.assert_awaited_with(reply_markup=None)  # card disarmed


async def test_refund_credits_writes_marker_and_credit_together(db: Database) -> None:
    # The repository primitive must do both in one call — the handler relies on
    # the marker and the credit being inseparable.
    await db.consume_credits(TESTER, 2)
    left = await db.refund_credits(TESTER, 2, analytics.CREDITS_REFUNDED, {"credits": 2})
    assert left == DEFAULT_CREDITS  # credited
    refunds = await db.events_for(TESTER, analytics.CREDITS_REFUNDED, limit=1)
    assert refunds and refunds[0][1]["credits"] == 2  # marker recorded


async def test_refund_confirm_is_idempotent(db: Database) -> None:
    # A double-tap / stale card / repeat 🐞 report must never refund twice:
    # the refund marker settles the charge, so every later press refuses.
    await _charge(db, credits=2)
    bot = AsyncMock()
    await admin.on_refund_confirm(_admin_cb(f"refundok:{TESTER}"), db, bot)
    again = _admin_cb(f"refundok:{TESTER}")
    await admin.on_refund_confirm(again, db, bot)
    assert await db.credits_left(TESTER) == DEFAULT_CREDITS  # credited exactly once
    assert "нечего" in again.answer.await_args.args[0]
    request = _admin_cb(f"refund:{TESTER}")  # repeat report finds nothing either
    await admin.on_refund_request(request, db)
    assert "нечего" in request.answer.await_args.args[0]


async def test_new_charge_after_refund_is_refundable_again(db: Database) -> None:
    # The marker settles only PAST charges: a genuinely new defective pack
    # after a refund must get its own refund card.
    await _charge(db, credits=2)
    await admin.on_refund_confirm(_admin_cb(f"refundok:{TESTER}"), db, AsyncMock())
    await _charge(db, credits=1, mode="extend")
    cb = _admin_cb(f"refund:{TESTER}")
    await admin.on_refund_request(cb, db)
    assert "extend" in cb.message.answer.await_args.args[0]


async def test_refund_handlers_reject_non_admin(db: Database) -> None:
    await _charge(db)
    for handler, data in (
        (lambda cb: admin.on_refund_request(cb, db), f"refund:{TESTER}"),
        (lambda cb: admin.on_refund_confirm(cb, db, AsyncMock()), f"refundok:{TESTER}"),
    ):
        cb = _admin_cb(data)
        cb.from_user = SimpleNamespace(id=12345)  # not an admin
        await handler(cb)
        cb.message.answer.assert_not_awaited()
    assert await db.credits_left(TESTER) == DEFAULT_CREDITS - 2  # untouched


async def test_refund_cancel_just_answers(db: Database) -> None:
    cb = _admin_cb("refundno")
    await admin.on_refund_cancel(cb)
    cb.answer.assert_awaited_with("Отменено")
