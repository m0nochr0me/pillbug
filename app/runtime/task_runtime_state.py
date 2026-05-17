"""Per-task runtime state used by the scheduler's goal contract (plan P2 #12).

While a scheduled task is running, this module tracks the set of tool names the task
has declared `forbidden_actions` for. The MCP layer consults
`task_forbidden_actions_for_session` to gate tools beyond what planning mode covers.
The registry is keyed by `runtime_session_key` (same key used by session_binding)
because the scheduler's session ids resolve to that key once the task's chat session
binds through `bind_mcp_session_to_runtime_session`.
"""

from __future__ import annotations

from threading import Lock

_lock = Lock()
_task_forbidden_actions: dict[str, frozenset[str]] = {}


def set_task_forbidden_actions(session_key: str, forbidden_actions: tuple[str, ...]) -> None:
    normalized = session_key.strip()
    if not normalized:
        return
    cleaned = frozenset(action.strip() for action in forbidden_actions if action and action.strip())
    with _lock:
        if cleaned:
            _task_forbidden_actions[normalized] = cleaned
        else:
            _task_forbidden_actions.pop(normalized, None)


def clear_task_forbidden_actions(session_key: str) -> None:
    normalized = session_key.strip()
    if not normalized:
        return
    with _lock:
        _task_forbidden_actions.pop(normalized, None)


def task_forbidden_actions_for_session(session_key: str | None) -> frozenset[str]:
    if not session_key:
        return frozenset()
    normalized = session_key.strip()
    if not normalized:
        return frozenset()
    with _lock:
        return _task_forbidden_actions.get(normalized, frozenset())
