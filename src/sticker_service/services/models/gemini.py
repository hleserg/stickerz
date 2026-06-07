"""Gemini 3 Pro Image wrapper — the canonical/sticker workhorse (§8).

The SDK is imported lazily so the package installs and tests run without the
optional ``models`` extra. Construction validates the API key so misconfiguration
fails fast. Network bodies are excluded from coverage (they need live keys);
their parsing helpers are pure and unit-tested.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from sticker_service.services.models.base import ImageModel, ModelError, ModelRefusalError

#: Generation model (image-to-image with a photo reference, §8).
IMAGE_MODEL = "gemini-3-pro-image"
#: Vision/text model for the geometry gate and emoji picking (cheap + fast).
VISION_MODEL = "gemini-2.5-flash"

_REFUSAL_REASONS = ("SAFETY", "PROHIBITED", "BLOCK", "RECITATION")
_FLOAT_RE = re.compile(r"\d+(?:\.\d+)?|\.\d+")
# Emoji-ish codepoint ranges, mirrors stickers.emoji validation.
_EMOJI_RE = re.compile("[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f1e6-\U0001f1ff❤⭐⭕]")


def _mime(data: bytes) -> str:
    return "image/jpeg" if data[:3] == b"\xff\xd8\xff" else "image/png"


def parse_score(text: str) -> float:
    """Extract a 0..1 geometry score from the vision model's reply."""
    match = _FLOAT_RE.search(text or "")
    if not match:
        return 0.0
    return max(0.0, min(1.0, float(match.group())))


def parse_emoji(text: str) -> str | None:
    """Extract the first emoji from the vision model's reply, or None."""
    match = _EMOJI_RE.search(text or "")
    return match.group() if match else None


class GeminiImageModel(ImageModel):
    """Image model backed by Google Gemini."""

    name = "gemini"

    def __init__(self, *, api_key: str, proxy: str = "") -> None:
        if not api_key:
            raise ValueError("GEMINI_KEY is required for the gemini provider")
        self._api_key = api_key
        self._proxy = proxy
        self._client = None  # lazily created

    def _get_client(self):  # pragma: no cover - needs the SDK + network
        if self._client is None:
            try:
                from google import genai
            except ImportError as exc:
                raise ModelError("google-genai not installed; install the 'models' extra") from exc
            self._client = genai.Client(api_key=self._api_key)
        return self._client

    async def generate(self, prompt: str, refs: Sequence[bytes] = ()) -> bytes:  # pragma: no cover
        from google.genai import types

        client = self._get_client()
        contents: list[Any] = [
            types.Part.from_bytes(data=ref, mime_type=_mime(ref)) for ref in refs
        ]
        contents.append(prompt)
        response = await client.aio.models.generate_content(
            model=IMAGE_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(response_modalities=["TEXT", "IMAGE"]),
        )
        candidate = response.candidates[0] if response.candidates else None
        if candidate is None or self._is_refusal(candidate):
            raise ModelRefusalError(f"Gemini refused generation: {self._finish(candidate)}")
        content = candidate.content
        for part in (content.parts if content else None) or []:
            if part.inline_data and part.inline_data.data:
                return part.inline_data.data
        raise ModelError("Gemini returned no image part")

    async def judge_geometry(self, frame_a: bytes, frame_b: bytes) -> float:  # pragma: no cover
        from google.genai import types

        client = self._get_client()
        prompt = (
            "Это один и тот же человек с одинаковой геометрией лица на обоих "
            "изображениях? Оцени совпадение геометрии лица числом от 0 до 1. "
            "Ответь ТОЛЬКО числом."
        )
        response = await client.aio.models.generate_content(
            model=VISION_MODEL,
            contents=[
                types.Part.from_bytes(data=frame_a, mime_type=_mime(frame_a)),
                types.Part.from_bytes(data=frame_b, mime_type=_mime(frame_b)),
                prompt,
            ],
        )
        return parse_score(response.text or "")

    async def pick_emoji(self, image: bytes) -> str:  # pragma: no cover
        from google.genai import types

        client = self._get_client()
        prompt = (
            "Подбери ОДИН эмодзи под эмоцию/жест на этом стикере. Ответь только эмодзи, без текста."
        )
        response = await client.aio.models.generate_content(
            model=VISION_MODEL,
            contents=[types.Part.from_bytes(data=image, mime_type=_mime(image)), prompt],
        )
        return parse_emoji(response.text or "") or "🙂"

    @staticmethod
    def _finish(candidate: object) -> str:  # pragma: no cover - diagnostic
        return str(getattr(candidate, "finish_reason", "unknown"))

    @classmethod
    def _is_refusal(cls, candidate: object) -> bool:  # pragma: no cover - needs live resp
        reason = str(getattr(candidate, "finish_reason", "")).upper()
        return any(flag in reason for flag in _REFUSAL_REASONS)
