"""Authenticated operator control HTTP routes: /control/*."""

from typing import Any, Literal

# Re-exported tool objects and the aiohttp module: tests and external callers
# reach them as attributes of `app.mcp` (e.g. mcp_mod.execute_command,
# monkeypatch on mcp_mod.aiohttp). Keep this surface stable.
import aiohttp as aiohttp  # noqa: E402
from fastapi import HTTPException, Request

from app.core.config import settings
from app.core.telemetry import runtime_telemetry
from app.mcp.auth import (
    _audit_control_action,
    _authorize_control,
    _operator_response,
)
from app.mcp.server import (
    mcp_app,
)
from app.mcp.shared import _display_path
from app.mcp.tools.commands import draft_command as draft_command  # noqa: E402
from app.mcp.tools.commands import execute_command as execute_command  # noqa: E402
from app.mcp.tools.commands import run_approved_command as run_approved_command  # noqa: E402
from app.mcp.tools.fetch import fetch_url as fetch_url  # noqa: E402
from app.mcp.tools.files import find_files as find_files  # noqa: E402
from app.mcp.tools.files import list_files as list_files  # noqa: E402
from app.mcp.tools.files import read_file as read_file  # noqa: E402
from app.mcp.tools.files import replace_file_text as replace_file_text  # noqa: E402
from app.mcp.tools.files import search_file_regex as search_file_regex  # noqa: E402
from app.mcp.tools.files import write_new_file as write_new_file  # noqa: E402
from app.mcp.tools.outbound import commit_outbound_message as commit_outbound_message  # noqa: E402
from app.mcp.tools.outbound import draft_outbound_message as draft_outbound_message  # noqa: E402
from app.mcp.tools.outbound import list_a2a_peers as list_a2a_peers  # noqa: E402
from app.mcp.tools.outbound import request_a2a_response as request_a2a_response  # noqa: E402
from app.mcp.tools.outbound import send_a2a_message as send_a2a_message  # noqa: E402
from app.mcp.tools.outbound import send_file as send_file  # noqa: E402
from app.mcp.tools.outbound import send_message as send_message  # noqa: E402
from app.mcp.tools.planning import _write_planning_artifact
from app.mcp.tools.planning import enter_planning_mode as enter_planning_mode  # noqa: E402
from app.mcp.tools.planning import exit_planning_mode as exit_planning_mode  # noqa: E402
from app.mcp.tools.runtime_info import get_runtime_info as get_runtime_info  # noqa: E402
from app.mcp.tools.tasks import manage_agent_task as manage_agent_task  # noqa: E402
from app.mcp.tools.todo import manage_todo_list as manage_todo_list  # noqa: E402
from app.runtime.approvals import approval_store, outbound_draft_store
from app.runtime.channels import get_channel_plugin, register_channel_conversation
from app.runtime.outbound_dispatch import (
    dispatch_outbound_draft as _dispatch_outbound_draft,
)
from app.runtime.scheduler import task_scheduler
from app.runtime.session_mode import (
    SessionMode,
    get_planning_state,
    get_session_mode,
)
from app.runtime.session_mode import (
    enter_planning_mode as _registry_enter_planning,
)
from app.runtime.session_mode import (
    exit_planning_mode as _registry_exit_planning,
)
from app.schema.control import (
    ApprovalDecision,
    ControlMessageRequest,
    OutboundDraftDecision,
    PlanningModeRequest,
    TaskCreateRequest,
    TaskUpdateRequest,
)
from app.util.clock import utcnow


