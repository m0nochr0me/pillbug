"""
Composition MCP Server
"""

import asyncio
import hashlib
import json
import mimetypes
import os
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal, cast

import aiohttp
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
from fastmcp import Context, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server import create_proxy
from fastmcp.server.middleware.logging import LoggingMiddleware

from app import __project__, __version__
from app.core.agent_card import build_extended_agent_card, build_public_agent_card
from app.core.config import settings
from app.core.log import logger, uvicorn_log_config
from app.core.telemetry import runtime_telemetry
from app.core.url_shortener import local_url_shortener
from app.middleware.compactor import CompactorMiddleware
from app.runtime.channels import describe_channel_telemetry, get_channel_plugin, register_channel_conversation
from app.runtime.scheduler import task_scheduler
from app.runtime.session_binding import (
    bind_runtime_session_todo_snapshot,
    get_runtime_session_for_mcp_session,
    get_runtime_session_origin_metadata,
    record_pending_outbound_injection,
    split_runtime_session_key,
)
from app.schema.control import (
    AuthScope,
    AuthTokenBinding,
    ControlMessageRequest,
    OperatorResponse,
    RuntimeAuthConfiguration,
)
from app.schema.messages import (
    A2AEnvelope,
    A2ATarget,
    OutboundAttachment,
    build_a2a_origin_routing_metadata,
    extract_a2a_origin_channel_metadata,
    extract_a2a_origin_route,
)
from app.schema.telemetry import ChannelsTelemetrySnapshot, RuntimeMetadata
from app.schema.todo import TodoItem, TodoListSnapshot
from app.util.web import (
    build_fetch_output_path,
    decode_text_payload,
    extract_readable_html,
    looks_like_html,
    looks_like_text,
    render_readable_html_document,
)
from app.util.workspace import (
    async_read_text_file,
    async_write_bytes_file,
    async_write_text_file,
    display_path,
    is_hidden_path,
    resolve_path_within_root,
    truncate_text,
)

__all__ = ("create_mcp_server", "mcp", "mcp_app")

mcp = FastMCP(f"{__project__}-composition-server")
mcp.add_middleware(LoggingMiddleware(include_payloads=True, max_payload_length=1000))

_TODO_LIST_STATE_KEY = "todo_list"


def _display_path(path: Path) -> str:
    return display_path(path, settings.WORKSPACE_ROOT)


def _resolve_workspace_path(path: str | Path) -> Path:
    return resolve_path_within_root(path, settings.WORKSPACE_ROOT)


def _validate_page_size(page_size: int) -> int:
    if page_size < 1:
        raise ValueError("page_size must be at least 1")

    return min(page_size, settings.MCP_MAX_PAGE_SIZE)


def _validate_max_results(max_results: int) -> int:
    if max_results < 1:
        raise ValueError("max_results must be at least 1")

    return min(max_results, settings.MCP_MAX_SEARCH_RESULTS)


def _validate_command_timeout(timeout_seconds: float) -> float:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be greater than 0")

    return min(timeout_seconds, settings.MCP_MAX_COMMAND_TIMEOUT_SECONDS)


def _validate_fetch_url_max_bytes(max_bytes: int) -> int:
    if max_bytes < 1:
        raise ValueError("max_bytes must be at least 1")

    return min(max_bytes, settings.MCP_FETCH_URL_MAX_BYTES)


def _get_command_shell() -> str:
    shell = os.environ.get("SHELL")
    if shell and Path(shell).is_file():
        return shell

    return "/bin/sh"


def _parse_channel_target(channel: str) -> tuple[str, str]:
    channel_name, separator, conversation_id = channel.strip().partition(":")
    if not channel_name:
        raise ValueError("channel must not be empty")

    if not separator:
        return channel_name, ""

    if not conversation_id:
        raise ValueError("channel targets using ':' must include a destination after the channel name")

    return channel_name, conversation_id


def _resolve_a2a_origin_routing_metadata(
    runtime_session_key: str,
    session_origin_metadata: dict[str, object] | None,
) -> dict[str, object] | None:
    if session_origin_metadata is not None and (
        existing_origin_route := extract_a2a_origin_route(session_origin_metadata)
    ):
        return build_a2a_origin_routing_metadata(
            channel_name=existing_origin_route[0],
            conversation_id=existing_origin_route[1],
            channel_metadata=extract_a2a_origin_channel_metadata(session_origin_metadata),
        )

    origin_route = split_runtime_session_key(runtime_session_key)
    if origin_route is None:
        return None

    return build_a2a_origin_routing_metadata(
        channel_name=origin_route[0],
        conversation_id=origin_route[1],
        channel_metadata=session_origin_metadata,
    )


def _get_outbound_a2a_metadata(ctx: Context | None) -> dict[str, object] | None:
    if ctx is None:
        return None

    runtime_session_key = get_runtime_session_for_mcp_session(ctx.session_id)
    if runtime_session_key is None:
        return None

    origin_channel_metadata = get_runtime_session_origin_metadata(runtime_session_key)
    resolved_origin_metadata = _resolve_a2a_origin_routing_metadata(runtime_session_key, origin_channel_metadata)
    if resolved_origin_metadata is None:
        return None

    return dict(resolved_origin_metadata)


