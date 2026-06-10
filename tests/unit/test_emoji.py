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
    emoji_for_caption,
    extract_emoji,
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

    async def generate(  # pragma: no cover
        self, prompt: str, refs: Sequence[bytes] = (), **_: object
    ) -> bytes:
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


def test_emoji_for_caption_standard_and_inline() -> None:
    assert emoji_for_caption("Привет!") == "👋"  # standard map
    assert emoji_for_caption("Окей 😉") == "😉"  # standard caption that holds an emoji
    assert emoji_for_caption("Огонь 🔥") == "🔥"  # emoji typed into a custom caption
    assert emoji_for_caption("Бежим") is None  # plain custom → needs the model
    assert extract_emoji("просто текст") is None


class _CountingEmojiModel(MockImageModel):
    """Mock that counts how many times the vision emoji call is actually made."""

    def __init__(self) -> None:
        super().__init__(emoji="🙂")
        self.emoji_calls = 0

    async def pick_emoji(self, image: bytes) -> str:
        self.emoji_calls += 1
        return await super().pick_emoji(image)


async def test_assign_emojis_skips_model_for_known_captions() -> None:
    model = _CountingEmojiModel()
    captions = ["Привет!", "Класс!", "Бежим"]  # 2 standard + 1 plain custom
    emojis = await assign_emojis(model, [b"a", b"b", b"c"], captions)
    assert emojis == ["👋", "👍", "🙂"]  # standard mapped, custom via model fallback
    assert model.emoji_calls == 1  # only the plain custom caption hit the model