@mcp_app.post("/control/sessions/{session_id}/clear")
async def clear_control_session(session_id: str, request: Request) -> dict[str, Any]:
    scope = await _authorize_control(request.headers.get("authorization"))
    application_loop = request.app.state.application_loop
    if application_loop is None:
        await _audit_control_action(
            request,
            action="session.clear",
            scope=scope,
            level="warning",
            message="rejected",
            details={"session_id": session_id, "reason": "application_loop_not_running"},
        )
        raise HTTPException(status_code=503, detail="Application loop is not running.")

    try:
        session_key, dropped_message_count = await application_loop.clear_session(session_id)
    except ValueError as exc:
        await _audit_control_action(
            request,
            action="session.clear",
            scope=scope,
            level="warning",
            message="rejected",
            details={"session_id": session_id, "error": str(exc)},
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    details = {
        "session_id": session_key,
        "dropped_pending_messages": dropped_message_count,
    }
    await _audit_control_action(
        request,
        action="session.clear",
        scope=scope,
        message="accepted",
        details=details,
    )
    return _operator_response(
        action="session.clear",
        message=f"Cleared session {session_key}.",
        scope=scope,
        details=details,
    )


@mcp_app.post("/control/sessions/{session_id}/planning-mode")
async def set_session_planning_mode(
    session_id: str,
    payload: PlanningModeRequest,
    request: Request,
) -> dict[str, Any]:
    scope = await _authorize_control(request.headers.get("authorization"))
    application_loop = request.app.state.application_loop
    if application_loop is None:
        await _audit_control_action(
            request,
            action="session.planning-mode",
            scope=scope,
            level="warning",
            message="rejected",
            details={"session_id": session_id, "reason": "application_loop_not_running"},
        )
        raise HTTPException(status_code=503, detail="Application loop is not running.")

    try:
        session_key = application_loop._resolve_session_key(session_id)
    except ValueError as exc:
        await _audit_control_action(
            request,
            action="session.planning-mode",
            scope=scope,
            level="warning",
            message="rejected",
            details={"session_id": session_id, "error": str(exc)},
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if payload.state == "planning":
        state = _registry_enter_planning(
            session_key,
            objective=payload.objective or "",
            scope=payload.scope,
            source="control-api",
        )
        await runtime_telemetry.record_event(
            event_type="session.planning.entered",
            source="control-api",
            message="Session entered planning mode.",
            data={
                "session_key": session_key,
                "objective": state.objective,
                "scope": state.scope,
                "source": state.source,
            },
        )
        details = {
            "session_id": session_key,
            "mode": SessionMode.PLANNING.value,
            "objective": state.objective,
            "scope": state.scope,
        }
        await _audit_control_action(
            request,
            action="session.planning-mode",
            scope=scope,
            message="accepted",
            details=details,
        )
        return _operator_response(
            action="session.planning-mode",
            message=f"Session {session_key} entered planning mode.",
            scope=scope,
            details=details,
        )

    if get_session_mode(session_key) is not SessionMode.PLANNING:
        await _audit_control_action(
            request,
            action="session.planning-mode",
            scope=scope,
            level="warning",
            message="rejected",
            details={"session_id": session_key, "reason": "not_in_planning_mode"},
        )
        raise HTTPException(status_code=409, detail=f"Session {session_key} is not in planning mode.")

    plan_state = get_planning_state(session_key)
    objective = plan_state.objective if plan_state is not None else ""
    plan_scope = plan_state.scope if plan_state is not None else None
    entered_at = plan_state.entered_at if plan_state is not None else utcnow()
    enter_source = plan_state.source if plan_state is not None else "control-api"

    exited_at = utcnow()
    plan_path = await _write_planning_artifact(
        session_key=session_key,
        objective=objective,
        scope=plan_scope,
        plan_summary=payload.plan_summary or "(operator-cleared without plan summary)",
        entered_at=entered_at,
        exited_at=exited_at,
        source="control-api",
    )
    plan_display_path = _display_path(plan_path)

    _registry_exit_planning(session_key)

    await runtime_telemetry.record_event(
        event_type="session.planning.exited",
        source="control-api",
        message="Session exited planning mode.",
        data={
            "session_key": session_key,
            "plan_path": plan_display_path,
            "source": "control-api",
            "entered_by": enter_source,
        },
    )
    details = {
        "session_id": session_key,
        "mode": SessionMode.NORMAL.value,
        "plan_path": plan_display_path,
        "entered_by": enter_source,
    }
    await _audit_control_action(
        request,
        action="session.planning-mode",
        scope=scope,
        message="accepted",
        details=details,
    )
    return _operator_response(
        action="session.planning-mode",
        message=f"Session {session_key} exited planning mode.",
        scope=scope,
        details=details,
    )


@mcp_app.post("/control/messages/send")
async def post_control_message(payload: ControlMessageRequest, request: Request) -> dict[str, Any]:
    scope = await _authorize_control(request.headers.get("authorization"))
    application_loop = request.app.state.application_loop
    channel_plugin = get_channel_plugin(payload.channel, create=True)
    if channel_plugin is None:
        await _audit_control_action(
            request,
            action="message.send",
            scope=scope,
            level="warning",
            message="rejected",
            details={"channel": payload.channel, "reason": "channel_unavailable"},
        )
        raise HTTPException(status_code=404, detail=f"Channel is not enabled or available: {payload.channel}")

    try:
        await channel_plugin.send_message(payload.conversation_id or "", payload.message, metadata=None)
    except Exception as exc:
        await _audit_control_action(
            request,
            action="message.send",
            scope=scope,
            level="error",
            message="failed",
            details={"channel": payload.channel, "conversation_id": payload.conversation_id, "error": str(exc)},
        )
        raise HTTPException(status_code=502, detail="Failed to send outbound control message.") from exc

    if payload.conversation_id:
        register_channel_conversation(payload.channel, payload.conversation_id)
        if application_loop is not None:
            application_loop.track_outbound_conversation(payload.channel, payload.conversation_id)

    details = {
        "channel": payload.channel,
        "conversation_id": payload.conversation_id,
        "chars_sent": len(payload.message),
    }
    await _audit_control_action(
        request,
        action="message.send",
        scope=scope,
        message="accepted",
        details=details,
    )
    return _operator_response(
        action="message.send",
        message="Outbound control message sent.",
        scope=scope,
        details=details,
    )


@mcp_app.post("/control/tasks/{task_id}/enable")
async def enable_control_task(task_id: str, request: Request) -> dict[str, Any]:
    scope = await _authorize_control(request.headers.get("authorization"))
    try:
        result = await task_scheduler.enable_task(task_id)
    except ValueError as exc:
        await _audit_control_action(
            request,
            action="task.enable",
            scope=scope,
            level="warning",
            message="rejected",
            details={"task_id": task_id, "error": str(exc)},
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    task = result["task"]
    details = {"task_id": task_id, "changed": result["changed"], "enabled": task["enabled"]}
    await _audit_control_action(
        request,
        action="task.enable",
        scope=scope,
        message="accepted",
        details=details,
    )
    response_message = f"Enabled task {task_id}." if result["changed"] else f"Task {task_id} is already enabled."
    return _operator_response(
        action="task.enable",
        message=response_message,
        scope=scope,
        details=details,
    )


@mcp_app.post("/control/tasks/{task_id}/disable")
async def disable_control_task(task_id: str, request: Request) -> dict[str, Any]:
    scope = await _authorize_control(request.headers.get("authorization"))
    try:
        result = await task_scheduler.disable_task(task_id)
    except ValueError as exc:
        await _audit_control_action(
            request,
            action="task.disable",
            scope=scope,
            level="warning",
            message="rejected",
            details={"task_id": task_id, "error": str(exc)},
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    task = result["task"]
    details = {"task_id": task_id, "changed": result["changed"], "enabled": task["enabled"]}
    await _audit_control_action(
        request,
        action="task.disable",
        scope=scope,
        message="accepted",
        details=details,
    )
    response_message = f"Disabled task {task_id}." if result["changed"] else f"Task {task_id} is already disabled."
    return _operator_response(
        action="task.disable",
        message=response_message,
        scope=scope,
        details=details,
    )


def _task_not_found(message: str) -> bool:
    return message.startswith("Task not found")


@mcp_app.post("/control/tasks")
async def create_control_task(
    request: Request,
    payload: TaskCreateRequest,
) -> dict[str, Any]:
    scope = await _authorize_control(request.headers.get("authorization"))
    try:
        result = await task_scheduler.create_task(
            name=payload.name,
            prompt=payload.prompt,
            schedule_type=payload.schedule_type,
            cron_expression=payload.cron_expression,
            delay_seconds=payload.delay_seconds,
            timezone_name=payload.timezone_name or settings.TIMEZONE,
            enabled=payload.enabled,
            repeat=payload.repeat,
            clean_session=payload.clean_session,
            goal=payload.goal,
        )
    except ValueError as exc:
        await _audit_control_action(
            request,
            action="task.create",
            scope=scope,
            level="warning",
            message="rejected",
            details={"error": str(exc)},
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    task = result["task"]
    details = {
        "task_id": task["task_id"],
        "name": task["name"],
        "schedule_kind": task["schedule"]["kind"],
        "enabled": task["enabled"],
    }
    await _audit_control_action(
        request,
        action="task.create",
        scope=scope,
        message="accepted",
        details=details,
    )
    return _operator_response(
        action="task.create",
        message=f"Created task {task['task_id']}.",
        scope=scope,
        details={**details, "task": task},
    )


@mcp_app.patch("/control/tasks/{task_id}")
async def update_control_task(
    task_id: str,
    request: Request,
    payload: TaskUpdateRequest,
) -> dict[str, Any]:
    scope = await _authorize_control(request.headers.get("authorization"))
    try:
        result = await task_scheduler.update_task(
            task_id,
            name=payload.name,
            prompt=payload.prompt,
            schedule_type=payload.schedule_type,
            cron_expression=payload.cron_expression,
            delay_seconds=payload.delay_seconds,
            timezone_name=payload.timezone_name,
            enabled=payload.enabled,
            repeat=payload.repeat,
            clean_session=payload.clean_session,
            goal=payload.goal,
            clear_goal=payload.clear_goal,
        )
    except ValueError as exc:
        message = str(exc)
        status_code = 404 if _task_not_found(message) else 400
        await _audit_control_action(
            request,
            action="task.update",
            scope=scope,
            level="warning",
            message="rejected",
            details={"task_id": task_id, "error": message},
        )
        raise HTTPException(status_code=status_code, detail=message) from exc

    task = result["task"]
    details = {
        "task_id": task["task_id"],
        "name": task["name"],
        "revision": task["revision"],
        "enabled": task["enabled"],
        "schedule_kind": task["schedule"]["kind"],
    }
    await _audit_control_action(
        request,
        action="task.update",
        scope=scope,
        message="accepted",
        details=details,
    )
    return _operator_response(
        action="task.update",
        message=f"Updated task {task['task_id']}.",
        scope=scope,
        details={**details, "task": task},
    )


@mcp_app.delete("/control/tasks/{task_id}")
async def delete_control_task(task_id: str, request: Request) -> dict[str, Any]:
    scope = await _authorize_control(request.headers.get("authorization"))
    try:
        result = await task_scheduler.delete_task(task_id)
    except ValueError as exc:
        message = str(exc)
        status_code = 404 if _task_not_found(message) else 400
        await _audit_control_action(
            request,
            action="task.delete",
            scope=scope,
            level="warning",
            message="rejected",
            details={"task_id": task_id, "error": message},
        )
        raise HTTPException(status_code=status_code, detail=message) from exc

    details = {"task_id": task_id, "deleted": bool(result.get("deleted"))}
    await _audit_control_action(
        request,
        action="task.delete",
        scope=scope,
        message="accepted",
        details=details,
    )
    return _operator_response(
        action="task.delete",
        message=f"Deleted task {task_id}.",
        scope=scope,
        details=details,
    )


@mcp_app.post("/control/tasks/{task_id}/run-now")
async def run_control_task_now(task_id: str, request: Request) -> dict[str, Any]:
    scope = await _authorize_control(request.headers.get("authorization"))
    try:
        result = await task_scheduler.run_task_now(task_id)
    except ValueError as exc:
        await _audit_control_action(
            request,
            action="task.run-now",
            scope=scope,
            level="warning",
            message="rejected",
            details={"task_id": task_id, "error": str(exc)},
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    run_result = result["run"]
    details = {
        "task_id": task_id,
        "action_result": run_result["action"],
        "message": run_result["message"],
    }
    await _audit_control_action(
        request,
        action="task.run-now",
        scope=scope,
        message="accepted",
        details=details,
    )
    return _operator_response(
        action="task.run-now",
        message=f"Ran task {task_id} immediately.",
        scope=scope,
        details=details,
    )


async def _decide_approval(
    draft_id: str,
    *,
    request: Request,
    payload: ApprovalDecision,
    decision: Literal["approve", "deny"],
) -> dict[str, Any]:
    scope = await _authorize_control(request.headers.get("authorization"))
    action = f"approvals.{decision}"
    try:
        if decision == "approve":
            record = await approval_store.approve(
                draft_id,
                decided_by=scope.value,
                comment=payload.comment,
            )
        else:
            record = await approval_store.deny(
                draft_id,
                decided_by=scope.value,
                comment=payload.comment,
            )
    except LookupError as exc:
        await _audit_control_action(
            request,
            action=action,
            scope=scope,
            level="warning",
            message="rejected",
            details={"draft_id": draft_id, "reason": "not_found"},
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        await _audit_control_action(
            request,
            action=action,
            scope=scope,
            level="warning",
            message="rejected",
            details={"draft_id": draft_id, "reason": "wrong_state"},
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    details = {
        "draft_id": record.id,
        "status": record.status,
        "command": record.command,
        "comment": payload.comment,
    }
    await _audit_control_action(
        request,
        action=action,
        scope=scope,
        message="accepted",
        details=details,
    )
    return _operator_response(
        action=action,
        message=f"Draft {record.id} {record.status}.",
        scope=scope,
        details=details,
    )


@mcp_app.post("/control/approvals/{draft_id}/approve")
async def approve_command_draft(
    draft_id: str,
    request: Request,
    payload: ApprovalDecision | None = None,
) -> dict[str, Any]:
    return await _decide_approval(
        draft_id,
        request=request,
        payload=payload or ApprovalDecision(),
        decision="approve",
    )


@mcp_app.post("/control/approvals/{draft_id}/deny")
async def deny_command_draft(
    draft_id: str,
    request: Request,
    payload: ApprovalDecision | None = None,
) -> dict[str, Any]:
    return await _decide_approval(
        draft_id,
        request=request,
        payload=payload or ApprovalDecision(),
        decision="deny",
    )


@mcp_app.post("/control/drafts/{draft_id}/commit")
async def commit_outbound_draft_via_control(
    draft_id: str,
    request: Request,
    payload: OutboundDraftDecision | None = None,
) -> dict[str, Any]:
    scope = await _authorize_control(request.headers.get("authorization"))
    decision = payload or OutboundDraftDecision()
    try:
        record = await outbound_draft_store.commit(
            draft_id,
            decided_by=scope.value,
            comment=decision.comment,
        )
    except LookupError as exc:
        await _audit_control_action(
            request,
            action="drafts.commit",
            scope=scope,
            level="warning",
            message="rejected",
            details={"draft_id": draft_id, "reason": "not_found"},
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        await _audit_control_action(
            request,
            action="drafts.commit",
            scope=scope,
            level="warning",
            message="rejected",
            details={"draft_id": draft_id, "reason": "wrong_state"},
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    dispatch_result = await _dispatch_outbound_draft(record, ctx=None)
    dispatch_failed = isinstance(dispatch_result, dict) and dispatch_result.get("status") == "error"

    audit_details = {
        "draft_id": record.id,
        "kind": record.kind.value,
        "channel": record.channel,
        "target": record.target,
        "comment": decision.comment,
        "dispatch_failed": dispatch_failed,
    }
    await _audit_control_action(
        request,
        action="drafts.commit",
        scope=scope,
        level="error" if dispatch_failed else "info",
        message="dispatch_failed" if dispatch_failed else "accepted",
        details=audit_details,
    )

    return _operator_response(
        action="drafts.commit",
        message=(
            f"Draft {record.id} committed but dispatch failed."
            if dispatch_failed
            else f"Draft {record.id} committed and dispatched."
        ),
        scope=scope,
        details={**audit_details, "dispatch_result": dispatch_result},
    )


@mcp_app.post("/control/drafts/{draft_id}/discard")
async def discard_outbound_draft_via_control(
    draft_id: str,
    request: Request,
    payload: OutboundDraftDecision | None = None,
) -> dict[str, Any]:
    scope = await _authorize_control(request.headers.get("authorization"))
    decision = payload or OutboundDraftDecision()
    try:
        record = await outbound_draft_store.discard(
            draft_id,
            decided_by=scope.value,
            comment=decision.comment,
        )
    except LookupError as exc:
        await _audit_control_action(
            request,
            action="drafts.discard",
            scope=scope,
            level="warning",
            message="rejected",
            details={"draft_id": draft_id, "reason": "not_found"},
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        await _audit_control_action(
            request,
            action="drafts.discard",
            scope=scope,
            level="warning",
            message="rejected",
            details={"draft_id": draft_id, "reason": "wrong_state"},
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    details = {
        "draft_id": record.id,
        "kind": record.kind.value,
        "channel": record.channel,
        "comment": decision.comment,
    }
    await _audit_control_action(
        request,
        action="drafts.discard",
        scope=scope,
        message="accepted",
        details=details,
    )
    return _operator_response(
        action="drafts.discard",
        message=f"Draft {record.id} discarded.",
        scope=scope,
        details=details,
    )


@mcp_app.post("/control/runtime/drain")
async def drain_runtime(request: Request) -> dict[str, Any]:
    scope = await _authorize_control(request.headers.get("authorization"))
    application_loop = request.app.state.application_loop
    if application_loop is None:
        await _audit_control_action(
            request,
            action="runtime.drain",
            scope=scope,
            level="warning",
            message="rejected",
            details={"reason": "application_loop_not_running"},
        )
        raise HTTPException(status_code=503, detail="Application loop is not running.")

    accepted = await application_loop.request_drain(reason="control-api")
    details = {"already_draining": not accepted}
    await _audit_control_action(
        request,
        action="runtime.drain",
        scope=scope,
        message="accepted",
        details=details,
    )
    response_message = "Runtime drain requested." if accepted else "Runtime is already draining."
    return _operator_response(
        action="runtime.drain",
        message=response_message,
        scope=scope,
        details=details,
    )


@mcp_app.post("/control/runtime/shutdown")
async def shutdown_runtime(request: Request) -> dict[str, Any]:
    scope = await _authorize_control(request.headers.get("authorization"))
    application_loop = request.app.state.application_loop
    server = request.app.state.uvicorn_server

    if application_loop is None and server is None:
        await _audit_control_action(
            request,
            action="runtime.shutdown",
            scope=scope,
            level="warning",
            message="rejected",
            details={"reason": "runtime_not_running"},
        )
        raise HTTPException(status_code=503, detail="Runtime is not running.")

    accepted = await application_loop.request_shutdown(reason="control-api") if application_loop is not None else True
    if server is not None:
        server.should_exit = True

    details = {
        "already_requested": not accepted,
        "application_loop_bound": application_loop is not None,
        "server_bound": server is not None,
    }
    await _audit_control_action(
        request,
        action="runtime.shutdown",
        scope=scope,
        message="accepted",
        details=details,
    )
    response_message = "Runtime shutdown requested." if accepted else "Runtime shutdown is already requested."
    return _operator_response(
        action="runtime.shutdown",
        message=response_message,
        scope=scope,
        details=details,
    )
