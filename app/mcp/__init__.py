"""
Composition MCP Server
"""

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any, Literal, cast

# Re-exported tool objects and the aiohttp module: tests and external callers
# reach them as attributes of `app.mcp` (e.g. mcp_mod.execute_command,
# monkeypatch on mcp_mod.aiohttp). Keep this surface stable.
import aiohttp as aiohttp  # noqa: E402
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse

from app.core.agent_card import build_extended_agent_card, build_public_agent_card
from app.core.config import settings
from app.core.telemetry import runtime_telemetry
from app.core.url_shortener import local_url_shortener
from app.mcp.auth import (
    _audit_control_action,
    _authorize_a2a,
    _authorize_control,
    _authorize_telemetry,
    _ensure_a2a_discovery_available,
    _operator_response,
)
from app.mcp.server import (
    _mcp_http_app,
    bind_application_loop,
    create_mcp_server,
    mcp,
    mcp_app,
    serve_mcp_server,
    wait_for_server_startup,
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
from app.runtime.channels import describe_channel_telemetry, get_channel_plugin, register_channel_conversation
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
from app.schema.messages import A2AEnvelope
from app.schema.telemetry import ChannelsTelemetrySnapshot
from app.util.clock import utcnow

__all__ = (
    "bind_application_loop",
    "create_mcp_server",
    "mcp",
    "mcp_app",
    "serve_mcp_server",
    "wait_for_server_startup",
)


def _agent_card_response(request: Request, payload: dict[str, Any], *, cache_control: str) -> JSONResponse:
    response_body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    etag = hashlib.sha256(response_body).hexdigest()

    if request.headers.get("if-none-match") == etag:
        return JSONResponse(
            status_code=304,
            content=None,
            headers={
                "Cache-Control": cache_control,
                "ETag": etag,
            },
            media_type="application/a2a+json",
        )

    return JSONResponse(
        content=payload,
        media_type="application/a2a+json",
        headers={
            "Cache-Control": cache_control,
            "ETag": etag,
        },
    )


def _format_sse_event(
    event_payload: dict[str, Any], *, event_name: str = "message", event_id: str | None = None
) -> str:
    lines: list[str] = []
    if event_id:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event_name}")
    lines.append(f"data: {json.dumps(event_payload, separators=(',', ':'))}")
    return "\n".join(lines) + "\n\n"


@mcp_app.get("/health")
async def get_health_status(request: Request) -> dict[str, Any]:
    await _authorize_telemetry(request.headers.get("authorization"))
    return (await runtime_telemetry.build_health_status()).model_dump(mode="json")


@mcp_app.get("/telemetry/runtime")
async def get_runtime_telemetry(request: Request) -> dict[str, Any]:
    await _authorize_telemetry(request.headers.get("authorization"))
    return (await runtime_telemetry.build_runtime_snapshot(mcp_app.state.runtime_auth_configuration)).model_dump(
        mode="json"
    )


@mcp_app.get("/telemetry/channels")
async def get_channel_telemetry(request: Request) -> dict[str, Any]:
    await _authorize_telemetry(request.headers.get("authorization"))
    snapshot = ChannelsTelemetrySnapshot(
        runtime_id=settings.runtime_id,
        enabled_channels=settings.enabled_channels(),
        channels=await describe_channel_telemetry(),
    )
    return snapshot.model_dump(mode="json")


@mcp_app.get("/telemetry/sessions")
async def get_session_telemetry(request: Request) -> dict[str, Any]:
    await _authorize_telemetry(request.headers.get("authorization"))
    return (await runtime_telemetry.build_sessions_snapshot()).model_dump(mode="json")


@mcp_app.get("/telemetry/sessions/{session_key}/history")
async def get_session_history_telemetry(
    request: Request,
    session_key: str,
    limit: int | None = None,
) -> dict[str, Any]:
    await _authorize_telemetry(request.headers.get("authorization"))

    effective_limit = limit if limit is not None else settings.SESSION_HISTORY_PREVIEW_LIMIT
    if effective_limit <= 0:
        raise HTTPException(status_code=400, detail="limit must be greater than 0")
    if effective_limit > settings.SESSION_HISTORY_PREVIEW_LIMIT:
        raise HTTPException(
            status_code=400,
            detail=f"limit must not exceed PB_SESSION_HISTORY_PREVIEW_LIMIT ({settings.SESSION_HISTORY_PREVIEW_LIMIT})",
        )

    try:
        preview = await runtime_telemetry.build_session_history_preview(session_key, limit=effective_limit)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Session not tracked: {session_key}") from exc

    return preview.model_dump(mode="json")


@mcp_app.get("/telemetry/tasks")
async def get_task_telemetry(request: Request) -> dict[str, Any]:
    await _authorize_telemetry(request.headers.get("authorization"))
    return (await runtime_telemetry.build_tasks_snapshot()).model_dump(mode="json")


