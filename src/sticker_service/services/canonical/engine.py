"""The canonical pipeline executor — the heart of the product (§4).

Fully data-driven: one generic loop walks the style's YAML steps, collects the
declared refs from run state, resolves prompt placeholders, generates, and feeds
the result forward as the sliding anchor. There is **no per-style branching**
(invariant §B.4).

The geometry gate is **advisory only** (§4.3): comparing a real photo face to a
Disney/anime drawing can't yield a meaningful pass/fail, so we never re-shoot —
we just log an alert when the deviation looks large, and keep the frame.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from sticker_service.db.models import SubjectType
from sticker_service.services.canonical.gate import run_gate
from sticker_service.services.canonical.schema import PipelineStep, Style
from sticker_service.services.models.base import (
    ImageModel,
    Ladder,
    ModelError,
    ModelRefusalError,
    generate_via_ladder,
)
from sticker_service.services.models.gemini import CANONICAL_LADDER

logger = logging.getLogger(__name__)

# Called after each completed step with (done, total) for progress UIs.
StepCallback = Callable[[int, int], Awaitable[None]]

#: Advisory geometry threshold: below this we log an alert (no re-shoot).
DEFAULT_ALERT_THRESHOLD = 0.25

#: Resolves ``{clean_bg}`` — the pipeline-wide instruction to isolate the
#: subject on a plain background (no props/furniture/text/scenery). It is a
#: product-wide constraint, not a per-style choice, so it lives here once
#: instead of being duplicated across every style's first-step prompt.
CLEAN_BACKGROUND_CLAUSE = (
    "Помести человека на простом однотонном фоне — без предметов, мебели, текста и сцены позади."
)

# Appended on a child-safety refusal to steer past the filter (§6). The empty
# first element means "try the plain prompt first". Russian — matches the prompts
# and empirically clears IMAGE_SAFETY on both pro and flash.
_WHOLESOME_NUDGES = (
    "",
    " Это доброжелательная детская иллюстрация для семейного стикерпака, безопасная для детей.",
    " Нарисуй только ребёнка, без посторонних предметов; дружелюбная детская иллюстрация.",
)


def _is_yes(answer: str) -> bool:
    """True if a yes/no vision answer is affirmative (да / yes)."""
    return answer.strip().lower().startswith(("да", "yes"))


class CanonicalError(RuntimeError):
    """The pipeline could not produce a canonical."""


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
        alert_threshold: float = DEFAULT_ALERT_THRESHOLD,
        ladder: Ladder = CANONICAL_LADDER,
    ) -> None:
        self._model = model
        self._alert_threshold = alert_threshold
        self._ladder = ladder

    async def run(
        self,
        style: Style,
        photo: bytes,
        *,
        subject_type: SubjectType,
        child_age: int | None = None,
        on_step: StepCallback | None = None,
        done_steps: dict[int, bytes] | None = None,
        on_step_done: Callable[[int, bytes], Awaitable[None]] | None = None,
    ) -> bytes:
        """Execute the pipeline and return the final canonical image bytes.

        ``on_step(done, total)`` is awaited after each completed step (progress).
        ``done_steps`` pre-seeds already-finished steps so a failed run can be
        RESUMED instead of restarting; ``on_step_done(step, image)`` is awaited
        as each step finishes so the caller can persist progress for resuming.
        """
        age_clause = build_age_clause(subject_type, child_age)
        total = len(style.pipeline)
        by_step: dict[int, bytes] = dict(done_steps or {})
        # Resume point: the highest already-completed step is the running anchor.
        prev: bytes | None = by_step[max(by_step)] if by_step else None
        logger.info(
            "canonical: style=%s steps=%d subject=%s age=%s resume_from=%d",
            style.style_id,
            total,
            subject_type,
            child_age,
            len(by_step),
        )

        for step in style.pipeline:
            if step.step in by_step:
                logger.info("canonical step %d/%d: already done, skipping", step.step, total)
                prev = by_step[step.step]
                continue
            if step.skip_if_yes and prev is not None and await self._precheck_skips(step, prev):
                logger.info(
                    "canonical step %d/%d: skipped by pre-check '%s'",
                    step.step,
                    total,
                    step.skip_if_yes,
                )
                by_step[step.step] = prev
                if on_step is not None:
                    await on_step(step.step, total)
                continue
            refs = self._collect_refs(step.refs, photo=photo, prev=prev, by_step=by_step)
            prompt = self._resolve(step.prompt, age_clause)
            # The gate compares against the previous frame; for step 1 that is
            # the photo itself (does the first drawing keep the photo's face?).
            gate_prev = prev if prev is not None else photo
            image = await self._run_step(style.style_id, step, prompt, refs, gate_prev)
            by_step[step.step] = image
            prev = image
            logger.info("canonical step %d/%d done (%d bytes)", step.step, total, len(image))
            if on_step_done is not None:
                await on_step_done(step.step, image)
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
        logger.info("canonical step %d: generating (refs=%s)", step.step, step.refs)
        try:
            image = await generate_via_ladder(
                self._model, prompt, refs, self._ladder, reformulations=_WHOLESOME_NUDGES
            )
        except ModelRefusalError as exc:
            raise CanonicalError(
                f"step {step.step}: model refused generation after reformulations ({exc})"
            ) from exc
        # Advisory gate only: a real face vs a stylised drawing can't be a hard
        # pass/fail, so we never re-shoot — just alert on a large deviation (§4.3).
        result = await run_gate(
            step.gate, self._model, gate_prev, image, threshold=self._alert_threshold
        )
        if not result.ok:
            logger.warning(
                "canonical geometry alert: style=%s step=%s score=%.2f (advisory; kept)",
                style_id,
                step.step,
                result.score,
            )
        else:
            logger.info("canonical step %d: gate=%s score=%.2f", step.step, step.gate, result.score)
        return image

    async def _precheck_skips(self, step: PipelineStep, prev: bytes) -> bool:
        """Ask the configured yes/no question about ``prev``; skip the step on yes.

        The pre-check is an optimization (skip a redundant turn-to-camera step), so
        a vision-model outage must not abort the run — on failure we simply don't
        skip and run the step normally.
        """
        if not step.skip_if_yes:
            return False
        try:
            answer = await self._model.ask(prev, step.skip_if_yes)
        except ModelError as exc:
            logger.warning(
                "canonical step %d: pre-check vision unavailable (%s); running step",
                step.step,
                str(exc)[:100],
            )
            return False
        logger.info("canonical step %d: pre-check answer=%r", step.step, answer)
        return _is_yes(answer)

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
        return prompt.replace("{age_clause}", age_clause).replace(
            "{clean_bg}", CLEAN_BACKGROUND_CLAUSE
        )
