"""Application settings.

Single, typed source of runtime configuration. Reads from environment
variables and an optional local ``.env`` file. Secrets must never be
committed — keep them in ``.env`` (git-ignored) and document keys in
``.env.example``.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

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

    # --- Flow resilience (watchdog): per-step timeouts in seconds ---
    # A hung automated step must not leave the user staring at a frozen flow. The
    # photo check is short, so on timeout we just let the photo through; the
    # generation step is long, so it gets a generous cap before we apologize.
    photo_check_timeout_s: float = 60.0
    generation_timeout_s: float = 600.0
    # How many sheets may run CPU/RAM-heavy postprocessing (chroma key + slice)
    # at the same time. One 4K sheet peaks at ~0.6-0.9 GB RSS and ~7 s of pure
    # CPU, so unbounded parallelism OOMs a small VDS; queued sheets just wait a
    # few seconds. See docs/operations/CAPACITY.md.
    postprocess_concurrency: int = 2
    # Max update-handler tasks running at once (aiogram long polling). Bounds
    # the coroutine/memory fan-out under bursts and button spam. 0 = unbounded.
    polling_tasks_limit: int = 64

    # --- Maintenance (bound disk/db growth on the small VDS) ---
    # Unpublished draft packs (created mid-flow, then published or abandoned) and
    # their PNGs are garbage-collected by the maintenance loop when older than
    # this many days. Published packs are never touched. 0 disables the sweep.
    draft_retention_days: int = 30
    # Analytics events older than this are pruned by the maintenance loop
    # (generation_done is always kept — the alpha budget counts it all-time).
    # 0 disables the sweep.
    events_retention_days: int = 180
    # FSM rows untouched for this many days are dropped (flows abandoned
    # mid-wizard; resuming them weeks later is meaningless). 0 keeps them and
    # only drops rows already cleared by state.clear().
    fsm_retention_days: int = 14
    # The housekeeping pass (GC + prune + FSM sweep + disk check) runs at boot
    # and then every this many hours. <=0 restores the old run-once-at-boot mode.
    maintenance_interval_hours: int = 24
    # Alert admins when the data_dir filesystem is at least this % full
    # (checked on every maintenance pass). 0 disables the alert.
    disk_alert_threshold_pct: int = 80

    # --- Meme pool refresh (default-pack ideas follow Runet trends) ---
    # Every this many days the text model rewrites the meme-idea pool (a cheap
    # search-grounded text call — fractions of a cent weekly). 0 disables the
    # refresh; the bundled pool then stays active forever.
    meme_refresh_days: int = 7

    # --- Watermark (virality; off-switch for B2B) ---
    watermark_enabled: bool = True
    watermark_text: str = "@yuki_stickers_bot"

    # --- Showcase (the Telegraph demo page surfaced to newcomers) ---
    # Shown as a url-button on /start and the alpha-application screen, and as
    # a line in /help. Empty string hides it everywhere.
    demo_page_url: str = "https://telegra.ph/Yuki--stikerpak-iz-odnogo-foto-06-10"

    # --- Sentry (disabled by default; empty DSN is a no-op) ---
    sentry_dsn: str = ""
    sentry_environment: str = "development"
    sentry_release: str = ""
    sentry_traces_sample_rate: float = 0.0

    @property
    def admin_id_list(self) -> list[int]:
        """Parse ``admin_ids`` into an ordered list of integers (ignores blanks).

        A non-integer token is skipped (with a warning) rather than raised, so a
        typo in ``APP_ADMIN_IDS`` can't hard-fail every admin/access check.
        """
        out: list[int] = []
        for chunk in self.admin_ids.split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                out.append(int(chunk))
            except ValueError:
                logger.warning("ignoring non-integer admin id %r in APP_ADMIN_IDS", chunk)
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
