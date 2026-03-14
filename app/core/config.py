"""
Config Maker
"""

# pyright: basic

import sys
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.schema.log_entry import LogEntry

__all__ = ("settings",)


class Settings(BaseSettings):

    DEBUG: bool = False

    REDIS_HOST: str | None = None
    REDIS_PORT: int = 6379
    REDIS_PASSWORD: str | None = None
    REDIS_DB: int = 7

    CACHE_TTL: int = 7200  # 2 hours

    LOG_DIR: Path = Path.home() / ".pillbug/logs"

    GEMINI_MODEL: str = "gemini-3.1-pro-preview"
    GEMINI_TEMPERATURE: float = 1.0
    GEMINI_TOP_P: float = 0.6
    GEMINI_MAX_OUTPUT_TOKENS: int = 16536
    GEMINI_THINKING_LEVEL: str = "high"
    GEMINI_API_KEY: str

    MCP_HOST: str = "127.0.0.1"
    MCP_PORT: int = 8000

    MCP_DEFAULT_PAGE_SIZE: int = 200
    MCP_MAX_PAGE_SIZE: int = 1000
    MCP_MAX_SEARCH_RESULTS: int = 200
    MCP_DEFAULT_COMMAND_TIMEOUT_SECONDS: float = 30.0
    MCP_MAX_COMMAND_TIMEOUT_SECONDS: float = 300.0
    MCP_MAX_COMMAND_OUTPUT_CHARS: int = 20000

    WORKSPACE_ROOT: Path = Path.home() / ".pillbug/workspace"

    TIMEZONE: str = "UTC"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="PB_",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )


try:
    settings = Settings()  # type: ignore[call-arg]
except ValidationError as e:
    print(
        LogEntry(
            asctime=datetime.now(),
            levelname="CRITICAL",
            message=f"{type(e).__name__}: {e}",
        ).model_dump_json()
    )
    sys.exit(1)
