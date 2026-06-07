"""Provider-agnostic image-model interface (§8).

One interface, three operations the pipeline needs:
- ``generate``  — produce an image from a prompt + reference images;
- ``judge_geometry`` — vision-LLM gate score 0..1 between two frames (§4.3);
- ``pick_emoji`` — choose an emoji for a finished sticker (§9).

The whole photo is passed as a reference (never a cropped face — §8), so refs
are raw image bytes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence


class ModelError(RuntimeError):
    """Generic failure talking to the image model."""


class ModelRefusalError(ModelError):
    """Model declined to generate (e.g., child-safety filter — §6)."""


class ImageModel(ABC):
    """Abstract image model. Concrete providers live alongside this module."""

    #: Short provider name for logging/tagging.
    name: str = "abstract"

    @abstractmethod
    async def generate(self, prompt: str, refs: Sequence[bytes] = ()) -> bytes:
        """Generate an image; raise :class:`ModelRefusalError` on a safety refusal."""

    @abstractmethod
    async def judge_geometry(self, frame_a: bytes, frame_b: bytes) -> float:
        """Return a 0..1 score for "same face geometry" between two frames."""

    @abstractmethod
    async def pick_emoji(self, image: bytes) -> str:
        """Return a single emoji matching the sticker's emotion/gesture."""

    async def ask(self, image: bytes, question: str) -> str:
        """Answer a short free-form question about an image (vision Q&A).

        Default returns an empty string (treated as "no" by callers). Providers
        override this; used e.g. for the gaze pre-check before the turn-to-camera
        step.
        """
        return ""
