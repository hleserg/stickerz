"""A/B comparison harness for image models (HLE-1040 / HLE-1048).

Runs two axes and builds a side-by-side gallery + a metrics table so the result
can be judged at a glance:

- **A/B-1 (canonical):** each photo through pro vs flash → success (esp. children
  IMAGE_SAFETY), latency, cost.
- **A/B-2 (sheet):** a fixed canonical → {pro,flash}×{2K,4K} → real slicing to
  512px → cyrillic/sharpness, cost.

Live run only (needs Gemini credits + photos). Photos are read from a local dir
and never committed. Outputs to ``.experiments/ab_out`` (git-ignored)::

    uv run python scripts/ab_compare.py --photos .experiments/fixtures
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import html
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from sticker_service.config import get_settings
from sticker_service.db.models import SubjectType
from sticker_service.services import cost
from sticker_service.services.canonical import CanonicalEngine, StyleLoader
from sticker_service.services.models.base import ImageModel
from sticker_service.services.models.gemini import _IMAGE_FALLBACKS, IMAGE_MODEL, GeminiImageModel
from sticker_service.services.postprocess import grid_for, process_sheet
from sticker_service.services.stickers import STANDARD_BLOCK
from sticker_service.services.stickers.generate import generate_sheet

logger = logging.getLogger("ab_compare")

PRO = IMAGE_MODEL  # gemini-3-pro-image
FLASH = _IMAGE_FALLBACKS[0]  # gemini-3.1-flash-image
LABEL = {PRO: "pro", FLASH: "flash"}
STYLE_ID = "watercolor"

# (filename, subject_type, child_age) — user-provided adults + children for the
# IMAGE_SAFETY axis (flash tends to refuse children).
FIXTURES: list[tuple[str, SubjectType, int | None]] = [
    ("ab_man.jpg", "adult", None),
    ("ab_woman.jpg", "adult", None),
    ("test_child.jpg", "child", 6),
    ("test_child2.jpg", "child", 3),
]


class ForcedModel(ImageModel):
    """Wrap the real Gemini model, force one image model/resolution, count calls."""

    name = "forced"

    def __init__(
        self, base: GeminiImageModel, *, image_model: str, image_size: str | None = None
    ) -> None:
        self._base = base
        self._image_model = image_model
        self._image_size = image_size
        self.image_calls: list[tuple[str, str]] = []
        self.input_refs = 0
        self.vision_calls = 0

    async def generate(self, prompt: str, refs: Sequence[bytes] = ()) -> bytes:
        self.image_calls.append((LABEL[self._image_model], self._image_size or "1K"))
        self.input_refs += len(refs)
        return await self._base.generate(
            prompt, refs, model=self._image_model, image_size=self._image_size
        )

    async def judge_geometry(self, frame_a: bytes, frame_b: bytes) -> float:
        self.vision_calls += 1
        return await self._base.judge_geometry(frame_a, frame_b)

    async def pick_emoji(self, image: bytes) -> str:
        self.vision_calls += 1
        return await self._base.pick_emoji(image)

    async def ask(self, image: bytes, question: str) -> str:
        self.vision_calls += 1
        return await self._base.ask(image, question)


@dataclass
class Result:
    case: str
    variant: str
    status: str
    seconds: float
    image_path: str | None
    error: str = ""
    cost_row: dict[str, float] = field(default_factory=dict)


def _cost_row(model: ForcedModel) -> dict[str, float]:
    return cost.breakdown(
        image_calls=model.image_calls,
        input_refs=model.input_refs,
        vision_calls=model.vision_calls,
    ).as_row()


async def run_ab1(
    base: GeminiImageModel, loader: StyleLoader, photos: Path, out: Path
) -> tuple[list[Result], bytes | None]:
    """Canonical: pro vs flash on each fixture. Returns (rows, a pro canonical for A/B-2)."""
    style = loader.get(STYLE_ID)
    results: list[Result] = []
    pro_canonical: bytes | None = None
    for fname, subject, age in FIXTURES:
        src = photos / fname
        if not src.exists():
            logger.warning("skip missing fixture %s", fname)
            continue
        photo = src.read_bytes()
        for model_id in (PRO, FLASH):
            label = LABEL[model_id]
            fm = ForcedModel(base, image_model=model_id)
            engine = CanonicalEngine(fm)
            t = time.time()
            try:
                canon = await engine.run(style, photo, subject_type=subject, child_age=age)
                path = out / f"ab1_{src.stem}_{label}.png"
                path.write_bytes(canon)
                if label == "pro" and subject == "adult":
                    pro_canonical = canon
                results.append(
                    Result(src.stem, label, "ok", time.time() - t, path.name, "", _cost_row(fm))
                )
            except Exception as exc:
                results.append(
                    Result(
                        src.stem,
                        label,
                        "FAIL",
                        time.time() - t,
                        None,
                        f"{type(exc).__name__}: {str(exc)[:140]}",
                        _cost_row(fm),
                    )
                )
            logger.info("A/B-1 %s/%s: %s", src.stem, label, results[-1].status)
    return results, pro_canonical


async def run_ab2(
    base: GeminiImageModel, loader: StyleLoader, canonical: bytes, out: Path
) -> list[Result]:
    """Sheet: {pro,flash}×{2K,4K} on a fixed canonical → real slice → 512 montage."""
    from io import BytesIO

    from PIL import Image

    style = loader.get(STYLE_ID)
    captions = list(STANDARD_BLOCK)
    results: list[Result] = []
    for model_id in (PRO, FLASH):
        for size in ("2K", "4K"):
            label = f"{LABEL[model_id]}_{size}"
            fm = ForcedModel(base, image_model=model_id, image_size=size)
            t = time.time()
            try:
                sheet = await generate_sheet(fm, canonical, style, captions, subject_type="adult")
                pieces = process_sheet(sheet, grid=grid_for(len(captions)), expected=len(captions))
                montage = _montage([Image.open(BytesIO(p)) for p in pieces])
                path = out / f"ab2_{label}.png"
                montage.save(path)
                results.append(
                    Result("sheet", label, "ok", time.time() - t, path.name, "", _cost_row(fm))
                )
            except Exception as exc:
                results.append(
                    Result(
                        "sheet",
                        label,
                        "FAIL",
                        time.time() - t,
                        None,
                        f"{type(exc).__name__}: {str(exc)[:140]}",
                        _cost_row(fm),
                    )
                )
            logger.info("A/B-2 %s: %s", label, results[-1].status)
    return results


def _montage(images: list) -> object:
    from PIL import Image

    if not images:
        return Image.new("RGB", (64, 64), (230, 230, 230))
    cell, cols = 200, 5
    rows = (len(images) + cols - 1) // cols
    sheet = Image.new("RGBA", (cols * cell, rows * cell), (230, 230, 230, 255))
    for i, im in enumerate(images):
        im = im.convert("RGBA")
        im.thumbnail((cell - 8, cell - 8))
        sheet.alpha_composite(im, ((i % cols) * cell + 4, (i // cols) * cell + 4))
    return sheet.convert("RGB")


def _img_tag(out: Path, name: str | None, width: int = 220) -> str:
    if not name:
        return "<span style='color:#b00'>—</span>"
    data = base64.b64encode((out / name).read_bytes()).decode()
    return f"<img src='data:image/png;base64,{data}' width='{width}'>"


def _metrics_table(rows: list[Result]) -> str:
    cols = ["case", "variant", "status", "sec", "₽", "$", "error"]
    head = "<tr>" + "".join(f"<th>{c}</th>" for c in cols) + "</tr>"
    body = ""
    for r in rows:
        c = r.cost_row
        body += (
            f"<tr><td>{html.escape(r.case)}</td><td>{html.escape(r.variant)}</td>"
            f"<td>{r.status}</td><td>{r.seconds:.0f}</td>"
            f"<td>{c.get('total_rub', 0)}</td><td>{c.get('total_usd', 0)}</td>"
            f"<td style='color:#b00'>{html.escape(r.error)}</td></tr>"
        )
    return f"<table border=1 cellpadding=4 style='border-collapse:collapse'>{head}{body}</table>"


def build_gallery(out: Path, ab1: list[Result], ab2: list[Result]) -> Path:
    cases = sorted({r.case for r in ab1})
    ab1_rows = ""
    for case in cases:
        cells = ""
        for label in ("pro", "flash"):
            r = next((x for x in ab1 if x.case == case and x.variant == label), None)
            inner = _img_tag(out, r.image_path) if r else "—"
            tag = "" if (r and r.status == "ok") else " <b style='color:#b00'>FAIL</b>"
            cells += f"<td align=center>{label}{tag}<br>{inner}</td>"
        ab1_rows += f"<tr><td><b>{html.escape(case)}</b></td>{cells}</tr>"

    ab2_cells = ""
    for r in ab2:
        inner = _img_tag(out, r.image_path, width=300)
        tag = "" if r.status == "ok" else " <b style='color:#b00'>FAIL</b>"
        ab2_cells += f"<td align=center>{r.variant}{tag}<br>{inner}</td>"

    tbl = "<table border=1 cellpadding=6 style='border-collapse:collapse'>"
    page = (
        "<html><meta charset='utf-8'><body style='font-family:sans-serif'>"
        "<h1>A/B: flash vs pro (HLE-1040)</h1>"
        "<h2>A/B-1 — каноникал (pro vs flash)</h2>"
        f"{tbl}<tr><th>фото</th><th>pro</th><th>flash</th></tr>{ab1_rows}</table>"
        "<h2>A/B-2 — лист {pro,flash}×{2K,4K} → нарезка 512</h2>"
        f"{tbl}<tr>{ab2_cells}</tr></table>"
        "<h2>Метрики и коста</h2>"
        f"{_metrics_table(ab1 + ab2)}"
        "<p><i>Коста — оценка по прайсу HLE-1040 (июнь 2026, ~73.5₽/$). "
        "image-выход + input-референсы + vision-вызовы.</i></p>"
        "</body></html>"
    )
    path = out / "gallery.html"
    path.write_text(page, encoding="utf-8")
    return path


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="A/B flash vs pro harness.")
    p.add_argument("--photos", type=Path, default=Path(".experiments/fixtures"))
    p.add_argument("--out", type=Path, default=Path(".experiments/ab_out"))
    p.add_argument("--skip-ab1", action="store_true", help="only run the sheet A/B-2")
    p.add_argument("--skip-ab2", action="store_true")
    return p.parse_args(argv)


async def _main(args: argparse.Namespace) -> int:
    settings = get_settings()
    args.out.mkdir(parents=True, exist_ok=True)
    base = GeminiImageModel(api_key=settings.gemini_api_key)
    loader = StyleLoader(settings.styles_dir)

    ab1: list[Result] = []
    canonical: bytes | None = None
    if not args.skip_ab1:
        ab1, canonical = await run_ab1(base, loader, args.photos, args.out)
    if canonical is None:
        # fall back to a pre-made canonical for A/B-2 if A/B-1 produced none
        for cand in (
            args.out / "ab1_test_adult_pro.png",
            Path(".experiments/out_verify/watercolor.png"),
        ):
            if cand.exists():
                canonical = cand.read_bytes()
                break

    ab2: list[Result] = []
    if not args.skip_ab2 and canonical is not None:
        ab2 = await run_ab2(base, loader, canonical, args.out)

    page = build_gallery(args.out, ab1, ab2)
    logger.info("done. gallery: %s", page)
    print(f"GALLERY {page}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    return asyncio.run(_main(_parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
