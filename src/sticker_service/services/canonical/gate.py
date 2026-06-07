"""Face-geometry gate between neighboring pipeline frames (§4.3).

A narrow check — "did the face geometry slip?" — not a subjective "is it a good
likeness?" (that's the user's call, §4.3). The gate **type comes from the YAML
step**, never hardcoded (invariant §B.4). MVP starts with ``vision_judge``;
``face_geometry`` (InsightFace/MediaPipe) is a post-MVP optimization.
"""

from __future__ import annotations

from dataclasses import dataclass

from sticker_service.services.canonical.schema import Gate
from sticker_service.services.models.base import ImageModel


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
    """
    if gate is Gate.NONE:
        return GateResult(ok=True, score=1.0)
    if gate is Gate.VISION_JUDGE:
        score = await model.judge_geometry(prev, current)
        return GateResult(ok=score >= threshold, score=score)
    if gate is Gate.FACE_GEOMETRY:  # pragma: no cover - post-MVP optimization
        raise NotImplementedError(
            "face_geometry gate (InsightFace/MediaPipe) is a post-MVP optimization (§4.3)"
        )
    raise ValueError(f"unknown gate: {gate!r}")  # pragma: no cover - enum-exhaustive
