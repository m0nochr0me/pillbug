"""Planning mode MCP tools and the read-only tool gate."""

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

from fastmcp import Context

from app.core.telemetry import runtime_telemetry
from app.mcp.server import (
    mcp,
)
from app.mcp.shared import (
    _display_path,
    _resolve_runtime_session_key,
    _resolve_workspace_path,
)
from app.runtime.session_mode import (
    SessionMode,
    get_planning_state,
    get_session_mode,
    planning_block_reminder,
)
from app.runtime.session_mode import (
    enter_planning_mode as _registry_enter_planning,
)
from app.runtime.session_mode import (
    exit_planning_mode as _registry_exit_planning,
)
from app.runtime.task_runtime_state import task_forbidden_actions_for_session
from app.util.clock import utcnow
from app.util.tool_result import envelope_error, tool_error
from app.util.workspace import (
    async_write_text_file,
)

_PLANNING_READ_ONLY_TOOLS: tuple[str, ...] = (
    "list_files",
    "read_file",
    "find_files",
    "search_file_regex",
    "fetch_url",
    "exit_planning_mode",
)


def _enforce_planning_gate(tool_name: str, ctx: Context | None) -> dict[str, Any] | None:
    """P2 #11: deny mutating tools while the session is in planning mode.

    Also enforces per-task `goal.forbidden_actions` (plan P2 #12) for scheduled-task
    sessions; both checks share this gate so the call sites stay symmetric.
    """
    runtime_session_key = _resolve_runtime_session_key(ctx)
    if runtime_session_key is None:
        return None
    if get_session_mode(runtime_session_key) is SessionMode.PLANNING:
        return tool_error(
            "denied",
            planning_block_reminder(),
            next_valid_actions=_PLANNING_READ_ONLY_TOOLS,
            details={
                "tool": tool_name,
                "session_key": runtime_session_key,
                "reason": "planning_mode_blocked",
            },
        )
    forbidden = task_forbidden_actions_for_session(runtime_session_key)
    if forbidden and _tool_matches_forbidden_action(tool_name, forbidden):
        return tool_error(
            "denied",
            f"Tool {tool_name!r} is in this task's forbidden_actions list.",
            details={
                "tool": tool_name,
                "session_key": runtime_session_key,
                "reason": "task_forbidden_action",
                "forbidden_actions": sorted(forbidden),
            },
        )
    return None


def _tool_matches_forbidden_action(tool_name: str, forbidden: frozenset[str]) -> bool:
    """Match a tool name against the forbidden_actions set.

    A bare entry like `manage_agent_task` blocks every sub-action; the tool itself
    passes its action-suffixed name (e.g. `manage_agent_task.create`) so we match on
    both the full name and on the part before any `.` boundary.
    """
    if tool_name in forbidden:
        return True
    prefix = tool_name.partition(".")[0]
    return prefix in forbidden


_PLANNING_ARTIFACT_DIRECTORY = "plans/active"


def _planning_artifact_path(session_key: str, timestamp: datetime) -> Path:
    safe_session_key = session_key.replace(":", "__").replace("/", "_") or "session"
    timestamp_label = timestamp.strftime("%Y%m%dT%H%M%SZ")
    return _resolve_workspace_path(f"{_PLANNING_ARTIFACT_DIRECTORY}/{safe_session_key}-{timestamp_label}.md")


def _render_planning_artifact(
    *,
    session_key: str,
    objective: str,
    scope: str | None,
    plan_summary: str,
    entered_at: datetime,
    exited_at: datetime,
    source: str,
) -> str:
    front_matter_lines = [
        "---",
        f"session_key: {session_key}",
        f"source: {source}",
        f"entered_at: {entered_at.isoformat()}",
        f"exited_at: {exited_at.isoformat()}",
        f"objective: {objective}",
    ]
    if scope is not None:
        front_matter_lines.append(f"scope: {scope}")
    front_matter_lines.append("---")
    front_matter_lines.append("")
    return "\n".join(front_matter_lines) + "\n" + plan_summary.rstrip() + "\n"


