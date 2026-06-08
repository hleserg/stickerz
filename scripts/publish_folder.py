"""Publish a folder of 512px PNG stickers as a NEW Telegram set, registered in the
bot's DB so it shows up in /mypacks (HLE — manual pack import).

Run on a Telegram-reachable host (the VDS — the sandbox can't reach Telegram).
Uses the configured BOT_TOKEN and the bot's sqlite DB.

    uv run python scripts/publish_folder.py \
        --dir ./pack_out --owner <your_telegram_user_id> \
        --title "Йога-блокер" --emoji 🎮

Notes:
- The owner must have pressed /start in the bot first, or Telegram rejects it.
- Creates a placeholder "imported" character to satisfy the pack→character link;
  the pack appears in /mypacks (published) with Open/Download.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from aiogram import Bot

from sticker_service.config import get_settings
from sticker_service.db import Database
from sticker_service.services.canonical import StyleLoader
from sticker_service.services.models import build_model
from sticker_service.services.orchestrator import Orchestrator
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
    settings = get_settings()
    bot = Bot(settings.bot_token)
    db = await Database.connect(settings.data_dir / "sticker_service.sqlite")
    try:
        me = await bot.get_me()
        orch = Orchestrator(
            model=build_model("mock"),  # unused by publish_new; avoids needing keys
            db=db,
            publisher=Publisher(bot, me.username or ""),
            loader=StyleLoader(settings.styles_dir),
            storage_dir=settings.data_dir,
        )
        png_bytes = [p.read_bytes() for p in pngs]
        # Placeholder character so the pack is linked + appears in /mychars too.
        character = await orch.save_character(
            owner_id=args.owner,
            name=args.title,
            style_id="imported",
            subject_type="adult",
            child_age=None,
            canonical=png_bytes[0],
        )
        stickers = [(data, args.emoji) for data in png_bytes]
        log.info("publishing %d stickers as '%s' for owner %s …", len(pngs), args.title, args.owner)
        result = await orch.publish_new(
            owner_id=args.owner, character=character, stickers=stickers, title=args.title
        )
        log.info("PUBLISHED: %s (in /mypacks)", result.link)
        print(result.link, flush=True)
        return 0
    finally:
        await db.close()
        await bot.session.close()


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_main(_parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
