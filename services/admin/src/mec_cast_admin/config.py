"""Runtime configuration, read from the environment or a local .env file."""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Service settings. Every field maps to a ``MECADM_``-prefixed env var."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="MECADM_",
        extra="ignore",
    )

    # Where run manifests live. One directory per run, matching what
    # scripts/run-experiment.sh already writes.
    runs_dir: str = "runs"

    # Optional: post admin lifecycle events here so operator actions land on
    # the same timeline as the measurements. Empty disables it.
    logging_url: str = ""

    # Keep-alive. These numbers are also compiled into both node clients, so
    # they are published in the welcome frame rather than assumed.
    keepalive_s: float = Field(default=10.0, gt=0)
    offline_timeout_s: float = Field(default=30.0, gt=0)

    # How long a run may sit in `starting` before it is declared failed.
    start_timeout_s: float = Field(default=30.0, gt=0)

    # Cadence of the derived-diagnostics pass and of UI snapshot broadcasts.
    diagnostics_interval_s: float = Field(default=5.0, gt=0)
    ui_broadcast_min_interval_s: float = Field(default=1.0, gt=0)

    api_prefix: str = "/api/v1"
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, loaded once."""
    return Settings()
