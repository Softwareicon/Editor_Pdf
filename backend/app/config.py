"""Application settings, loaded from environment variables (prefix PDFMASTER_) or .env."""
from __future__ import annotations

import logging
import secrets
from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]
logger = logging.getLogger("pdfmaster.config")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(REPO_ROOT / ".env"), env_prefix="PDFMASTER_", extra="ignore"
    )

    environment: str = "development"  # development | production | test
    secret_key: str = ""
    database_url: str = f"sqlite:///{(REPO_ROOT / 'storage' / 'pdfmaster.db').as_posix()}"
    storage_dir: Path = REPO_ROOT / "storage"
    frontend_dir: Path = REPO_ROOT / "frontend"
    serve_frontend: bool = True
    cors_origins: str = ""  # comma-separated list of allowed frontend origins

    access_token_minutes: int = 15
    refresh_token_days: int = 14
    cookie_secure: bool = False
    cookie_samesite: str = "strict"  # use "none" (+ cookie_secure) for a cross-site frontend

    registration_enabled: bool = True
    min_password_length: int = 10
    auth_rate_limit_per_minute: int = 10

    max_upload_mb: int = 100
    max_pages: int = 5000
    inspect_timeout_seconds: int = 30
    inspect_memory_limit_mb: int = 1024
    temp_max_age_hours: int = 6

    @model_validator(mode="after")
    def _check_secret(self) -> "Settings":
        if self.environment == "production":
            if len(self.secret_key) < 32:
                raise ValueError("PDFMASTER_SECRET_KEY must be set to at least 32 characters in production")
        elif not self.secret_key:
            # Dev convenience only: sessions are invalidated on every restart.
            self.secret_key = secrets.token_urlsafe(48)
            logger.warning("PDFMASTER_SECRET_KEY not set; using a temporary key for this process")
        if self.cookie_samesite.lower() == "none" and not self.cookie_secure:
            raise ValueError("cookie_samesite=none requires cookie_secure=true")
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip().rstrip("/") for o in self.cors_origins.split(",") if o.strip()]

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def uploads_dir(self) -> Path:
        return self.storage_dir / "uploads"

    @property
    def temp_dir(self) -> Path:
        return self.storage_dir / "temp"


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    for d in (settings.uploads_dir, settings.temp_dir, settings.storage_dir / "processed"):
        d.mkdir(parents=True, exist_ok=True)
    return settings
