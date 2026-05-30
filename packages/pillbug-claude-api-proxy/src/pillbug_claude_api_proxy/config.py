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
from pathlib import Path

from pydantic import ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.schema.log_entry import LogEntry

__all__ = ("ProxySettings", "settings")


_DEFAULT_CLAUDE_CODE_PREFIX = "You are Claude Code, Anthropic's official CLI for Claude."
_DEFAULT_OAUTH_BETA = ""

_DEFAULT_ELEVENLABS_BASE_URL = "https://api.elevenlabs.io"
_DEFAULT_ELEVENLABS_MODEL = "scribe_v2"
_AUDIO_MODES = ("placeholder", "elevenlabs")


class ProxySettings(BaseSettings):
    HOST: str = "127.0.0.1"
    PORT: int = 9033

    MODEL: str = ""
    MAX_TOKENS: int = 8192

    REQUEST_TIMEOUT_SECONDS: float = 600.0

    OAUTH_TOKEN: str = ""

    CLAUDE_CODE_SYSTEM_PREFIX: str = _DEFAULT_CLAUDE_CODE_PREFIX
    OAUTH_BETA_HEADER: str = _DEFAULT_OAUTH_BETA

    # Inbound audio: Claude has no native audio modality, so voice notes are either
    # transcribed via ElevenLabs Scribe ("elevenlabs") or replaced with a short text
    # note ("placeholder"). See audio.py.
    AUDIO_MODE: str = "placeholder"
    ELEVENLABS_MODEL: str = _DEFAULT_ELEVENLABS_MODEL
    ELEVENLABS_BASE_URL: str = _DEFAULT_ELEVENLABS_BASE_URL

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

    @field_validator("AUDIO_MODE")
    @classmethod
    def validate_audio_mode(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in _AUDIO_MODES:
            raise ValueError(f"PB_CLAUDE_API_PROXY_AUDIO_MODE must be one of {_AUDIO_MODES}, got {value!r}")
        return normalized

    def resolved_oauth_token(self) -> str | None:
        """Resolve the OAuth bearer token from PB-prefixed env or the standard fallback."""

        return self.OAUTH_TOKEN or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or None

    @staticmethod
    def _read_credential(env_name: str) -> str | None:
        """Resolve a credential the way skills/text-to-speech/scripts/synthesize.sh does.

        Prefer the Docker/Kubernetes secret file `/run/secrets/<lowercased name>`,
        then fall back to the environment variable of the same name. Returns None
        when neither yields a non-empty value.
        """

        secret_file = Path("/run/secrets") / env_name.lower()
        try:
            secret_value = secret_file.read_text(encoding="utf-8").strip()
        except OSError:
            secret_value = ""
        if secret_value:
            return secret_value
        return os.environ.get(env_name, "").strip() or None

    def resolved_elevenlabs_api_key(self) -> str | None:
        """ElevenLabs Scribe API key for PB_CLAUDE_API_PROXY_AUDIO_MODE=elevenlabs."""

        return self._read_credential("ELEVENLABS_API_KEY")


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
