"""Face-geometry gate between neighboring pipeline frames (§4.3).

A narrow check — "did the face geometry slip?" — not a subjective "is it a good
likeness?" (that's the user's call, §4.3). The gate **type comes from the YAML
step**, never hardcoded (invariant §B.4). MVP starts with ``vision_judge``;
``face_geometry`` (InsightFace/MediaPipe) is a post-MVP optimization.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sticker_service.services.canonical.schema import Gate
from sticker_service.services.models.base import ImageModel, ModelError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GateResult:
    """Outcome of a gate check."""

    ok: bool
    score: float


async def run_gate(
    gate: Gate,
    model: ImageModel,
    prev: bytes,
    current: bytes,
    *,
    threshold: float,
) -> GateResult:
    """Compare two neighboring frames per the configured gate type.

    ``none`` always passes; ``vision_judge`` asks the model for a 0..1 geometry
    score; ``face_geometry`` is not yet implemented (post-MVP, §4.3).

    The gate is **advisory** (§4.3): we never re-shoot, only alert. So a vision
    model outage (e.g. a 503 "high demand" on the cheap vision model) must NOT
    abort the run — we keep the frame, exactly as we do for a low score, and let
    the caller log it.
    """
    if gate is Gate.NONE:
        return GateResult(ok=True, score=1.0)
    if gate is Gate.VISION_JUDGE:
        try:
            score = await model.judge_geometry(prev, current)
        except ModelError as exc:
            logger.warning("gate vision unavailable (%s); keeping frame (advisory)", str(exc)[:100])
            return GateResult(ok=True, score=-1.0)
        return GateResult(ok=score >= threshold, score=score)
    if gate is Gate.FACE_GEOMETRY:  # pragma: no cover - post-MVP optimization
        raise NotImplementedError(
            "face_geometry gate (InsightFace/MediaPipe) is a post-MVP optimization (§4.3)"
        )
    raise ValueError(f"unknown gate: {gate!r}")  # pragma: no cover - enum-exhaustive
