"""One place to classify model errors → retry policy + user-facing messages (§8).

Keeps the "is this worth retrying / is the account out of money / what do we tell
the user" decision in a single module instead of scattering string matching
across the Gemini adapter and the handlers. Categories:

- ``REFUSAL``   — safety/policy refusal; reformulate, don't blindly retry;
- ``QUOTA``     — account out of credits/quota; permanent until topped up;
- ``TRANSIENT`` — overload / rate limit (503/500/429); worth a retry;
- ``NETWORK``   — proxy/DNS/TLS/timeout; check connectivity;
- ``UNKNOWN``   — everything else (surface the raw text).
"""

from __future__ import annotations

from sticker_service.services.models.base import ModelQuotaError, ModelRefusalError

REFUSAL = "refusal"
QUOTA = "quota"
TRANSIENT = "transient"
NETWORK = "network"
UNKNOWN = "unknown"

# Order of checks matters: a depleted-credits 429 must read as QUOTA, not TRANSIENT.
_QUOTA = ("credits are depleted", "prepayment", "out of credits", "billing", "insufficient")
_REFUSAL = ("safety", "prohibited", "recitation", "refus")
_TRANSIENT = ("503", "500", "unavailable", "internal", "overload", "high demand", "429")
_NETWORK = ("proxy", "connect", "timeout", "resolve", "ssl", "network", "getaddrinfo")

_USER_MESSAGES = {
    QUOTA: (
        "⚠️ Генерация временно недоступна (исчерпан лимит у провайдера). "
        "Мы уже разбираемся — попробуй позже."
    ),
    REFUSAL: "⚠️ Модель отклонила генерацию (фильтр). Попробуй другое фото или возраст.",
    TRANSIENT: "⚠️ Модель сейчас перегружена. Попробуй ещё раз через минуту.",
    NETWORK: "⚠️ Нет доступа к модели (сеть/прокси). Проверь APP_MODELS_PROXY_URL и логи.",
}


def classify(exc: Exception) -> str:
    """Map an exception to one of the category constants above."""
    if isinstance(exc, ModelQuotaError):
        return QUOTA
    if isinstance(exc, ModelRefusalError):
        return REFUSAL
    s = str(exc).lower()
    if any(t in s for t in _QUOTA):
        return QUOTA
    if any(t in s for t in _REFUSAL):
        return REFUSAL
    if any(t in s for t in _TRANSIENT):
        return TRANSIENT
    if any(t in s for t in _NETWORK):
        return NETWORK
    return UNKNOWN


def user_message(exc: Exception) -> str:
    """A short, friendly RU message for the given error (raw text if unknown)."""
    kind = classify(exc)
    return _USER_MESSAGES.get(kind, f"⚠️ Не получилось: {exc}")


def is_retryable(exc: Exception) -> bool:
    """True only for transient overload/rate-limit errors worth retrying."""
    return classify(exc) == TRANSIENT


def is_quota(exc: Exception) -> bool:
    """True when the account is out of credits/quota (permanent until topped up)."""
    return classify(exc) == QUOTA
