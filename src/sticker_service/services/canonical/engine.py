"""The canonical pipeline executor — the heart of the product (§4).

Fully data-driven: one generic loop walks the style's YAML steps, collects the
declared refs from run state, resolves prompt placeholders, generates, runs the
gate, and feeds the result forward as the sliding anchor. There is **no
per-style branching** (invariant §B.4).

On a gate slip, only *that* step is rolled back and re-shot with a smaller delta
(a nudge appended to the prompt) — never the whole chain (§4.3).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from sticker_service.db.models import SubjectType
from sticker_service.services.canonical.gate import run_gate
from sticker_service.services.canonical.schema import PipelineStep, Style
from sticker_service.services.models.base import ImageModel

logger = logging.getLogger(__name__)

# Called after each completed step with (done, total) for progress UIs.
StepCallback = Callable[[int, int], Awaitable[None]]

_SMALLER_DELTA_NUDGE = (
    " Make a SMALLER change from the reference this time: keep the face geometry "
    "identical, move only slightly toward the target style."
)


class CanonicalError(RuntimeError):
    """The pipeline could not produce a canonical."""


class CanonicalGateError(CanonicalError):
    """A step kept failing the geometry gate after all retries (§4.3)."""


def build_age_clause(subject_type: SubjectType, child_age: int | None) -> str:
    """Resolve ``{age_clause}`` (§5.1.2 / §B.4).

    Empty for adults — age is never imposed on grown-ups. For children, anchor
    the age so the model does not systematically age them up.
    """
    if subject_type == "child":
        if child_age is None:
            raise ValueError("child subject requires child_age")
        return (
            f"Это ребёнок примерно {child_age} лет: сохрани детские пропорции лица, "
            "не делай старше."
        )
    return ""


class CanonicalEngine:
    """Runs a style's pipeline from a photo to a confirmed-ready canonical."""

    def __init__(
        self,
        model: ImageModel,
        *,
        gate_threshold: float = 0.6,
        max_step_retries: int = 2,
    ) -> None:
        self._model = model
        self._threshold = gate_threshold
        self._max_step_retries = max_step_retries

    async def run(
        self,
        style: Style,
        photo: bytes,
        *,
        subject_type: SubjectType,
        child_age: int | None = None,
        on_step: StepCallback | None = None,
    ) -> bytes:
        """Execute the pipeline and return the final canonical image bytes.

        ``on_step(done, total)`` is awaited after each completed step, for
        progress reporting.
        """
        age_clause = build_age_clause(subject_type, child_age)
        total = len(style.pipeline)
        logger.info(
            "canonical: style=%s steps=%d subject=%s age=%s",
            style.style_id,
            total,
            subject_type,
            child_age,
        )
        by_step: dict[int, bytes] = {}
        prev: bytes | None = None

        for step in style.pipeline:
            refs = self._collect_refs(step.refs, photo=photo, prev=prev, by_step=by_step)
            prompt = self._resolve(step.prompt, age_clause)
            # The gate compares against the previous frame; for step 1 that is
            # the photo itself (does the first drawing keep the photo's face?).
            gate_prev = prev if prev is not None else photo
            image = await self._run_step(style.style_id, step, prompt, refs, gate_prev)
            by_step[step.step] = image
            prev = image
            logger.info("canonical step %d/%d done (%d bytes)", step.step, total, len(image))
            if on_step is not None:
                await on_step(step.step, total)

        if prev is None:  # pragma: no cover - schema guarantees a non-empty pipeline
            raise CanonicalError("empty pipeline produced no canonical")
        logger.info("canonical: complete for style=%s", style.style_id)
        return prev

    async def _run_step(
        self,
        style_id: str,
        step: PipelineStep,
        prompt: str,
        refs: list[bytes],
        gate_prev: bytes,
    ) -> bytes:
        attempts = self._max_step_retries + 1
        for attempt in range(attempts):
            attempt_prompt = prompt if attempt == 0 else prompt + _SMALLER_DELTA_NUDGE
            logger.info(
                "canonical step %d: generating (attempt %d/%d, refs=%s)",
                step.step,
                attempt + 1,
                attempts,
                step.refs,
            )
            image = await self._model.generate(attempt_prompt, refs)
            result = await run_gate(
                step.gate, self._model, gate_prev, image, threshold=self._threshold
            )
            logger.info(
                "canonical step %d: gate=%s score=%.2f ok=%s",
                step.step,
                step.gate,
                result.score,
                result.ok,
            )
            if result.ok:
                return image
            logger.warning(
                "gate slip on style=%s step=%s attempt=%d (score=%.2f); re-shooting",
                style_id,
                step.step,
                attempt + 1,
                result.score,
            )
        raise CanonicalGateError(
            f"style={style_id} step={step.step} failed the geometry gate after {attempts} attempts"
        )

    @staticmethod
    def _collect_refs(
        refs: list[str],
        *,
        photo: bytes,
        prev: bytes | None,
        by_step: dict[int, bytes],
    ) -> list[bytes]:
        collected: list[bytes] = []
        for ref in refs:
            if ref == "photo":
                collected.append(photo)
            elif ref == "prev":
                if prev is None:
                    raise CanonicalError("ref 'prev' used on the first step")
                collected.append(prev)
            elif ref.startswith("step_"):
                index = int(ref.removeprefix("step_"))
                if index not in by_step:
                    raise CanonicalError(f"ref '{ref}' references a step not yet produced")
                collected.append(by_step[index])
            else:  # 'canonical' is only valid during sticker generation, not here
                raise CanonicalError(f"ref '{ref}' is not valid inside the canonical pipeline")
        return collected

    @staticmethod
    def _resolve(prompt: str, age_clause: str) -> str:
        # Only known placeholders are substituted (schema already validated them).
        return prompt.replace("{age_clause}", age_clause)
