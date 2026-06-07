"""Select the image model from config (§15.1: both paths live behind config).

Provider dispatch (not a hardcoded model) keeps Gemini and GPT swappable for the
prototype bake-off. ``mock`` needs no keys and is the safe default for tests.
"""

from __future__ import annotations

from sticker_service.config import get_settings
from sticker_service.services.models.base import ImageModel
from sticker_service.services.models.gemini import GeminiImageModel
from sticker_service.services.models.gpt import GptImageModel
from sticker_service.services.models.mock import MockImageModel


def build_model(provider: str | None = None) -> ImageModel:
    """Build the configured image model. ``provider`` overrides settings."""
    settings = get_settings()
    chosen = (provider or settings.model_provider).lower()
    proxy = settings.models_proxy_url

    if chosen == "mock":
        return MockImageModel()
    if chosen == "gemini":
        return GeminiImageModel(api_key=settings.gemini_api_key, proxy=proxy)
    if chosen == "gpt":
        return GptImageModel(api_key=settings.openai_api_key, proxy=proxy)
    raise ValueError(f"unknown model provider: {chosen!r} (use gemini | gpt | mock)")
