"""Per-runtime-session mode registry (plan P2 #11).

PLANNING gates mutating MCP tools until the model produces a plan artifact and
calls `exit_planning_mode`. The registry is keyed by the runtime session key
(`channel:conversation_id`) so MCP tools can resolve via session_binding without
crossing into ApplicationLoop internals.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

__all__ = (
    "PlanningState",
    "SessionMode",
    "clear_session_mode",
    "enter_planning_mode",
    "exit_planning_mode",
    "get_planning_state",
    "get_session_mode",
)


class SessionMode(StrEnum):
    NORMAL = "normal"
    PLANNING = "planning"


@dataclass(slots=True)
class PlanningState:
    objective: str
    scope: str | None
    entered_at: datetime
    source: str = "model"  # "model" or "control-api"


_session_mode: dict[str, SessionMode] = {}
_planning_state: dict[str, PlanningState] = {}

_PLANNING_BLOCK_REMINDER: Final[str] = (
    "Currently in planning mode. Call exit_planning_mode after producing a plan, "
    "or use one of the read-only tools: list_files, read_file, find_files, "
    "search_file_regex, fetch_url."
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def get_session_mode(session_key: str) -> SessionMode:
    normalized = session_key.strip()
    if not normalized:
        return SessionMode.NORMAL
    return _session_mode.get(normalized, SessionMode.NORMAL)


def get_planning_state(session_key: str) -> PlanningState | None:
    normalized = session_key.strip()
    if not normalized:
        return None
    return _planning_state.get(normalized)


def enter_planning_mode(
    session_key: str,
    *,
    objective: str,
    scope: str | None = None,
    source: str = "model",
) -> PlanningState:
    normalized = session_key.strip()
    if not normalized:
        raise ValueError("session_key must not be empty")
    state = PlanningState(
        objective=objective.strip(),
        scope=(scope.strip() or None) if scope is not None else None,
        entered_at=_utcnow(),
        source=source,
    )
    _session_mode[normalized] = SessionMode.PLANNING
    _planning_state[normalized] = state
    return state


def exit_planning_mode(session_key: str) -> PlanningState | None:
    normalized = session_key.strip()
    if not normalized:
        return None
    _session_mode[normalized] = SessionMode.NORMAL
    return _planning_state.pop(normalized, None)


def clear_session_mode(session_key: str) -> None:
    normalized = session_key.strip()
    if not normalized:
        return
    _session_mode.pop(normalized, None)
    _planning_state.pop(normalized, None)


def planning_block_reminder() -> str:
    return _PLANNING_BLOCK_REMINDER
