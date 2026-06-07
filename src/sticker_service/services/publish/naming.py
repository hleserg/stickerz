"""Machine set-name generation for Telegram sticker sets (§3.3).

The user types a human name (cyrillic/emoji ok) → that becomes the visible
*title*. The machine id is generated silently: ``<translit>_<rand4-6>_by_<bot>``,
matching Telegram's ``[a-z0-9_]`` + ``_by_<botusername>`` requirement. A random
suffix makes collisions vanishingly unlikely; we never pre-check availability
(§3.3) — a single occupied-retry covers the race reactively.
"""

from __future__ import annotations

import random
import re
import string

# Practical lowercase transliteration of Russian for a readable URL slug.
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "",
    "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}  # fmt: skip


def transliterate(text: str) -> str:
    """Lowercase RU→latin slug; keeps [a-z0-9], collapses the rest."""
    out: list[str] = []
    for ch in text.lower():
        if ch in _TRANSLIT:
            out.append(_TRANSLIT[ch])
        elif ch.isascii() and ch.isalnum():
            out.append(ch)
    return "".join(out)


def build_set_name(
    title: str,
    bot_username: str,
    *,
    rng: random.Random | None = None,
    max_base: int = 20,
) -> str:
    """Generate a Telegram-valid machine set name ending in ``_by_<bot>``."""
    # Suffix is for collision avoidance, not security/crypto.
    rng = rng or random.Random()  # nosec B311
    base = transliterate(title)[:max_base]
    if not base or not base[0].isalpha():
        base = "s" + base
    length = rng.randint(4, 6)
    alphabet = string.ascii_lowercase + string.digits
    suffix = "".join(rng.choice(alphabet) for _ in range(length))
    name = f"{base}_{suffix}_by_{bot_username.lower()}"
    # Defensive: enforce the allowed charset.
    if not re.fullmatch(r"[a-z][a-z0-9_]*", name):  # pragma: no cover - belt and suspenders
        raise ValueError(f"generated invalid set name: {name!r}")
    return name


def sticker_set_link(set_name: str) -> str:
    """Public add link for a published set."""
    return f"https://t.me/addstickers/{set_name}"
