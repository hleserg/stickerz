"""Tests for emoji assignment via Vision with fallback (§9)."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from sticker_service.services.models import MockImageModel
from sticker_service.services.models.base import ImageModel, ModelError
from sticker_service.services.stickers import (
    DEFAULT_EMOJI,
    assign_emoji,
    assign_emojis,
    is_single_emoji,
)


@pytest.mark.parametrize("good", ["🙂", "🔥", "👍", "❤️", "👍🏽", "🤦‍♂️"])
def test_is_single_emoji_accepts(good: str) -> None:
    assert is_single_emoji(good)


@pytest.mark.parametrize("bad", ["", "  ", "smile", "lol", ":)", "abc", "🙂🔥🎉🥳"])
def test_is_single_emoji_rejects(bad: str) -> None:
    assert not is_single_emoji(bad)


async def test_assign_emoji_uses_valid_model_output() -> None:
    assert await assign_emoji(MockImageModel(emoji="🔥"), b"img") == "🔥"


async def test_assign_emoji_falls_back_on_invalid() -> None:
    assert await assign_emoji(MockImageModel(emoji="haha"), b"img") == DEFAULT_EMOJI


class _ErroringModel(ImageModel):
    name = "err"

    async def generate(self, prompt: str, refs: Sequence[bytes] = ()) -> bytes:  # pragma: no cover
        return b""

    async def judge_geometry(self, frame_a: bytes, frame_b: bytes) -> float:  # pragma: no cover
        return 1.0

    async def pick_emoji(self, image: bytes) -> str:
        raise ModelError("vision down")


async def test_assign_emoji_falls_back_on_error() -> None:
    assert await assign_emoji(_ErroringModel(), b"img") == DEFAULT_EMOJI


async def test_assign_emojis_maps_all() -> None:
    emojis = await assign_emojis(MockImageModel(emoji="👍"), [b"a", b"b", b"c"])
    assert emojis == ["👍", "👍", "👍"]
