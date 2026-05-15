"""Async CRUD helpers for the trigger sources JSON file."""

import asyncio

from pydantic import TypeAdapter, ValidationError

from app.util.workspace import async_read_text_file, async_write_text_file
from pillbug_trigger.config import ensure_trigger_sources_file, settings
from pillbug_trigger.schema import TriggerSourceConfig, Urgency

__all__ = (
    "TriggerSourceAlreadyExistsError",
    "TriggerSourceNotFoundError",
    "create_source",
    "delete_source",
    "get_source",
    "list_sources",
    "update_source",
)

_ADAPTER = TypeAdapter(list[TriggerSourceConfig])
_write_lock = asyncio.Lock()


class TriggerSourceNotFoundError(KeyError):
    """Raised when a trigger source name does not exist in the store."""


class TriggerSourceAlreadyExistsError(ValueError):
    """Raised when attempting to create a trigger source that already exists."""


async def _read_sources() -> list[TriggerSourceConfig]:
    await asyncio.to_thread(ensure_trigger_sources_file)
    text = await async_read_text_file(settings.SOURCES_PATH)
    if not text.strip():
        return []
    try:
        return list(_ADAPTER.validate_json(text))
    except ValidationError as exc:
        raise ValueError(f"Trigger sources file is malformed: {exc}") from exc


async def _write_sources(sources: list[TriggerSourceConfig]) -> None:
    payload = _ADAPTER.dump_json(sources, indent=2).decode("utf-8") + "\n"
    await async_write_text_file(settings.SOURCES_PATH, payload, mode="w")


async def list_sources() -> list[TriggerSourceConfig]:
    return await _read_sources()


async def get_source(source: str) -> TriggerSourceConfig:
    for cfg in await _read_sources():
        if cfg.source == source:
            return cfg
    raise TriggerSourceNotFoundError(source)


async def create_source(
    source: str,
    prompt: str,
    urgency_override: Urgency | None,
) -> TriggerSourceConfig:
    async with _write_lock:
        sources = await _read_sources()
        if any(cfg.source == source for cfg in sources):
            raise TriggerSourceAlreadyExistsError(source)
        new_config = TriggerSourceConfig(
            source=source,
            prompt=prompt,
            urgency_override=urgency_override,
        )
        sources.append(new_config)
        await _write_sources(sources)
        return new_config


async def update_source(
    source: str,
    prompt: str | None,
    urgency_override: Urgency | None,
) -> TriggerSourceConfig:
    async with _write_lock:
        sources = await _read_sources()
        for index, cfg in enumerate(sources):
            if cfg.source != source:
                continue
            updated = cfg.model_copy(
                update={
                    "prompt": prompt if prompt is not None else cfg.prompt,
                    "urgency_override": (urgency_override if urgency_override is not None else cfg.urgency_override),
                },
            )
            sources[index] = updated
            await _write_sources(sources)
            return updated
        raise TriggerSourceNotFoundError(source)


async def delete_source(source: str) -> str:
    async with _write_lock:
        sources = await _read_sources()
        remaining = [cfg for cfg in sources if cfg.source != source]
        if len(remaining) == len(sources):
            raise TriggerSourceNotFoundError(source)
        await _write_sources(remaining)
        return source
