"""Gemini 3 Pro Image wrapper — the canonical/sticker workhorse (§8).

The SDK is imported lazily so the package installs and tests run without the
optional ``models`` extra. Construction validates the API key so misconfiguration
fails fast. Network bodies are excluded from coverage (they need live keys);
their parsing helpers are pure and unit-tested.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
from typing import Any

from sticker_service.services.models import errors as model_errors
from sticker_service.services.models.base import (
    ImageModel,
    ModelError,
    ModelQuotaError,
    ModelRefusalError,
    emit_notice,
)

logger = logging.getLogger(__name__)

#: Generation model (image-to-image with a photo reference, §8). Primary holds
#: the face best; fallbacks are more available when the primary is overloaded.
IMAGE_MODEL = "gemini-3-pro-image"
_IMAGE_FALLBACKS = ("gemini-3.1-flash-image", "gemini-2.5-flash-image")
#: Vision/text model for the geometry gate, gaze pre-check and emoji picking
#: (cheap + fast). It is the single most-overloaded model in practice ("high
#: demand" 503s), so it gets its own fallback ladder — failing over to other
#: vision models keeps the advisory checks answered during a single-model spike
#: instead of degrading them all at once.
VISION_MODEL = "gemini-2.5-flash"
_VISION_FALLBACKS = ("gemini-2.5-flash-lite", "gemini-2.5-pro")
#: Ordered vision models tried in turn: cheap primary → cheaper fallback →
#: stronger last resort.
VISION_LADDER: tuple[str, ...] = (VISION_MODEL, *_VISION_FALLBACKS)
#: Brief retries per model before failing over to the next one.
_VISION_ATTEMPTS_PER_MODEL = 2

FLASH_MODEL = _IMAGE_FALLBACKS[0]  # gemini-3.1-flash-image
# Fallback ladders (model, resolution) — HLE-1055. pro primary (quality), flash
# fallback when pro is overloaded; resolution steps down to survive demand spikes.
CANONICAL_LADDER: tuple[tuple[str, str], ...] = (
    (IMAGE_MODEL, "1K"),  # canonical reference — small is enough
    # Flash fallbacks capped at 2K: a 4K canonical balloons every later step
    # (refs, gates, persistence) and was the RAM spike that OOMed the 1 GB VDS
    # during an overload storm; as a reference image 2K loses nothing.
    (FLASH_MODEL, "2K"),
    (FLASH_MODEL, "1K"),
)
SHEET_LADDER: tuple[tuple[str, str], ...] = (
    (IMAGE_MODEL, "4K"),  # sheet carries Cyrillic captions — keep it sharp
    (IMAGE_MODEL, "2K"),
    (FLASH_MODEL, "4K"),
)

_MAX_GEN_ATTEMPTS = 6
#: Per-call deadline (seconds). When Gemini is overloaded a socket can stall
#: mid-request with no response and no error; without a ceiling the SDK call
#: blocks the user's whole flow indefinitely (observed: a 5-minute "frozen"
#: step). A timeout turns that stall into a plain ``TimeoutError`` — already
#: classified retryable — so the loop backs off and fails over down the model
#: ladder instead of hanging. Image gen (esp. 4K sheets) is legitimately slow,
#: so its ceiling is generous; vision calls are quick.
_IMAGE_TIMEOUT_S = 120.0
_VISION_TIMEOUT_S = 60.0
_REFUSAL_REASONS = ("SAFETY", "PROHIBITED", "BLOCK", "RECITATION")
_FLOAT_RE = re.compile(r"\d+(?:\.\d+)?|\.\d+")
# Emoji-ish codepoint ranges, mirrors stickers.emoji validation.
_EMOJI_RE = re.compile("[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f1e6-\U0001f1ff❤⭐⭕]")


def _mime(data: bytes) -> str:
    return "image/jpeg" if data[:3] == b"\xff\xd8\xff" else "image/png"


async def _capped(awaitable: Any, timeout: float) -> Any:
    """Await ``awaitable`` with a hard deadline; a stalled call raises ``TimeoutError``.

    Wraps every network call so an overloaded-model socket stall fails fast into
    the retry/fallback ladder instead of freezing the user's flow forever.
    """
    return await asyncio.wait_for(awaitable, timeout)


# Thin wrappers over the shared taxonomy (single source of retry/billing policy).
_is_billing = model_errors.is_quota
_is_retryable = model_errors.is_retryable


def image_model_for_attempt(attempt: int) -> str:
    """Pick the image model for a 0-based attempt: primary first, then fallbacks."""
    if attempt < 2:
        return IMAGE_MODEL
    return _IMAGE_FALLBACKS[min(attempt - 2, len(_IMAGE_FALLBACKS) - 1)]


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

    async def generate(  # pragma: no cover
        self,
        prompt: str,
        refs: Sequence[bytes] = (),
        *,
        model: str | None = None,
        image_size: str | None = None,
    ) -> bytes:
        """Generate an image.

        ``model`` forces a specific image model (e.g. for an A/B run) and disables
        the family fallback so the comparison stays clean. ``image_size`` ("1K" /
        "2K" / "4K") sets the output resolution when the model supports it.
        """
        from google.genai import types

        client = self._get_client()
        contents: list[Any] = [
            types.Part.from_bytes(data=ref, mime_type=_mime(ref)) for ref in refs
        ]
        contents.append(prompt)
        image_config = types.ImageConfig(image_size=image_size) if image_size else None
        config = types.GenerateContentConfig(
            response_modalities=["TEXT", "IMAGE"], image_config=image_config
        )

        last: Exception | None = None
        for attempt in range(_MAX_GEN_ATTEMPTS):
            # Forced model stays fixed across retries; otherwise walk the fallback chain.
            model_id = model or image_model_for_attempt(attempt)
            logger.info(
                "gemini.generate model=%s size=%s refs=%d attempt=%d",
                model_id,
                image_size or "default",
                len(refs),
                attempt + 1,
            )
            try:
                response = await _capped(
                    client.aio.models.generate_content(
                        model=model_id, contents=contents, config=config
                    ),
                    _IMAGE_TIMEOUT_S,
                )
                return self._extract_image(response)
            except ModelRefusalError:
                # A real safety refusal — let the engine reformulate, don't retry blindly.
                raise
            except Exception as exc:  # ANY other error -> backoff + fallback model
                if _is_billing(exc):  # out of credits: retrying/failover can't help
                    raise ModelQuotaError(
                        "Gemini account is out of credits/quota — top up billing to "
                        f"resume generation: {str(exc)[:120]}"
                    ) from exc
                last = exc
                kind = "transient" if _is_retryable(exc) else "error"
                if attempt < _MAX_GEN_ATTEMPTS - 1:
                    # Tell the user we're still working: a model swap reads as
                    # "less-loaded model", a same-model retry as "retrying".
                    next_model = model or image_model_for_attempt(attempt + 1)
                    await emit_notice("fallback" if next_model != model_id else "retry")
                    logger.warning("gemini %s %s (%s); retrying", model_id, kind, str(exc)[:100])
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                raise ModelError(
                    f"Gemini generation failed after {_MAX_GEN_ATTEMPTS} attempts: {exc}"
                ) from exc
        raise ModelError(f"Gemini image generation failed after retries: {last}")

    @staticmethod
    def _extract_image(response: Any) -> bytes:  # pragma: no cover - needs live resp
        candidate = response.candidates[0] if response.candidates else None
        if candidate is None or GeminiImageModel._is_refusal(candidate):
            raise ModelRefusalError(
                f"Gemini refused generation: {GeminiImageModel._finish(candidate)}"
            )
        content = candidate.content
        for part in (content.parts if content else None) or []:
            if part.inline_data and part.inline_data.data:
                data = part.inline_data.data
                logger.info("gemini.generate ok (%d bytes)", len(data))
                return data
        raise ModelError("Gemini returned no image part")

    async def _vision_text(
        self, contents: list[Any], config: Any | None = None
    ) -> str:  # pragma: no cover - network
        """Run a vision/text call, failing over across :data:`VISION_LADDER`.

        Each model gets a couple of quick retries; if it still can't answer
        (typically a 503 "high demand"), we drop to the next vision model rather
        than giving up. Only when every model on the ladder is exhausted do we
        raise — so a single overloaded model no longer takes the gate, gaze
        pre-check and emoji down with it.
        """
        client = self._get_client()
        last: Exception | None = None
        for model_id in VISION_LADDER:
            for attempt in range(_VISION_ATTEMPTS_PER_MODEL):
                try:
                    response = await _capped(
                        client.aio.models.generate_content(
                            model=model_id, contents=contents, config=config
                        ),
                        _VISION_TIMEOUT_S,
                    )
                    return response.text or ""
                except Exception as exc:  # retry, then fail over to the next model
                    last = exc
                    final_attempt = attempt == _VISION_ATTEMPTS_PER_MODEL - 1
                    logger.warning(
                        "gemini vision %s failed (%s); %s",
                        model_id,
                        str(exc)[:100],
                        "failing over" if final_attempt else "retrying",
                    )
                    if not final_attempt:
                        await asyncio.sleep(2 * (attempt + 1))
        raise ModelError(f"Gemini vision failed across {len(VISION_LADDER)} models: {last}")

    async def judge_geometry(self, frame_a: bytes, frame_b: bytes) -> float:  # pragma: no cover
        from google.genai import types

        prompt = (
            "Это один и тот же человек с одинаковой геометрией лица на обоих "
            "изображениях? Оцени совпадение геометрии лица числом от 0 до 1. "
            "Ответь ТОЛЬКО числом."
        )
        text = await self._vision_text(
            [
                types.Part.from_bytes(data=frame_a, mime_type=_mime(frame_a)),
                types.Part.from_bytes(data=frame_b, mime_type=_mime(frame_b)),
                prompt,
            ]
        )
        return parse_score(text)

    async def pick_emoji(self, image: bytes) -> str:  # pragma: no cover
        from google.genai import types

        prompt = (
            "Подбери ОДИН эмодзи под эмоцию/жест на этом стикере. Ответь только эмодзи, без текста."
        )
        text = await self._vision_text(
            [types.Part.from_bytes(data=image, mime_type=_mime(image)), prompt]
        )
        return parse_emoji(text) or "🙂"

    async def ask(self, image: bytes, question: str) -> str:  # pragma: no cover
        from google.genai import types

        return await self._vision_text(
            [types.Part.from_bytes(data=image, mime_type=_mime(image)), question]
        )

    async def generate_text(self, prompt: str) -> str:  # pragma: no cover - network
        """Text-only call on the vision ladder, grounded with Google Search.

        Grounding lets the weekly meme-pool refresh see actually-current Runet
        trends instead of the model's training-time memory.
        """
        from google.genai import types

        config = types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())])
        return await self._vision_text([prompt], config=config)

    @staticmethod
    def _finish(candidate: object) -> str:  # pragma: no cover - diagnostic
        return str(getattr(candidate, "finish_reason", "unknown"))

    @classmethod
    def _is_refusal(cls, candidate: object) -> bool:  # pragma: no cover - needs live resp
        reason = str(getattr(candidate, "finish_reason", "")).upper()
        return any(flag in reason for flag in _REFUSAL_REASONS)
