"""Reject obscene / profane / gory captions BEFORE they reach any model (§safety).

A short local blocklist with light de-obfuscation. Two normalized forms are
checked: a latin form (for English stems) and a cyrillic form with latin→cyrillic
homoglyph folding (for Russian stems). Deliberately conservative so nothing
inappropriate is ever sent to the image model or published to Telegram. Captions
are short, so stem matching on the normalized form catches the obvious cases.
"""

from __future__ import annotations

import re

# Latin/digit glyphs commonly used to spoof Cyrillic letters.
_HOMOGLYPHS = str.maketrans(
    {
        "a": "а", "b": "в", "c": "с", "e": "е", "h": "н", "k": "к", "m": "м",
        "o": "о", "p": "р", "t": "т", "x": "х", "y": "у", "3": "з", "0": "о",
        "1": "и", "@": "а", "$": "с",
    }
)  # fmt: skip

# Russian stems (matched on the cyrillic-folded form). Specific enough to avoid
# common-word collisions (хлеб / корабль / голова / голос / голод must pass).
_RU_STEMS: tuple[str, ...] = (
    # profanity
    "хуй",
    "хуё",
    "хуе",
    "хуи",
    "пизд",
    "ебал",
    "ебан",
    "ебат",
    "ёбан",
    "еблан",
    "выеб",
    "заеб",
    "наеб",
    "уеба",
    "поеб",
    "хуес",
    "бляд",
    "блят",
    "сука",
    "суки",
    "суке",
    "мудак",
    "мудач",
    "залуп",
    "пидор",
    "пидар",
    "пидр",
    "гандон",
    "манда",
    "дроч",
    "шлюх",
    "говно",
    "говн",
    "сран",
    "насрал",
    "обосра",
    "долбоёб",
    "долбоеб",
    # nudity / sexual (enumerated «голый» forms — not bare «голо»)
    "голая",
    "голое",
    "голые",
    "голый",
    "голую",
    "голым",
    "голых",
    "голой",
    "голого",
    "обнаж",
    "секс",
    "порн",
    "эроти",
    "член",
    "сиськ",
    "сись",
    "титьк",
    "сосок",
    "анал",
    "минет",
    "оргазм",
    "вагин",
    "пенис",
    # gore / violence
    "кровь",
    "кровав",
    "кровищ",
    "расчлен",
    "труп",
    "кишк",
    "зарез",
    "насил",
    "убийств",
)

# English stems (matched on the latin form).
_EN_STEMS: tuple[str, ...] = (
    "fuck",
    "shit",
    "bitch",
    "cunt",
    "dick",
    "pussy",
    "porn",
    "nude",
    "naked",
    "sex",
    "boob",
    "tits",
    "vagina",
    "penis",
    "blood",
    "gore",
    "nigg",
    "nazi",
)


def _collapse(text: str) -> str:
    return re.sub(r"(.)\1{2,}", r"\1", text)  # collapse 3+ repeats


def _latin_form(text: str) -> str:
    return _collapse(re.sub(r"[^a-z]", "", text.lower()))


def _cyrillic_form(text: str) -> str:
    return _collapse(re.sub(r"[^а-яё]", "", text.lower().translate(_HOMOGLYPHS)))


def caption_rejection_reason(text: str) -> str | None:
    """Return a user-facing reason if the caption is disallowed, else ``None``."""
    reason = "мат, непристойность или жестокость недопустимы"
    cyr = _cyrillic_form(text)
    if any(stem in cyr for stem in _RU_STEMS):
        return reason
    lat = _latin_form(text)
    if any(stem in lat for stem in _EN_STEMS):
        return reason
    return None


def is_clean(text: str) -> bool:
    """True if the caption passes moderation."""
    return caption_rejection_reason(text) is None
