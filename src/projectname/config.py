"""Application settings.

Single, typed source of runtime configuration. Reads from environment
variables and an optional local ``.env`` file. Secrets must never be
committed — keep them in ``.env`` (git-ignored) and document keys in
``.env.example``.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    sentry_dsn: str = ""
    sentry_environment: str = "development"
    sentry_release: str = ""
    sentry_traces_sample_rate: float = 0.0


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so the environment is parsed once. In tests, call
    ``get_settings.cache_clear()`` after mutating the environment.
    """
    return Settings()
