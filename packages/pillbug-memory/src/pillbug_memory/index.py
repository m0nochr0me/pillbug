"""MEMORY.md ledger handling: append, remove, atomic rewrite, and repair."""

import asyncio
import os
from pathlib import Path

from app.util.workspace import async_read_text_file, async_write_text_file
from pillbug_memory.schema import MemoryRecord, parse_memory_file

__all__ = ("INDEX_FILE_NAME", "append_index_entry", "remove_index_entry", "rebuild_index")

INDEX_FILE_NAME = "MEMORY.md"
_INDEX_HEADER = "# Memory Index\n\n"


def _format_entry(record: MemoryRecord) -> str:
    description = record.description.replace("\n", " ").strip()
    return f"- [{record.name}]({record.id}.md) — {description}"


def _entry_marker(record_id: str) -> str:
    return f"]({record_id}.md)"


async def _read_index(index_path: Path) -> str:
    if not await asyncio.to_thread(index_path.is_file):
        return ""
    return await async_read_text_file(index_path)


async def _atomic_write(index_path: Path, content: str) -> None:
    temp_path = index_path.with_suffix(index_path.suffix + ".tmp")
    await async_write_text_file(temp_path, content, mode="w")
    await asyncio.to_thread(os.replace, temp_path, index_path)


async def append_index_entry(memory_dir: Path, record: MemoryRecord) -> None:
    index_path = memory_dir / INDEX_FILE_NAME
    existing = await _read_index(index_path)
    marker = _entry_marker(record.id)

    if marker in existing:
        await rebuild_index(memory_dir)
        return

    if not existing:
        new_content = _INDEX_HEADER + _format_entry(record) + "\n"
    else:
        suffix = "" if existing.endswith("\n") else "\n"
        new_content = existing + suffix + _format_entry(record) + "\n"

    await _atomic_write(index_path, new_content)


async def remove_index_entry(memory_dir: Path, record_id: str) -> None:
    index_path = memory_dir / INDEX_FILE_NAME
    existing = await _read_index(index_path)
    if not existing:
        return

    marker = _entry_marker(record_id)
    kept_lines = [line for line in existing.splitlines() if marker not in line]
    new_content = "\n".join(kept_lines)
    if new_content and not new_content.endswith("\n"):
        new_content += "\n"

    await _atomic_write(index_path, new_content)


async def rebuild_index(memory_dir: Path) -> int:
    """Rebuild MEMORY.md from frontmatter on disk. Returns the number of indexed entries."""

    index_path = memory_dir / INDEX_FILE_NAME

    def list_memory_files() -> list[Path]:
        if not memory_dir.is_dir():
            return []
        return sorted(
            path
            for path in memory_dir.iterdir()
            if path.is_file() and path.suffix == ".md" and path.name != INDEX_FILE_NAME
        )

    memory_files = await asyncio.to_thread(list_memory_files)
    records: list[MemoryRecord] = []
    for memory_file in memory_files:
        try:
            text = await async_read_text_file(memory_file)
            records.append(parse_memory_file(text))
        except OSError, ValueError:
            continue

    records.sort(key=lambda record: (record.name.lower(), record.id))

    lines = [_INDEX_HEADER.rstrip("\n"), ""]
    lines.extend(_format_entry(record) for record in records)
    new_content = "\n".join(lines) + "\n"

    await _atomic_write(index_path, new_content)
    return len(records)