@mcp_app.get("/telemetry/drafts")
async def get_drafts_telemetry(request: Request, status: str = "pending") -> dict[str, Any]:
    await _authorize_telemetry(request.headers.get("authorization"))
    if status not in ("pending", "all"):
        raise HTTPException(status_code=400, detail="status must be 'pending' or 'all'")

    status_filter = "pending" if status == "pending" else None
    outbound_records, command_records = await asyncio.gather(
        outbound_draft_store.list(status=status_filter),
        approval_store.list(status=status_filter),
    )
    return {
        "runtime_id": settings.runtime_id,
        "status_filter": status,
        "outbound": [record.model_dump(mode="json") for record in outbound_records],
        "command": [record.model_dump(mode="json") for record in command_records],
    }


@mcp_app.get("/telemetry/events")
async def stream_telemetry_events(
    request: Request,
    replay: int = 20,
) -> StreamingResponse:
    await _authorize_telemetry(request.headers.get("authorization"))
    replay = max(0, min(replay, 100))

    async def event_stream():
        queue, replay_events = await runtime_telemetry.subscribe(replay=replay)
        try:
            initial_payload = (
                await runtime_telemetry.build_runtime_snapshot(mcp_app.state.runtime_auth_configuration)
            ).model_dump(mode="json")
            yield _format_sse_event(initial_payload, event_name="runtime.snapshot")

            for event in replay_events:
                yield _format_sse_event(
                    event.model_dump(mode="json"), event_name=event.event_type, event_id=event.event_id
                )

            while True:
                if await request.is_disconnected():
                    break

                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    yield ": keep-alive\n\n"
                    continue

                yield _format_sse_event(
                    event.model_dump(mode="json"), event_name=event.event_type, event_id=event.event_id
                )
        finally:
            await runtime_telemetry.unsubscribe(queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


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


@mcp_app.get("/.well-known/agent-card.json")
async def get_public_agent_card(request: Request) -> JSONResponse:
    _ensure_a2a_discovery_available()
    card = build_public_agent_card()
    return _agent_card_response(
        request,
        card.model_dump(mode="json", by_alias=True, exclude_none=True),
        cache_control="public, max-age=300",
    )


@mcp_app.get("/extendedAgentCard")
async def get_extended_agent_card(request: Request) -> JSONResponse:
    _ensure_a2a_discovery_available()
    await _authorize_a2a(request.headers.get("authorization"))

    card = build_extended_agent_card()
    if card is None:
        raise HTTPException(status_code=404, detail="Extended Agent Card is not enabled on this runtime.")

    return _agent_card_response(
        request,
        card.model_dump(mode="json", by_alias=True, exclude_none=True),
        cache_control="private, max-age=60",
    )


@mcp_app.post("/a2a/messages")
async def post_a2a_message(envelope: A2AEnvelope, request: Request) -> dict[str, Any]:
    await _authorize_a2a(request.headers.get("authorization"))

    try:
        channel_plugin = get_channel_plugin("a2a", create=True)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if channel_plugin is None:
        raise HTTPException(status_code=404, detail="A2A channel is not enabled.")

    enqueue_envelope = getattr(channel_plugin, "enqueue_envelope", None)
    if not callable(enqueue_envelope):
        raise HTTPException(status_code=503, detail="Configured A2A channel does not support HTTP ingress.")

    enqueue_envelope_callable = cast(
        "Callable[..., Awaitable[Any]]",
        enqueue_envelope,
    )

    try:
        inbound_message = await enqueue_envelope_callable(
            envelope,
            client_host=request.client.host if request.client is not None else None,
        )
    except ValueError as exc:
        await runtime_telemetry.record_event(
            event_type="a2a.message.rejected",
            source="a2a-http",
            level="warning",
            message="Rejected inbound A2A envelope.",
            data={
                "sender_runtime_id": envelope.sender_runtime_id,
                "target_runtime_id": envelope.target_runtime_id,
                "conversation_id": envelope.conversation_id,
                "error": str(exc),
            },
        )
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    await runtime_telemetry.record_event(
        event_type="a2a.message.accepted",
        source="a2a-http",
        message="Accepted inbound A2A envelope.",
        data={
            "sender_runtime_id": envelope.sender_runtime_id,
            "target_runtime_id": envelope.target_runtime_id,
            "conversation_id": envelope.conversation_id,
            "local_conversation_id": inbound_message.conversation_id,
            "intent": envelope.intent.value,
            "message_id": envelope.message_id,
        },
    )
    return {
        "ok": True,
        "runtime_id": settings.runtime_id,
        "accepted": True,
        "channel": "a2a",
        "local_conversation_id": inbound_message.conversation_id,
        "message_id": envelope.message_id,
    }


@mcp_app.get(f"{settings.mcp_shortener_route_prefix()}/{{token}}", include_in_schema=False)
async def redirect_short_url(token: str) -> RedirectResponse:
    original_url = await local_url_shortener.resolve(token)
    if original_url is None:
        raise HTTPException(status_code=404, detail="Short URL not found")

    return RedirectResponse(url=original_url, status_code=307)


mcp_app.mount("/", _mcp_http_app)
