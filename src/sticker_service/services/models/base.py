"""Provider-agnostic image-model interface (§8).

One interface, three operations the pipeline needs:
- ``generate``  — produce an image from a prompt + reference images;
- ``judge_geometry`` — vision-LLM gate score 0..1 between two frames (§4.3);
- ``pick_emoji`` — choose an emoji for a finished sticker (§9).

The whole photo is passed as a reference (never a cropped face — §8), so refs
are raw image bytes.
"""

from __future__ import annotations

import contextlib
import contextvars
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Iterator, Sequence


class ModelError(RuntimeError):
    """Generic failure talking to the image model."""


class ModelRefusalError(ModelError):
    """Model declined to generate (e.g., child-safety filter — §6)."""


class ModelQuotaError(ModelError):
    """Provider account is out of credits/quota — permanent until topped up.

    Distinct from a transient ``429`` rate limit: retrying or failing over to
    another model cannot help, so callers should fail fast and alert the admin.
    """


class ImageModel(ABC):
    """Abstract image model. Concrete providers live alongside this module."""

    #: Short provider name for logging/tagging.
    name: str = "abstract"

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        refs: Sequence[bytes] = (),
        *,
        model: str | None = None,
        image_size: str | None = None,
    ) -> bytes:
        """Generate an image; raise :class:`ModelRefusalError` on a safety refusal.

        ``model``/``image_size`` let a caller force a specific image model and
        output resolution (used by the fallback ladder); providers that don't
        support them ignore them.
        """

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

    async def generate_text(self, prompt: str) -> str:
        """Generate free-form TEXT, no images (e.g. the weekly meme-pool refresh).

        Default raises so callers treat text generation as unavailable instead
        of silently acting on an empty reply.
        """
        raise ModelError(f"model '{self.name}' does not support text generation")


# --- live retry/fallback notices ---------------------------------------------
# A long model call (overloaded model, stalled socket) must never look frozen to
# the user. Whenever a call is re-issued or fails over to another model we push a
# short notice key ("retry"/"fallback") so the flow can swap the status line.
NoticeCallback = Callable[[str], Awaitable[None]]

# The sink lives in a ContextVar rather than a parameter so the deep retry logic
# stays out of every ``generate()`` signature: the flow installs a sink around
# its generation call, and the model reads it back in the SAME task. Concurrent
# users stay isolated because ContextVars are per-task.
_notice_sink: contextvars.ContextVar[NoticeCallback | None] = contextvars.ContextVar(
    "model_notice_sink", default=None
)


async def emit_notice(key: str) -> None:
    """Push a retry/fallback notice to the active flow's sink, if one is installed."""
    sink = _notice_sink.get()
    if sink is not None:
        with contextlib.suppress(Exception):  # a UI hiccup must never fail generation
            await sink(key)


@contextlib.contextmanager
def notice_sink(callback: NoticeCallback | None) -> Iterator[None]:
    """Install ``callback`` as the retry/fallback notice sink for the wrapped call."""
    token = _notice_sink.set(callback)
    try:
        yield
    finally:
        _notice_sink.reset(token)


# --- model health (stall circuit breaker) ------------------------------------
# When a model STALLS (per-call timeouts, not quick 503s), burning the whole
# generation budget rediscovering that on every step is fatal: 5 stalled
# attempts × 120s ate a tester's full 10-minute window before the ladder ever
# reached the healthy fallback (live incident, 11.06). A stalled model is
# remembered as unhealthy for a few minutes so every later step, sheet and
# user goes straight to the next rung.
_UNHEALTHY_SECONDS = 240.0
_unhealthy_until: dict[str, float] = {}


def mark_unhealthy(model_id: str, seconds: float = _UNHEALTHY_SECONDS) -> None:
    """Remember that ``model_id`` is stalling; ladders will skip it for a while."""
    import time

    _unhealthy_until[model_id] = time.monotonic() + seconds


def is_unhealthy(model_id: str) -> bool:
    """True while ``model_id`` is inside its unhealthy cool-off window."""
    import time

    until = _unhealthy_until.get(model_id)
    return until is not None and time.monotonic() < until


# A fallback ladder is an ordered list of (image_model, image_size) rungs.
Ladder = Sequence[tuple[str, str]]


async def generate_via_ladder(
    model: ImageModel,
    prompt: str,
    refs: Sequence[bytes],
    ladder: Ladder,
    *,
    reformulations: Sequence[str] = ("",),
) -> bytes:
    """Walk (model, resolution) rungs until one yields an image (§4.3, HLE-1055).

    Per rung the underlying ``generate`` already retries transient 503s; if the
    rung still can't produce (overload/availability) we drop to the next rung. A
    safety **refusal** short-circuits the ladder — a different model won't un-flag
    the content — but we first try the ``reformulations`` (gentler prompts) on the
    refusing rung. A quota/credits error fails fast.
    """
    last: Exception | None = None
    # Skip rungs whose model is currently stalling — unless that would skip
    # everything (then try them anyway: a degraded attempt beats none).
    rungs = list(ladder)
    healthy = [rung for rung in rungs if not is_unhealthy(rung[0])]
    if healthy:
        rungs = healthy
    for rung, (image_model, image_size) in enumerate(rungs):
        if rung > 0:  # dropped off the previous rung → a less-loaded model/res
            await emit_notice("fallback")
        refusal: ModelRefusalError | None = None
        for nudge in reformulations:
            try:
                return await model.generate(
                    prompt + nudge, refs, model=image_model, image_size=image_size
                )
            except ModelRefusalError as exc:
                refusal = exc  # try the next gentler reformulation on this rung
            except ModelQuotaError:
                raise  # out of credits — failing over can't help
            except ModelError as exc:
                last = exc  # transient/availability exhausted on this rung
                break
        if refusal is not None:
            raise refusal  # all reformulations refused → another model won't help
    raise last or ModelError("generation ladder exhausted with no attempts")
