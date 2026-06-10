"""A2A inbound message route and public/extended Agent Card discovery."""

import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any, cast

# Re-exported tool objects and the aiohttp module: tests and external callers
# reach them as attributes of `app.mcp` (e.g. mcp_mod.execute_command,
# monkeypatch on mcp_mod.aiohttp). Keep this surface stable.
import aiohttp as aiohttp  # noqa: E402
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from app.core.agent_card import build_extended_agent_card, build_public_agent_card
from app.core.config import settings
from app.core.telemetry import runtime_telemetry
from app.mcp.auth import (
    _authorize_a2a,
    _ensure_a2a_discovery_available,
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
from app.runtime.channels import get_channel_plugin
from app.schema.messages import A2AEnvelope


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
