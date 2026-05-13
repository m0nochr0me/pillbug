"""Async CRUD for flat-file Markdown memories under workspace/memory/."""

import asyncio
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.util.workspace import async_read_text_file, async_write_text_file
from pillbug_memory.index import INDEX_FILE_NAME, append_index_entry, rebuild_index, remove_index_entry
from pillbug_memory.schema import MemoryRecord, MemoryType, parse_memory_file, render_memory_file

__all__ = ("MemoryNotFoundError", "MemoryStore")


def _new_memory_id() -> str:
    generator = getattr(uuid, "uuid7", None) or uuid.uuid4
    return generator().hex


def _now() -> datetime:
    return datetime.now(UTC)


class MemoryNotFoundError(LookupError):
    """Raised when a memory id (or prefix) cannot be resolved."""


class MemoryStore:
    """File-backed memory store. All paths must stay within ``base_dir``."""

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    async def ensure_ready(self) -> None:
        await asyncio.to_thread(self._base_dir.mkdir, parents=True, exist_ok=True)

    def _path_for(self, record_id: str) -> Path:
        return self._base_dir / f"{record_id}.md"

    async def _resolve_id(self, id_or_prefix: str) -> str:
        normalized = id_or_prefix.strip().lower()
        if not normalized:
            raise ValueError("id must not be blank")

        direct = self._path_for(normalized)
        if await asyncio.to_thread(direct.is_file):
            return normalized

        def find_prefix_matches() -> list[str]:
            if not self._base_dir.is_dir():
                return []
            matches = [
                path.stem
                for path in self._base_dir.iterdir()
                if path.is_file()
                and path.suffix == ".md"
                and path.name != INDEX_FILE_NAME
                and path.stem.startswith(normalized)
            ]
            return matches

        matches = await asyncio.to_thread(find_prefix_matches)
        if not matches:
            raise MemoryNotFoundError(f"no memory found for id or prefix: {id_or_prefix}")
        if len(matches) > 1:
            raise ValueError(f"id prefix is ambiguous ({len(matches)} matches): {id_or_prefix}")
        return matches[0]

    async def create(
        self,
        *,
        name: str,
        description: str,
        body: str,
        type: MemoryType,
        tags: Iterable[str] | None = None,
    ) -> MemoryRecord:
        await self.ensure_ready()
        now = _now()
        record = MemoryRecord(
            id=_new_memory_id(),
            name=name,
            description=description,
            type=type,
            tags=tuple(tags or ()),
            created=now,
            updated=now,
            body=body,
        )

        path = self._path_for(record.id)
        if await asyncio.to_thread(path.exists):
            raise FileExistsError(f"memory id collision: {record.id}")

        await async_write_text_file(path, render_memory_file(record), mode="x")
        await append_index_entry(self._base_dir, record)
        return record

    async def get(self, id_or_prefix: str) -> MemoryRecord:
        resolved_id = await self._resolve_id(id_or_prefix)
        text = await async_read_text_file(self._path_for(resolved_id))
        return parse_memory_file(text)

    async def update(
        self,
        id_or_prefix: str,
        *,
        name: str | None = None,
        description: str | None = None,
        body: str | None = None,
        type: MemoryType | None = None,
        tags: Iterable[str] | None = None,
    ) -> MemoryRecord:
        record = await self.get(id_or_prefix)
        updated = record.model_copy(
            update={
                "name": name if name is not None else record.name,
                "description": description if description is not None else record.description,
                "body": body if body is not None else record.body,
                "type": type if type is not None else record.type,
                "tags": tuple(tags) if tags is not None else record.tags,
                "updated": _now(),
            }
        )
        # Re-run validators in case caller passed dirty input.
        validated = MemoryRecord.model_validate(updated.model_dump())

        await async_write_text_file(self._path_for(validated.id), render_memory_file(validated), mode="w")
        await rebuild_index(self._base_dir)
        return validated

    async def delete(self, id_or_prefix: str) -> str:
        resolved_id = await self._resolve_id(id_or_prefix)
        await asyncio.to_thread(self._path_for(resolved_id).unlink)
        await remove_index_entry(self._base_dir, resolved_id)
        return resolved_id

    async def list(
        self,
        *,
        query: str | None = None,
        type: MemoryType | None = None,
        tag: str | None = None,
        limit: int = 50,
    ) -> list[MemoryRecord]:
        if limit < 1:
            raise ValueError("limit must be at least 1")

        await self.ensure_ready()
        query_term = query.strip().lower() if query else None
        tag_term = tag.strip() if tag else None

        def list_memory_files() -> list[Path]:
            return sorted(
                path
                for path in self._base_dir.iterdir()
                if path.is_file() and path.suffix == ".md" and path.name != INDEX_FILE_NAME
            )

        files = await asyncio.to_thread(list_memory_files)

        results: list[MemoryRecord] = []
        for memory_file in files:
            try:
                text = await async_read_text_file(memory_file)
                record = parse_memory_file(text)
            except OSError, ValueError:
                continue

            if type is not None and record.type != type:
                continue
            if tag_term is not None and tag_term not in record.tags:
                continue
            if query_term and query_term not in record.name.lower() and query_term not in record.description.lower():
                continue

            results.append(record)
            if len(results) >= limit:
                break

        return results

    async def repair_index(self) -> int:
        await self.ensure_ready()
        return await rebuild_index(self._base_dir)

    @staticmethod
    def summarize(record: MemoryRecord) -> dict[str, Any]:
        return {
            "id": record.id,
            "name": record.name,
            "description": record.description,
            "type": record.type,
            "tags": list(record.tags),
            "created": record.created.isoformat().replace("+00:00", "Z"),
            "updated": record.updated.isoformat().replace("+00:00", "Z"),
        }

    @staticmethod
    def to_payload(record: MemoryRecord) -> dict[str, Any]:
        payload = MemoryStore.summarize(record)
        payload["body"] = record.body
        return payload