def _track_outbound_conversation(channel_name: str, conversation_id: str) -> None:
    if not conversation_id:
        return

    register_channel_conversation(channel_name, conversation_id)
    application_loop = mcp_app.state.application_loop
    if application_loop is not None:
        application_loop.track_outbound_conversation(channel_name, conversation_id)


def _get_a2a_channel_plugin(create: bool = True) -> Any:
    channel_plugin = get_channel_plugin("a2a", create=create)
    if channel_plugin is None:
        raise ValueError("A2A channel is not enabled or available")

    return channel_plugin


def _normalize_a2a_target(target: str) -> str:
    return A2ATarget.parse(target).as_conversation_target()


def _assert_not_echoing_a2a_origin(
    *,
    channel_name: str,
    conversation_id: str,
    ctx: Context | None,
) -> None:
    if ctx is None:
        return

    runtime_session_key = get_runtime_session_for_mcp_session(ctx.session_id)
    if runtime_session_key is None:
        return

    session_route = split_runtime_session_key(runtime_session_key)
    if session_route is None or session_route[0] != "a2a":
        return

    session_origin_metadata = get_runtime_session_origin_metadata(runtime_session_key)
    if session_origin_metadata is None:
        return

    origin_route = extract_a2a_origin_route(session_origin_metadata)
    if origin_route is None:
        return

    if (channel_name, conversation_id) != origin_route:
        return

    raise ValueError(
        "This local A2A session already knows how to reach the preserved origin channel. "
        "Reply normally in the current session instead of using send_message to echo the result manually."
    )


async def _get_todo_snapshot(ctx: Context) -> TodoListSnapshot:
    state = await ctx.get_state(_TODO_LIST_STATE_KEY)
    if state is None:
        _sync_todo_snapshot_to_runtime_session(ctx, None)
        return TodoListSnapshot()

    snapshot = TodoListSnapshot.model_validate(state)
    _sync_todo_snapshot_to_runtime_session(ctx, snapshot)
    return snapshot


def _sync_todo_snapshot_to_runtime_session(ctx: Context | None, snapshot: TodoListSnapshot | None) -> None:
    if ctx is None:
        return

    runtime_session_key = get_runtime_session_for_mcp_session(ctx.session_id)
    if runtime_session_key is None:
        return

    bind_runtime_session_todo_snapshot(runtime_session_key, snapshot)


def _serialize_todo_snapshot(action: str, snapshot: TodoListSnapshot) -> dict[str, Any]:
    return {
        "action": action,
        "items": [item.model_dump(mode="json") for item in snapshot.items],
        "total": len(snapshot.items),
        "counts": snapshot.counts,
        "explanation": snapshot.explanation,
        "updated_at": snapshot.updated_at.isoformat(),
    }


def _build_runtime_metadata() -> RuntimeMetadata:
    return runtime_telemetry.metadata()


def _build_runtime_auth_configuration() -> RuntimeAuthConfiguration:
    token_bindings: list[AuthTokenBinding] = []
    dashboard_token = settings.dashboard_bearer_token()
    a2a_token = settings.a2a_bearer_token()

    if dashboard_token is not None:
        token_bindings.append(
            AuthTokenBinding(
                token_name="dashboard-bearer",
                principal="dashboard",
                scopes=(AuthScope.TELEMETRY, AuthScope.CONTROL),
            )
        )

    if a2a_token is not None:
        token_bindings.append(
            AuthTokenBinding(
                token_name="a2a-bearer",
                principal="a2a",
                scopes=(AuthScope.A2A,),
            )
        )

    return RuntimeAuthConfiguration(
        token_bindings=tuple(token_bindings),
        telemetry_protected=dashboard_token is not None,
        control_protected=True,
        a2a_protected=a2a_token is not None,
    )


def _extract_bearer_token(authorization: str | None) -> str | None:
    if authorization is None:
        return None

    scheme, _, token = authorization.strip().partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None

    return token.strip() or None


async def _authorize_telemetry(authorization: str | None) -> AuthScope | None:
    expected_token = settings.dashboard_bearer_token()
    if expected_token is None:
        return None

    presented_token = _extract_bearer_token(authorization)
    if presented_token is None or not secrets.compare_digest(presented_token, expected_token):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return AuthScope.TELEMETRY


async def _authorize_control(authorization: str | None) -> AuthScope:
    expected_token = settings.dashboard_bearer_token()
    if expected_token is None:
        raise HTTPException(
            status_code=503,
            detail="Control API requires PB_DASHBOARD_BEARER_TOKEN to be configured.",
        )

    presented_token = _extract_bearer_token(authorization)
    if presented_token is None or not secrets.compare_digest(presented_token, expected_token):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return AuthScope.CONTROL


