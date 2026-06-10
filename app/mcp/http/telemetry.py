"""Read-only telemetry HTTP routes: /health and /telemetry/*."""

import asyncio
import json
from typing import Any

# Re-exported tool objects and the aiohttp module: tests and external callers
# reach them as attributes of `app.mcp` (e.g. mcp_mod.execute_command,
# monkeypatch on mcp_mod.aiohttp). Keep this surface stable.
import aiohttp as aiohttp  # noqa: E402
from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse

from app.core.config import settings
from app.core.telemetry import runtime_telemetry
from app.mcp.auth import (
    _authorize_telemetry,
)
from app.mcp.server import (
    mcp_app,
)
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
from app.mcp.tools.planning import enter_planning_mode as enter_planning_mode  # noqa: E402
from app.mcp.tools.planning import exit_planning_mode as exit_planning_mode  # noqa: E402
from app.mcp.tools.runtime_info import get_runtime_info as get_runtime_info  # noqa: E402
from app.mcp.tools.tasks import manage_agent_task as manage_agent_task  # noqa: E402
from app.mcp.tools.todo import manage_todo_list as manage_todo_list  # noqa: E402
from app.runtime.approvals import approval_store, outbound_draft_store
from app.runtime.channels import describe_channel_telemetry
from app.schema.telemetry import ChannelsTelemetrySnapshot


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
