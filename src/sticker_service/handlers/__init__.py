"""aiogram handlers and the root dispatcher factory."""

from __future__ import annotations

from aiogram import Dispatcher

from sticker_service.handlers import start


def build_dispatcher() -> Dispatcher:
    """Build the root :class:`Dispatcher` with all feature routers registered.

    Kept separate from the runner so it can be constructed and inspected in
    tests without a real bot token or network.
    """
    dp = Dispatcher()
    dp.include_router(start.router)
    return dp


__all__ = ["build_dispatcher"]
