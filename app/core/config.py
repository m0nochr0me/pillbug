"""
Config Maker
"""

# pyright: basic

import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.schema.log_entry import LogEntry

__all__ = ("settings",)


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


class Settings(BaseSettings):

    DEBUG: bool = False

    REDIS_HOST: str | None = None
    REDIS_PORT: int = 6379
    REDIS_PASSWORD: str | None = None
    REDIS_DB: int = 7

    CACHE_TTL: int = 7200  # 2 hours

    BASE_DIR: Path = Path.home() / ".pillbug"

    LOG_DIR: Path = BASE_DIR / "logs"
    SESSIONS_DIR: Path = BASE_DIR / "sessions"
    TASKS_DIR: Path = BASE_DIR / "tasks"
    TASKS_STORE_PATH: Path = TASKS_DIR / "agent_tasks.json"
    SECURITY_PATTERNS_PATH: Path = BASE_DIR / "security_patterns.json"

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

    DOCKET_NAME: str = "pillbug"
    DOCKET_URL: str | None = None
    DOCKET_WORKER_CONCURRENCY: int = 3
    DOCKET_REDELIVERY_TIMEOUT_SECONDS: float = 300.0
    DOCKET_EXECUTION_TTL_SECONDS: float = 900.0

    ENABLED_CHANNELS: str = "cli"
    CHANNEL_PLUGIN_FACTORIES: str = ""
    INBOUND_DEBOUNCE_SECONDS: float = 1.5
    INBOUND_MAX_MESSAGE_CHARS: int = 4000

    WORKSPACE_ROOT: Path = BASE_DIR / "workspace"

    TIMEZONE: str = "UTC"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="PB_",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    def enabled_channels(self) -> tuple[str, ...]:
        enabled_channels = _split_csv(self.ENABLED_CHANNELS)
        return enabled_channels or ("cli",)

    def channel_plugin_factories(self) -> dict[str, str]:
        mappings: dict[str, str] = {}

        for raw_mapping in _split_csv(self.CHANNEL_PLUGIN_FACTORIES):
            channel_name, separator, import_path = raw_mapping.partition("=")
            if not separator or not channel_name.strip() or not import_path.strip():
                raise ValueError(
                    "PB_CHANNEL_PLUGIN_FACTORIES entries must use the format channel=package.module:factory"
                )

            mappings[channel_name.strip()] = import_path.strip()

        return mappings

    def docket_url(self) -> str:
        if self.DOCKET_URL:
            return self.DOCKET_URL

        if not self.REDIS_HOST:
            return "memory://"

        auth = ""
        if self.REDIS_PASSWORD:
            auth = f":{quote(self.REDIS_PASSWORD, safe='')}@"

        return f"redis://{auth}{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"


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
