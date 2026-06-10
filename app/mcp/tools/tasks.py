"""Scheduled agent task MCP tool."""

from typing import Any, Literal

from fastmcp import Context

from app.core.config import settings
from app.mcp.server import (
    mcp,
)
from app.mcp.tools.planning import _enforce_planning_gate
from app.runtime.scheduler import task_scheduler
from app.schema.tasks import AgentTaskGoal
from app.util.tool_result import envelope_error, tool_error


def _build_agent_task_goal(
    *,
    done_condition: str | None,
    validation_prompt: str | None,
    max_steps_per_run: int | None,
    max_cost_per_run_usd: float | None,
    forbidden_actions: list[str] | None,
    progress_log_path: str | None,
) -> AgentTaskGoal | None:
    """Construct an AgentTaskGoal from the manage_agent_task kwargs, or None when all empty."""
    has_any_field = any(
        value is not None and value != []
        for value in (
            done_condition,
            validation_prompt,
            max_steps_per_run,
            max_cost_per_run_usd,
            forbidden_actions,
            progress_log_path,
        )
    )
    if not has_any_field:
        return None
    return AgentTaskGoal(
        done_condition=done_condition,
        validation_prompt=validation_prompt,
        max_steps_per_run=max_steps_per_run,
        max_cost_per_run_usd=max_cost_per_run_usd,
        forbidden_actions=tuple(forbidden_actions or ()),
        progress_log_path=progress_log_path,
    )


@mcp.tool
@envelope_error
async def manage_agent_task(
    action: Literal["list", "get", "create", "update", "delete"],
    task_id: str | None = None,
    name: str | None = None,
    prompt: str | None = None,
    schedule_type: Literal["cron", "delayed"] | None = None,
    cron_expression: str | None = None,
    delay_seconds: int | None = None,
    enabled: bool | None = None,
    repeat: bool | None = None,
    clean_session: bool | None = None,
    done_condition: str | None = None,
    validation_prompt: str | None = None,
    max_steps_per_run: int | None = None,
    max_cost_per_run_usd: float | None = None,
    forbidden_actions: list[str] | None = None,
    progress_log_path: str | None = None,
    clear_goal: bool = False,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """
    Creates, lists, reads, updates, and deletes scheduled background AI tasks.

    Name is required for create and update actions.
    Prompt and schedule parameters are also required for create, while update allows partial updates of these fields.
    Supported schedule_type values are cron and delayed.
    Cron tasks use cron_expression.
    Delayed tasks use delay_seconds. They are one-shot by default and only repeat when repeat=true is explicitly set.
    Tasks run in a clean session by default (no history from previous runs). Set clean_session=false to preserve session history across runs.

    If a scheduled task's prompt instructs the model to send_message / send_a2a_message to a
    channel that is not in PB_OUTBOUND_AUTOSEND_CHANNELS, every cron run will accumulate a
    pending outbound draft that the operator must commit. Configure the autosend allowlist for
    the destination channel if the task is meant to fire-and-forget.
    """

    normalized_action = action.strip().lower()

    if normalized_action in {"create", "update", "delete"}:
        if blocked := _enforce_planning_gate(f"manage_agent_task.{normalized_action}", ctx):
            return blocked

    try:
        if normalized_action == "list":
            return await task_scheduler.list_tasks()

        if normalized_action == "get":
            if not task_id:
                return tool_error("invalid_arguments", "task_id is required for get")
            return await task_scheduler.get_task(task_id)

        if normalized_action == "create":
            if not name or not name.strip():
                return tool_error("invalid_arguments", "name is required for create")
            if not prompt or not prompt.strip():
                return tool_error("invalid_arguments", "prompt is required for create")
            if not schedule_type or not schedule_type.strip():
                return tool_error("invalid_arguments", "schedule_type is required for create")

            goal = _build_agent_task_goal(
                done_condition=done_condition,
                validation_prompt=validation_prompt,
                max_steps_per_run=max_steps_per_run,
                max_cost_per_run_usd=max_cost_per_run_usd,
                forbidden_actions=forbidden_actions,
                progress_log_path=progress_log_path,
            )

            return await task_scheduler.create_task(
                name=name,
                prompt=prompt,
                schedule_type=schedule_type,
                cron_expression=cron_expression,
                delay_seconds=delay_seconds,
                timezone_name=settings.TIMEZONE,
                enabled=enabled if enabled is not None else True,
                repeat=repeat if repeat is not None else False,
                clean_session=clean_session if clean_session is not None else True,
                goal=goal,
            )

        if normalized_action == "update":
            if not task_id:
                return tool_error("invalid_arguments", "task_id is required for update")

            goal = _build_agent_task_goal(
                done_condition=done_condition,
                validation_prompt=validation_prompt,
                max_steps_per_run=max_steps_per_run,
                max_cost_per_run_usd=max_cost_per_run_usd,
                forbidden_actions=forbidden_actions,
                progress_log_path=progress_log_path,
            )

            return await task_scheduler.update_task(
                task_id,
                name=name,
                prompt=prompt,
                schedule_type=schedule_type,
                cron_expression=cron_expression,
                delay_seconds=delay_seconds,
                timezone_name=settings.TIMEZONE,
                enabled=enabled,
                repeat=repeat,
                clean_session=clean_session,
                goal=goal,
                clear_goal=clear_goal,
            )

        if normalized_action == "delete":
            if not task_id:
                return tool_error("invalid_arguments", "task_id is required for delete")
            return await task_scheduler.delete_task(task_id)
    except ValueError as exc:
        # Scheduler raises ValueError for "Task not found" and schedule validation errors;
        # translate "Task not found" to a typed not_found envelope and leave the rest as
        # invalid_arguments so the model can recover.
        message = str(exc)
        if message.startswith("Task not found"):
            return tool_error(
                "not_found",
                message,
                next_valid_actions=("list",),
                details={"task_id": task_id},
            )
        return tool_error("invalid_arguments", message)

    return tool_error(
        "invalid_arguments",
        f"Unsupported action: {action}",
        details={"supported_actions": ["list", "get", "create", "update", "delete"]},
    )
