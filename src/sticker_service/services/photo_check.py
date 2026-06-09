"""Foolproof photo validation via a single vision call (on upload).

Asks the model four yes/no questions in one shot and classifies the first
problem. Lenient: only an explicit negative/positive triggers a rejection, so an
ambiguous answer passes. Returns a problem code (or ``None`` if the photo is OK);
the flow maps codes to specific hints and strikes nudity.
"""

from __future__ import annotations

import re

from sticker_service.services.models.base import ImageModel

PROMPT = (
    "Посмотри на фото и ответь СТРОГО одной строкой флагами через запятую: "
    "person=<yes/no>, big=<yes/no>, nude=<yes/no>, single=<yes/no>. "
    "person — есть ли на фото человек. big — человек занимает не меньше 1/5 кадра. "
    "nude — есть ли нагота или обнажённые интимные части тела: оголённая грудь или "
    "соски, гениталии, голые ягодицы, либо человек в нижнем белье или откровенно "
    "сексуальном виде (частичная нагота тоже считается nude=yes). "
    "single — на фото ровно один человек."
)

# Problem codes (priority order).
NUDE = "NUDE"
NO_PERSON = "NO_PERSON"
MULTI = "MULTI"
SMALL = "SMALL"


def _flag(answer: str, name: str) -> bool | None:
    """Parse ``name=yes/no/да/нет`` → True/False, or None if absent."""
    match = re.search(rf"{name}\s*[=:]\s*(yes|no|да|нет|true|false)", answer)
    if not match:
        return None
    return match.group(1) in ("yes", "да", "true")


def classify(answer: str) -> str | None:
    """Return the first photo problem code from a vision answer, or None."""
    text = answer.lower()
    if _flag(text, "nude") is True:
        return NUDE
    if _flag(text, "person") is False:
        return NO_PERSON
    if _flag(text, "single") is False:
        return MULTI
    if _flag(text, "big") is False:
        return SMALL
    return None


async def validate_photo(model: ImageModel, image: bytes) -> str | None:
    """Run the vision check; return a problem code or None if the photo is OK."""
    answer = await model.ask(image, PROMPT)
    return classify(answer)
