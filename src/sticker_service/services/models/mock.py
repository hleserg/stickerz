"""Deterministic in-memory image model for tests and key-less runs.

No network. ``generate`` returns a tiny PNG whose color is derived from the
prompt+refs so output is stable and inspectable. Can be told to refuse on a
substring (to exercise the child-safety retry path, §6) and to return a fixed
gate score / emoji.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from io import BytesIO

from PIL import Image

from sticker_service.services.models.base import ImageModel, ModelRefusalError


def _png_for(seed: bytes) -> bytes:
    digest = hashlib.sha256(seed).digest()
    color = (digest[0], digest[1], digest[2], 255)
    image = Image.new("RGBA", (8, 8), color)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


class MockImageModel(ImageModel):
    """Configurable fake model. Deterministic; safe to call without keys."""

    name = "mock"

    def __init__(
        self,
        *,
        refuse_on: str | None = None,
        judge_score: float = 0.9,
        emoji: str = "😄",
    ) -> None:
        self._refuse_on = refuse_on
        self._judge_score = judge_score
        self._emoji = emoji
        #: prompts seen, in order — handy for asserting one-call-per-sheet etc.
        self.generate_calls: list[str] = []

    async def generate(self, prompt: str, refs: Sequence[bytes] = ()) -> bytes:
        self.generate_calls.append(prompt)
        if self._refuse_on is not None and self._refuse_on in prompt:
            raise ModelRefusalError("mock refusal (safety filter simulation)")
        seed = prompt.encode("utf-8") + b"".join(refs)
        return _png_for(seed)

    async def judge_geometry(self, frame_a: bytes, frame_b: bytes) -> float:
        return self._judge_score

    async def pick_emoji(self, image: bytes) -> str:
        return self._emoji
