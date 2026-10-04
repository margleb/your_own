"""Only PASTORAL_* environment variables; no personal .env or settings store."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class BotSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PASTORAL_", extra="ignore")

    telegram_token: SecretStr = SecretStr("")
    openrouter_api_key: SecretStr = SecretStr("")
    database_url: str = Field(default="", repr=False)
    budget_database_url: str = Field(default="", repr=False)
    state_dir: Path = Path("/var/lib/pastoral-bot")
    model: str = "google/gemini-2.5-flash"
    daily_answer_limit: int = Field(default=10, ge=1)
    daily_budget_usd: Decimal = Field(default=Decimal("5"), gt=0)
    concurrency: int = Field(default=4, ge=1, le=32)
    max_pending_jobs: int = Field(default=256, ge=4, le=4096)
    temporary_idle_seconds: int = Field(default=1800, ge=1)
    temporary_max_seconds: int = Field(default=7200, ge=1)
    max_message_chars: int = Field(default=6000, ge=1, le=12000)
    max_output_tokens: int = Field(default=1200, ge=128, le=4096)
    max_input_tokens: int = Field(default=24000, ge=4096)
    input_price_per_million: Decimal = Field(default=Decimal("0.30"), gt=0)
    output_price_per_million: Decimal = Field(default=Decimal("2.50"), gt=0)
    request_timeout_seconds: float = Field(default=90, gt=0)
    campaigns: str = ""
    backup_key: SecretStr = SecretStr("")
    backup_retention_days: int = Field(default=7, ge=1, le=7)
    web_enabled: bool = False
    web_host: str = "0.0.0.0"
    web_port: int = Field(default=8091, ge=1, le=65535)
    web_public_url: str = "https://bot-ams.margleb.ru/pastoral/"
    web_auth_max_age_seconds: int = Field(default=7200, ge=60, le=7200)

    @field_validator("database_url", "budget_database_url")
    @classmethod
    def isolated_database(cls, value: str) -> str:
        if value and not value.startswith("postgresql+asyncpg://"):
            raise ValueError("PASTORAL_DATABASE_URL must use postgresql+asyncpg")
        if value and value.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1] == "your_own":
            raise ValueError("Use a separate pastoral database, not your_own")
        return value

    @model_validator(mode="after")
    def temporary_bounds(self):
        if self.temporary_idle_seconds > self.temporary_max_seconds:
            raise ValueError("Temporary idle lifetime must not exceed maximum lifetime")
        if self.web_enabled:
            public = urlsplit(self.web_public_url)
            if public.scheme != "https" or not public.hostname or public.username or public.password or public.query or public.fragment or not public.path.endswith("/"):
                raise ValueError("PASTORAL_WEB_PUBLIC_URL must be an HTTPS URL ending in / without credentials, query or fragment")
        return self

    @property
    def campaign_ids(self) -> set[str]:
        return {x.strip() for x in self.campaigns.split(",") if x.strip()}

    def require_runtime(self) -> None:
        if not self.database_url or not self.telegram_token.get_secret_value() or not self.openrouter_api_key.get_secret_value():
            raise ValueError("Set PASTORAL_DATABASE_URL, PASTORAL_TELEGRAM_TOKEN and PASTORAL_OPENROUTER_API_KEY")
