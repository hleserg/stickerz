"""Tests for the canonical pipeline engine and the geometry gate."""

from __future__ import annotations

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
