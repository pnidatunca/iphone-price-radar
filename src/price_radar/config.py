"""Settings: non-secret values from config/settings.yaml, secrets from environment variables."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel

from price_radar.ingest.http_client import CrawlPolicy

REPO_ROOT = Path(__file__).resolve().parents[2]
SETTINGS_PATH = REPO_ROOT / "config" / "settings.yaml"
ENV_FILE = REPO_ROOT / ".env"


class CrawlerSettings(BaseModel):
    user_agent: str
    delay_seconds: tuple[float, float] = (2.0, 3.5)
    max_requests_per_source: int = 1500
    max_consecutive_errors: int = 10
    max_retry_after_seconds: float = 60.0
    timeout_seconds: float = 25.0

    def policy(self) -> CrawlPolicy:
        return CrawlPolicy(
            user_agent=self.user_agent,
            delay_seconds=self.delay_seconds,
            max_requests=self.max_requests_per_source,
            max_consecutive_errors=self.max_consecutive_errors,
            max_retry_after_seconds=self.max_retry_after_seconds,
            timeout_seconds=self.timeout_seconds,
        )


class ScopeSettings(BaseModel):
    excluded_models: list[str] = []

    def includes(self, model_name: str) -> bool:
        """True if a canonical model name such as "iPhone 13 Pro Max" is in scope (case-insensitive)."""
        excluded = {name.casefold() for name in self.excluded_models}
        return model_name.strip().casefold() not in excluded


class SourceSettings(BaseModel):
    enabled: bool = True
    base_url: str


class Settings(BaseModel):
    crawler: CrawlerSettings
    scope: ScopeSettings = ScopeSettings()
    sources: dict[str, SourceSettings]
    retention: dict[str, int] = {}
    model: dict[str, float | int | str] = {}


def load_settings(path: Path = SETTINGS_PATH) -> Settings:
    with path.open(encoding="utf-8") as f:
        return Settings.model_validate(yaml.safe_load(f))


def secret(name: str, env_file: Path = ENV_FILE) -> str | None:
    """A secret from the environment (a GitHub Actions secret). For local runs a ``NAME=value`` line in the
    repository's .env file is the fallback. Secrets never come from the settings file."""
    value = os.environ.get(name)
    if value:
        return value
    if not env_file.exists():
        return None
    for raw in env_file.read_text(encoding="utf-8-sig").splitlines():
        key, sep, rest = raw.strip().partition("=")
        if sep and key.strip() == name:
            return rest.strip().strip("\"'") or None
    return None


def database_url(env_file: Path = ENV_FILE) -> str | None:
    """Neon connection string (``NEON_DATABASE_URL``), see ``secret``."""
    return secret("NEON_DATABASE_URL", env_file)
