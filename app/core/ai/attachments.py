"""MIME/attachment helpers for inbound multimodal content."""

import mimetypes
from pathlib import Path
from typing import Any

from google.genai import types
from pydantic import ValidationError

from app.core.config import settings
from app.core.log import logger
from app.schema.ai import InboundAttachment
from app.util.workspace import resolve_path_within_root

_TEXT_ATTACHMENT_MIME_TYPES = {
    "text/markdown": "text/markdown",
    "text/plain": "text/plain",
    "text/x-markdown": "text/markdown",
}
_ATTACHMENT_MIME_TYPE_OVERRIDES = {
    ".markdown": "text/markdown",
    ".md": "text/markdown",
    ".pdf": "application/pdf",
    ".txt": "text/plain",
}
_INLINE_ATTACHMENT_MAX_BYTES = 8 * 1024 * 1024


def _has_file_data_parts(history: list[types.Content]) -> bool:
    for content in history:
        for part in content.parts or []:
            if getattr(part, "file_data", None) is not None:
                return True
    return False


def _strip_file_data_parts(history: list[types.Content]) -> list[types.Content]:
    sanitized: list[types.Content] = []
    for content in history:
        parts = content.parts or []
        kept = [part for part in parts if getattr(part, "file_data", None) is None]
        if not kept:
            continue
        sanitized.append(types.Content(role=content.role, parts=kept))
    return sanitized


def _extract_injectable_content(history: list[types.Content]) -> types.Content | None:
    """Return the last model response from history with only text and thought parts (thought_signature preserved)."""
    for content in reversed(history):
        if getattr(content, "role", None) != "model":
            continue
        injectable_parts = [
            p
            for p in (content.parts or [])
            if getattr(p, "thought", False) or isinstance(getattr(p, "text", None), str)
        ]
        if injectable_parts:
            return types.Content(role="model", parts=injectable_parts)
    return None


def _normalize_supported_attachment_mime_type(mime_type: str) -> str | None:
    normalized_mime_type = mime_type.strip().lower()
    if not normalized_mime_type:
        return None
    if normalized_mime_type.startswith("audio/"):
        return normalized_mime_type
    if normalized_mime_type.startswith("image/"):
        return normalized_mime_type
    if normalized_mime_type == "application/pdf":
        return normalized_mime_type
    return _TEXT_ATTACHMENT_MIME_TYPES.get(normalized_mime_type)


def _supported_attachment_mime_type(attachment_path: Path, attachment: InboundAttachment) -> str | None:
    candidates: list[str] = []

    if attachment.mime_type:
        candidates.append(attachment.mime_type)

    suffix_override = _ATTACHMENT_MIME_TYPE_OVERRIDES.get(attachment_path.suffix.lower())
    if suffix_override is not None:
        candidates.append(suffix_override)

    guessed_mime_type, _ = mimetypes.guess_type(attachment_path.name)
    if guessed_mime_type is not None:
        candidates.append(guessed_mime_type)

    if attachment.kind == "photo":
        candidates.append("image/jpeg")

    for candidate in candidates:
        if normalized_candidate := _normalize_supported_attachment_mime_type(candidate):
            return normalized_candidate

    return None


def _legacy_attachment_from_metadata(metadata: dict[str, Any]) -> InboundAttachment | None:
    attachment_path = metadata.get("telegram_attachment_download_path")
    if not isinstance(attachment_path, str) or not attachment_path.strip():
        return None

    return InboundAttachment(
        path=attachment_path,
        mime_type=metadata.get("telegram_attachment_mime_type")
        if isinstance(metadata.get("telegram_attachment_mime_type"), str)
        else None,
        display_name=(
            metadata.get("telegram_attachment_original_file_name")
            if isinstance(metadata.get("telegram_attachment_original_file_name"), str)
            else None
        ),
        source="telegram",
        kind=metadata.get("telegram_attachment_type")
        if isinstance(metadata.get("telegram_attachment_type"), str)
        else None,
    )


def resolve_inbound_attachment_path(attachment_path: str, channel_source: str | None) -> Path | None:
    """Resolve an inbound attachment within the per-channel sub-root (plan P2 #17).

    Returns the resolved path when both the workspace sandbox and (if configured) the
    per-channel sub-root accept it; returns None when the attachment escapes either
    boundary. Channels without an explicit sub-root entry fall back to the workspace
    root unchanged so unrelated plugins keep working.
    """
    try:
        resolved = resolve_path_within_root(attachment_path, settings.WORKSPACE_ROOT)
    except ValueError:
        return None
    sub_root = settings.inbound_attachment_roots().get(channel_source or "")
    if sub_root is None:
        return resolved
    expected_root = (settings.WORKSPACE_ROOT / sub_root).resolve()
    try:
        resolved.relative_to(expected_root)
    except ValueError:
        return None
    return resolved


def _extract_inbound_attachments(metadata: dict[str, Any]) -> list[InboundAttachment]:
    attachments: list[InboundAttachment] = []
    raw_attachments = metadata.get("inbound_attachments")

    raw_values: list[object] = []
    if isinstance(raw_attachments, list | tuple):
        raw_values.extend(raw_attachments)
    elif isinstance(raw_attachments, dict):
        raw_values.append(raw_attachments)

    for raw_value in raw_values:
        try:
            attachments.append(InboundAttachment.model_validate(raw_value))
        except ValidationError as exc:
            logger.warning(f"Skipping invalid inbound attachment metadata entry: {exc}")

    if not attachments and (legacy_attachment := _legacy_attachment_from_metadata(metadata)) is not None:
        attachments.append(legacy_attachment)

    return attachments
