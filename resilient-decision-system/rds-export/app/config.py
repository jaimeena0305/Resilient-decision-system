"""
app/config.py  —  Application settings (Pydantic-Settings powered).
"""

from __future__ import annotations
from functools import lru_cache
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    All configuration is read from environment variables (or a .env file).
    Sensible defaults allow the app to boot with zero configuration.
    """
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ── Database ──────────────────────────────────────────────────────────
    database_url: str = "sqlite:///./decisions.db"

    # ── Redis ─────────────────────────────────────────────────────────────
    redis_url: Optional[str] = None   # e.g. "redis://localhost:6379/0"
    idempotency_ttl_seconds: int = 86_400

    # ── App ───────────────────────────────────────────────────────────────
    app_name:    str = "Resilient Decision System"
    app_version: str = "1.0.0"
    debug:       bool = False

    # ── Logging ───────────────────────────────────────────────────────────
    log_level: str = "INFO"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings instance — call this everywhere instead of Settings()."""
    return Settings()
