"""Gemini 3 Pro Image wrapper — the canonical/sticker workhorse (§8).

The SDK is imported lazily so the package installs and tests run without the
optional ``models`` extra. Exact request shapes are calibrated on the prototype
(§15.1); the network bodies are excluded from coverage. Construction validates
the API key so misconfiguration fails fast and clearly.
"""

from __future__ import annotations

from collections.abc import Sequence

from sticker_service.services.models.base import ImageModel, ModelError


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
        raise ModelError("Gemini generate() is calibrated on the prototype (§15.1)")

    async def judge_geometry(self, frame_a: bytes, frame_b: bytes) -> float:  # pragma: no cover
        raise ModelError("Gemini judge_geometry() is calibrated on the prototype (§15.1)")

    async def pick_emoji(self, image: bytes) -> str:  # pragma: no cover
        raise ModelError("Gemini pick_emoji() is calibrated on the prototype (§15.1)")