async def _write_planning_artifact(
    *,
    session_key: str,
    objective: str,
    scope: str | None,
    plan_summary: str,
    entered_at: datetime,
    exited_at: datetime,
    source: str,
) -> Path:
    target_file = _planning_artifact_path(session_key, exited_at)
    await asyncio.to_thread(target_file.parent.mkdir, parents=True, exist_ok=True)
    content = _render_planning_artifact(
        session_key=session_key,
        objective=objective,
        scope=scope,
        plan_summary=plan_summary,
        entered_at=entered_at,
        exited_at=exited_at,
        source=source,
    )
    await async_write_text_file(target_file, content, mode="w")
    return target_file


@mcp.tool
@envelope_error
async def enter_planning_mode(
    objective: str,
    scope: str | None = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """
    Switches the current session into planning mode (plan P2 #11).

    Use this for ambiguous or high-impact requests. While planning, the model can
    still read freely (list_files, read_file, find_files, search_file_regex,
    fetch_url) but the following mutating tools return a denied envelope:
    execute_command, run_approved_command, write_new_file, replace_file_text,
    send_message, send_file, send_a2a_message, request_a2a_response,
    commit_outbound_message, and the create/update/delete actions of
    manage_agent_task. draft_command and draft_outbound_message remain available
    for staging proposals while a plan is being drafted.

    Call exit_planning_mode(plan_summary) to record the plan as an artifact under
    plans/active/ and re-enable mutating tools.
    """

    normalized_objective = objective.strip()
    if not normalized_objective:
        return tool_error("invalid_arguments", "objective must not be empty")

    runtime_session_key = _resolve_runtime_session_key(ctx)
    if runtime_session_key is None:
        return tool_error(
            "permission_denied",
            "Planning mode requires a runtime-session-bound MCP context.",
        )

    state = _registry_enter_planning(
        runtime_session_key,
        objective=normalized_objective,
        scope=scope,
        source="model",
    )

    await runtime_telemetry.record_event(
        event_type="session.planning.entered",
        source="mcp",
        message="Session entered planning mode.",
        data={
            "session_key": runtime_session_key,
            "objective": state.objective,
            "scope": state.scope,
            "source": state.source,
        },
    )

    return {
        "status": "ok",
        "mode": SessionMode.PLANNING.value,
        "session_key": runtime_session_key,
        "objective": state.objective,
        "scope": state.scope,
        "reminder": planning_block_reminder(),
        "next_valid_actions": list(_PLANNING_READ_ONLY_TOOLS),
    }


@mcp.tool
@envelope_error
async def exit_planning_mode(
    plan_summary: str,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """
    Exits planning mode and records the plan as a workspace artifact under
    plans/active/ (plan P2 #11). On success, mutating tools are re-enabled for
    this session.

    The plan_summary should be a concrete plan: ordered steps, concrete commands
    or send targets, and the success criteria. Operators reading plans/active/
    rely on this content to decide whether to let the model proceed.
    """

    normalized_summary = plan_summary.strip()
    if not normalized_summary:
        return tool_error("invalid_arguments", "plan_summary must not be empty")

    runtime_session_key = _resolve_runtime_session_key(ctx)
    if runtime_session_key is None:
        return tool_error(
            "permission_denied",
            "Planning mode requires a runtime-session-bound MCP context.",
        )

    if get_session_mode(runtime_session_key) is not SessionMode.PLANNING:
        return tool_error(
            "conflict",
            "Session is not in planning mode.",
            next_valid_actions=("enter_planning_mode",),
            details={"session_key": runtime_session_key, "mode": SessionMode.NORMAL.value},
        )

    state = get_planning_state(runtime_session_key)
    objective = state.objective if state is not None else ""
    scope = state.scope if state is not None else None
    entered_at = state.entered_at if state is not None else utcnow()
    enter_source = state.source if state is not None else "model"

    exited_at = utcnow()
    plan_path = await _write_planning_artifact(
        session_key=runtime_session_key,
        objective=objective,
        scope=scope,
        plan_summary=normalized_summary,
        entered_at=entered_at,
        exited_at=exited_at,
        source=enter_source,
    )
    plan_display_path = _display_path(plan_path)

    _registry_exit_planning(runtime_session_key)

    await runtime_telemetry.record_event(
        event_type="session.planning.exited",
        source="mcp",
        message="Session exited planning mode.",
        data={
            "session_key": runtime_session_key,
            "plan_path": plan_display_path,
            "source": "model",
        },
    )

    return {
        "status": "ok",
        "mode": SessionMode.NORMAL.value,
        "session_key": runtime_session_key,
        "plan_path": plan_display_path,
        "objective": objective,
        "scope": scope,
    }
