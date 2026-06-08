"""Seed canonical characters for an owner — one per style — from a single photo.

Built for the "save my test generations as ``Kate_<style>``" request: it runs the
canonical pipeline for every installed style against one photo and persists each
result as a reusable character for the given owner (default: the first admin).

Why a module and not inline: live generation needs the model credentials and a
real photo, which only exist on the deployment host — never in git. It ships
inside the image so it runs in the container with the bot's exact env + DB. Run
it *after* a deploy (and after topping up Gemini billing), e.g.::

    docker compose cp kate.jpg bot:/app/data/kate.jpg
    docker compose exec bot python -m sticker_service.maintenance.seed_kate \
        --photo /app/data/kate.jpg

Idempotent: a style whose ``<prefix>_<style>`` character already exists for the
owner is skipped, so re-running after a partial failure only fills the gaps.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from sticker_service.config import get_settings
from sticker_service.db import Database
from sticker_service.services.canonical import StyleLoader
from sticker_service.services.models.factory import build_model
from sticker_service.services.orchestrator import Orchestrator
from sticker_service.services.publish import Publisher

logger = logging.getLogger("seed_kate")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Seed Kate_<style> characters from a photo.")
    p.add_argument("--photo", required=True, type=Path, help="Path to the source photo.")
    p.add_argument("--owner", type=int, default=None, help="Owner id (default: first admin).")
    p.add_argument("--prefix", default="Kate", help="Character name prefix (default: Kate).")
    p.add_argument("--subject", choices=("adult", "child"), default="adult", help="Subject type.")
    p.add_argument("--age", type=int, default=None, help="Child age (only when --subject child).")
    return p.parse_args(argv)


async def _seed(args: argparse.Namespace) -> int:
    settings = get_settings()
    owner = args.owner if args.owner is not None else settings.first_admin_id
    if owner is None:
        logger.error("No owner given and no admin configured (set ADMIN_IDS or --owner).")
        return 2
    if not args.photo.exists():
        logger.error("Photo not found: %s", args.photo)
        return 2

    photo = args.photo.read_bytes()
    db = await Database.connect(settings.data_dir / "sticker_service.sqlite")
    try:
        loader = StyleLoader(settings.styles_dir)
        # Publisher is unused here (we only build/save canonicals, never publish),
        # but the orchestrator requires one; a stub username is enough.
        orch = Orchestrator(
            model=build_model("gemini"),
            db=db,
            publisher=Publisher(object(), "seed"),
            loader=loader,
            storage_dir=settings.data_dir,
        )

        existing = {c.name for c in await db.list_characters(owner)}
        styles = sorted(loader.styles)
        logger.info("Seeding %d styles for owner=%s: %s", len(styles), owner, ", ".join(styles))

        seeded = 0
        for style_id in styles:
            name = f"{args.prefix}_{style_id}"
            if name in existing:
                logger.info("skip %s (already exists)", name)
                continue
            logger.info("generating %s …", name)
            canonical = await orch.build_canonical(
                photo=photo,
                style_id=style_id,
                subject_type=args.subject,
                child_age=args.age if args.subject == "child" else None,
            )
            await orch.save_character(
                owner_id=owner,
                name=name,
                style_id=style_id,
                subject_type=args.subject,
                child_age=args.age if args.subject == "child" else None,
                canonical=canonical,
            )
            seeded += 1
            logger.info("saved %s", name)

        logger.info("done: %d new character(s) seeded for owner=%s", seeded, owner)
        return 0
    finally:
        await db.close()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    return asyncio.run(_seed(_parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
