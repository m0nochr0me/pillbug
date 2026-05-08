"""Configuration for the websocket channel plugin."""

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class WebsocketSettings(BaseSettings):
    HOST: str = "127.0.0.1"
    PORT: int = 9200
    BEARER_TOKEN: SecretStr
    IDLE_TIMEOUT_SECONDS: float = 600.0
    JANITOR_INTERVAL_SECONDS: float = 30.0
    CORS_ALLOWED_ORIGINS: str = "*"
    SOCKETIO_PATH: str = "/socket.io"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="PB_WEBSOCKET_",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    @field_validator("BEARER_TOKEN", mode="before")
    @classmethod
    def _validate_bearer_token(cls, value: str | SecretStr | None) -> str:
        if value is None:
            raise ValueError("PB_WEBSOCKET_BEARER_TOKEN is required when the websocket channel is enabled")

        token = value.get_secret_value() if isinstance(value, SecretStr) else str(value)
        normalized = token.strip()
        if not normalized:
            raise ValueError("PB_WEBSOCKET_BEARER_TOKEN must not be blank")

        return normalized

    @field_validator("IDLE_TIMEOUT_SECONDS", "JANITOR_INTERVAL_SECONDS")
    @classmethod
    def _validate_positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("must be greater than zero")
        return value


settings = WebsocketSettings()  # type: ignore[call-arg]
