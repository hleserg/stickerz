"""GPT Image wrapper — the parallel path for the prototype bake-off (§15.1).

Lazy SDK import; network bodies excluded from coverage. Note (§8): GPT Image 2
does not support transparent background — only gpt-image-1.5 / gpt-image-1 do —
so this path leans on white-bg + chroma slicing rather than native alpha.
"""

from __future__ import annotations

from collections.abc import Sequence

from sticker_service.services.models.base import ImageModel, ModelError


class GptImageModel(ImageModel):
    """Image model backed by OpenAI GPT Image."""

    name = "gpt"

    def __init__(self, *, api_key: str, proxy: str = "") -> None:
        if not api_key:
            raise ValueError("GPT_KEY is required for the gpt provider")
        self._api_key = api_key
        self._proxy = proxy
        self._client = None

    def _get_client(self):  # pragma: no cover - needs the SDK + network
        if self._client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError as exc:
                raise ModelError("openai not installed; install the 'models' extra") from exc
            self._client = AsyncOpenAI(api_key=self._api_key)
        return self._client

    async def generate(self, prompt: str, refs: Sequence[bytes] = ()) -> bytes:  # pragma: no cover
        raise ModelError("GPT generate() is calibrated on the prototype (§15.1)")

    async def judge_geometry(self, frame_a: bytes, frame_b: bytes) -> float:  # pragma: no cover
        raise ModelError("GPT judge_geometry() is calibrated on the prototype (§15.1)")

    async def pick_emoji(self, image: bytes) -> str:  # pragma: no cover
        raise ModelError("GPT pick_emoji() is calibrated on the prototype (§15.1)")
