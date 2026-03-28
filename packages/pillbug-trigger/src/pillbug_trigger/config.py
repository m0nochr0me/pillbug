"""Configuration for the trigger channel plugin."""

from functools import cache
from pathlib import Path

from pydantic import TypeAdapter, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.config import settings as runtime_settings
from app.core.log import logger
from pillbug_trigger.schema import TriggerSourceConfig

_DEFAULT_TRIGGER_SOURCES_JSON = "[]\n"
_TRIGGER_SOURCE_CONFIGS_ADAPTER = TypeAdapter(tuple[TriggerSourceConfig, ...])


class TriggerSettings(BaseSettings):
    HOST: str = "127.0.0.1"
    PORT: int = 9100
    BEARER_TOKEN: str
    SOURCES_PATH: Path = runtime_settings.BASE_DIR / "trigger_sources.json"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="PB_TRIGGER_",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )


settings = TriggerSettings()  # type: ignore[call-arg]


def render_default_trigger_sources() -> str:
    return _DEFAULT_TRIGGER_SOURCES_JSON


def ensure_trigger_sources_file() -> None:
    settings.SOURCES_PATH.parent.mkdir(parents=True, exist_ok=True)
    if settings.SOURCES_PATH.is_file():
        return

    settings.SOURCES_PATH.write_text(render_default_trigger_sources(), encoding="utf-8")


@cache
def _load_trigger_source_configs_from_disk(
    config_path: str,
    modified_at_ns: int,
) -> tuple[TriggerSourceConfig, ...]:
    del modified_at_ns
    path = Path(config_path)

    try:
        config_text = path.read_text(encoding="utf-8")
        return _TRIGGER_SOURCE_CONFIGS_ADAPTER.validate_json(config_text)
    except (OSError, ValidationError) as exc:
        logger.warning(f"Failed to load trigger source configs from {config_path}: {exc}. Using empty config.")
        return ()


def get_trigger_source_configs() -> tuple[TriggerSourceConfig, ...]:
    ensure_trigger_sources_file()

    try:
        modified_at_ns = settings.SOURCES_PATH.stat().st_mtime_ns
    except OSError as exc:
        logger.warning(f"Failed to stat trigger source config file {settings.SOURCES_PATH}: {exc}. Using empty config.")
        return ()

    return _load_trigger_source_configs_from_disk(str(settings.SOURCES_PATH), modified_at_ns)


def get_trigger_source_config_map() -> dict[str, TriggerSourceConfig]:
    source_configs: dict[str, TriggerSourceConfig] = {}

    for source_config in get_trigger_source_configs():
        if source_config.source in source_configs:
            logger.warning(
                "Duplicate trigger source config detected; last entry wins",
                source=source_config.source,
                config_path=str(settings.SOURCES_PATH),
            )
        source_configs[source_config.source] = source_config

    return source_configs
