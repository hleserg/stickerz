"""Application settings.

Single, typed source of runtime configuration. Reads from environment
variables and an optional local ``.env`` file. Secrets must never be
committed — keep them in ``.env`` (git-ignored) and document keys in
``.env.example``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Package directory (…/sticker_service). Style plugins ship inside the package,
# so resolve them relative to it — works both from source and an installed wheel.
_PACKAGE_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    """Runtime configuration loaded from the environment.

    # PLAYBOOK-START
    # id: typed-settings-singleton
    # title: Typed settings as a cached singleton
    # status: refined
    # category: configuration
    # tags: [pydantic, config, 12factor]
    # Centralize all env access in one typed object resolved once via an
    # lru_cache'd accessor. Code never reads os.environ directly; tests
    # override by clearing the cache. Substitution test passes: useful in
    # any 12-factor service regardless of domain.
    # PLAYBOOK-END
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="APP_",
        extra="ignore",
    )

    environment: str = "development"
    debug: bool = False

    # --- Secrets (accept both APP_-prefixed and the bare repo-secret name) ---
    # The repo stores these as BOT_TOKEN / GEMINI_KEY / GPT_KEY; AliasChoices
    # lets the same field resolve from either spelling without code changes.
    bot_token: str = Field(
        default="",
        validation_alias=AliasChoices("APP_BOT_TOKEN", "BOT_TOKEN"),
    )
    gemini_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("APP_GEMINI_API_KEY", "GEMINI_KEY", "GEMINI_API_KEY"),
    )
    openai_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("APP_OPENAI_API_KEY", "GPT_KEY", "OPENAI_API_KEY"),
    )

    # --- Model generation (§8, §15.1: both paths live behind config) ---
    # Provider for the canonical/sticker generation pipeline.
    model_provider: str = "gemini"  # gemini | gpt | mock
    # Default face-geometry gate between pipeline steps (§4.3). YAML per-step
    # value overrides this; never hardcode the gate in code (invariant §B.4).
    default_gate: str = "vision_judge"  # vision_judge | face_geometry | none
    # Outbound proxy for reaching Gemini/GPT from RU (§10). Empty = direct.
    models_proxy_url: str = ""

    # --- Access control (§11.1) ---
    # Comma-separated Telegram user_ids that may administer the whitelist.
    admin_ids: str = ""

    # --- Paths ---
    # Runtime data (sqlite, photos, generated stickers): relative to CWD so it
    # lands in the mounted ./data volume in Docker (WORKDIR=/app -> /app/data).
    data_dir: Path = Path("data")
    # Style plugins ship inside the package.
    styles_dir: Path = _PACKAGE_DIR / "services" / "canonical" / "styles"
    redis_url: str = "redis://localhost:6379/0"

    # --- Watermark (virality; off-switch for B2B) ---
    watermark_enabled: bool = True
    watermark_text: str = "@yuki_stickers_bot"

    # --- Sentry (disabled by default; empty DSN is a no-op) ---
    sentry_dsn: str = ""
    sentry_environment: str = "development"
    sentry_release: str = ""
    sentry_traces_sample_rate: float = 0.0

    @property
    def admin_id_list(self) -> list[int]:
        """Parse ``admin_ids`` into an ordered list of integers (ignores blanks)."""
        out: list[int] = []
        for chunk in self.admin_ids.split(","):
            chunk = chunk.strip()
            if chunk:
                out.append(int(chunk))
        return out

    @property
    def admin_id_set(self) -> frozenset[int]:
        """Admin ids as a set (membership checks)."""
        return frozenset(self.admin_id_list)

    @property
    def first_admin_id(self) -> int | None:
        """The first admin id (owner): receives reports/errors, switches mode."""
        ids = self.admin_id_list
        return ids[0] if ids else None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so the environment is parsed once. In tests, call
    ``get_settings.cache_clear()`` after mutating the environment.
    """
    return Settings()
