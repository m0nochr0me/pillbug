"""
Config Maker
"""

# pyright: basic

import re
import sys
from datetime import datetime
from functools import cached_property
from pathlib import Path
from typing import Self
from urllib.parse import quote
from uuid import uuid4

from pydantic import SecretStr, ValidationError, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.schema.log_entry import LogEntry

__all__ = ("settings",)


_RUNTIME_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$")
_MIN_BEARER_TOKEN_LENGTH = 16


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _validate_runtime_identifier(value: str, *, source: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{source} must not be empty")

    if not _RUNTIME_ID_PATTERN.fullmatch(normalized):
        raise ValueError(
            f"{source} must start with an alphanumeric character and only contain letters, numbers, '.', '_' or '-'"
        )

    return normalized


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
    WORKSPACE_ROOT: Path = BASE_DIR / "workspace"

    RUNTIME_ID: str | None = None
    RUNTIME_ID_PATH: Path = BASE_DIR / "runtime_id.txt"
    AGENT_NAME: str | None = None
    DASHBOARD_BEARER_TOKEN: SecretStr | None = None

    A2A_BEARER_TOKEN: SecretStr | None = None
    A2A_SELF_BASE_URL: str | None = None
    A2A_INGRESS_PATH: str = "/a2a/messages"
    A2A_OUTBOUND_TIMEOUT_SECONDS: float = 15.0
    A2A_CONVERGENCE_MAX_HOPS: int = 2
    A2A_AGENT_DESCRIPTION: str | None = None
    A2A_PROVIDER_ORGANIZATION: str = "Pillbug"
    A2A_PROVIDER_URL: str | None = None
    A2A_DOCUMENTATION_URL: str | None = None
    A2A_ICON_URL: str | None = None

    GEMINI_MODEL: str = "gemini-3.1-pro-preview"
    GEMINI_TEMPERATURE: float = 1.0
    GEMINI_TOP_P: float = 0.6
    GEMINI_MAX_OUTPUT_TOKENS: int = 16536
    GEMINI_THINKING_LEVEL: str = "high"
    GEMINI_API_KEY: str

    MCP_HOST: str = "127.0.0.1"
    MCP_PORT: int = 8000
    MCP_SHORTENER_BASE_URL: str | None = None
    MCP_SHORTENER_ROUTE_PREFIX: str = "/u"
    MCP_SHORTENER_TOKEN_LENGTH: int = 10
    MCP_SHORTENER_STORE_PATH: Path = BASE_DIR / "short_urls.json"

    MCP_DEFAULT_PAGE_SIZE: int = 200
    MCP_MAX_PAGE_SIZE: int = 1000
    MCP_MAX_SEARCH_RESULTS: int = 200
    MCP_DEFAULT_COMMAND_TIMEOUT_SECONDS: float = 30.0
    MCP_MAX_COMMAND_TIMEOUT_SECONDS: float = 300.0
    MCP_MAX_COMMAND_OUTPUT_CHARS: int = 20000
    MCP_USE_COMPACTOR_MIDDLEWARE: bool = True
    MCP_FETCH_URL_OUTPUT_DIR: Path = WORKSPACE_ROOT / "fetched"
    MCP_FETCH_URL_MAX_BYTES: int = 20 * 1024 * 1024
    MCP_FETCH_URL_TIMEOUT_SECONDS: float = 30.0

    DOCKET_NAME: str = "pillbug"
    DOCKET_URL: str | None = None
    DOCKET_WORKER_CONCURRENCY: int = 3
    DOCKET_REDELIVERY_TIMEOUT_SECONDS: float = 300.0
    DOCKET_EXECUTION_TTL_SECONDS: float = 900.0

    ENABLED_CHANNELS: str = "cli"
    CHANNEL_PLUGIN_FACTORIES: str = ""
    INBOUND_DEBOUNCE_SECONDS: float = 1.5
    INBOUND_MAX_MESSAGE_CHARS: int = 4000

    TIMEZONE: str = "UTC"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="PB_",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    @field_validator("RUNTIME_ID")
    @classmethod
    def validate_runtime_id(cls, value: str | None) -> str | None:
        if value is None:
            return None

        return _validate_runtime_identifier(value, source="PB_RUNTIME_ID")

    @field_validator("A2A_CONVERGENCE_MAX_HOPS")
    @classmethod
    def validate_a2a_convergence_max_hops(cls, value: int) -> int:
        if value < 1:
            raise ValueError("PB_A2A_CONVERGENCE_MAX_HOPS must be at least 1")

        return value

    @field_validator("A2A_OUTBOUND_TIMEOUT_SECONDS")
    @classmethod
    def validate_a2a_outbound_timeout_seconds(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("PB_A2A_OUTBOUND_TIMEOUT_SECONDS must be greater than 0")

        return value

    @field_validator("DASHBOARD_BEARER_TOKEN", "A2A_BEARER_TOKEN", mode="before")
    @classmethod
    def validate_bearer_tokens(cls, value: str | SecretStr | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None

        token = value.get_secret_value() if isinstance(value, SecretStr) else str(value)
        normalized = token.strip()
        env_name = f"PB_{info.field_name}"

        if not normalized:
            raise ValueError(f"{env_name} must not be blank")

        if len(normalized) < _MIN_BEARER_TOKEN_LENGTH:
            raise ValueError(f"{env_name} must be at least {_MIN_BEARER_TOKEN_LENGTH} characters long")

        return normalized

    @model_validator(mode="after")
    def normalize_paths_and_validate_auth_configuration(self) -> Self:
        explicitly_configured_fields = set(self.model_fields_set)

        if "LOG_DIR" not in explicitly_configured_fields:
            self.LOG_DIR = self.BASE_DIR / "logs"

        if "SESSIONS_DIR" not in explicitly_configured_fields:
            self.SESSIONS_DIR = self.BASE_DIR / "sessions"

        if "TASKS_DIR" not in explicitly_configured_fields:
            self.TASKS_DIR = self.BASE_DIR / "tasks"

        if "TASKS_STORE_PATH" not in explicitly_configured_fields:
            self.TASKS_STORE_PATH = self.TASKS_DIR / "agent_tasks.json"

        if "SECURITY_PATTERNS_PATH" not in explicitly_configured_fields:
            self.SECURITY_PATTERNS_PATH = self.BASE_DIR / "security_patterns.json"

        if "WORKSPACE_ROOT" not in explicitly_configured_fields:
            self.WORKSPACE_ROOT = self.BASE_DIR / "workspace"

        if "RUNTIME_ID_PATH" not in explicitly_configured_fields:
            self.RUNTIME_ID_PATH = self.BASE_DIR / "runtime_id.txt"

        if "MCP_SHORTENER_STORE_PATH" not in explicitly_configured_fields:
            self.MCP_SHORTENER_STORE_PATH = self.BASE_DIR / "short_urls.json"

        if "MCP_FETCH_URL_OUTPUT_DIR" not in explicitly_configured_fields:
            self.MCP_FETCH_URL_OUTPUT_DIR = self.WORKSPACE_ROOT / "fetched"

        dashboard_token = self.dashboard_bearer_token()
        a2a_token = self.a2a_bearer_token()

        if dashboard_token and a2a_token and dashboard_token == a2a_token:
            raise ValueError(
                "PB_DASHBOARD_BEARER_TOKEN and PB_A2A_BEARER_TOKEN must differ so dashboard/control access stays isolated from A2A peer access"
            )

        return self

    def enabled_channels(self) -> tuple[str, ...]:
        enabled_channels = _split_csv(self.ENABLED_CHANNELS)
        return enabled_channels or ("cli",)

    @cached_property
    def runtime_id(self) -> str:
        if self.RUNTIME_ID is not None:
            return self.RUNTIME_ID

        runtime_id_path = self.RUNTIME_ID_PATH
        runtime_id_path.parent.mkdir(parents=True, exist_ok=True)

        if runtime_id_path.is_file():
            stored_runtime_id = runtime_id_path.read_text(encoding="utf-8").strip()
            return _validate_runtime_identifier(stored_runtime_id, source=str(runtime_id_path))

        generated_runtime_id = f"pillbug-{uuid4().hex[:12]}"
        try:
            with runtime_id_path.open("x", encoding="utf-8") as runtime_id_file:
                runtime_id_file.write(f"{generated_runtime_id}\n")
        except FileExistsError:
            pass

        stored_runtime_id = runtime_id_path.read_text(encoding="utf-8").strip()
        return _validate_runtime_identifier(stored_runtime_id, source=str(runtime_id_path))

    def ensure_runtime_identity(self) -> str:
        return self.runtime_id

    def dashboard_bearer_token(self) -> str | None:
        if self.DASHBOARD_BEARER_TOKEN is None:
            return None

        return self.DASHBOARD_BEARER_TOKEN.get_secret_value()

    def a2a_bearer_token(self) -> str | None:
        if self.A2A_BEARER_TOKEN is None:
            return None

        return self.A2A_BEARER_TOKEN.get_secret_value()

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

    def mcp_shortener_base_url(self) -> str:
        if self.MCP_SHORTENER_BASE_URL:
            return self.MCP_SHORTENER_BASE_URL.rstrip("/")

        host = self.MCP_HOST.strip()
        if host in {"", "0.0.0.0", "::"}:
            host = "127.0.0.1"
        elif ":" in host and not host.startswith("["):
            host = f"[{host}]"

        return f"http://{host}:{self.MCP_PORT}"

    def mcp_shortener_route_prefix(self) -> str:
        prefix = self.MCP_SHORTENER_ROUTE_PREFIX.strip()
        if not prefix:
            raise ValueError("PB_MCP_SHORTENER_ROUTE_PREFIX must not be empty")

        if not prefix.startswith("/"):
            prefix = f"/{prefix}"

        prefix = prefix.rstrip("/")
        if not prefix:
            raise ValueError("PB_MCP_SHORTENER_ROUTE_PREFIX must not resolve to the root path")

        return prefix


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
