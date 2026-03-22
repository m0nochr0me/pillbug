"""Dashboard configuration."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class DashboardSettings(BaseSettings):
    APP_TITLE: str = "Pillbug Dashboard"
    HOST: str = "127.0.0.1"
    PORT: int = 8010

    BASE_DIR: Path = Path.home() / ".pillbug-dashboard"
    RUNTIME_REGISTRY_PATH: Path = BASE_DIR / "runtimes.json"
    RUNTIME_TIMEOUT_SECONDS: float = 10.0

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="PB_DASHBOARD_",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    def ensure_directories(self) -> None:
        self.BASE_DIR.mkdir(parents=True, exist_ok=True)
        self.RUNTIME_REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)


settings = DashboardSettings()
