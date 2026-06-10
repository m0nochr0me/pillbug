"""
Shared helpers for channel plugin packages.

Channel integrations (pillbug-telegram, pillbug-matrix, pillbug-slack, ...) used to carry
near-identical private copies of these utilities. They live here so a fix lands once.
Helpers that differ only by a constant take it as a required keyword argument
(message size limit, conversation-id separator); genuinely channel-specific logic
(file-type dispatch, msgtype resolution, transient-error classification) stays in the
plugins.
"""

import re
from pathlib import Path

from app.core.config import settings
from app.schema.messages import OutboundAttachment

_FILENAME_SANITIZER = re.compile(r"[^A-Za-z0-9._-]+")


def split_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


def chunk_message(text: str, *, max_chars: int) -> tuple[str, ...]:
    """Split text into chunks of at most max_chars, preferring paragraph and word breaks."""
    remaining = text.strip() or " "
    chunks: list[str] = []

    while len(remaining) > max_chars:
        split_at = remaining.rfind("\n\n", 0, max_chars + 1)
        if split_at < max_chars // 2:
            split_at = remaining.rfind("\n", 0, max_chars + 1)
        if split_at < max_chars // 2:
            split_at = remaining.rfind(" ", 0, max_chars + 1)
        if split_at < max_chars // 2:
            split_at = max_chars

        chunk = remaining[:split_at].rstrip()
        if chunk:
            chunks.append(chunk)

        remaining = remaining[split_at:].lstrip()

    if remaining:
        chunks.append(remaining)

    return tuple(chunks)


def sanitize_filename(value: str) -> str:
    sanitized = _FILENAME_SANITIZER.sub("_", value).strip("._")
    return sanitized or "attachment"


def resolve_attachment_path(attachment: OutboundAttachment) -> Path:
    """Resolve an outbound attachment path relative to the workspace root."""
    raw_path = Path(attachment.path)
    if raw_path.is_absolute():
        return raw_path
    return settings.WORKSPACE_ROOT / raw_path


def parse_threaded_conversation_id(
    conversation_id: str,
    *,
    separator: str,
    empty_error: str,
) -> tuple[str, str | None]:
    """Split a `<channel-id><separator><thread-id>` conversation id into its parts."""
    channel_id, found_separator, thread_id = conversation_id.partition(separator)
    channel_id = channel_id.strip()
    if not channel_id:
        raise ValueError(empty_error)

    if not found_separator:
        return channel_id, None

    normalized_thread_id = thread_id.strip()
    return channel_id, normalized_thread_id or None


def build_threaded_conversation_id(channel_id: str, thread_id: str | None, *, separator: str) -> str:
    if thread_id:
        return f"{channel_id}{separator}{thread_id}"
    return channel_id


def render_attachment_text(
    *,
    channel_label: str,
    attachment_label: str,
    caption_text: str,
    workspace_path: str | None,
    original_file_name: str | None,
    mime_type: str | None,
    duration_seconds: int | None = None,
    download_failed: bool,
) -> str:
    """Render the model-facing text that describes an inbound attachment."""
    lines: list[str] = []

    if caption_text:
        lines.append(caption_text)

    if download_failed:
        lines.append(
            f"{channel_label} {attachment_label} received, but saving it to the workspace downloads directory failed."
        )
    elif workspace_path is not None:
        lines.append(f"{channel_label} {attachment_label} saved to workspace path: {workspace_path}.")
    else:
        lines.append(f"{channel_label} {attachment_label} received.")

    if original_file_name:
        lines.append(f"Original filename: {original_file_name}.")
    if mime_type:
        lines.append(f"MIME type: {mime_type}.")
    if duration_seconds is not None:
        lines.append(f"Duration: {duration_seconds} seconds.")

    return "\n".join(lines)
