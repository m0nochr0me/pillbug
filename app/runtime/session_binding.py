"""Helpers for correlating MCP client sessions with Pillbug runtime sessions."""

from copy import deepcopy
from typing import Final

from app.schema.todo import TodoListSnapshot

_SESSION_KEY_SEPARATOR: Final[str] = ":"
_mcp_runtime_sessions: dict[str, str] = {}
_runtime_session_origin_metadata: dict[str, dict[str, object]] = {}
_runtime_session_todo_snapshots: dict[str, TodoListSnapshot] = {}
_pending_outbound_injections: dict[str, list[str]] = {}
_runtime_session_loaded_skills: dict[str, set[str]] = {}


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


def bind_runtime_session_todo_snapshot(runtime_session_key: str, snapshot: TodoListSnapshot | None) -> None:
    normalized_runtime_session_key = runtime_session_key.strip()
    if not normalized_runtime_session_key:
        return

    if snapshot is None or not snapshot.items:
        _runtime_session_todo_snapshots.pop(normalized_runtime_session_key, None)
        return

    _runtime_session_todo_snapshots[normalized_runtime_session_key] = snapshot.model_copy(deep=True)


def get_runtime_session_todo_snapshot(runtime_session_key: str) -> TodoListSnapshot | None:
    normalized_runtime_session_key = runtime_session_key.strip()
    if not normalized_runtime_session_key:
        return None

    snapshot = _runtime_session_todo_snapshots.get(normalized_runtime_session_key)
    if snapshot is None:
        return None

    return snapshot.model_copy(deep=True)


def record_pending_outbound_injection(source_session_key: str, target_session_key: str) -> None:
    normalized_source = source_session_key.strip()
    normalized_target = target_session_key.strip()
    if not normalized_source or not normalized_target:
        return

    _pending_outbound_injections.setdefault(normalized_source, []).append(normalized_target)


def consume_pending_outbound_injections(source_session_key: str) -> list[str]:
    return _pending_outbound_injections.pop(source_session_key.strip(), [])


def record_runtime_session_skill_load(runtime_session_key: str, skill_name: str) -> bool:
    """Mark a skill as loaded for the session. Returns True only on the first load.

    The returned bool lets the read_file hook (plan P2 #18) emit a `skill.loaded`
    telemetry event exactly once per (session, skill) instead of on every reread.
    """
    normalized_key = runtime_session_key.strip()
    normalized_skill = skill_name.strip()
    if not normalized_key or not normalized_skill:
        return False
    loaded = _runtime_session_loaded_skills.setdefault(normalized_key, set())
    if normalized_skill in loaded:
        return False
    loaded.add(normalized_skill)
    return True


def get_runtime_session_loaded_skills(runtime_session_key: str) -> tuple[str, ...]:
    normalized_key = runtime_session_key.strip()
    if not normalized_key:
        return ()
    return tuple(sorted(_runtime_session_loaded_skills.get(normalized_key, ())))


def split_runtime_session_key(runtime_session_key: str) -> tuple[str, str] | None:
    channel_name, separator, conversation_id = runtime_session_key.strip().partition(_SESSION_KEY_SEPARATOR)
    if not separator or not channel_name or not conversation_id:
        return None

    return channel_name, conversation_id
