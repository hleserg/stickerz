"""Ingest user-uploaded stickers: a sheet picture or a ZIP of ready images.

The /upload flow (owner's spec, 13.06) lets a tester publish stickers they
already have. Every input is still gated by code: vision confirms the picture
LOOKS like a sticker sheet before slicing, the slicer's honesty criterion
refuses busy backgrounds, and ZIP contents are validated image by image.
"""

from __future__ import annotations

import io
import logging
import zipfile
from typing import TYPE_CHECKING

from PIL import Image

from sticker_service.services.postprocess import encode_sticker, fit_to_512

if TYPE_CHECKING:
    from sticker_service.services.models.base import ImageModel

logger = logging.getLogger(__name__)

MAX_ZIP_BYTES = 20 * 1024 * 1024
MAX_ZIP_FILES = 120  # a Telegram set's hard cap
# Honest PNG/WebP content can't inflate a 20 MB zip far past this; a crafted
# bomb can — reject by the declared sizes before reading a single entry.
MAX_UNPACKED_BYTES = 80 * 1024 * 1024

_SHEET_QUESTION = (
    "На картинке лист (сетка) стикеров — один или несколько отдельных рисунков "
    "на общем ровном фоне или с прозрачностью? Ответь ровно: ДА или НЕТ"
)


async def looks_like_sticker_sheet(model: ImageModel, image: bytes) -> bool | None:
    """Vision opinion on «is this a sticker sheet?»; ``None`` = unavailable.

    Fails OPEN (None): the slicer's own honesty criterion is the real gate —
    this check only saves a doomed slicing attempt and gives a clearer refusal.
    """
    try:
        answer = (await model.ask(image, _SHEET_QUESTION) or "").strip().casefold()
    except Exception:
        logger.warning("upload sheet check failed; deferring to the slicer", exc_info=True)
        return None
    if not answer:
        return None
    return answer.startswith(("да", "yes"))


class ZipRejectedError(ValueError):
    """The archive can't become stickers; ``str()`` is a user-facing RU reason."""


def extract_zip_stickers(archive: bytes) -> list[bytes]:
    """Validated stickers from a user ZIP: every entry an image → 512px PNG.

    Oversized archives, zip bombs, non-image entries and empty archives are
    rejected with a user-facing reason. Sticker order follows the archive.
    """
    if len(archive) > MAX_ZIP_BYTES:
        raise ZipRejectedError("Архив больше 20 МБ — пришли поменьше.")
    try:
        bundle = zipfile.ZipFile(io.BytesIO(archive))
    except zipfile.BadZipFile as exc:
        raise ZipRejectedError("Не смог открыть архив — нужен обычный ZIP.") from exc
    entries = [
        info
        for info in bundle.infolist()
        if not info.is_dir() and not info.filename.startswith(("__MACOSX/", "."))
    ]
    if not entries:
        raise ZipRejectedError("Архив пустой — в нём нет картинок.")
    if len(entries) > MAX_ZIP_FILES:
        raise ZipRejectedError(
            f"В архиве больше {MAX_ZIP_FILES} файлов — столько в один пак не влезает."
        )
    if sum(info.file_size for info in entries) > MAX_UNPACKED_BYTES:
        raise ZipRejectedError("Архив распаковывается слишком большим — пришли поменьше.")
    stickers: list[bytes] = []
    for info in entries:
        data = bundle.read(info)
        try:
            image = Image.open(io.BytesIO(data))
            image.load()
        except Exception as exc:
            raise ZipRejectedError(
                f"«{info.filename}» не похож на картинку — в архиве должны быть "
                "только PNG/WebP/JPEG."
            ) from exc
        stickers.append(encode_sticker(fit_to_512(image)))
    return stickers
