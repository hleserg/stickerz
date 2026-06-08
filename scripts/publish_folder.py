"""Publish a folder of 512px PNG stickers as a NEW Telegram set (owner = a user).

Run on a host with Telegram access (the VDS — the Claude Code sandbox can't reach
api.telegram.org). Uses the configured BOT_TOKEN from .env.

    uv run python scripts/publish_folder.py \
        --dir ./pack_out --owner <your_telegram_user_id> \
        --title "Йога-блокер" --emoji 🎮

Notes:
- The owner must have pressed /start in the bot first, or Telegram rejects it.
- The set is owned by the user (not the bot), per §B.4.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from aiogram import Bot

from sticker_service.config import get_settings
from sticker_service.services.publish import Publisher

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("publish_folder")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Publish a folder of PNG stickers as a Telegram set.")
    p.add_argument("--dir", type=Path, required=True, help="Folder of 512px PNG stickers.")
    p.add_argument("--owner", type=int, required=True, help="Owner Telegram user_id.")
    p.add_argument("--title", required=True, help="Human-readable set title.")
    p.add_argument("--emoji", default="🙂", help="Emoji applied to every sticker.")
    return p.parse_args(argv)


async def _main(args: argparse.Namespace) -> int:
    pngs = sorted(args.dir.glob("*.png"))
    if not pngs:
        log.error("no PNGs in %s", args.dir)
        return 2
    bot = Bot(get_settings().bot_token)
    try:
        me = await bot.get_me()
        publisher = Publisher(bot, me.username or "")
        stickers = [(p.read_bytes(), args.emoji) for p in pngs]
        log.info("publishing %d stickers as '%s' for owner %s …", len(pngs), args.title, args.owner)
        name = await publisher.create_pack(user_id=args.owner, title=args.title, stickers=stickers)
        link = f"https://t.me/addstickers/{name}"
        log.info("PUBLISHED: %s", link)
        print(link, flush=True)
        return 0
    finally:
        await bot.session.close()


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_main(_parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
