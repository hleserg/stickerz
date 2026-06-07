"""aiogram handlers and the root dispatcher factory."""

from __future__ import annotations

from aiogram import Dispatcher

from sticker_service.db import Database
from sticker_service.handlers import admin, start
from sticker_service.handlers.middleware import WhitelistMiddleware


def build_dispatcher(db: Database | None = None) -> Dispatcher:
    """Build the root :class:`Dispatcher` with all feature routers registered.

    When ``db`` is provided, the whitelist middleware and admin commands are
    wired and ``db`` is injected into handler scope. Kept argument-optional so
    the dispatcher can be constructed and inspected in tests without a DB.
    """
    dp = Dispatcher()
    if db is not None:
        dp["db"] = db
        dp.message.outer_middleware(WhitelistMiddleware(db))
        dp.callback_query.outer_middleware(WhitelistMiddleware(db))
        dp.include_router(admin.build_router())
    dp.include_router(start.build_router())
    return dp


__all__ = ["build_dispatcher"]
