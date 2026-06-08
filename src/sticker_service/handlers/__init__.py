"""aiogram handlers and the root dispatcher factory."""

from __future__ import annotations

from aiogram import Dispatcher

from sticker_service.db import Database
from sticker_service.handlers import admin, flow, report, start
from sticker_service.handlers.errors import on_error
from sticker_service.handlers.middleware import WhitelistMiddleware
from sticker_service.services.canonical.loader import StyleLoader
from sticker_service.services.orchestrator import Orchestrator


def build_dispatcher(
    db: Database | None = None,
    orchestrator: Orchestrator | None = None,
    loader: StyleLoader | None = None,
) -> Dispatcher:
    """Build the root :class:`Dispatcher` with all feature routers registered.

    When ``db`` is provided, the whitelist middleware and admin commands are
    wired. When ``orchestrator`` (and ``loader``) are provided, the full
    pack-building flow is included. Dependencies are injected into handler scope.
    Argument-optional so the dispatcher can be built and inspected in tests.
    """
    dp = Dispatcher()
    dp.errors.register(on_error)
    if db is not None:
        dp["db"] = db
        dp.message.outer_middleware(WhitelistMiddleware(db))
        dp.callback_query.outer_middleware(WhitelistMiddleware(db))
        dp.include_router(admin.build_router())
        dp.include_router(report.build_router())
    if orchestrator is not None:
        dp["orchestrator"] = orchestrator
        if loader is not None:
            dp["loader"] = loader
        dp.include_router(flow.build_router())
    dp.include_router(start.build_router())
    return dp


__all__ = ["build_dispatcher"]
