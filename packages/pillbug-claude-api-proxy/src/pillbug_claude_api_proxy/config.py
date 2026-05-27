"""Configuration for the Gemini-to-Anthropic-API proxy.

Authentication uses the Claude Code subscription OAuth token created by
`claude setup-token`. The Anthropic Python SDK accepts the token via its
`auth_token` constructor argument, which becomes the `Authorization: Bearer`
header — distinct from API-key (`x-api-key`) auth.

The Claude Code subscription path also requires the system prompt to start
with `You are Claude Code, Anthropic's official CLI for Claude.` —
verified empirically against api.anthropic.com:

  - Request without the prefix → HTTP 429 `rate_limit_error: "Error"`
    (abuse-rejection masquerading as a rate limit; quota is fine).
  - Request with the prefix    → HTTP 200.

This is NOT documented by Anthropic; it is observed behavior of the
production OAuth subscription path. The `anthropic-beta` header is left
configurable for forward-compatibility but is not currently required.

"""

import os
import sys
from datetime import datetime

from pydantic import ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.schema.log_entry import LogEntry

__all__ = ("ProxySettings", "settings")


_DEFAULT_CLAUDE_CODE_PREFIX = "You are Claude Code, Anthropic's official CLI for Claude."
_DEFAULT_OAUTH_BETA = ""


class ProxySettings(BaseSettings):
    HOST: str = "127.0.0.1"
    PORT: int = 9033

    MODEL: str = ""
    MAX_TOKENS: int = 8192

    REQUEST_TIMEOUT_SECONDS: float = 600.0

    OAUTH_TOKEN: str = ""

    CLAUDE_CODE_SYSTEM_PREFIX: str = _DEFAULT_CLAUDE_CODE_PREFIX
    OAUTH_BETA_HEADER: str = _DEFAULT_OAUTH_BETA

    LOG_INCLUDE_TRACEBACK: bool = False

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="PB_CLAUDE_API_PROXY_",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    @field_validator("REQUEST_TIMEOUT_SECONDS")
    @classmethod
    def validate_timeout(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("PB_CLAUDE_API_PROXY_REQUEST_TIMEOUT_SECONDS must be greater than 0")
        return value

    @field_validator("MAX_TOKENS")
    @classmethod
    def validate_max_tokens(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("PB_CLAUDE_API_PROXY_MAX_TOKENS must be greater than 0")
        return value

    def resolved_oauth_token(self) -> str | None:
        """Resolve the OAuth bearer token from PB-prefixed env or the standard fallback."""

        return self.OAUTH_TOKEN or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or None


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
