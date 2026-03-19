"""
Workspace-oriented path and file helpers.
"""

from pathlib import Path

import aiofile

__all__ = (
    "async_read_text_file",
    "async_write_bytes_file",
    "async_write_text_file",
    "display_path",
    "is_hidden_path",
    "read_text_file",
    "resolve_path_within_root",
    "truncate_text",
    "write_bytes_file",
    "write_text_file",
)


def display_path(path: Path, workspace_root: Path) -> str:
    if path == workspace_root:
        return "."

    return str(path.relative_to(workspace_root))


def resolve_path_within_root(path: str | Path, workspace_root: Path) -> Path:
    raw_path = Path(path)
    candidate = raw_path if raw_path.is_absolute() else workspace_root / raw_path
    resolved = candidate.resolve()

    if not resolved.is_relative_to(workspace_root):
        raise ValueError(f"Path escapes workspace root: {path}")

    return resolved


async def async_read_text_file(path: Path) -> str:
    async with aiofile.AIOFile(path, "rb") as file:
        payload = await file.read()
    return bytes(payload).decode("utf-8", errors="replace")


async def async_write_text_file(path: Path, content: str, *, mode: str) -> int:
    async with aiofile.AIOFile(path, mode, encoding="utf-8") as file:
        await file.write(content)
    return len(content)


async def async_write_bytes_file(path: Path, content: bytes, *, mode: str = "wb") -> int:
    async with aiofile.AIOFile(path, mode) as file:
        await file.write(content)
    return len(content)


def read_text_file(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def write_text_file(path: Path, content: str, *, mode: str) -> int:
    with path.open(mode, encoding="utf-8") as file:
        return file.write(content)


def write_bytes_file(path: Path, content: bytes) -> int:
    return path.write_bytes(content)


def is_hidden_path(relative_path: Path) -> bool:
    return any(part.startswith(".") for part in relative_path.parts)


def truncate_text(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False

    omitted_chars = len(text) - limit
    suffix = f"\n... truncated {omitted_chars} characters"
    truncated = text[: max(limit - len(suffix), 0)] + suffix
    return truncated, True
