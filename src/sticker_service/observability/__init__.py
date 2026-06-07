"""Observability: Sentry initialization and component tagging."""

from sticker_service.observability.sentry import init_sentry, tag_component

__all__ = ["init_sentry", "tag_component"]
