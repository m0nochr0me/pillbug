"""Pydantic models and frontmatter (de)serialization for memory files."""

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = (
    "FRONTMATTER_DELIMITER",
    "MemoryRecord",
    "MemoryType",
    "parse_memory_file",
    "render_memory_file",
)

MemoryType = Literal["user", "feedback", "project", "reference"]

FRONTMATTER_DELIMITER = "---"


class MemoryRecord(BaseModel):
    model_config = ConfigDict(frozen=False)

    id: str
    name: str
    description: str
    type: MemoryType
    tags: tuple[str, ...] = Field(default_factory=tuple)
    created: datetime
    updated: datetime
    body: str = ""

    @field_validator("tags", mode="before")
    @classmethod
    def normalize_tags(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            value = [value]
        normalized: list[str] = []
        for item in value:
            text = str(item).strip()
            if text and text not in normalized:
                normalized.append(text)
        return tuple(normalized)

    @field_validator("name", "description", "id", mode="before")
    @classmethod
    def strip_required_strings(cls, value: Any) -> str:
        text = "" if value is None else str(value).strip()
        if not text:
            raise ValueError("must not be blank")
        return text


def _strip_wrapping_quotes(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ('"', "'"):
        return text[1:-1]
    return text


def _parse_scalar(raw: str) -> Any:
    text = raw.strip()
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            inner = text[1:-1].strip()
            if not inner:
                return []
            return [_strip_wrapping_quotes(item.strip()) for item in inner.split(",")]
        return parsed if isinstance(parsed, list) else [parsed]
    return _strip_wrapping_quotes(text)


def parse_memory_file(text: str) -> MemoryRecord:
    """Parse a memory file (frontmatter + body) into a MemoryRecord."""

    lines = text.splitlines()
    if not lines or lines[0].strip() != FRONTMATTER_DELIMITER:
        raise ValueError("missing frontmatter opening delimiter")

    fields: dict[str, Any] = {}
    body_start: int | None = None

    for index in range(1, len(lines)):
        line = lines[index]
        stripped = line.strip()

        if stripped == FRONTMATTER_DELIMITER:
            body_start = index + 1
            break

        if not stripped or stripped.startswith("#"):
            continue

        key, separator, raw_value = line.partition(":")
        if not separator:
            continue

        fields[key.strip().lower()] = _parse_scalar(raw_value)

    if body_start is None:
        raise ValueError("missing frontmatter closing delimiter")

    body_lines = lines[body_start:]
    if body_lines and body_lines[0].strip() == "":
        body_lines = body_lines[1:]
    body = "\n".join(body_lines)

    return MemoryRecord(**fields, body=body)


def _render_iso(value: datetime) -> str:
    rendered = value.isoformat()
    return rendered.replace("+00:00", "Z")


def render_memory_file(record: MemoryRecord) -> str:
    """Render a MemoryRecord back to its on-disk representation."""

    tag_payload = "[" + ", ".join(json.dumps(tag) for tag in record.tags) + "]"

    frontmatter_lines = [
        FRONTMATTER_DELIMITER,
        f"id: {record.id}",
        f"name: {json.dumps(record.name)}",
        f"description: {json.dumps(record.description)}",
        f"type: {record.type}",
        f"tags: {tag_payload}",
        f"created: {_render_iso(record.created)}",
        f"updated: {_render_iso(record.updated)}",
        FRONTMATTER_DELIMITER,
    ]

    body = record.body
    if body and not body.endswith("\n"):
        body += "\n"

    return "\n".join(frontmatter_lines) + "\n\n" + body
