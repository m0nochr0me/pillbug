"""Configuration for the Gemini-to-OpenAI proxy."""

import sys
from datetime import datetime
from typing import Self

from pydantic import SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.schema.log_entry import LogEntry

__all__ = ("ProxySettings", "settings")


class ProxySettings(BaseSettings):
    HOST: str = "127.0.0.1"
    PORT: int = 9031

    UPSTREAM_URL: str = ""
    UPSTREAM_API_KEY: SecretStr | None = None
    UPSTREAM_MODEL: str | None = None

    REQUEST_TIMEOUT_SECONDS: float = 600.0
    UPSTREAM_VERIFY_TLS: bool = True

    LOG_INCLUDE_TRACEBACK: bool = False

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="PB_GENAI_PROXY_",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    @field_validator("UPSTREAM_URL", mode="before")
    @classmethod
    def normalize_upstream_url(cls, value: str | None) -> str:
        if value is None:
            return ""
        normalized = str(value).strip().rstrip("/")
        return normalized

    @field_validator("REQUEST_TIMEOUT_SECONDS")
    @classmethod
    def validate_timeout(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("PB_GENAI_PROXY_REQUEST_TIMEOUT_SECONDS must be greater than 0")
        return value

    @model_validator(mode="after")
    def require_upstream(self) -> Self:
        if not self.UPSTREAM_URL:
            raise ValueError("PB_GENAI_PROXY_UPSTREAM_URL is required")
        return self

    def upstream_api_key(self) -> str | None:
        if self.UPSTREAM_API_KEY is None:
            return None
        return self.UPSTREAM_API_KEY.get_secret_value()


try:
    settings = ProxySettings()  # type: ignore[call-arg]
except ValidationError as e:
    print(
        LogEntry(
            asctime=datetime.now(),
            levelname="CRITICAL",
            message=f"{type(e).__name__}: {e}",
        ).model_dump_json()
    )
    sys.exit(1)
