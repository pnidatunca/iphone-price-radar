"""Settings: non-secret values from config/settings.yaml, secrets from environment variables."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel

from price_radar.ingest.http_client import CrawlPolicy

REPO_ROOT = Path(__file__).resolve().parents[2]
SETTINGS_PATH = REPO_ROOT / "config" / "settings.yaml"
CONTACT_PLACEHOLDER = "CONTACT_EMAIL_PENDING"


class CrawlerSettings(BaseModel):
    user_agent: str
    delay_seconds: tuple[float, float] = (2.0, 3.5)
    max_requests_per_source: int = 1500
    max_consecutive_errors: int = 10
    max_retry_after_seconds: float = 60.0
    timeout_seconds: float = 25.0

    @property
    def has_contact(self) -> bool:
        """False while the contact address in the User-Agent is still the placeholder."""
        return CONTACT_PLACEHOLDER not in self.user_agent

    def policy(self) -> CrawlPolicy:
        return CrawlPolicy(
            user_agent=self.user_agent,
            delay_seconds=self.delay_seconds,
            max_requests=self.max_requests_per_source,
            max_consecutive_errors=self.max_consecutive_errors,
            max_retry_after_seconds=self.max_retry_after_seconds,
            timeout_seconds=self.timeout_seconds,
        )


class SourceSettings(BaseModel):
    enabled: bool = True
    base_url: str


class Settings(BaseModel):
    crawler: CrawlerSettings
    sources: dict[str, SourceSettings]
    retention: dict[str, int] = {}
    model: dict[str, float | int | str] = {}


def load_settings(path: Path = SETTINGS_PATH) -> Settings:
    with path.open(encoding="utf-8") as f:
        return Settings.model_validate(yaml.safe_load(f))


def database_url() -> str | None:
    """Neon connection string; it only ever comes from the environment, never from the settings file."""
    return os.environ.get("NEON_DATABASE_URL") or None
