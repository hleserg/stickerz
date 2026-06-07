"""Image-model adapters (Gemini / GPT) behind a common interface + a mock."""

from __future__ import annotations

from sticker_service.services.models.base import (
    ImageModel,
    ModelError,
    ModelRefusalError,
)
from sticker_service.services.models.factory import build_model
from sticker_service.services.models.gemini import GeminiImageModel
from sticker_service.services.models.gpt import GptImageModel
from sticker_service.services.models.mock import MockImageModel

__all__ = [
    "GeminiImageModel",
    "GptImageModel",
    "ImageModel",
    "MockImageModel",
    "ModelError",
    "ModelRefusalError",
    "build_model",
]
