"""Helpers for correlating MCP client sessions with Pillbug runtime sessions."""

from copy import deepcopy
from typing import Final

_SESSION_KEY_SEPARATOR: Final[str] = ":"
_mcp_runtime_sessions: dict[str, str] = {}
_runtime_session_origin_metadata: dict[str, dict[str, object]] = {}


def bind_mcp_session_to_runtime_session(mcp_session_id: str, runtime_session_key: str) -> None:
    normalized_mcp_session_id = mcp_session_id.strip()
    normalized_runtime_session_key = runtime_session_key.strip()
    if not normalized_mcp_session_id or not normalized_runtime_session_key:
        return

    _mcp_runtime_sessions[normalized_mcp_session_id] = normalized_runtime_session_key


def get_runtime_session_for_mcp_session(mcp_session_id: str) -> str | None:
    normalized_mcp_session_id = mcp_session_id.strip()
    if not normalized_mcp_session_id:
        return None

    runtime_session_key = _mcp_runtime_sessions.get(normalized_mcp_session_id)
    if runtime_session_key is None:
        return None

    return runtime_session_key.strip() or None


def bind_runtime_session_origin_metadata(runtime_session_key: str, metadata: dict[str, object]) -> None:
    normalized_runtime_session_key = runtime_session_key.strip()
    if not normalized_runtime_session_key:
        return

    _runtime_session_origin_metadata[normalized_runtime_session_key] = deepcopy(metadata)


def get_runtime_session_origin_metadata(runtime_session_key: str) -> dict[str, object] | None:
    normalized_runtime_session_key = runtime_session_key.strip()
    if not normalized_runtime_session_key:
        return None

    metadata = _runtime_session_origin_metadata.get(normalized_runtime_session_key)
    if metadata is None:
        return None

    return deepcopy(metadata)


def split_runtime_session_key(runtime_session_key: str) -> tuple[str, str] | None:
    channel_name, separator, conversation_id = runtime_session_key.strip().partition(_SESSION_KEY_SEPARATOR)
    if not separator or not channel_name or not conversation_id:
        return None

    return channel_name, conversation_id