async def _authorize_a2a(authorization: str | None) -> AuthScope:
    expected_token = settings.a2a_bearer_token()
    if expected_token is None:
        return AuthScope.A2A

    presented_token = _extract_bearer_token(authorization)
    if presented_token is None or not secrets.compare_digest(presented_token, expected_token):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return AuthScope.A2A


def _ensure_a2a_discovery_available() -> None:
    if "a2a" not in settings.enabled_channels():
        raise HTTPException(status_code=404, detail="A2A discovery is not enabled on this runtime.")


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


def _operator_response(
    *,
    action: str,
    message: str,
    scope: AuthScope,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return OperatorResponse(
        runtime_id=settings.runtime_id,
        ok=True,
        action=action,
        message=message,
        scope=scope,
        details=details,
    ).model_dump(mode="json")


async def _audit_control_action(
    request: Request,
    *,
    action: str,
    scope: AuthScope,
    message: str,
    level: Literal["info", "warning", "error"] = "info",
    details: dict[str, Any] | None = None,
) -> None:
    payload = {
        "runtime_id": settings.runtime_id,
        "scope": scope.value,
        "action": action,
        "path": str(request.url.path),
        "client_host": request.client.host if request.client is not None else None,
        **{key: value for key, value in (details or {}).items() if value is not None},
    }
    rendered_payload = json.dumps(payload, sort_keys=True, default=str)

    if level == "error":
        logger.error(f"Control action {message} {rendered_payload}")
    elif level == "warning":
        logger.warning(f"Control action {message} {rendered_payload}")
    else:
        logger.info(f"Control action {message} {rendered_payload}")

    await runtime_telemetry.record_event(
        event_type=f"control.{action}",
        source="control-api",
        level=level,
        message=message,
        data=payload,
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


@mcp.tool
def get_runtime_info() -> dict[str, Any]:
    """
    Provides a runtime info
    """
    return _build_runtime_metadata().model_dump(mode="json")


@mcp.tool
async def list_files(
    directory: str = ".",
    include_hidden: bool = False,
) -> dict[str, Any]:
    """
    Lists files and directories directly under a workspace-relative directory.
    """

    target_directory = _resolve_workspace_path(directory)

    if not await asyncio.to_thread(target_directory.exists):
        raise ValueError(f"Directory does not exist: {directory}")

    if not await asyncio.to_thread(target_directory.is_dir):
        raise ValueError(f"Path is not a directory: {directory}")

    def build_entries() -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []

        for entry in sorted(target_directory.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
            relative_path = entry.relative_to(settings.WORKSPACE_ROOT)
            if not include_hidden and is_hidden_path(relative_path):
                continue

            entry_type = "directory" if entry.is_dir() else "file"
            entries.append(
                {
                    "name": entry.name,
                    "path": _display_path(entry),
                    "type": entry_type,
                    "size": entry.stat().st_size if entry.is_file() else None,
                },
            )

        return entries

    entries = await asyncio.to_thread(build_entries)
    logger.debug(f"Listed {len(entries)} entries in {_display_path(target_directory)}")

    return {
        "directory": _display_path(target_directory),
        "entries": entries,
        "count": len(entries),
    }


@mcp.tool
async def read_file(
    path: str,
    start_line: int = 1,
    page_size: int = settings.MCP_DEFAULT_PAGE_SIZE,
) -> dict[str, Any]:
    """
    Reads a UTF-8 text file from the workspace with line-based pagination.
    """

    if start_line < 1:
        raise ValueError("start_line must be at least 1")

    page_size = _validate_page_size(page_size)
    target_file = _resolve_workspace_path(path)

    if not await asyncio.to_thread(target_file.exists):
        raise ValueError(f"File does not exist: {path}")

    if not await asyncio.to_thread(target_file.is_file):
        raise ValueError(f"Path is not a file: {path}")

    content = await async_read_text_file(target_file)
    lines = content.splitlines(keepends=True)
    total_lines = len(lines)
    start_index = min(start_line - 1, total_lines)
    page_lines = lines[start_index : start_index + page_size]
    end_line = start_index + len(page_lines)

    logger.debug(f"Read {len(page_lines)} lines from {_display_path(target_file)} starting at line {start_line}")

    return {
        "path": _display_path(target_file),
        "start_line": start_line,
        "end_line": end_line,
        "page_size": page_size,
        "total_lines": total_lines,
        "has_more": end_line < total_lines,
        "content": "".join(page_lines),
    }


@mcp.tool
async def write_new_file(
    path: str,
    content: str,
    make_parents: bool = True,
) -> dict[str, Any]:
    """
    Creates a new UTF-8 text file in the workspace and fails if it already exists.
    """

    target_file = _resolve_workspace_path(path)

    if await asyncio.to_thread(target_file.exists):
        raise ValueError(f"File already exists: {path}")

    if make_parents:
        await asyncio.to_thread(target_file.parent.mkdir, parents=True, exist_ok=True)
    elif not await asyncio.to_thread(target_file.parent.exists):
        raise ValueError(f"Parent directory does not exist: {_display_path(target_file.parent)}")

    chars_written = await async_write_text_file(target_file, content, mode="x")
    logger.info(f"Created file {_display_path(target_file)}")

    return {
        "path": _display_path(target_file),
        "chars_written": chars_written,
    }


@mcp.tool
async def replace_file_text(
    path: str,
    old_text: str,
    new_text: str,
    replace_all: bool = False,
    expected_occurrences: int | None = None,
) -> dict[str, Any]:
    """
    Replaces literal text inside an existing UTF-8 text file.
    """

    if not old_text:
        raise ValueError("old_text must not be empty")

    target_file = _resolve_workspace_path(path)

    if not await asyncio.to_thread(target_file.exists):
        raise ValueError(f"File does not exist: {path}")

    if not await asyncio.to_thread(target_file.is_file):
        raise ValueError(f"Path is not a file: {path}")

    content = await async_read_text_file(target_file)
    occurrences = content.count(old_text)

    if occurrences == 0:
        raise ValueError("old_text was not found in the file")

    if expected_occurrences is not None and occurrences != expected_occurrences:
        raise ValueError(f"Expected {expected_occurrences} occurrences of old_text, but found {occurrences}")

    replacement_count = occurrences if replace_all else 1
    updated_content = content.replace(old_text, new_text, replacement_count)
    await async_write_text_file(target_file, updated_content, mode="w")
    logger.info(f"Replaced {replacement_count} occurrence(s) in {_display_path(target_file)}")

    return {
        "path": _display_path(target_file),
        "occurrences_found": occurrences,
        "occurrences_replaced": replacement_count,
    }


@mcp.tool
async def search_file_regex(
    path: str,
    pattern: str,
    max_results: int = 50,
) -> dict[str, Any]:
    """
    Searches a UTF-8 text file line by line using a regular expression.
    """

    target_file = _resolve_workspace_path(path)
    max_results = _validate_max_results(max_results)

    if not await asyncio.to_thread(target_file.exists):
        raise ValueError(f"File does not exist: {path}")

    if not await asyncio.to_thread(target_file.is_file):
        raise ValueError(f"Path is not a file: {path}")

    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"Invalid regular expression: {exc}") from exc

    content = await async_read_text_file(target_file)
    matches: list[dict[str, Any]] = []
    truncated = False

    for line_number, line in enumerate(content.splitlines(), start=1):
        for match in regex.finditer(line):
            matches.append(
                {
                    "line": line_number,
                    "start_column": match.start() + 1,
                    "end_column": match.end(),
                    "match": match.group(0),
                    "line_text": line,
                },
            )

            if len(matches) >= max_results:
                truncated = True
                break

        if truncated:
            break

    logger.debug(f"Found {len(matches)} regex matches in {_display_path(target_file)}")

    return {
        "path": _display_path(target_file),
        "pattern": pattern,
        "matches": matches,
        "count": len(matches),
        "truncated": truncated,
    }


@mcp.tool
async def find_files(
    pattern: str,
    include_hidden: bool = False,
) -> dict[str, Any]:
    """
    Finds workspace files by glob pattern relative to the workspace root.
    """

    def run_glob() -> list[str]:
        matches: list[str] = []

        for candidate in sorted(settings.WORKSPACE_ROOT.glob(pattern)):
            if not candidate.is_file():
                continue

            relative_path = candidate.relative_to(settings.WORKSPACE_ROOT)
            if not include_hidden and is_hidden_path(relative_path):
                continue

            matches.append(str(relative_path))

        return matches

    matches = await asyncio.to_thread(run_glob)
    logger.debug(f"Glob pattern {pattern} matched {len(matches)} files")

    return {
        "pattern": pattern,
        "matches": matches,
        "count": len(matches),
    }


@mcp.tool
async def send_message(
    channel: str,
    message: str,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """
    Sends a direct outbound message to a configured channel.

    Use this when the agent needs to initiate or continue a message outside the normal reply path,
    such as proactive follow-ups from a scheduled task or notifying a non-A2A channel.
    Do not use this to answer the current inbound message on the same channel during the active turn;
    the application loop will send that response automatically.

    Do not use this tool for cross-runtime A2A communication.
    Use send_a2a_message for asynchronous A2A handoffs and request_a2a_response when you need a peer reply in the same turn.

    The channel argument accepts either a bare channel name for default destinations such as cli,
    or a session-style target in the form channel_name:conversation_id such as telegram:123456789.
    """

    if not message.strip():
        raise ValueError("message must not be empty")

    channel_name, conversation_id = _parse_channel_target(channel)
    _assert_not_echoing_a2a_origin(
        channel_name=channel_name,
        conversation_id=conversation_id,
        ctx=ctx,
    )
    channel_plugin = get_channel_plugin(channel_name, create=True)
    if channel_plugin is None:
        raise ValueError(f"Channel is not enabled or available: {channel_name}")

    await channel_plugin.send_message(conversation_id, message, metadata=None)
    _track_outbound_conversation(channel_name, conversation_id)

    if settings.SESSION_CONTINUITY and ctx is not None:
        source_session_key = get_runtime_session_for_mcp_session(ctx.session_id)
        target_session_key = f"{channel_name}:{conversation_id}" if conversation_id else channel_name
        if source_session_key and source_session_key != target_session_key:
            record_pending_outbound_injection(source_session_key, target_session_key)

    logger.info(f"Sent outbound message via channel={channel_name} destination={conversation_id or '<default>'}")

    return {
        "channel": channel_name,
        "conversation_id": conversation_id or None,
        "chars_sent": len(message),
    }


@mcp.tool
async def send_file(
    channel: str,
    path: str,
    caption: str | None = None,
    send_as: str | None = None,
) -> dict[str, Any]:
    """
    Sends a workspace file as an attachment to a configured channel.

    Use this to deliver files from the workspace to a user on a specific channel.
    The channel argument accepts the same format as send_message:
    a bare channel name like cli, or a session-style target like telegram:123456789.

    The path argument should be a workspace-relative or absolute path to the file.
    The optional send_as argument hints how the channel should deliver the file:
    voice (Telegram voice message, best with .ogg opus files),
    audio (music/audio player), photo, video, or document (default, any file type).
    If omitted, the channel infers the delivery method from the file MIME type.
    """

    channel_name, conversation_id = _parse_channel_target(channel)
    channel_plugin = get_channel_plugin(channel_name, create=True)
    if channel_plugin is None:
        raise ValueError(f"Channel is not enabled or available: {channel_name}")

    resolved_path = _resolve_workspace_path(path)
    if not resolved_path.is_file():
        raise ValueError(f"File not found in workspace: {_display_path(resolved_path)}")

    mime_type = mimetypes.guess_type(resolved_path.name)[0]
    attachment = OutboundAttachment(
        path=str(resolved_path),
        mime_type=mime_type,
        display_name=caption,
        send_as=send_as,
    )

    await channel_plugin.send_message(
        conversation_id,
        caption or "",
        attachments=(attachment,),
    )

    logger.info(
        f"Sent file via channel={channel_name} destination={conversation_id or '<default>'} path={_display_path(resolved_path)}"
    )

    return {
        "channel": channel_name,
        "conversation_id": conversation_id or None,
        "file": _display_path(resolved_path),
        "send_as": send_as or "auto",
    }


@mcp.tool
async def send_a2a_message(
    target: str,
    message: str,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """
    Sends an asynchronous A2A message to another runtime and returns immediately.

    Use this when another runtime should continue work in the background and you will handle the result later.
    The target may use runtime_id/conversation_id or a2a:runtime_id/conversation_id such as runtime-b/deploy-42
    or a2a:runtime-b/deploy-42.

    If you later receive a terminal A2A result in a local a2a session, answer normally in that session when you want
    to respond to the original requester. The runtime will route that final response back to the preserved origin channel.
    """

    if not message.strip():
        raise ValueError("message must not be empty")

    normalized_target = _normalize_a2a_target(target)
    channel_plugin = _get_a2a_channel_plugin(create=True)
    metadata = _get_outbound_a2a_metadata(ctx)

    await channel_plugin.send_message(normalized_target, message, metadata=metadata)
    _track_outbound_conversation("a2a", normalized_target)

    return {
        "channel": "a2a",
        "mode": "async",
        "target": normalized_target,
        "chars_sent": len(message),
    }


@mcp.tool
async def request_a2a_response(
    target: str,
    message: str,
    timeout_seconds: float = 60.0,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """
    Sends a synchronous A2A request to another runtime and waits for its terminal reply.

    Use this when you need the peer runtime's answer before you continue the current turn.
    The target may use runtime_id/conversation_id or a2a:runtime_id/conversation_id such as runtime-b/deploy-42
    or a2a:runtime-b/deploy-42.
    The returned response_text can be used directly in your final answer or incorporated into a larger response.
    """

    if not message.strip():
        raise ValueError("message must not be empty")

    normalized_target = _normalize_a2a_target(target)
    timeout_seconds = _validate_command_timeout(timeout_seconds)
    channel_plugin = _get_a2a_channel_plugin(create=True)
    send_request = getattr(channel_plugin, "send_request", None)
    if not callable(send_request):
        raise ValueError("Configured A2A channel does not support synchronous requests")

    send_request_callable = cast("Callable[..., Awaitable[A2AEnvelope]]", send_request)

    metadata = _get_outbound_a2a_metadata(ctx)
    response_envelope = await send_request_callable(
        normalized_target,
        message,
        metadata=metadata,
        timeout_seconds=timeout_seconds,
    )

    return {
        "channel": "a2a",
        "mode": "sync",
        "target": normalized_target,
        "sender_runtime_id": response_envelope.sender_runtime_id,
        "conversation_id": response_envelope.conversation_id,
        "intent": response_envelope.intent.value,
        "response_text": response_envelope.text,
        "message_id": response_envelope.message_id,
        "reply_to_message_id": response_envelope.reply_to_message_id,
        "hop_count": response_envelope.convergence_state.hop_count,
        "max_hops": response_envelope.convergence_state.max_hops,
        "stop_requested": response_envelope.convergence_state.stop_requested,
    }


_peer_card_cache: dict[str, tuple[float, dict[str, Any]]] = {}


async def _fetch_agent_card(base_url: str, timeout_seconds: float) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/.well-known/agent-card.json"
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    async with aiohttp.ClientSession(timeout=timeout) as session, session.get(url) as response:
        response.raise_for_status()
        return await response.json(content_type=None)


async def _get_cached_peer_card(
    runtime_id: str,
    base_url: str,
    *,
    timeout_seconds: float,
    force_refresh: bool,
) -> tuple[dict[str, Any] | None, str | None]:
    now = time.monotonic()
    cached = _peer_card_cache.get(runtime_id)
    if not force_refresh and cached is not None:
        fetched_at, card = cached
        if now - fetched_at < settings.A2A_PEER_CARD_CACHE_TTL_SECONDS:
            return card, None

    try:
        card = await _fetch_agent_card(base_url, timeout_seconds)
    except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError) as exc:
        return None, f"{type(exc).__name__}: {exc}"

    _peer_card_cache[runtime_id] = (now, card)
    return card, None


@mcp.tool
async def list_a2a_peers(
    fetch_cards: bool = True,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """
    Lists A2A peer runtimes configured via PB_A2A_PEERS_JSON, optionally fetching each peer's
    public agent card so you can see which skills it advertises.

    Use this to discover which peer runtime_id to target with send_a2a_message or
    request_a2a_response, and to verify a peer is reachable before delegating work. Cards are
    cached for PB_A2A_PEER_CARD_CACHE_TTL_SECONDS; set force_refresh=True to bypass the cache.
    """
    channel_plugin = _get_a2a_channel_plugin(create=True)
    instruction_context = channel_plugin.instruction_context() or {}
    peers = list(instruction_context.get("peers", ()))
    timeout_seconds = settings.A2A_PEER_CARD_FETCH_TIMEOUT_SECONDS

    results: list[dict[str, Any]] = []
    for peer in peers:
        runtime_id = peer.get("runtime_id")
        base_url = peer.get("base_url")
        entry: dict[str, Any] = {
            "runtime_id": runtime_id,
            "base_url": base_url,
            "send_target": peer.get("send_target"),
            "agent_card_url": peer.get("agent_card_url"),
        }
        if fetch_cards and runtime_id and base_url:
            card, error = await _get_cached_peer_card(
                runtime_id,
                base_url,
                timeout_seconds=timeout_seconds,
                force_refresh=force_refresh,
            )
            if card is not None:
                entry["card"] = {
                    "name": card.get("name"),
                    "description": card.get("description"),
                    "version": card.get("version"),
                    "skills": [
                        {
                            "id": skill.get("id"),
                            "name": skill.get("name"),
                            "description": skill.get("description"),
                            "tags": skill.get("tags"),
                        }
                        for skill in (card.get("skills") or [])
                    ],
                }
                entry["health"] = "ok"
            else:
                entry["health"] = "unreachable"
                entry["error"] = error
        results.append(entry)

    return {
        "count": len(results),
        "peers": results,
        "cache_ttl_seconds": settings.A2A_PEER_CARD_CACHE_TTL_SECONDS,
    }


@mcp.tool
async def manage_todo_list(
    action: Literal["get", "set", "clear"] = "get",
    todo_list: list[TodoItem] | None = None,
    explanation: str | None = None,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """
    Stores and retrieves a session-scoped todo list for multi-step work.

    Use get to inspect the current plan, set to replace the full plan, and clear to remove it.
    Todo lists may contain at most one in-progress item at a time.
    """

    normalized_action = action.strip().lower()

    if normalized_action == "get":
        snapshot = await _get_todo_snapshot(ctx)
        return _serialize_todo_snapshot("get", snapshot)

    if normalized_action == "clear":
        await ctx.delete_state(_TODO_LIST_STATE_KEY)
        _sync_todo_snapshot_to_runtime_session(ctx, None)
        return _serialize_todo_snapshot("clear", TodoListSnapshot())

    if normalized_action == "set":
        if todo_list is None:
            raise ValueError("todo_list is required for set")

        snapshot = TodoListSnapshot(items=todo_list, explanation=explanation)
        await ctx.set_state(_TODO_LIST_STATE_KEY, snapshot.model_dump(mode="json"))
        _sync_todo_snapshot_to_runtime_session(ctx, snapshot)
        logger.debug(f"Updated todo list with {len(snapshot.items)} items")
        return _serialize_todo_snapshot("set", snapshot)

    raise ValueError(f"Unsupported action: {action}")


def _build_command_environment(ctx: Context | None) -> dict[str, str]:
    environment = dict(os.environ)
    environment["PB_RUNTIME_ID"] = settings.runtime_id
    environment["PB_WORKSPACE_ROOT"] = str(settings.WORKSPACE_ROOT)

    if ctx is not None:
        runtime_session_key = get_runtime_session_for_mcp_session(ctx.session_id)
        if runtime_session_key:
            environment["PB_SESSION_KEY"] = runtime_session_key
            environment["PB_SESSION_KEY_SAFE"] = runtime_session_key.replace(":", "__")

    return environment


@mcp.tool
async def execute_command(
    command: str,
    directory: str = ".",
    timeout_seconds: float = settings.MCP_DEFAULT_COMMAND_TIMEOUT_SECONDS,
    ctx: Context = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """
    Executes a shell command inside the workspace and returns its captured output.

    The subprocess environment is augmented with PB_RUNTIME_ID, PB_WORKSPACE_ROOT, and, when the
    caller is bound to a runtime session, PB_SESSION_KEY (channel:conversation:user) and
    PB_SESSION_KEY_SAFE (filesystem-safe form with ':' replaced by '__'). Helper scripts can
    read these via os.environ to locate per-session state without re-deriving identity.
    """

    if not command.strip():
        raise ValueError("command must not be empty")

    timeout_seconds = _validate_command_timeout(timeout_seconds)
    target_directory = _resolve_workspace_path(directory)

    if not await asyncio.to_thread(target_directory.exists):
        raise ValueError(f"Directory does not exist: {directory}")

    if not await asyncio.to_thread(target_directory.is_dir):
        raise ValueError(f"Path is not a directory: {directory}")

    shell = _get_command_shell()
    environment = _build_command_environment(ctx)

    process = await asyncio.create_subprocess_shell(
        command,
        cwd=str(target_directory),
        executable=shell,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=environment,
    )

    timed_out = False

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
    except TimeoutError:
        timed_out = True
        process.kill()
        stdout_bytes, stderr_bytes = await process.communicate()

    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    combined_output, combined_truncated = truncate_text(stdout + stderr, settings.MCP_MAX_COMMAND_OUTPUT_CHARS)
    stdout, stdout_truncated = truncate_text(stdout, settings.MCP_MAX_COMMAND_OUTPUT_CHARS)
    stderr, stderr_truncated = truncate_text(stderr, settings.MCP_MAX_COMMAND_OUTPUT_CHARS)

    logger.info(f"Executed command in {_display_path(target_directory)} with exit code {process.returncode}: {command}")

    return {
        "command": command,
        "directory": _display_path(target_directory),
        "shell": shell,
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "stdout": stdout,
        "stderr": stderr,
        "combined_output": combined_output,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "combined_output_truncated": combined_truncated,
    }


@mcp.tool
async def fetch_url(
    url: str,
    output_path: str | None = None,
    max_bytes: int = settings.MCP_FETCH_URL_MAX_BYTES,
    timeout_seconds: float = settings.MCP_FETCH_URL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """
    Fetches a remote resource with aiohttp, saves it into the workspace, and returns the saved file path.

    HTML responses are converted into a reduced reading-mode markdown document before saving.
    """

    normalized_url = url.strip()
    if not normalized_url:
        raise ValueError("url must not be empty")

    max_bytes = _validate_fetch_url_max_bytes(max_bytes)
    timeout_seconds = _validate_command_timeout(timeout_seconds)

    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "User-Agent": f"{__project__}/{__version__}",
    }

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        try:
            async with session.get(normalized_url, allow_redirects=True) as response:
                response.raise_for_status()

                if response.content_length is not None and response.content_length > max_bytes:
                    raise ValueError(
                        f"Resource size {response.content_length} bytes exceeds the configured limit of {max_bytes} bytes"
                    )

                payload = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    payload.extend(chunk)
                    if len(payload) > max_bytes:
                        raise ValueError(
                            f"Resource exceeded the configured limit of {max_bytes} bytes while downloading"
                        )

                final_url = str(response.url)
                content_type = response.content_type.lower() if response.content_type else "application/octet-stream"
                charset = response.charset
                status_code = response.status
        except aiohttp.ClientError as exc:
            raise ValueError(f"Unable to fetch URL: {exc}") from exc

    shortened_urls = await local_url_shortener.shorten_many((normalized_url, final_url))
    readable_html = looks_like_html(content_type, final_url)

    if output_path is not None:
        target_file = _resolve_workspace_path(output_path)
        if await asyncio.to_thread(target_file.exists) and not await asyncio.to_thread(target_file.is_file):
            raise ValueError(f"Path is not a file: {output_path}")
    else:
        target_file = build_fetch_output_path(
            final_url,
            content_type,
            _resolve_workspace_path(settings.MCP_FETCH_URL_OUTPUT_DIR),
            readable_html=readable_html,
        )

    await asyncio.to_thread(target_file.parent.mkdir, parents=True, exist_ok=True)

    if readable_html:
        title, readable_text = await extract_readable_html(bytes(payload), final_url, charset)
        stored_content = render_readable_html_document(
            title,
            shortened_urls.get(final_url, final_url),
            readable_text,
        )
        stored_bytes = len(stored_content.encode("utf-8"))
        await async_write_text_file(target_file, stored_content, mode="w")
        content_mode = "readable-html"
    elif looks_like_text(content_type, final_url):
        text_content = decode_text_payload(bytes(payload), charset)
        stored_bytes = len(text_content.encode("utf-8"))
        await async_write_text_file(target_file, text_content, mode="w")
        content_mode = "text"
    else:
        stored_bytes = await async_write_bytes_file(target_file, bytes(payload))
        content_mode = "binary"

    logger.info(f"Fetched URL {normalized_url} into {_display_path(target_file)}")

    return {
        "url": normalized_url,
        "short_url": shortened_urls.get(normalized_url, normalized_url),
        "final_url": final_url,
        "final_short_url": shortened_urls.get(final_url, final_url),
        "path": _display_path(target_file),
        "content_type": content_type,
        "content_mode": content_mode,
        "status_code": status_code,
        "bytes_downloaded": len(payload),
        "bytes_saved": stored_bytes,
        "max_bytes": max_bytes,
    }


@mcp.tool
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
) -> dict[str, Any]:
    """
    Creates, lists, reads, updates, and deletes scheduled background AI tasks.

    Name is required for create and update actions.
    Prompt and schedule parameters are also required for create, while update allows partial updates of these fields.
    Supported schedule_type values are cron and delayed.
    Cron tasks use cron_expression.
    Delayed tasks use delay_seconds. They are one-shot by default and only repeat when repeat=true is explicitly set.
    Tasks run in a clean session by default (no history from previous runs). Set clean_session=false to preserve session history across runs.
    """

    normalized_action = action.strip().lower()

    if normalized_action == "list":
        return await task_scheduler.list_tasks()

    if normalized_action == "get":
        if not task_id:
            raise ValueError("task_id is required for get")
        return await task_scheduler.get_task(task_id)

    if normalized_action == "create":
        if not name or not name.strip():
            raise ValueError("name is required for create")
        if not prompt or not prompt.strip():
            raise ValueError("prompt is required for create")
        if not schedule_type or not schedule_type.strip():
            raise ValueError("schedule_type is required for create")

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
        )

    if normalized_action == "update":
        if not task_id:
            raise ValueError("task_id is required for update")

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
        )

    if normalized_action == "delete":
        if not task_id:
            raise ValueError("task_id is required for delete")
        return await task_scheduler.delete_task(task_id)

    raise ValueError(f"Unsupported action: {action}")


# Load MCP server configuration from app/mcp.json if it exists, and mount configured servers
if (mcp_config_file := settings.BASE_DIR / "mcp.json").is_file():
    logger.info(f"Loading MCP config from {mcp_config_file}")

    from app.schema.mcp_config import MCPConfig

    mcp_config = MCPConfig.model_validate_json(mcp_config_file.read_text(encoding="utf-8"))

    for server in mcp_config.servers.values():
        proxy = create_proxy(StreamableHttpTransport(server.url, headers=server.headers))
        if settings.MCP_USE_COMPACTOR_MIDDLEWARE and server.compacting:
            proxy.add_middleware(CompactorMiddleware(cleanup_stages=server.compacting))
        mcp.mount(proxy, namespace=server.name)


_mcp_http_app = mcp.http_app(
    transport="streamable-http",
    json_response=True,
)

mcp_app = FastAPI(
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=_mcp_http_app.lifespan,
)

mcp_app.state.runtime_metadata = _build_runtime_metadata()
mcp_app.state.runtime_auth_configuration = _build_runtime_auth_configuration()
mcp_app.state.application_loop = None
mcp_app.state.uvicorn_server = None


def bind_application_loop(application_loop: Any | None) -> None:
    mcp_app.state.application_loop = application_loop


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


@mcp_app.get("/telemetry/tasks")
async def get_task_telemetry(request: Request) -> dict[str, Any]:
    await _authorize_telemetry(request.headers.get("authorization"))
    return (await runtime_telemetry.build_tasks_snapshot()).model_dump(mode="json")


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


def create_mcp_server() -> uvicorn.Server:
    server = uvicorn.Server(
        uvicorn.Config(
            mcp_app,
            host=settings.MCP_HOST,
            port=settings.MCP_PORT,
            reload=False,
            log_config=uvicorn_log_config,
        )
    )
    mcp_app.state.uvicorn_server = server
    return server


async def wait_for_server_startup(
    server_task: asyncio.Task[None],
    server_started: Callable[[], bool],
) -> None:
    for _ in range(100):
        if server_started():
            return
        if server_task.done():
            if error := server_task.exception():
                raise RuntimeError("Composition MCP server failed to start") from error
            raise RuntimeError("Composition MCP server exited before startup completed")
        await asyncio.sleep(0.05)
    raise TimeoutError("Timed out waiting for Composition MCP server to start")


async def serve_mcp_server() -> None:
    server = create_mcp_server()
    server_task = asyncio.create_task(server.serve())

    try:
        await wait_for_server_startup(server_task, lambda: server.started)
        await task_scheduler.ensure_started()
        await server_task
    finally:
        server.should_exit = True
        await task_scheduler.aclose()
        with suppress(asyncio.CancelledError):
            await server_task


if __name__ == "__main__":
    asyncio.run(serve_mcp_server())
