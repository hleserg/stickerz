"""Tests for the canonical pipeline engine and the geometry gate."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from sticker_service.services.canonical import (
    CanonicalEngine,
    CanonicalGateError,
    Gate,
    GateResult,
    Style,
    build_age_clause,
    run_gate,
)
from sticker_service.services.models import MockImageModel
from sticker_service.services.models.base import ImageModel, ModelRefusalError


class _RefuseThenOk(ImageModel):
    """Refuses the first ``refuse_times`` generate calls, then succeeds."""

    name = "refuse"

    def __init__(self, refuse_times: int) -> None:
        self._left = refuse_times
        self.calls = 0

    async def generate(self, prompt: str, refs: Sequence[bytes] = ()) -> bytes:
        self.calls += 1
        if self._left > 0:
            self._left -= 1
            raise ModelRefusalError("IMAGE_SAFETY")
        return b"\x89PNGok"

    async def judge_geometry(self, frame_a: bytes, frame_b: bytes) -> float:
        return 0.95

    async def pick_emoji(self, image: bytes) -> str:
        return "🙂"


def _style(steps: list[dict[str, object]], suffix: str = "") -> Style:
    return Style.model_validate(
        {
            "schema_version": 1,
            "style_id": "demo",
            "display_name": "Demo",
            "distance": "far",
            "pipeline": steps,
            "sticker_style_suffix": suffix,
        }
    )


def _watercolor_like() -> Style:
    return _style(
        [
            {"step": 1, "prompt": "photo->portrait {age_clause}", "refs": ["photo"]},
            {"step": 2, "prompt": "->semi {age_clause}", "refs": ["prev"]},
            {"step": 3, "prompt": "->final {age_clause}", "refs": ["prev"]},
        ]
    )


# --- age clause (§5.1.2 / §B.4) ---------------------------------------------


def test_age_clause_empty_for_adult() -> None:
    assert build_age_clause("adult", None) == ""


def test_age_clause_set_for_child() -> None:
    clause = build_age_clause("child", 6)
    assert "6" in clause and "ребёнок" in clause


def test_age_clause_child_requires_age() -> None:
    with pytest.raises(ValueError, match="child_age"):
        build_age_clause("child", None)


# --- pipeline execution ------------------------------------------------------


async def test_runs_three_steps_and_returns_canonical() -> None:
    model = MockImageModel(judge_score=0.9)
    engine = CanonicalEngine(model)
    canonical = await engine.run(_watercolor_like(), b"PHOTO", subject_type="adult")
    assert canonical.startswith(b"\x89PNG")
    assert len(model.generate_calls) == 3  # one generate per step, gate passes


async def test_adult_prompts_have_no_age_text() -> None:
    model = MockImageModel()
    await CanonicalEngine(model).run(_watercolor_like(), b"P", subject_type="adult")
    assert all("ребёнок" not in p for p in model.generate_calls)
    assert all("{age_clause}" not in p for p in model.generate_calls)  # placeholder resolved


async def test_child_prompts_carry_age_anchor() -> None:
    model = MockImageModel()
    await CanonicalEngine(model).run(_watercolor_like(), b"P", subject_type="child", child_age=6)
    assert all("ребёнок" in p and "6" in p for p in model.generate_calls)


async def test_gate_slip_rolls_back_only_that_step() -> None:
    # Low score -> gate always fails -> step 1 retried then raises.
    model = MockImageModel(judge_score=0.1)
    engine = CanonicalEngine(model, max_step_retries=2)
    with pytest.raises(CanonicalGateError):
        await engine.run(_watercolor_like(), b"P", subject_type="adult")
    # 3 attempts on the FIRST step only (retries are per-step, not whole chain).
    assert len(model.generate_calls) == 3


async def test_retry_prompt_gets_smaller_delta_nudge() -> None:
    model = MockImageModel(judge_score=0.1)
    with pytest.raises(CanonicalGateError):
        await CanonicalEngine(model, max_step_retries=1).run(
            _style([{"step": 1, "prompt": "base", "refs": ["photo"]}]),
            b"P",
            subject_type="adult",
        )
    assert model.generate_calls[0] == "base"
    assert "SMALLER change" in model.generate_calls[1]


async def test_gate_none_skips_judge() -> None:
    model = MockImageModel(judge_score=0.0)  # would fail vision_judge
    style = _style([{"step": 1, "prompt": "x", "refs": ["photo"], "gate": "none"}])
    canonical = await CanonicalEngine(model).run(style, b"P", subject_type="adult")
    assert canonical  # gate=none lets it through despite score 0.0


async def test_on_step_callback_reports_progress() -> None:
    model = MockImageModel()
    seen: list[tuple[int, int]] = []

    async def cb(done: int, total: int) -> None:
        seen.append((done, total))

    await CanonicalEngine(model).run(_watercolor_like(), b"P", subject_type="adult", on_step=cb)
    assert seen == [(1, 3), (2, 3), (3, 3)]


async def test_refusal_retries_with_reformulation() -> None:
    model = _RefuseThenOk(refuse_times=1)  # refuse once, then succeed
    style = _style([{"step": 1, "prompt": "p", "refs": ["photo"], "gate": "none"}])
    canonical = await CanonicalEngine(model).run(style, b"P", subject_type="adult")
    assert canonical == b"\x89PNGok"
    assert model.calls == 2  # 1 refusal + 1 success


async def test_refusal_gives_up_after_reformulations() -> None:
    from sticker_service.services.canonical import CanonicalError

    model = _RefuseThenOk(refuse_times=99)  # always refuses
    style = _style([{"step": 1, "prompt": "p", "refs": ["photo"], "gate": "none"}])
    with pytest.raises(CanonicalError, match="refused"):
        await CanonicalEngine(model).run(style, b"P", subject_type="adult")
    assert model.calls == 3  # plain + 2 wholesome reformulations


async def test_skip_if_yes_skips_step_when_answer_yes() -> None:
    model = MockImageModel(ask_answer="Да")
    style = _style(
        [
            {"step": 1, "prompt": "a", "refs": ["photo"], "gate": "none"},
            {
                "step": 2,
                "prompt": "turn to camera",
                "refs": ["prev"],
                "gate": "none",
                "skip_if_yes": "смотрит в камеру?",
            },
        ]
    )
    await CanonicalEngine(model).run(style, b"P", subject_type="adult")
    # Only step 1 generated; step 2 skipped by the pre-check.
    assert model.generate_calls == ["a"]


async def test_skip_if_yes_runs_step_when_answer_no() -> None:
    model = MockImageModel(ask_answer="Нет")
    style = _style(
        [
            {"step": 1, "prompt": "a", "refs": ["photo"], "gate": "none"},
            {
                "step": 2,
                "prompt": "turn to camera",
                "refs": ["prev"],
                "gate": "none",
                "skip_if_yes": "смотрит в камеру?",
            },
        ]
    )
    await CanonicalEngine(model).run(style, b"P", subject_type="adult")
    assert model.generate_calls == ["a", "turn to camera"]  # step 2 ran


async def test_step_ref_collection() -> None:
    model = MockImageModel()
    style = _style(
        [
            {"step": 1, "prompt": "a", "refs": ["photo"]},
            {"step": 2, "prompt": "b", "refs": ["prev", "step_1"]},
        ]
    )
    canonical = await CanonicalEngine(model).run(style, b"P", subject_type="adult")
    assert canonical and len(model.generate_calls) == 2


async def test_prev_on_first_step_errors() -> None:
    from sticker_service.services.canonical import CanonicalError

    style = _style([{"step": 1, "prompt": "x", "refs": ["prev"]}])
    with pytest.raises(CanonicalError, match="prev"):
        await CanonicalEngine(MockImageModel()).run(style, b"P", subject_type="adult")


async def test_canonical_ref_inside_pipeline_errors() -> None:
    from sticker_service.services.canonical import CanonicalError

    style = _style([{"step": 1, "prompt": "x", "refs": ["canonical"]}])
    with pytest.raises(CanonicalError, match="canonical"):
        await CanonicalEngine(MockImageModel()).run(style, b"P", subject_type="adult")


async def test_ref_to_unproduced_step_errors() -> None:
    from sticker_service.services.canonical import CanonicalError

    style = _style(
        [
            {"step": 1, "prompt": "a", "refs": ["photo"]},
            {"step": 2, "prompt": "b", "refs": ["step_5"]},
        ]
    )
    with pytest.raises(CanonicalError, match="step_5"):
        await CanonicalEngine(MockImageModel()).run(style, b"P", subject_type="adult")


# --- gate unit ---------------------------------------------------------------


async def test_run_gate_vision_judge_threshold() -> None:
    model = MockImageModel(judge_score=0.7)
    ok = await run_gate(Gate.VISION_JUDGE, model, b"a", b"b", threshold=0.6)
    assert ok == GateResult(ok=True, score=0.7)
    bad = await run_gate(Gate.VISION_JUDGE, model, b"a", b"b", threshold=0.8)
    assert bad.ok is False


async def test_run_gate_none_always_ok() -> None:
    model = MockImageModel(judge_score=0.0)
    assert (await run_gate(Gate.NONE, model, b"a", b"b", threshold=0.9)).ok is True
